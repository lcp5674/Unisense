"""feedback 反馈处理状态落库（TD §12.10 / FR-16 反馈采纳闭环）。

背景：此前 ``Feedback`` 无 status 列，``update_feedback_status`` 仅把状态文本
追加进 comment，状态不可查询/过滤，"反馈采纳闭环"未真正落地。本迁移为
feedback 表新增 ``status``（默认 pending）与 ``resolution_note`` 两列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034_feedback_status"
down_revision = "0033_data_source_spark_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feedback",
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending",
            comment="处理状态：pending/adopted/rejected/in_progress",
        ),
    )
    op.add_column(
        "feedback",
        sa.Column("resolution_note", sa.Text(), nullable=True, comment="处理说明（resolver 填写）"),
    )


def downgrade() -> None:
    op.drop_column("feedback", "resolution_note")
    op.drop_column("feedback", "status")
