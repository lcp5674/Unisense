"""llm_config 增补路由字段：name（实例名称）+ priority（路由优先级）。

背景：LLM 平台配置从「单例」升级为「多实例轮询路由 + 故障转移」——
平台可配置多个 OpenAI 协议兼容的 LLM 实例，请求按优先级轮询（round-robin），
单实例不可用时自动切换下一个可用实例，避免单点 LLM 不可用造成服务不可用。
本迁移为 llm_config 表新增：
1. name —— 实例名称（如「主用 DeepSeek」「备用通义」），便于界面识别；
2. priority —— 路由优先级（数值小者优先，0 最高），决定轮询起始顺序。

可逆：downgrade 回退两列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_llm_router"
down_revision = "0038_lineage_ingest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_config",
        sa.Column(
            "name",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="实例名称（如 主用/备用）",
        ),
    )
    op.add_column(
        "llm_config",
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="路由优先级（小者优先，0 最高）",
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_config", "priority")
    op.drop_column("llm_config", "name")
