"""OpenAI 协议兼容的 LLM 客户端。

支持多种 LLM 提供商：
- OpenAI（gpt-4/gpt-3.5-turbo）
- 国内主流：DeepSeek、通义千问、文心一言等（通过 OpenAI 兼容接口）
- kilo.ai 网关（测试环境）

配置方式：
  UNISENSE_LLM_PROVIDER=openai|deepseek|kilo          # 提供商
  UNISENSE_LLM_BASE_URL=https://api.deepseek.com      # 基础 URL
  UNISENSE_LLM_API_KEY=sk-xxx                         # API 密钥
  UNISENSE_LLM_MODEL=deepseek-chat                    # 模型名称
  UNISENSE_LLM_TIMEOUT=30                             # 超时秒数

测试环境密钥（kilo.ai 网关）：
  UNISENSE_LLM_PROVIDER=kilo
  UNISENSE_LLM_BASE_URL=https://api.kilo.ai/api/gateway
  UNISENSE_LLM_API_KEY=eyJhbGciOiJIUzI1NiIs...  # 测试密钥
  UNISENSE_LLM_MODEL=poolside/laguna-m.1:free

P2 增强：
  chat 方法返回结构化结果（dict 含 content+confidence+reasoning+candidates），
  通过 Pydantic Schema 校验确保输出格式一致。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from app.core.config import settings
from app.core.resilience import get_circuit_breaker

logger = logging.getLogger(__name__)

# 熔断 + 重试参数：LLM 网关为外部强依赖，必须快速失败（熔断）并在瞬时故障时退避重试，
# 否则单点故障会拖垮 AI 问数链路（对齐 TD §11 韧性 / retry+circuit breaker 要求）。
_LLM_BREAKER = get_circuit_breaker("LLM")
_LLM_MAX_RETRIES = 2  # 额外重试次数（总计最多 MAX_RETRIES+1 次）
_LLM_BACKOFF_BASE = 0.2  # 指数退避基数（秒）

# 多实例路由（LlmRouterClient）参数：
# - 单实例在路由层连续失败阈值（达到后暂时摘除进入冷却，冷却结束自动恢复）
# - 冷却秒数：故障实例在此窗口内不再被轮询选中（由剩余健康实例承接流量）
_ROUTER_FAILOVER_THRESHOLD = 3
_ROUTER_COOLDOWN_SECONDS = 30.0

# 国内主流 LLM 提供商的默认配置
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode", "model": "qwen-turbo"},
    "ernie": {"base_url": "https://aip.baidubce.com/rpc/2.0/ai_custom", "model": "ernie-bot-turbo"},
    "kilo": {"base_url": "https://api.kilo.ai/api/gateway", "model": "poolside/laguna-m.1:free"},
}


def chat_completions_url(base_url: str) -> str:
    """规范化 OpenAI 协议 chat/completions 端点 URL。

    base_url 存在三种合法形态，若一律追加 ``/v1/chat/completions`` 会拼出
    ``/v1/v1/...`` 或 ``.../completions/v1/...`` 导致 404：
    - 裸域名/根路径：``https://api.deepseek.com`` → 追加 ``/v1/chat/completions``
    - 已含版本前缀：``https://api.openai.com/v1`` → 追加 ``/chat/completions``
    - 已含完整端点：``https://xxx/v1/chat/completions`` → 原样返回

    Args:
        base_url: 用户配置的 base_url（可能含尾部斜杠）。

    Returns:
        可直接请求的 chat/completions 完整 URL；base_url 为空时返回空串。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return url
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


