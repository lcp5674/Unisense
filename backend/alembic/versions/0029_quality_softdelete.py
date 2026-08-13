"""为 external_benchmark / reconciliation_record 补 deleted_at 软删列（对齐 SoftDeleteMixin）。

端到端断层修复（P0-1/P0-2）：两表模型继承 ``Base, BaseModel``（含 SoftDeleteMixin.deleted_at），
但迁移 0014 建表未含该列，导致质量中心「基准列表」「对账记录」查询执行
``SELECT ... deleted_at`` 报 ``Unknown column`` → 500。本迁移补齐列并加软删索引，
非破坏性、可回滚。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_quality_softdelete"
down_revision = "0028_escalation_record"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "external_benchmark",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_benchmark_deleted",
        "external_benchmark",
        ["deleted_at", "created_at"],
    )
    op.add_column(
        "reconciliation_record",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_recon_deleted",
        "reconciliation_record",
        ["deleted_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_recon_deleted", "reconciliation_record")
    op.drop_column("reconciliation_record", "deleted_at")
    op.drop_index("idx_benchmark_deleted", "external_benchmark")
    op.drop_column("external_benchmark", "deleted_at")
