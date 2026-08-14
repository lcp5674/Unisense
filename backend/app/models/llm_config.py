"""LLM 平台配置模型（单例行）。

支持在前端「AI 助手」页配置 OpenAI 协议兼容的 LLM（base_url/model/api_key），
API Key 经 ``SecretManager`` Fernet 加密落库（与数据源连接配置同一套密钥体系），
避免明文密钥入库/日志泄露。

配置优先级：DB 行（enabled=true） > 环境变量（UNISENSE_LLM_*） > 未配置降级。
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class LlmConfig(Base, BaseModel):
    """LLM 平台配置（单例行，仅允许一条生效配置）。

    Attributes:
        provider: 提供商标识（openai/deepseek/qwen/ernie/kilo/custom）。
        base_url: OpenAI 兼容接口基础 URL。
        model: 模型名称。
        api_key_enc: API Key（Fernet 加密令牌）。
        timeout: 请求超时秒数。
        enabled: 是否启用该配置。
        updated_by: 最后编辑者用户 ID。
    """

    __tablename__ = "llm_config"

    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="custom", comment="提供商标识"
    )
    base_url: Mapped[str] = mapped_column(
        String(256), nullable=False, default="", comment="OpenAI 兼容接口基础 URL"
    )
    model: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", comment="模型名称"
    )
    api_key_enc: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="API Key（Fernet 加密令牌）"
    )
    timeout: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, comment="请求超时秒数"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否启用该配置"
    )
    updated_by: Mapped[int | None] = mapped_column(
        nullable=True, comment="最后编辑者用户 ID"
    )
