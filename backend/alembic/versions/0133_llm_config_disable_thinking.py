"""LLM 实例配置新增「关闭思考模式」开关。

背景：本地部署 Qwen3（llama.cpp）默认 ``enable_thinking=True``，Unisense 的
``LlmClient`` 请求体未携带 ``chat_template_kwargs``，模型每次请求先输出一大段
``<think>`` 推理——``max_tokens`` 被思考耗尽、``content`` 残缺/为空，触发
``_infer_description_structured`` 的多轮重试与路由 failover，单次推断被放大到
160s（日志实证：task 6 生成 300 token 中大部分是 thinking，content 空）。

本迁移为 ``llm_config`` 增加 ``disable_thinking`` 布尔开关（默认关=不附加参数，
保持既有行为），启用后 ``LlmClient.chat`` 对该实例请求体附加
``chat_template_kwargs: {"enable_thinking": false}``，从模板层关闭 Qwen3 思考
（llama.cpp server 支持，仅对本地/兼容网关生效；OpenAI 等远程 provider 忽略
未知字段，无副作用）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0133_llm_config_disable_thinking"
down_revision = "0132_query_engine_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_config",
        sa.Column(
            "disable_thinking",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="是否关闭模型思考模式（Qwen3 等默认思考，开启可避免 token 被思考耗尽/延迟翻倍）",
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_config", "disable_thinking")
