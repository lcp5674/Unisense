"""consume 消费层表（TD §12.6 / FR-12,13）

新增：api_client（消费方接入方）、metric_value_snapshot（结果快照 WORM）、
user_preference（用户收藏/偏好）。

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005_quality"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "api_client",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False, comment="接入方 ID"),
        sa.Column(
            "client_secret_ref", sa.String(255), nullable=False, comment="secret bcrypt 哈希"
        ),
        sa.Column("scope_domain", sa.String(64), nullable=True, comment="授权域"),
        sa.Column("metric_whitelist", sa.JSON(), nullable=True, comment="指标白名单"),
        sa.Column("qps", sa.BigInteger(), nullable=False, server_default="20", comment="QPS 配额"),
        sa.Column(
            "daily_quota",
            sa.BigInteger(),
            nullable=False,
            server_default="100000",
            comment="日查询配额",
        ),
        sa.Column("scan_row_limit", sa.BigInteger(), nullable=True, comment="行扫描上限"),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "REVOKED", name="client_status"),
            nullable=False,
            server_default="ACTIVE",
            comment="接入方状态",
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=False, comment="创建人"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", name="uk_api_client_id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("ix_api_client_status", "api_client", ["status"])

    op.create_table(
        "metric_value_snapshot",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("metric_code", sa.String(64), nullable=False, comment="指标码"),
        sa.Column("version", sa.BigInteger(), nullable=False, comment="生效版本"),
        sa.Column("dims", sa.JSON(), nullable=False, comment="维度组合"),
        sa.Column("date_range", sa.String(64), nullable=False, comment="日期区间"),
        sa.Column("value_json", sa.JSON(), nullable=False, comment="结果值"),
        sa.Column("quality_flag", sa.String(32), nullable=True, comment="质量标记"),
        sa.Column(
            "generated_at", sa.DateTime(timezone=True), nullable=False, comment="数据生成时间"
        ),
        sa.Column(
            "generated_by",
            sa.Enum("QUERY", "MATERIALIZE", name="snapshot_generated_by"),
            nullable=False,
            server_default="QUERY",
            comment="生成来源",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("ix_snapshot_metric_code", "metric_value_snapshot", ["metric_code"])

    op.create_table(
        "user_preference",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="用户 ID"),
        sa.Column("preference_key", sa.String(64), nullable=False, comment="偏好键"),
        sa.Column("preference_value", sa.JSON(), nullable=False, comment="偏好值"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "preference_key", name="uk_pref"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("ix_user_preference_user_id", "user_preference", ["user_id"])


def downgrade() -> None:
    op.drop_table("user_preference")
    op.drop_table("metric_value_snapshot")
    op.drop_index("ix_api_client_status", table_name="api_client")
    op.drop_table("api_client")
