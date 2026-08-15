"""metric 表评审指派字段：reviewer_id / reviewer_type / reviewer_domain。

治理闭环（TD §13）落地：提交评审时可指定评审用户（user）或域评审组（domain），
approve/reject 仅被指派评审人（或 platform_admin 兜底）可操作。

- reviewer_id（BIGINT，可空）：指定评审用户 ID（reviewer_type=user 时生效）；
- reviewer_type（String(16)）：评审指派类型 user/domain；
- reviewer_domain（String(64)）：域评审组所在域（reviewer_type=domain 时生效）。

可逆：downgrade 删除上述列（无 FK 约束，删除安全）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053_metric_reviewer_assignment"
down_revision = "0052_data_source_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "metric",
        sa.Column(
            "reviewer_id",
            sa.BigInteger(),
            nullable=True,
            comment="指定评审用户 ID（reviewer_type=user 时生效）",
        ),
    )
    op.add_column(
        "metric",
        sa.Column(
            "reviewer_type",
            sa.String(length=16),
            nullable=True,
            comment="评审指派类型: user(指定用户)/domain(域评审组)",
        ),
    )
    op.add_column(
        "metric",
        sa.Column(
            "reviewer_domain",
            sa.String(length=64),
            nullable=True,
            comment="评审团队所在域（reviewer_type=domain 时生效）",
        ),
    )


def downgrade() -> None:
    op.drop_column("metric", "reviewer_domain")
    op.drop_column("metric", "reviewer_type")
    op.drop_column("metric", "reviewer_id")
