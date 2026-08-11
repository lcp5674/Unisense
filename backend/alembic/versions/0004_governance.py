"""role + grants + classification（权限与合规治理，TD §12.5 / FR-11）

Revision ID: 0004_governance
Revises: 0003_conflict
Create Date: 2026-08-07

对齐 TD §4.1 与 DEV_GUIDE §9（up + down 均可执行、数据无损）。
同时为 ``user.role`` 补齐 ``reviewer`` / ``compliance_officer`` 两个角色（PRD 4.9.2 六角色）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_governance"
down_revision = "0003_conflict"
branch_labels = None
depends_on = None

_ROLE_ENUM_NEW = (
    "platform_admin",
    "domain_admin",
    "metric_owner",
    "reviewer",
    "compliance_officer",
    "analyst",
    "viewer",
)
_ROLE_ENUM_OLD = (
    "platform_admin",
    "domain_admin",
    "metric_owner",
    "analyst",
    "viewer",
)


def upgrade() -> None:
    op.create_table(
        "role",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column(
            "name",
            sa.Enum(
                "platform_admin",
                "domain_admin",
                "metric_owner",
                "reviewer",
                "compliance_officer",
                "viewer",
                name="role_name",
            ),
            nullable=False,
            comment="角色名（对齐 PRD 4.9.2）",
        ),
        sa.Column("description", sa.String(256), nullable=True, comment="角色说明"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_role_name"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "grants",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="被授权用户 ID"),
        sa.Column("role_id", sa.BigInteger(), nullable=True, comment="关联角色 ID"),
        sa.Column("domain", sa.String(64), nullable=True, comment="授权主题域"),
        sa.Column("metric_whitelist", sa.JSON(), nullable=True, comment="指标白名单"),
        sa.Column(
            "row_level",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="行级权限开关",
        ),
        sa.Column(
            "grant_type",
            sa.Enum("READ", "WRITE", "READ_WRITE", name="grant_type"),
            nullable=False,
            comment="授权类型",
        ),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "EXPIRED", "REVOKED", name="grant_status"),
            nullable=False,
            comment="授权状态（到期自动回收，PRD 4.9.6）",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="临时授权 TTL，NULL=永久",
        ),
        sa.Column("granted_by", sa.BigInteger(), nullable=True, comment="授权操作人 ID"),
        sa.Column("reason", sa.String(512), nullable=True, comment="授权/回收事由"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_grant_user", "grants", ["user_id"], unique=False)
    op.create_index("idx_grant_domain", "grants", ["domain"], unique=False)
    op.create_index("idx_grant_status_expires", "grants", ["status", "expires_at"], unique=False)

    op.create_table(
        "classification",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column("catalog_id", sa.BigInteger(), nullable=False, comment="关联 db_catalog.id"),
        sa.Column(
            "sensitivity_level",
            sa.Enum(
                "PUBLIC",
                "INTERNAL",
                "CONFIDENTIAL",
                "PII",
                "UNKNOWN",
                name="classification_sensitivity",
            ),
            nullable=False,
            comment="敏感级别（UNKNOWN=分级引擎降级标记）",
        ),
        sa.Column("pii_columns", sa.JSON(), nullable=True, comment="命中 PII 的字段明细"),
        sa.Column("classified_by", sa.String(32), nullable=False, comment="分级来源"),
        sa.Column("model_version", sa.String(32), nullable=False, comment="规则/模型版本"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_classification_catalog", "classification", ["catalog_id"], unique=False)

    # user.role 扩容至六角色（保留历史 analyst，避免存量数据失效）
    op.alter_column(
        "user",
        "role",
        existing_type=sa.Enum(*_ROLE_ENUM_OLD, name="user_role"),
        type_=sa.Enum(*_ROLE_ENUM_NEW, name="user_role"),
        existing_nullable=False,
        existing_comment="用户角色",
    )


def downgrade() -> None:
    # 回滚前将新角色收敛为 viewer，保证枚举缩容不丢数据
    op.execute("UPDATE user SET role = 'viewer' WHERE role IN ('reviewer', 'compliance_officer')")
    op.alter_column(
        "user",
        "role",
        existing_type=sa.Enum(*_ROLE_ENUM_NEW, name="user_role"),
        type_=sa.Enum(*_ROLE_ENUM_OLD, name="user_role"),
        existing_nullable=False,
        existing_comment="用户角色",
    )

    op.drop_index("idx_classification_catalog", table_name="classification")
    op.drop_table("classification")
    op.drop_index("idx_grant_status_expires", table_name="grants")
    op.drop_index("idx_grant_domain", table_name="grants")
    op.drop_index("idx_grant_user", table_name="grants")
    op.drop_table("grants")
    op.drop_table("role")
