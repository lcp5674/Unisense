"""LLM 实例增加 max_tokens 可视化配置列。

背景：max_tokens 此前在全部业务调用点硬编码（100~2048），LLM 实例无法配置。
本迁移为 ``llm_config`` 增加 ``max_tokens``（单次请求最大生成长度上限），
管理员可在「系统配置 → LLM 实例」编辑弹窗可视化配置；实际请求取
``min(场景值, 实例上限)``。存量行默认 2048（不改变既有 ≤2048 调用行为）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0135_llm_config_max_tokens"
down_revision = "0134_batch_llm_infer_task"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 幂等：已加过（并行会话/重复执行）则跳过
    conn = op.get_bind()
    cols = {
        r["COLUMN_NAME"]
        for r in conn.execute(
            sa.text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'llm_config'"
            )
        )
    }
    if "max_tokens" not in cols:
        op.add_column(
            "llm_config",
            sa.Column(
                "max_tokens",
                sa.Integer(),
                nullable=False,
                server_default="2048",
                comment="单次请求最大生成长度上限（实际请求取 min(场景值, 实例上限)）",
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    cols = {
        r["COLUMN_NAME"]
        for r in conn.execute(
            sa.text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'llm_config'"
            )
        )
    }
    if "max_tokens" in cols:
        op.drop_column("llm_config", "max_tokens")
