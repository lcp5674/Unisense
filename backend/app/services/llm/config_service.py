"""LLM 平台配置服务（DB 优先、env 兜底，API Key Fernet 加密落库）。

配置优先级：llm_config 表（enabled=true 且 base_url/api_key 非空） > 环境变量 > 未配置降级。
前端「AI 助手」页通过 /ai/config 三端点读写本服务。
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.secrets import SecretManager
from app.models.llm_config import LlmConfig
from app.services.llm.client import DeterministicFallbackLlmClient, LlmClient
from app.services.llm.schemas import LlmConfigPayload, LlmConfigTestResult

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

#: 连通性测试的探针消息（极短，仅验证连通/鉴权/模型可用，不追求输出质量）
_PROBE_MESSAGES = [
    {"role": "user", "content": "ping"},
]


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
    """LLM 配置读写 + 连通性测试。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ---- 读取 ----

    async def get_config(self) -> LlmConfig | None:
        """读取单行配置（仅取未软删除的最新一条）。"""
        res = await self._db.execute(
            select(LlmConfig)
            .where(LlmConfig.deleted_at.is_(None))
            .order_by(LlmConfig.id)
            .limit(1)
        )
        return res.scalar_one_or_none()

    async def get_effective(self) -> dict[str, Any]:
        """获取生效配置（DB > env > none）。api_key 为解密后的明文（仅供进程内使用）。"""
        row = await self.get_config()
        if row is not None and row.enabled and row.base_url and row.api_key_enc:
            try:
                decrypted = SecretManager.decrypt(row.api_key_enc)
                api_key = (
                    decrypted.get("api_key")
                    if isinstance(decrypted, dict)
                    else str(decrypted)
                )
                return {
                    "provider": row.provider or "custom",
                    "base_url": row.base_url,
                    "model": row.model,
                    "api_key": api_key,
                    "timeout": row.timeout or 30,
                    "source": "db",
                    "updated_by": row.updated_by,
                    "updated_at": row.updated_at,
                }
            except Exception as exc:  # noqa: BLE001 - 解密失败降级 env，不阻断
                logger.error("llm_config_decrypt_failed: %s", exc)
        if settings.llm_base_url and settings.llm_api_key:
            return {
                "provider": _infer_provider(settings.llm_base_url),
                "base_url": settings.llm_base_url,
                "model": settings.llm_default_model,
                "api_key": settings.llm_api_key,
                "timeout": 30,
                "source": "env",
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
            "updated_by": None,
            "updated_at": None,
        }

    # ---- 写入 ----

    async def save(self, payload: LlmConfigPayload, updated_by: int) -> LlmConfig:
        """保存/更新单行配置。api_key 为空时保持原密钥不变。"""
        row = await self.get_config()
        if row is None:
            row = LlmConfig(
                provider=payload.provider,
                base_url=payload.base_url,
                model=payload.model,
                api_key_enc="",
                timeout=payload.timeout,
                enabled=payload.enabled,
                updated_by=updated_by,
            )
            self._db.add(row)
        else:
            row.provider = payload.provider
            row.base_url = payload.base_url
            row.model = payload.model
            row.timeout = payload.timeout
            row.enabled = payload.enabled
            row.updated_by = updated_by
        if payload.api_key.strip():
            row.api_key_enc = SecretManager.encrypt({"api_key": payload.api_key.strip()})
        await self._db.flush()
        return row

    # ---- 客户端构建 ----

    async def build_client(self) -> LlmClient | DeterministicFallbackLlmClient:
        """基于生效配置构建 LLM 客户端（DB 优先）。"""
        effective = await self.get_effective()
        if effective["source"] != "none" and effective["api_key"]:
            return LlmClient(
                base_url=effective["base_url"],
                api_key=effective["api_key"],
                model=effective["model"] or "deepseek-chat",
                timeout=float(effective["timeout"]),
            )
        logger.warning("LLM 未配置（DB/env 均无），使用确定性降级客户端")
        return DeterministicFallbackLlmClient()

    # ---- 连通性测试 ----

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

        if not base_url or not api_key:
            return LlmConfigTestResult(
                ok=False,
                error="未配置 base_url 或 api_key",
                model=model,
            )

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=base_url.rstrip("/"),
                timeout=httpx.Timeout(timeout),
                headers={"Authorization": f"Bearer {api_key}"},
            ) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": _PROBE_MESSAGES,
                        "max_tokens": 5,
                    },
                )
                latency_ms = int((time.monotonic() - start) * 1000)
                if resp.status_code == 200:
                    data = resp.json()
                    return LlmConfigTestResult(
                        ok=True,
                        latency_ms=latency_ms,
                        model=str(data.get("model", model)),
                    )
                body = resp.text[:200]
                return LlmConfigTestResult(
                    ok=False,
                    latency_ms=latency_ms,
                    model=model,
                    error=f"HTTP {resp.status_code}: {body}",
                    detail={"status_code": resp.status_code},
                )
        except httpx.HTTPError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.warning("llm_test_connection_failed: %s", exc)
            return LlmConfigTestResult(
                ok=False,
                latency_ms=latency_ms,
                model=model,
                error=f"{type(exc).__name__}: {exc}",
            )
