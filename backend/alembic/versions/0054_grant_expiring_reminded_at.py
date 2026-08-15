"""grants 表新增 expiring_reminded_at（授权到期提醒去重标记，TD §5.5 三梯队）。

背景：授权到期提醒（grant.expiring_soon）由治理 Worker 扫描 7 天内到期授权，
定向通知被授权人。若无去重标记，Worker 每轮都会重复提醒造成刷屏。
本列记录最近一次到期提醒时间：提醒后置当前时间，下一轮扫描跳过。
可逆：downgrade 删除该列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054_grant_expiring_reminded_at"
down_revision = "0053_metric_reviewer_assignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.add_column(
        "grants",
        sa.Column(
            "expiring_reminded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="授权到期提醒时间（非空=已提醒过，Worker 跳过）",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    op.drop_column("grants", "expiring_reminded_at")
