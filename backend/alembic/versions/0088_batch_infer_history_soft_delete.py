"""batch_infer_history 补软删列 deleted_at。

背景：0087 建表时仅含业务列 + created_at/updated_at，而模型 BatchInferHistory
继承 BaseModel（SoftDeleteMixin），ORM 全列查询会 SELECT deleted_at。
旧表无该列导致 /api/v1/catalogs/batch-infer-history 500（Unknown column
'deleted_at'）。补齐软删列使表结构与模型一致，并支撑历史清理软删语义。

revision 挂 0087_batch_infer_history（当前线性链后继）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0088_batch_infer_history_soft_delete"
down_revision = "0087_batch_infer_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "batch_infer_history",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="软删除时间（UTC），NULL 表示未删除",
        ),
    )


def downgrade() -> None:
    op.drop_column("batch_infer_history", "deleted_at")
