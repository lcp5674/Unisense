"""audit_log.actor_id 改为可空（X-4 登录失败审计不再 FK 违规 500）

Revision ID: 0097
Revises: 0096_sql_infer_eval_sample
Create Date: 2026-08-26

背景：登录失败路径以 actor_id=0 写审计（无对应用户），而 audit_log.actor_id
有 FK→user.id 且 nullable=False → commit 抛 IntegrityError，失败登录返回 500
而非 401 且失败审计丢失。改为可空，登录失败审计以 NULL 落库（保留审计能力）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0097"
down_revision = "0096_sql_infer_eval_sample"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MySQL 8 在 FK 约束下直接改列 nullable 会报 3780 不兼容，
    # 须先 drop FK → 改列 → 重建 FK。
    # 类型用 BigInteger 对齐 user.id（0001 迁移 actor_id 即 BigInteger；模型此前
    # Mapped[int] 默认映射 INTEGER 造成潜在类型漂移，一并在此修正）。
    op.drop_constraint("fk_audit_log_user", "audit_log", type_="foreignkey")
    op.alter_column(
        "audit_log",
        "actor_id",
        existing_type=sa.BigInteger(),
        type_=sa.BigInteger(),
        nullable=True,
        existing_nullable=False,
    )
    op.create_foreign_key("fk_audit_log_user", "audit_log", "user", ["actor_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_audit_log_user", "audit_log", type_="foreignkey")
    op.alter_column(
        "audit_log",
        "actor_id",
        existing_type=sa.BigInteger(),
        type_=sa.BigInteger(),
        nullable=False,
        existing_nullable=True,
    )
    op.create_foreign_key("fk_audit_log_user", "audit_log", "user", ["actor_id"], ["id"])
