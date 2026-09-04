"""dp_sync_run_log 增加字段级血缘统计列

方案 3（字段级 schema 感知解析）可观测性：run_log 记录每轮扫描写入的
字段映射数与降级字段边数，回答「字段级解析了多少/为什么缺失」。

Revision ID: 0142_dp_run_log_field_stats
Revises: 0141_dp_poll_interval_1440
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0142_dp_run_log_field_stats"
down_revision: str | None = "0141_dp_poll_interval_1440"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dp_sync_run_log",
        sa.Column(
            "field_mappings_written",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="字段级血缘映射写入数（schema 感知解析产出的真实/降级字段边）",
        ),
    )
    op.add_column(
        "dp_sync_run_log",
        sa.Column(
            "field_edges_degraded",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="字段级降级边数（SELECT * 无源表 schema 时列名缺失）",
        ),
    )


def downgrade() -> None:
    op.drop_column("dp_sync_run_log", "field_edges_degraded")
    op.drop_column("dp_sync_run_log", "field_mappings_written")
