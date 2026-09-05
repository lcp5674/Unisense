"""dp_sync_config 增加失败退避字段

源库不可达等整轮异常时，arq 周期任务按 poll_interval（可配到 1 分钟）
每分钟重试一次、持续空转刷 run_log（实测 280+ 轮 failed）。增加连续失败
计数与退避截止时间：整轮异常累积计数并写 next_scan_at（5/5/15/30/60min
阶梯），此时间前周期任务跳过自动扫描；成功一轮归零。手动「立即扫描」
（force=True）不受退避影响。

Revision ID: 0144_dp_config_backoff
Revises: 0143_metric_dict_enum_to_varchar
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0144_dp_config_backoff"
down_revision: str | None = "0143_metric_dict_enum_to_varchar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dp_sync_config",
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="连续整轮失败次数（成功一轮归零；≥1 次按阶梯退避）",
        ),
    )
    op.add_column(
        "dp_sync_config",
        sa.Column(
            "next_scan_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="退避截止时间（UTC）——此前周期任务跳过自动扫描；手动「立即扫描」不受限",
        ),
    )


def downgrade() -> None:
    op.drop_column("dp_sync_config", "next_scan_at")
    op.drop_column("dp_sync_config", "consecutive_failures")