def models_url(base_url: str) -> str:
    """规范化 OpenAI 协议模型列表端点（``GET /models``）URL。

    与 ``chat_completions_url`` 同构，保证与用户配置的 base_url 形态一致：
    - 裸域名/根路径：``https://api.deepseek.com`` → 追加 ``/v1/models``
    - 已含版本前缀：``https://api.openai.com/v1`` → 追加 ``/models``
    - 已含完整 chat 端点：``https://xxx/v1/chat/completions`` → 替换为 ``/v1/models``
    - 已含完整 models 端点：``https://xxx/v1/models`` → 原样返回

    Args:
        base_url: 用户配置的 base_url（可能含尾部斜杠或完整端点）。

    Returns:
        可直接请求的 /models 完整 URL；base_url 为空时返回空串。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return url
    if url.endswith("/models"):
        return url
    if url.endswith("/chat/completions"):
        return f"{url[: -len('/chat/completions')]}/models"
    if url.endswith("/v1"):
        return f"{url}/models"
    return f"{url}/v1/models"


def normalize_base_url(base_url: str) -> str:
    """归一化 base_url：无论用户输入完整端点还是裸 URL，都存为「干净 base URL」。

    用户可能输入三种形态之一：
    - ``http://host.docker.internal:19090/v1/chat/completions`` → ``http://host.docker.internal:19090``
    - ``http://host.docker.internal:19090/v1`` → ``http://host.docker.internal:19090``
    - ``http://host.docker.internal:19090`` → **不变**

    归一化后在库中统一存储裸 URL，前端展示、编辑时也一致。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return url
    for suffix in ("/v1/chat/completions", "/chat/completions", "/v1/models", "/models", "/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/") or url


# ---- P2: 结构化输出 Schema ----


class LlmStructuredOutput(BaseModel):
    """LLM 结构化输出 Schema（P2 置信度分流）。

    所有 LLM chat 调用统一返回此结构，下游服务依据 confidence 做分流决策。
    """

    content: str = Field(..., description="主内容文本")
    confidence: float = Field(
        0.5,
        ge=0.0,
        le=1.0,
        description="置信度 [0,1]，<0.7 标记 needs_review",
    )
    reasoning: str = Field(
        "",
        description="推理过程/依据说明",
    )
    candidates: list[dict[str, Any]] = Field(
        default_factory=list,
        description="候选结果列表（多选题/多分类场景）",
    )

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, v: Any) -> float:
        """将置信度钳制到 [0, 1] 区间。"""
        val = float(v) if v is not None else 0.5
        return max(0.0, min(1.0, val))


