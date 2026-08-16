"""metric 表新增驳回可追溯字段（reject_reason + reject_reviewer_id + rejected_at）。

背景（FR-005 闭环 / TD §13）：指标被审核驳回（REVIEW→DRAFT）时，驳回原因此前
仅进入 metric.rejected 事件（通知中心），不落库——提交人回到详情页看不到"为何被
驳回"，多次驳回历史丢失，只能靠记忆或翻通知中心。本次在 metric 主表落库最近一次
驳回原因/审核人/时间，详情页 DRAFT 状态展示"上次驳回原因"横幅引导提交人修改后重提。

可逆：downgrade 删除三列（存量数据随列删除，符合回退语义）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0063_metric_reject_reason"
down_revision = "0062_notification_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "metric",
        sa.Column(
            "reject_reason",
            sa.String(length=500),
            nullable=True,
            comment="最近一次审核驳回原因（REVIEW→DRAFT 时写入，用于详情页引导修改）",
        ),
    )
    op.add_column(
        "metric",
        sa.Column("reject_reviewer_id", sa.BigInteger(), nullable=True, comment="驳回审核人 ID"),
    )
    op.add_column(
        "metric",
        sa.Column("rejected_at", sa.DateTime(), nullable=True, comment="驳回时间"),
    )


def downgrade() -> None:
    op.drop_column("metric", "rejected_at")
    op.drop_column("metric", "reject_reviewer_id")
    op.drop_column("metric", "reject_reason")
