"""LLM 平台配置 Schemas（对齐 TD §12.7 / FR-14 扩展）。

提供前端「AI 助手」页配置 OpenAI 协议兼容 LLM 的载荷与响应结构。
API Key 前端提交时为明文（HTTPS 传输），后端经 SecretManager 加密后落库，
响应一律脱敏（仅返回 has_api_key 布尔标记）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class LlmConfigPayload(BaseModel):
    """LLM 配置保存载荷。

    api_key 可选：为空表示保持原密钥不变（编辑时不覆盖已有密钥）。
    """

    provider: str = Field("custom", max_length=32, description="提供商标识")
    base_url: str = Field("", max_length=256, description="OpenAI 兼容接口基础 URL")
    model: str = Field("", max_length=128, description="模型名称")
    api_key: str = Field("", description="API Key（留空表示保持原密钥）")
    timeout: int = Field(30, ge=1, le=300, description="请求超时秒数")
    enabled: bool = Field(False, description="是否启用")

    @field_validator("base_url", "model", mode="after")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


class LlmConfigResponse(BaseModel):
    """LLM 配置响应（脱敏：不含明文 API Key）。"""

    provider: str = ""
    base_url: str = ""
    model: str = ""
    has_api_key: bool = False
    timeout: int = 30
    enabled: bool = False
    source: str = "none"  # db | env | none
    can_edit: bool = False
    updated_by: int | None = None
    updated_at: str | None = None

    @classmethod
    def build(
        cls,
        *,
        provider: str,
        base_url: str,
        model: str,
        has_api_key: bool,
        timeout: int,
        enabled: bool,
        source: str,
        can_edit: bool,
        updated_by: int | None = None,
        updated_at: str | None = None,
    ) -> LlmConfigResponse:
        return cls(
            provider=provider,
            base_url=base_url,
            model=model,
            has_api_key=has_api_key,
            timeout=timeout,
            enabled=enabled,
            source=source,
            can_edit=can_edit,
            updated_by=updated_by,
            updated_at=updated_at,
        )


class LlmConfigTestResult(BaseModel):
    """LLM 连通性测试结果。"""

    ok: bool
    latency_ms: int = 0
    model: str = ""
    error: str = ""
    detail: dict[str, Any] | None = None
