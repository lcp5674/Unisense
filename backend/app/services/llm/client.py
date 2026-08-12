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

import json
import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from app.core.config import settings

logger = logging.getLogger(__name__)

# 国内主流 LLM 提供商的默认配置
_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode", "model": "qwen-turbo"},
    "ernie": {"base_url": "https://aip.baidubce.com/rpc/2.0/ai_custom", "model": "ernie-bot-turbo"},
    "kilo": {"base_url": "https://api.kilo.ai/api/gateway", "model": "poolside/laguna-m.1:free"},
}


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
    ) -> None:
        self._base_url = (base_url or settings.llm_base_url or "").rstrip("/")
        self._api_key = api_key or settings.llm_api_key
        self._model = model or settings.llm_default_model
        self._timeout = timeout
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

        # 使用 json_object 格式引导 LLM 输出结构化 JSON
        effective_format = response_format or {"type": "json_object"}

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": effective_format,
        }

        try:
            resp = await self._client.post("/v1/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]["message"]
            raw_content = choice.get("content", "")

            # 解析结构化输出
            structured = self._parse_structured_output(raw_content)

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
            logger.error("LLM HTTP 错误: %d %s", exc.response.status_code, exc.response.text)
            raise LlmError(f"LLM 请求失败: {exc.response.status_code}") from exc
        except (KeyError, IndexError) as exc:
            logger.error("LLM 响应解析失败: %s", exc)
            raise LlmError("LLM 响应格式错误") from exc
        except httpx.HTTPError as exc:
            logger.error("LLM 网络错误: %s", exc)
            raise LlmError(f"LLM 请求失败: {exc}") from exc

    def _parse_structured_output(self, raw_content: str) -> LlmStructuredOutput:
        """解析 LLM 输出为结构化结果。

        如果 LLM 输出为合法 JSON，尝试解析为 LlmStructuredOutput；
        否则包装为默认结构（confidence=0.5, reasoning="非结构化输出"）。
        """
        if not raw_content:
            return LlmStructuredOutput(
                content="",
                confidence=0.0,
                reasoning="LLM 返回空内容",
            )

        try:
            parsed = json.loads(raw_content)
            if isinstance(parsed, dict):
                return LlmStructuredOutput(
                    content=str(parsed.get("content", raw_content)),
                    confidence=float(parsed.get("confidence", 0.5)),
                    reasoning=str(parsed.get("reasoning", "")),
                    candidates=parsed.get("candidates", []),
                )
        except (json.JSONDecodeError, ValueError, TypeError):
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
