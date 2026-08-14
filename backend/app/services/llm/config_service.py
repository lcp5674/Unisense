"""LLM 平台配置服务（多实例轮询路由，DB 优先、env 兜底，API Key Fernet 加密落库）。

配置优先级：llm_config 表（enabled=true，按 priority 排序） > 环境变量 > 未配置降级。
多实例场景下 ``build_client`` 返回 ``LlmRouterClient``：请求按优先级轮询，
单实例失败自动切换下一个可用实例（failover），连续失败实例进入冷却自动恢复，
避免单点 LLM 不可用造成服务不可用。前端「系统配置」页通过 /ai/config 端点读写本服务。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.resilience import get_circuit_breaker
from app.core.secrets import SecretManager
from app.models.llm_config import LlmConfig
from app.services.llm.client import (
    DeterministicFallbackLlmClient,
    LlmClient,
    LlmRouterClient,
    models_url,
    normalize_base_url,
)
from app.services.llm.schemas import LlmConfigPayload, LlmConfigTestResult, LlmModelsResult

logger = get_logger("unisense.llm.config")

#: 主流 OpenAI 协议兼容提供商的默认配置（与 services/llm/client.py 保持一致）
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode",
        "model": "qwen-turbo",
    },
    "ernie": {
        "base_url": "https://aip.baidubce.com/rpc/2.0/ai_custom",
        "model": "ernie-bot-turbo",
    },
    "kilo": {
        "base_url": "https://api.kilo.ai/api/gateway",
        "model": "poolside/laguna-m.1:free",
    },
    "custom": {"base_url": "", "model": ""},
}

#: 指向回环地址的 base_url 片段（容器内 127.0.0.1/localhost 指向容器自身，而非宿主机）
_LOOPBACK_TOKENS = ("127.0.0.1", "localhost", "0.0.0.0")


def _extract_model_ids(resp: Any) -> list[str]:
    """从 ``GET /models`` 响应中提取模型 ID 列表（兼容 ``data[].id`` 结构）。

    Args:
        resp: httpx 响应对象（status_code 已判为 200）。

    Returns:
        模型 ID 列表；响应结构异常时返回空列表（不抛异常）。
    """
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 - 响应非 JSON 时按无模型处理
        return []
    items = data.get("data", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        return []
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
    return ids


def _looks_like_loopback(base_url: str) -> bool:
    """判断 base_url 是否指向回环地址（容器内无法经其访问宿主机服务）。"""
    lowered = (base_url or "").lower()
    return any(token in lowered for token in _LOOPBACK_TOKENS)


def _loopback_hint(base_url: str) -> str:
    """回环地址在容器场景的连通失败自诊断提示（附加到错误信息尾部）。

    后端运行在 Docker 容器中时，容器内 ``127.0.0.1/localhost`` 指向容器自身，
    无法访问宿主机上监听的 LLM 网关；应改用 ``host.docker.internal``。
    """
    if not _looks_like_loopback(base_url):
        return ""
    return (
        "；提示：后端运行在 Docker 容器中，127.0.0.1/localhost 指向容器自身，"
        "访问宿主机服务请改用 host.docker.internal"
    )


def _infer_provider(base_url: str) -> str:
    """根据 base_url 反推提供商标识（仅用于 env 兜底展示）。"""
    if not base_url:
        return "custom"
    for provider, defaults in PROVIDER_DEFAULTS.items():
        if defaults.get("base_url") and defaults["base_url"] in base_url:
            return provider
    if "openai" in base_url:
        return "openai"
    return "custom"


class LlmConfigService:
    """LLM 配置读写（多实例 CRUD）+ 轮询路由客户端构建 + 连通性测试。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ---- 读取 ----

    async def list_configs(self) -> list[LlmConfig]:
        """列出全部未软删除的 LLM 实例（按 priority 升序、id 升序）。"""
        res = await self._db.execute(
            select(LlmConfig)
            .where(LlmConfig.deleted_at.is_(None))
            .order_by(LlmConfig.priority, LlmConfig.id)
        )
        return list(res.scalars().all())

    async def get_row(self, instance_id: int) -> LlmConfig | None:
        """按 ID 读取单个未软删除实例。"""
        res = await self._db.execute(
            select(LlmConfig).where(LlmConfig.id == instance_id, LlmConfig.deleted_at.is_(None))
        )
        return res.scalar_one_or_none()

    async def get_effective(self) -> dict[str, Any]:
        """获取生效配置（DB 首个启用实例 > env > none）。api_key 为解密明文（仅供进程内）。"""
        rows = await self.list_configs()
        for row in rows:
            if row.enabled and row.base_url and row.api_key_enc:
                try:
                    decrypted = SecretManager.decrypt(row.api_key_enc)
                    api_key = (
                        decrypted.get("api_key") if isinstance(decrypted, dict) else str(decrypted)
                    )
                except Exception as exc:  # noqa: BLE001 - 解密失败降级 env，不阻断
                    logger.error("llm_config_decrypt_failed: %s", exc)
                    continue
                if not api_key:
                    continue
                return {
                    "provider": row.provider or "custom",
                    "base_url": row.base_url,
                    "model": row.model,
                    "api_key": api_key,
                    "timeout": row.timeout or 30,
                    "source": "db",
                    "instance_id": row.id,
                    "name": row.name,
                    "updated_by": row.updated_by,
                    "updated_at": row.updated_at,
                }
        if settings.llm_base_url and settings.llm_api_key:
            return {
                "provider": _infer_provider(settings.llm_base_url),
                "base_url": settings.llm_base_url,
                "model": settings.llm_default_model,
                "api_key": settings.llm_api_key,
                "timeout": 30,
                "source": "env",
                "instance_id": None,
                "name": "环境变量",
                "updated_by": None,
                "updated_at": None,
            }
        return {
            "provider": "custom",
            "base_url": "",
            "model": "",
            "api_key": "",
            "timeout": 30,
            "source": "none",
            "instance_id": None,
            "name": "",
            "updated_by": None,
            "updated_at": None,
        }

    async def get_secret(self, instance_id: int) -> str | None:
        """按需解密返回实例的明文 api_key（编辑回显用）。

        实例不存在、未配置密钥或解密失败均返回 None（由 API 层区分 404 原因）。
        """
        row = await self.get_row(instance_id)
        if row is None or not row.api_key_enc:
            return None
        try:
            decrypted = SecretManager.decrypt(row.api_key_enc)
            api_key = decrypted.get("api_key") if isinstance(decrypted, dict) else str(decrypted)
            return api_key or None
        except Exception as exc:  # noqa: BLE001 - 解密失败不抛 500，按无密钥处理
            logger.error("llm_config_secret_decrypt_failed: id=%s %s", instance_id, exc)
            return None

    # ---- 写入 ----

    async def create(self, payload: LlmConfigPayload, updated_by: int) -> LlmConfig:
        """新增一个 LLM 实例（api_key 必填，加密落库）。"""
        row = LlmConfig(
            name=payload.name,
            provider=payload.provider,
            base_url=normalize_base_url(payload.base_url),
            model=payload.model,
            api_key_enc="",
            timeout=payload.timeout,
            enabled=payload.enabled,
            priority=payload.priority,
            updated_by=updated_by,
        )
        if payload.api_key.strip():
            row.api_key_enc = SecretManager.encrypt({"api_key": payload.api_key.strip()})
        self._db.add(row)
        await self._db.flush()
        return row

    async def update(
        self, instance_id: int, payload: LlmConfigPayload, updated_by: int
    ) -> LlmConfig | None:
        """更新实例；api_key 为空时保持原密钥。实例不存在返回 None。"""
        row = await self.get_row(instance_id)
        if row is None:
            return None
        row.name = payload.name
        row.provider = payload.provider
        # 与 create 对称：base_url 统一归一化存储（用户可能填完整端点或基础 URL），
        # 避免编辑后库里残留 /v1/chat/completions 完整后缀导致展示/拼接不一致。
        row.base_url = normalize_base_url(payload.base_url)
        row.model = payload.model
        row.timeout = payload.timeout
        row.enabled = payload.enabled
        row.priority = payload.priority
        row.updated_by = updated_by
        if payload.api_key.strip():
            row.api_key_enc = SecretManager.encrypt({"api_key": payload.api_key.strip()})
        await self._db.flush()
        return row

    async def delete(self, instance_id: int) -> bool:
        """软删除实例；不存在返回 False。"""
        row = await self.get_row(instance_id)
        if row is None:
            return False
        row.deleted_at = datetime.now(UTC)
        await self._db.flush()
        return True

    # ---- 客户端构建（轮询路由）----

    async def build_client(
        self,
    ) -> LlmClient | LlmRouterClient | DeterministicFallbackLlmClient:
        """基于全部启用实例构建客户端（多实例 → LlmRouterClient）。

        启用实例按 priority 排序；环境变量作为最后兜底实例参与路由。
        无任何可用实例时返回确定性降级客户端。
        """
        instances: list[LlmClient] = []
        rows = await self.list_configs()
        for row in rows:
            if not (row.enabled and row.base_url and row.api_key_enc):
                continue
            try:
                decrypted = SecretManager.decrypt(row.api_key_enc)
                api_key = (
                    decrypted.get("api_key") if isinstance(decrypted, dict) else str(decrypted)
                )
            except Exception as exc:  # noqa: BLE001 - 单个实例解密失败跳过，不阻断路由
                logger.error("llm_build_client_decrypt_failed: id=%s %s", row.id, exc)
                continue
            if not api_key:
                continue
            # 每实例专属熔断器（LLM:{id}），实例间故障隔离
            breaker = get_circuit_breaker(f"LLM:{row.id}")
            instances.append(
                LlmClient(
                    base_url=row.base_url,
                    api_key=api_key,
                    model=row.model or "deepseek-chat",
                    timeout=float(row.timeout or 30),
                    breaker=breaker,
                    name=row.name or f"llm-{row.id}",
                )
            )
        # 环境变量兜底实例（始终参与路由，避免 DB 全部禁用时降级）
        if settings.llm_base_url and settings.llm_api_key:
            instances.append(
                LlmClient(
                    base_url=settings.llm_base_url,
                    api_key=settings.llm_api_key,
                    model=settings.llm_default_model,
                    breaker=get_circuit_breaker("LLM:env"),
                    name="env",
                )
            )
        if not instances:
            logger.warning("LLM 未配置（DB/env 均无可用实例），使用确定性降级客户端")
            return DeterministicFallbackLlmClient()
        if len(instances) == 1:
            return instances[0]
        logger.info("LLM 多实例轮询路由启用：%d 个实例", len(instances))
        return LlmRouterClient(instances)

    # ---- 连通性测试 ----

    async def test_instance(self, instance_id: int) -> LlmConfigTestResult:
        """测试已保存实例的连通性（用其落库明文密钥）。实例不存在返回失败。"""
        row = await self.get_row(instance_id)
        if row is None:
            return LlmConfigTestResult(ok=False, error="实例不存在", model="")
        try:
            decrypted = SecretManager.decrypt(row.api_key_enc)
            api_key = str(
                decrypted.get("api_key") if isinstance(decrypted, dict) else decrypted or ""
            )
        except Exception as exc:  # noqa: BLE001
            return LlmConfigTestResult(ok=False, error=f"密钥解密失败: {exc}", model=row.model)
        return await self._probe(
            base_url=row.base_url,
            api_key=api_key,
            model=row.model or "deepseek-chat",
            timeout=float(row.timeout or 30),
        )

    async def test_connection(self, payload: LlmConfigPayload | None) -> LlmConfigTestResult:
        """测试 OpenAI 协议连通性：base_url 可达 + 鉴权通过 + 模型可用。

        payload 为空时使用已保存的生效配置；否则使用载荷临时测试（不落库）。
        采用直接 POST /v1/chat/completions（短超时、无 response_format），
        兼容不支持 json_object 约束的中小网关；不经过熔断器（测试应独立于运行态）。
        """
        if payload is not None:
            base_url = payload.base_url.strip()
            api_key = payload.api_key.strip()
            if not api_key:
                # 前端编辑表单 api_key 留空表示"保持原密钥"（不覆盖）——
                # 测试连通性时回落到已保存/环境密钥，避免"未配置 api_key"误报。
                effective = await self.get_effective()
                api_key = effective["api_key"]
            model = payload.model.strip() or "deepseek-chat"
            timeout = payload.timeout
        else:
            effective = await self.get_effective()
            base_url = effective["base_url"]
            api_key = effective["api_key"]
            model = effective["model"] or "deepseek-chat"
            timeout = effective["timeout"]
        return await self._probe(base_url, api_key, model, timeout)

    async def _probe(
        self, base_url: str, api_key: str, model: str, timeout: float
    ) -> LlmConfigTestResult:
        """连通性测试（方案 A'）：仅做轻量 GET /models 验证连通 + 鉴权。

        不再触发真实推理——「连通成功」定义为地址可连、鉴权通过、模型列表可读，
        毫秒级反馈；真实推理耗时属于模型性能指标（本地模型 prefill 可达数秒），
        不阻塞连通性判断。网关不支持 /models 端点（404/405/501）时返回明确
        失败提示，而非回退慢速推理。
        """
        if not base_url or not api_key:
            return LlmConfigTestResult(
                ok=False,
                error="未配置 base_url 或 api_key",
                model=model,
            )
        return await self._quick_probe(base_url, api_key, timeout, model=model)

    async def _quick_probe(
        self, base_url: str, api_key: str, timeout: float, model: str = ""
    ) -> LlmConfigTestResult:
        """轻量快速探测：GET /models 验证连通 + 鉴权 + 模型可用（毫秒级）。

        成功（HTTP 200）即判连通成功，并带回可用模型列表；连通失败 / 鉴权失败 /
        网关 5xx / 网关不支持 /models（404/405/501）均直接返回明确失败原因，
        不再回退真实推理（避免本地模型 prefill 慢导致测试阻塞数秒）。
        """
        start = time.monotonic()
        try:
            req_url = models_url(base_url)
            async with httpx.AsyncClient(
                # 快速探测不等完整超时：上限 10 秒即可覆盖连通/鉴权判定
                timeout=httpx.Timeout(min(timeout, 10.0)),
                headers={"Authorization": f"Bearer {api_key}"},
            ) as client:
                resp = await client.get(req_url)
        except httpx.HTTPError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.warning("llm_models_probe_failed: %s", exc)
            return LlmConfigTestResult(
                ok=False,
                latency_ms=latency_ms,
                model=model,
                error=f"{type(exc).__name__}: {exc}{_loopback_hint(base_url)}",
            )
        latency_ms = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            models = _extract_model_ids(resp)
            logger.info(
                "llm_models_probe_ok: url=%s latency=%dms models=%d",
                req_url,
                latency_ms,
                len(models),
            )
            return LlmConfigTestResult(
                ok=True,
                latency_ms=latency_ms,
                model=model,
                models=models,
            )
        if resp.status_code in (401, 403):
            return LlmConfigTestResult(
                ok=False,
                latency_ms=latency_ms,
                model=model,
                error=f"鉴权失败（HTTP {resp.status_code}，请求 {req_url}）: {resp.text[:120]}",
                detail={"status_code": resp.status_code, "request_url": req_url},
            )
        if resp.status_code >= 500:
            return LlmConfigTestResult(
                ok=False,
                latency_ms=latency_ms,
                model=model,
                error=f"LLM 网关错误（HTTP {resp.status_code}，请求 {req_url}）: {resp.text[:120]}",
                detail={"status_code": resp.status_code, "request_url": req_url},
            )
        # 404/405/501 等：网关未实现 /models 端点，无法用快速探测验证连通。
        # 方案 A' 不再回退真实推理（本地模型可能阻塞数秒），返回明确提示。
        logger.info("llm_models_probe_unsupported: url=%s status=%d", req_url, resp.status_code)
        return LlmConfigTestResult(
            ok=False,
            latency_ms=latency_ms,
            model=model,
            error=(
                f"LLM 网关不支持 GET /models 端点（HTTP {resp.status_code}，请求 {req_url}）。"
                "无法自动验证连通性，请确认接口地址指向 OpenAI 兼容 /v1 服务。"
            ),
            detail={"status_code": resp.status_code, "request_url": req_url},
        )

    # ---- 一键获取模型 ----

    async def fetch_models_for_instance(self, instance_id: int) -> LlmModelsResult:
        """获取已保存实例的可用模型列表（用其落库密钥）。实例不存在返回不支持。"""
        row = await self.get_row(instance_id)
        if row is None:
            return LlmModelsResult(supported=False, error="实例不存在")
        try:
            decrypted = SecretManager.decrypt(row.api_key_enc)
            api_key = str(
                decrypted.get("api_key") if isinstance(decrypted, dict) else decrypted or ""
            )
        except Exception as exc:  # noqa: BLE001
            return LlmModelsResult(supported=False, error=f"密钥解密失败: {exc}")
        return await self.fetch_models(
            base_url=row.base_url,
            api_key=api_key,
            timeout=float(row.timeout or 30),
        )

    async def fetch_models(self, base_url: str, api_key: str, timeout: float) -> LlmModelsResult:
        """获取提供商可用模型列表（GET /models）。

        api_key 为空时回落已保存/环境密钥（前端编辑留空=保持原密钥）。
        网关不支持 /models 端点（404/405/501）或请求失败时返回
        ``supported=False`` + error，由调用方提示用户手动输入模型名。
        """
        base_url = base_url.strip()
        if not base_url:
            return LlmModelsResult(supported=False, error="未配置 base_url")
        if not api_key:
            effective = await self.get_effective()
            api_key = effective["api_key"]
        if not api_key:
            return LlmModelsResult(supported=False, error="未配置 API Key")
        start = time.monotonic()
        try:
            req_url = models_url(base_url)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(min(timeout, 10.0)),
                headers={"Authorization": f"Bearer {api_key}"},
            ) as client:
                resp = await client.get(req_url)
        except httpx.HTTPError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            return LlmModelsResult(
                supported=False,
                error=f"{type(exc).__name__}: {exc}{_loopback_hint(base_url)}",
                latency_ms=latency_ms,
            )
        latency_ms = int((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            try:
                data = resp.json()
                models = [
                    str(m.get("id"))
                    for m in data.get("data", [])
                    if isinstance(m, dict) and m.get("id")
                ]
            except Exception:  # noqa: BLE001 - 响应结构异常按不支持处理
                models = []
            return LlmModelsResult(models=models, supported=True, latency_ms=latency_ms)
        return LlmModelsResult(
            supported=False,
            error=f"HTTP {resp.status_code}（请求 {req_url}）: {resp.text[:120]}",
            latency_ms=latency_ms,
        )
