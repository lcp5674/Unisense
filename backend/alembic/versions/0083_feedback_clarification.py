"""feedback 增加质疑澄清字段（质疑→澄清→修订闭环）。

背景：P2「质疑闭环」——此前反馈状态机仅 pending/in_progress/adopted/rejected，
"口径冲突"类反馈只能被单方面采纳/驳回，缺乏「质疑方补充分歧说明→修订」的闭环。
新增：
- clarification：质疑方在 clarifying 状态补充的澄清内容（Text）；
- clarified_at：澄清提交时间。
状态 clarifying 由 API/service 层流转（不进迁移），列级仅新增两可空字段，不破坏存量。

revision 挂 0082_measure_catalog_review（并行 measure 审核态迁移之后，保持单链）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0083_feedback_clarification"
down_revision = "0082_measure_catalog_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feedback",
        sa.Column(
            "clarification",
            sa.Text(),
            nullable=True,
            comment="质疑澄清内容（提交人在 clarifying 状态补充）",
        ),
    )
    op.add_column(
        "feedback",
        sa.Column(
            "clarified_at",
            sa.DateTime(),
            nullable=True,
            comment="澄清提交时间",
        ),
    )


def downgrade() -> None:
    op.drop_column("feedback", "clarified_at")
    op.drop_column("feedback", "clarification")
