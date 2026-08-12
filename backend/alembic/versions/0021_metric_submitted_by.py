"""metric 表新增 submitted_by 列（禁止自审，对齐治理 COMPL-2）。

提交评审时记录提交人；approve/reject 时校验提交人与审核人不得同一。
可回滚：DROP COLUMN。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_metric_submitted_by"
down_revision = "0020_semantic_state_machine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "metric",
        sa.Column(
            "submitted_by",
            sa.BigInteger(),
            nullable=True,
            comment="提交评审人 ID（approve/reject 时禁止自审）",
        ),
    )


def downgrade() -> None:
    op.drop_column("metric", "submitted_by")
