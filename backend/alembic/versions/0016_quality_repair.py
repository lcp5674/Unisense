"""quality_event 增加 repair_suggestion 列（FR-10 修复建议生成，TD §4.8.5 / PRD 4.8.5）。

非破坏性、可回滚：仅对 quality_event 表 ADD COLUMN（nullable JSON），不改动既有列与数据。
修复建议含责任方 / 上游采集任务 / 建议 SQL 模板 / Owner 确认留痕，供异常事件 Owner 线下修复闭环。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_quality_repair"
down_revision = "0015_quality_observation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quality_event",
        sa.Column(
            "repair_suggestion",
            sa.JSON(),
            nullable=True,
            comment="修复建议（责任方/上游任务/建议SQL/确认留痕）",
        ),
    )


def downgrade() -> None:
    op.drop_column("quality_event", "repair_suggestion")
