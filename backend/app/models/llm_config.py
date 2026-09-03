"""LLM 平台配置模型（多实例，轮询路由）。

支持在前端「系统配置」页配置多个 OpenAI 协议兼容的 LLM 实例（base_url/model/api_key），
多实例间按优先级轮询路由 + 故障转移（单实例不可用时自动切换到下一个可用实例），
避免单点 LLM 不可用造成服务不可用。API Key 经 ``SecretManager`` Fernet 加密落库
（与数据源连接配置同一套密钥体系），避免明文密钥入库/日志泄露。

配置优先级：DB 行（enabled=true，按 priority 排序） > 环境变量（UNISENSE_LLM_*） > 未配置降级。
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class LlmConfig(Base, BaseModel):
    """LLM 平台配置（多实例，每行一个可用实例）。

    Attributes:
        name: 实例名称（如「主用 DeepSeek」「备用通义」）。
        provider: 提供商标识（openai/deepseek/qwen/ernie/kilo/custom）。
        base_url: OpenAI 兼容接口基础 URL。
        model: 模型名称。
        api_key_enc: API Key（Fernet 加密令牌）。
        timeout: 请求超时秒数。
        enabled: 是否启用该实例（仅启用实例参与路由）。
        priority: 路由优先级（数值小者优先轮询，0 为最高）。
        updated_by: 最后编辑者用户 ID。
    """

    __tablename__ = "llm_config"

    name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="实例名称（如 主用/备用）"
    )
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
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="路由优先级（小者优先，0 最高）"
    )
    disable_thinking: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否关闭模型思考模式（Qwen3 等默认思考，开启可避免 token 被思考耗尽）",
    )
    updated_by: Mapped[int | None] = mapped_column(
        nullable=True, comment="最后编辑者用户 ID"
    )
