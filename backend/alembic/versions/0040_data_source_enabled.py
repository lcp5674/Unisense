"""data_source 增补 enabled（停用/启用）字段。

背景：数据源管理此前仅支持删除，无法"停用"一个数据源。
本迁移为 data_source 表新增 enabled 列：
- enabled=1（默认）—— 正常参与定时调度与手动采集；
- enabled=0 —— 停用：调度器不再触发、手动采集/刷新/异步入队被拒，
  便于维护窗口期暂停某个源而不删除其采集目录与历史血缘。

可逆：downgrade 回退该列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_data_source_enabled"
down_revision = "0039_llm_router"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "data_source",
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
            comment="是否启用（停用后不参与定时调度与手动采集）",
        ),
    )


def downgrade() -> None:
    op.drop_column("data_source", "enabled")
