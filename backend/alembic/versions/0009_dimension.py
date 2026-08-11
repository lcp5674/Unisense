"""dimension 服务迁移：维度主表 / 成员 / 映射 / 指标-维度 / 口径对账（TD §12.15）。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0009_dimension"
down_revision = "0008_glossary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dimension",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column("dim_code", sa.String(64), nullable=False, comment="维度编码"),
        sa.Column("name", sa.String(128), nullable=False, comment="维度名称"),
        sa.Column("domain", sa.String(64), nullable=False, comment="业务域"),
        sa.Column(
            "type",
            sa.Enum("SCD1", "SCD2", name="dimension_type"),
            nullable=False,
            server_default="SCD1",
            comment="缓慢变化维类型",
        ),
        sa.Column("description", sa.Text(), nullable=True, comment="描述"),
        sa.Column("owner_id", sa.BigInteger(), nullable=False, comment="责任人 ID"),
        sa.Column(
            "status",
            sa.Enum("DRAFT", "PUBLISHED", "DEPRECATED", name="dimension_status"),
            nullable=False,
            server_default="DRAFT",
            comment="状态",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dim_code", name="uq_dimension_code"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "dimension_member",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column("dim_code", sa.String(64), nullable=False, comment="维度编码"),
        sa.Column("member_code", sa.String(64), nullable=False, comment="成员编码"),
        sa.Column("member_name", sa.String(128), nullable=False, comment="成员名称"),
        sa.Column("parent_code", sa.String(64), nullable=True, comment="父成员编码"),
        sa.Column("path", sa.String(512), nullable=True, comment="层级路径"),
        sa.Column("attributes", mysql.JSON(), nullable=True, comment="扩展属性"),
        sa.Column(
            "status",
            sa.Enum("DRAFT", "PUBLISHED", "DEPRECATED", name="dimension_status"),
            nullable=False,
            server_default="PUBLISHED",
            comment="状态",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dim_code", "member_code", name="uk_dim_member"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "dimension_mapping",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column("source_dim_code", sa.String(64), nullable=False, comment="源维度"),
        sa.Column("target_dim_code", sa.String(64), nullable=False, comment="目标维度"),
        sa.Column(
            "mapping_type",
            sa.Enum("EQUIVALENT", "PARTIAL", name="mapping_type"),
            nullable=False,
            comment="映射类型",
        ),
        sa.Column("expression", sa.Text(), nullable=True, comment="映射表达式"),
        sa.Column("created_by", sa.BigInteger(), nullable=False, comment="创建人 ID"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_dim_code", "target_dim_code", "mapping_type", name="uk_dim_map"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "metric_dimension",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column("metric_id", sa.BigInteger(), nullable=False, comment="指标 ID"),
        sa.Column("dim_code", sa.String(64), nullable=False, comment="维度编码"),
        sa.Column(
            "role",
            sa.Enum("PARTITION", "SPLICE", "FILTER", name="metric_dimension_role"),
            nullable=False,
            comment="关联角色",
        ),
        sa.Column("default_member", sa.String(64), nullable=True, comment="默认成员"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("metric_id", "dim_code", name="uk_metric_dim"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_metric_dimension_metric_id", "metric_dimension", ["metric_id"])

    op.create_table(
        "reconciliation",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column("metric_id", sa.BigInteger(), nullable=False, comment="指标 ID"),
        sa.Column("dim_code", sa.String(64), nullable=True, comment="维度编码"),
        sa.Column("expected_expr", sa.Text(), nullable=False, comment="期望口径"),
        sa.Column("actual_expr", sa.Text(), nullable=False, comment="实际口径"),
        sa.Column(
            "status",
            sa.Enum("PENDING", "APPROVED", "REJECTED", name="reconciliation_status"),
            nullable=False,
            server_default="PENDING",
            comment="对账状态",
        ),
        sa.Column("diff_summary", sa.Text(), nullable=True, comment="差异摘要"),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True, comment="复核人 ID"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间"),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True, comment="复核时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_reconciliation_metric_id", "reconciliation", ["metric_id"])
    op.create_index("ix_reconciliation_status", "reconciliation", ["status"])


def downgrade() -> None:
    op.drop_table("reconciliation")
    op.drop_table("metric_dimension")
    op.drop_table("dimension_mapping")
    op.drop_table("dimension_member")
    op.drop_table("dimension")
