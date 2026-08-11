"""创建 erasure_request 表（D9 被遗忘权 DSR 台账，TD §4.15.7 / R7-09③）。

非破坏性、可回滚：仅新建表与索引，不改动既有表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_erasure"
down_revision = "0012_audit_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "erasure_request",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subject_user_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "COMPLETED", "FAILED"),
            nullable=False,
        ),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column(
            "affected_rows",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("reason", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_erasure_subject", "erasure_request", ["subject_user_id"])
    op.create_index("idx_erasure_status", "erasure_request", ["status"])


def downgrade() -> None:
    op.drop_index("idx_erasure_status", "erasure_request")
    op.drop_index("idx_erasure_subject", "erasure_request")
    op.drop_table("erasure_request")
