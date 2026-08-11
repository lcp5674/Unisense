"""conflict + ruling_record（冲突与裁决，TD §12.4 / FR-09）

Revision ID: 0003_conflict
Revises: 0002_lineage_edge
Create Date: 2026-08-07

对齐 TD §12.4 与 DEV_GUIDE §9（up + down 均可执行、数据无损）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_conflict"
down_revision = "0002_lineage_edge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conflict",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column("conflict_id", sa.String(64), nullable=False, comment="业务冲突 ID"),
        sa.Column("metric_a", sa.BigInteger(), nullable=True, comment="指标 A"),
        sa.Column("metric_b", sa.BigInteger(), nullable=True, comment="指标 B"),
        sa.Column(
            "type",
            sa.Enum(
                "same_name_diff_def",
                "same_def_diff_name",
                "grain_unit",
                "cross_domain_same_def",
                "version_conflict",
                "pii",
                name="conflict_type",
            ),
            nullable=False,
            comment="冲突类型",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "OPEN",
                "NEGOTIATING",
                "ESCALATED",
                "RULED",
                "CLOSED",
                name="conflict_status",
            ),
            nullable=False,
            comment="仲裁状态",
        ),
        sa.Column("domain", sa.String(64), nullable=True, comment="主题域"),
        sa.Column("similarity_score", sa.Float(), nullable=False, comment="综合相似度"),
        sa.Column("metric_codes", sa.JSON(), nullable=True, comment="涉及指标编码"),
        sa.Column("arbitrator_id", sa.BigInteger(), nullable=True, comment="仲裁人"),
        sa.Column("decision_json", sa.JSON(), nullable=True, comment="裁决结论"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, comment="更新时间"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True, comment="解决时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conflict_id", name="uq_conflict_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_conflict_status", "conflict", ["status"], unique=False)
    op.create_index("ix_conflict_domain", "conflict", ["domain"], unique=False)

    op.create_table(
        "ruling_record",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column("conflict_id", sa.String(64), nullable=False, comment="关联冲突 ID"),
        sa.Column("metric_codes", sa.JSON(), nullable=True, comment="涉及指标编码"),
        sa.Column("dispute_desc", sa.String(512), nullable=True, comment="争议描述"),
        sa.Column("decision", sa.String(512), nullable=True, comment="裁决决定"),
        sa.Column("reason", sa.String(1024), nullable=True, comment="裁决理由"),
        sa.Column("arbitrator_id", sa.BigInteger(), nullable=True, comment="仲裁人"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True, comment="裁决时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.Index("ix_ruling_conflict", "conflict_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_index("ix_ruling_conflict", table_name="ruling_record")
    op.drop_table("ruling_record")
    op.drop_index("ix_conflict_domain", table_name="conflict")
    op.drop_index("ix_conflict_status", table_name="conflict")
    op.drop_table("conflict")