class LlmClient:
    """OpenAI 协议兼容的 LLM 客户端。

    支持流式和非流式调用，自动处理超时和重试。
    chat 方法返回结构化结果（LlmStructuredOutput）。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        breaker: Any | None = None,
        name: str = "llm",
    ) -> None:
        self._base_url = (base_url or settings.llm_base_url or "").rstrip("/")
        self._api_key = api_key or settings.llm_api_key
        self._model = model or settings.llm_default_model
        self._timeout = timeout
        self._name = name
        # 熔断器：多实例路由时传入「每实例专属熔断器」实现隔离（某实例故障不牵连其他实例）；
        # 缺省回落到模块级全局熔断器（单实例/直接构建场景，向后兼容）。
        self._breaker = breaker or _LLM_BREAKER
        # 兼容 base_url 是否已含 /v1 后缀（openai 预设含 /v1，deepseek 不含），
        # 统一在此解析出 chat/completions 完整端点，避免每次调用拼出 /v1/v1/... 404。
        self._chat_url = chat_completions_url(self._base_url)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout),
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    @property
    def enabled(self) -> bool:
        """检查 LLM 客户端是否已配置。"""
        return bool(self._base_url and self._api_key)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发送聊天请求，返回结构化结果。

        Args:
            messages: 消息列表，格式 [{"role": "user/assistant/system", "content": "..."}]
            temperature: 采样温度
            max_tokens: 最大生成长度
            response_format: 响应格式约束，如 {"type": "json_object"}

        Returns:
            结构化结果 dict，包含:
            - content: 主内容文本
            - confidence: 置信度 [0,1]
            - reasoning: 推理过程说明
            - candidates: 候选结果列表
            - model: 模型名称
            - finish_reason: 完成原因
            - usage: token 使用量

        Raises:
            LlmError: 请求失败时抛出
        """
        if not self.enabled:
            raise LlmError("LLM 未配置，请设置 UNISENSE_LLM_BASE_URL 和 UNISENSE_LLM_API_KEY")

        # 熔断：网关已故障（集群级 OPEN）时快速失败，避免雪崩与无谓等待
        if not self._breaker.allow():
            raise LlmError("LLM 熔断器已开启，请求被快速拒绝（依赖降级中）")

        # 使用 json_object 格式引导 LLM 输出结构化 JSON
        effective_format = response_format or {"type": "json_object"}

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": effective_format,
        }

        last_exc: Exception | None = None
        for attempt in range(_LLM_MAX_RETRIES + 1):
            try:
                resp = await self._client.post(self._chat_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]["message"]
                raw_content = choice.get("content", "")

                # 解析结构化输出
                structured = self._parse_structured_output(raw_content)

                # 成功 → 复位熔断计数
                self._breaker.record_success()
                return {
                    "content": structured.content,
                    "confidence": structured.confidence,
                    "reasoning": structured.reasoning,
                    "candidates": structured.candidates,
                    "model": data.get("model", self._model),
                    "finish_reason": choice.get("finish_reason", "stop"),
                    "usage": data.get("usage", {}),
                }
            except httpx.HTTPStatusError as exc:
                # 4xx（鉴权/参数错）为永久错误，不重试；5xx/429 视为瞬时重试
                status = exc.response.status_code if exc.response is not None else 0
                self._breaker.record_failure()
                last_exc = exc
                if attempt < _LLM_MAX_RETRIES and (status >= 500 or status == 429):
                    logger.warning("LLM HTTP 错误（将退避重试）: %d，attempt=%d", status, attempt)
                    await asyncio.sleep(_LLM_BACKOFF_BASE * (2**attempt))
                    continue
                logger.error(
                    "LLM HTTP 错误: %d %s",
                    status,
                    exc.response.text if exc.response is not None else "",
                )
                raise LlmError(f"LLM 请求失败: {status}") from exc
            except (KeyError, IndexError) as exc:
                # 响应结构异常：记为一次失败（可能上游降级返回垃圾），但不在此退避重试
                self._breaker.record_failure()
                logger.error("LLM 响应解析失败: %s", exc)
                raise LlmError("LLM 响应格式错误") from exc
            except httpx.HTTPError as exc:
                # 网络/超时等传输层瞬时故障：熔断计数 + 退避重试
                self._breaker.record_failure()
                last_exc = exc
                if attempt < _LLM_MAX_RETRIES:
                    logger.warning("LLM 网络错误（将退避重试）: %s，attempt=%d", exc, attempt)
                    await asyncio.sleep(_LLM_BACKOFF_BASE * (2**attempt))
                    continue
                logger.error("LLM 网络错误: %s", exc)
                raise LlmError(f"LLM 请求失败: {exc}") from exc
        # 不应到达；兜底抛出最后一次异常
        raise LlmError(f"LLM 请求失败: {last_exc}") from last_exc

    def _parse_structured_output(self, raw_content: str) -> LlmStructuredOutput:
        """解析 LLM 输出为结构化结果（委托统一解析器，含 fence 剥离）。"""
        from app.services.llm.parse import parse_json_object

        if not raw_content:
            return LlmStructuredOutput(
                content="",
                confidence=0.0,
                reasoning="LLM 返回空内容",
            )

        obj = parse_json_object(raw_content)
        if obj is not None:
            try:
                return LlmStructuredOutput(
                    content=str(obj.get("content", raw_content)),
                    confidence=float(obj.get("confidence", 0.5)),
                    reasoning=str(obj.get("reasoning", "")),
                    candidates=obj.get("candidates", []),
                )
            except (ValueError, TypeError):
                pass

        # 非结构化输出：包装为默认结构
        return LlmStructuredOutput(
            content=raw_content,
            confidence=0.5,
            reasoning="非结构化输出，默认置信度 0.5",
        )

    async def close(self) -> None:
        """关闭客户端连接。"""
        await self._client.aclose()


