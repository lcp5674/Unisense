"""observability 服务迁移：用户反馈表（TD §12.10 / FR-16）。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_observability"
down_revision = "0010_notify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="反馈人 ID"),
        sa.Column("target_type", sa.String(64), nullable=False, comment="反馈对象类型"),
        sa.Column("target_id", sa.String(64), nullable=True, comment="反馈对象 ID"),
        sa.Column("rating", sa.Integer(), nullable=True, comment="评分 1-5"),
        sa.Column("comment", sa.Text(), nullable=True, comment="反馈内容"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_feedback_user", "feedback", ["user_id"])


def downgrade() -> None:
    op.drop_table("feedback")