class LlmRouterClient:
    """多 LLM 实例轮询路由 + 故障转移客户端。

    承载「LLM 多实例高可用」：平台配置多个 OpenAI 协议兼容实例后，请求按优先级
    轮询（round-robin）选择起始实例，单实例调用失败（LlmError）时自动切换到下一个
    可用实例（failover），连续失败的实例进入冷却期（暂时摘除，冷却结束自动恢复），
    避免单点 LLM 不可用造成服务不可用（对齐 TD §11 韧性）。

    每个实例持有自己的熔断器（LlmClient breaker 注入），某实例熔断不影响其他实例。
    """

    def __init__(self, instances: list[LlmClient]) -> None:
        self._instances = list(instances)
        self._rotation = 0
        # 实例下标 -> 冷却到期时刻（time.monotonic）；期内不参与轮询
        self._cooldown_until: dict[int, float] = {}
        # 实例下标 -> 路由层连续失败计数
        self._consecutive_failures: dict[int, int] = {}

    @property
    def enabled(self) -> bool:
        """是否存在可用实例（至少一个）。"""
        return bool(self._instances)

    @property
    def instance_count(self) -> int:
        return len(self._instances)

    def _candidate_indexes(self, start: int) -> list[int]:
        """按轮询顺序返回候选实例下标，冷却中的实例跳过。

        全部冷却时退化为全量顺序（保证至少尝试一次，避免永久不可用）。
        """
        n = len(self._instances)
        now = time.monotonic()
        ordered = [(start + i) % n for i in range(n)]
        healthy = [i for i in ordered if now >= self._cooldown_until.get(i, 0.0)]
        return healthy or ordered

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按轮询顺序尝试各实例，单实例失败自动切换下一个。

        Raises:
            LlmError: 所有可用实例均失败时抛出（附带最后一个失败原因）。
        """
        if not self._instances:
            raise LlmError("LLM 未配置，请先配置至少一个 LLM 实例")
        n = len(self._instances)
        start = self._rotation % n
        last_exc: Exception | None = None
        for idx in self._candidate_indexes(start):
            try:
                result = await self._instances[idx].chat(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
                # 成功后推进轮询指针到下一实例，并复位该实例失败计数
                self._rotation = (idx + 1) % n
                self._consecutive_failures[idx] = 0
                return result
            except LlmError as exc:
                last_exc = exc
                self._consecutive_failures[idx] = self._consecutive_failures.get(idx, 0) + 1
                if self._consecutive_failures[idx] >= _ROUTER_FAILOVER_THRESHOLD:
                    self._cooldown_until[idx] = time.monotonic() + _ROUTER_COOLDOWN_SECONDS
                    logger.warning(
                        "LLM 实例 %d 连续失败 %d 次，进入冷却 %ss（由剩余实例承接流量）",
                        idx,
                        self._consecutive_failures[idx],
                        _ROUTER_COOLDOWN_SECONDS,
                    )
                continue
        self._rotation = (start + 1) % n
        raise LlmError(f"所有 LLM 实例均不可用：{last_exc}") from last_exc

    async def close(self) -> None:
        """关闭全部实例连接。"""
        for inst in self._instances:
            try:
                await inst.close()
            except Exception:  # noqa: BLE001 - 关闭异常不影响其他实例
                logger.warning("LLM 实例关闭失败", exc_info=True)


class LlmError(Exception):
    """LLM 客户端错误。"""


class DeterministicFallbackLlmClient:
    """确定性降级客户端：不调用外部服务，直接弃权。

    用于 LLM 不可用时的回退场景。
    P2 增强：返回结构化结果，confidence=0.0 标记为需人工审核。
    """

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """降级：返回弃权信号（结构化格式）。"""
        return {
            "content": "",
            "confidence": 0.0,
            "reasoning": "LLM 不可用，确定性降级客户端返回",
            "candidates": [],
            "model": "deterministic-fallback",
            "finish_reason": "length",
            "usage": {},
        }

    @property
    def enabled(self) -> bool:
        return True

    async def close(self) -> None:
        pass


def build_llm_client() -> LlmClient | DeterministicFallbackLlmClient:
    """根据配置构建 LLM 客户端。

    Returns:
        配置的 LlmClient 或 DeterministicFallbackLlmClient
    """
    if settings.llm_base_url and settings.llm_api_key:
        return LlmClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_default_model,
        )
    # 检查是否配置了提供商（使用默认 URL）
    provider = settings.llm_default_model.split("/")[0] if "/" in settings.llm_default_model else ""
    if provider in _PROVIDER_DEFAULTS and settings.llm_api_key:
        defaults = _PROVIDER_DEFAULTS[provider]
        return LlmClient(
            base_url=settings.llm_base_url or defaults["base_url"],
            api_key=settings.llm_api_key,
            model=settings.llm_default_model,
        )
    # 降级为确定性客户端
    logger.warning("LLM 未配置，使用确定性降级客户端")
    return DeterministicFallbackLlmClient()
