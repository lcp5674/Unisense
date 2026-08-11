"""initial schema (organization/user/term/data_source/metric/metric_version/db_catalog/audit_log)

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-07

对齐 TD §4.1 与 DEV_GUIDE §9（up + down 均可执行、数据无损）。
手写迁移，避免对运行态数据库的依赖；后续变更一律追加新 revision。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


# ---- 公共表选项（InnoDB + utf8mb4，对齐 DEV_GUIDE §8a.4）----
_TABLE_OPTS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    # ---- organization（租户，顶级数据隔离单元）----
    op.create_table(
        "organization",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column("name", sa.String(128), nullable=False, comment="组织名称"),
        sa.Column(
            "code",
            sa.String(64),
            nullable=False,
            unique=True,
            comment="组织编码（唯一）",
        ),
        sa.Column(
            "status",
            sa.Enum("active", "suspended", "deleted", name="org_status"),
            nullable=False,
            comment="组织状态",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_organization_code"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_organization_code", "organization", ["code"], unique=False)

    # ---- user ----
    op.create_table(
        "user",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column(
            "org_id",
            sa.BigInteger(),
            sa.ForeignKey("organization.id", name="fk_user_organization"),
            nullable=False,
            comment="所属组织 ID",
        ),
        sa.Column("username", sa.String(64), nullable=False, comment="用户名"),
        sa.Column("email", sa.String(128), nullable=False, unique=True, comment="邮箱（唯一）"),
        sa.Column("password_hash", sa.String(256), nullable=False, comment="密码哈希（bcrypt）"),
        sa.Column("display_name", sa.String(128), nullable=False, comment="显示名称"),
        sa.Column(
            "role",
            sa.Enum(
                "platform_admin",
                "domain_admin",
                "metric_owner",
                "analyst",
                "viewer",
                name="user_role",
            ),
            nullable=False,
            comment="用户角色",
        ),
        sa.Column("domain", sa.String(64), nullable=True, comment="所属域"),
        sa.Column(
            "status",
            sa.Enum("active", "disabled", "deleted", name="user_status"),
            nullable=False,
            comment="用户状态",
        ),
        sa.Column("last_login_at", sa.DateTime(), nullable=True, comment="最后登录时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_user_email"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_user_org", "user", ["org_id"], unique=False)
    op.create_index("idx_user_role", "user", ["role"], unique=False)

    # ---- term（术语库）----
    op.create_table(
        "term",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column(
            "term_code", sa.String(64), nullable=False, unique=True, comment="术语编码（唯一）"
        ),
        sa.Column("name", sa.String(128), nullable=False, comment="术语名称"),
        sa.Column("definition", sa.Text(), nullable=False, comment="术语定义"),
        sa.Column("domain", sa.String(64), nullable=False, comment="所属域"),
        sa.Column("synonyms", mysql.JSON(), nullable=False, comment="同义词列表"),
        sa.Column("boundary", sa.Text(), nullable=True, comment="边界说明"),
        sa.Column(
            "status",
            sa.Enum("DRAFT", "PUBLISHED", "DEPRECATED", name="term_status_enum"),
            nullable=False,
            comment="术语状态",
        ),
        sa.Column(
            "owner_id",
            sa.BigInteger(),
            sa.ForeignKey("user.id", name="fk_term_owner"),
            nullable=False,
            comment="Owner ID",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("term_code", name="uq_term_code"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_term_domain", "term", ["domain"], unique=False)
    op.create_index("idx_term_status", "term", ["status"], unique=False)

    # ---- data_source ----
    op.create_table(
        "data_source",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column(
            "source_id", sa.String(64), nullable=False, unique=True, comment="数据源标识（唯一）"
        ),
        sa.Column("name", sa.String(128), nullable=False, comment="数据源名称"),
        sa.Column(
            "source_type",
            sa.Enum(
                "mysql",
                "postgres",
                "hive",
                "doris",
                "starrocks",
                "clickhouse",
                "maxcompute",
                name="source_type_enum",
            ),
            nullable=False,
            comment="数据源类型",
        ),
        sa.Column("connection_config", sa.Text(), nullable=False, comment="连接配置（加密存储）"),
        sa.Column("domain", sa.String(64), nullable=False, comment="所属域"),
        sa.Column("coverage", sa.Float(), nullable=False, comment="资产覆盖率"),
        sa.Column(
            "quota", mysql.JSON(), nullable=False, comment="配额（max_concurrency/max_scan_rows）"
        ),
        sa.Column(
            "health_status",
            sa.Enum("healthy", "unhealthy", "unknown", name="health_status_enum"),
            nullable=False,
            comment="健康状态",
        ),
        sa.Column("cluster_id", sa.String(64), nullable=False, comment="物理集群标识"),
        sa.Column("last_health_check", sa.DateTime(), nullable=True, comment="最后健康检查时间"),
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("user.id", name="fk_data_source_user"),
            nullable=False,
            comment="创建人 ID",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", name="uq_data_source_source_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_data_source_domain", "data_source", ["domain"], unique=False)

    # ---- metric（语义层核心）----
    op.create_table(
        "metric",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column(
            "metric_code", sa.String(64), nullable=False, unique=True, comment="指标编码（唯一）"
        ),
        sa.Column("name", sa.String(128), nullable=False, comment="指标名称"),
        sa.Column("domain", sa.String(64), nullable=False, comment="所属域"),
        sa.Column(
            "type",
            sa.Enum("atomic", "derived", "composite", name="metric_type"),
            nullable=False,
            comment="指标类型",
        ),
        sa.Column("granularity", sa.String(64), nullable=False, comment="粒度"),
        sa.Column("unit", sa.String(32), nullable=False, comment="单位"),
        sa.Column("currency", sa.String(16), nullable=True, comment="币种"),
        sa.Column(
            "aggregation",
            sa.Enum("SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", name="agg_type"),
            nullable=False,
            comment="聚合方式",
        ),
        sa.Column(
            "time_semantics",
            sa.Enum("PERIOD", "YTD", "TTM", "AVG", name="time_sem"),
            nullable=False,
            comment="时间语义",
        ),
        sa.Column(
            "freshness",
            sa.Enum("REALTIME", "T1", "HOURLY", name="freshness_type"),
            nullable=False,
            comment="数据新鲜度",
        ),
        sa.Column("sla", sa.String(128), nullable=True, comment="SLA 契约"),
        sa.Column(
            "dw_layer",
            sa.Enum("ODS", "DWD", "DWS", "ADS", "DM", name="dw_layer_type"),
            nullable=False,
            comment="数仓分层",
        ),
        sa.Column(
            "metric_tier",
            sa.Enum("T1", "T2", "T3", name="metric_tier_type"),
            nullable=False,
            comment="指标分级",
        ),
        sa.Column(
            "serving_mode",
            sa.Enum("BATCH_ONLY", "REALTIME_ONLY", "BATCH_REALTIME_DUAL", name="serving_mode_type"),
            nullable=False,
            comment="服务模式",
        ),
        sa.Column(
            "additivity",
            sa.Enum("ADDITIVE", "SEMI_ADDITIVE", "NON_ADDITIVE", name="additivity_type"),
            nullable=False,
            comment="可加性",
        ),
        sa.Column("non_additive_dimensions", mysql.JSON(), nullable=True, comment="不可加维度列表"),
        sa.Column("definition_json", mysql.JSON(), nullable=False, comment="口径定义"),
        sa.Column("version", sa.Integer(), nullable=False, comment="当前版本号"),
        sa.Column("row_version", sa.Integer(), nullable=False, comment="乐观锁行版本"),
        sa.Column(
            "term_id",
            sa.BigInteger(),
            sa.ForeignKey("term.id", name="fk_metric_term"),
            nullable=True,
            comment="关联术语 ID",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "REVIEW",
                "PUBLISHED",
                "EXPERIMENTAL",
                "DEPRECATED",
                "DATA_SOURCE_DROPPED",
                name="metric_status",
            ),
            nullable=False,
            comment="指标状态",
        ),
        sa.Column(
            "owner_id",
            sa.BigInteger(),
            sa.ForeignKey("user.id", name="fk_metric_owner"),
            nullable=False,
            comment="主 Owner ID",
        ),
        sa.Column(
            "backup_owner_id",
            sa.BigInteger(),
            sa.ForeignKey("user.id", name="fk_metric_backup_owner"),
            nullable=True,
            comment="副 Owner ID",
        ),
        sa.Column(
            "approver_id",
            sa.BigInteger(),
            sa.ForeignKey("user.id", name="fk_metric_approver"),
            nullable=True,
            comment="审批人 ID",
        ),
        sa.Column("pii_flag", sa.Boolean(), nullable=False, comment="是否含 PII"),
        sa.Column("compliance_reviewed", sa.Boolean(), nullable=False, comment="是否已合规审核"),
        sa.Column("effective_version", sa.Integer(), nullable=True, comment="当前生效版本"),
        sa.Column("consumption_guide", mysql.JSON(), nullable=True, comment="消费指南"),
        sa.Column("batch_id", sa.String(64), nullable=True, comment="批量注册批次 ID"),
        sa.Column("successor_code", sa.String(64), nullable=True, comment="替代指标码"),
        sa.Column("deprecated_at", sa.DateTime(), nullable=True, comment="废弃时间"),
        sa.Column("sunset_until", sa.Date(), nullable=True, comment="Sunset 截止日期"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("metric_code", name="uq_metric_code"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_metric_status", "metric", ["status"], unique=False)
    op.create_index("idx_metric_domain", "metric", ["domain"], unique=False)
    op.create_index("idx_metric_tier", "metric", ["metric_tier"], unique=False)
    op.create_index("idx_metric_batch", "metric", ["batch_id"], unique=False)

    # ---- metric_version ----
    op.create_table(
        "metric_version",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column(
            "metric_id",
            sa.BigInteger(),
            sa.ForeignKey("metric.id", name="fk_metric_version_metric"),
            nullable=False,
            comment="指标 ID",
        ),
        sa.Column("version", sa.Integer(), nullable=False, comment="版本号"),
        sa.Column(
            "change_type",
            sa.Enum(
                "CREATE",
                "UPDATE",
                "BREAKING",
                "DEPRECATE",
                "RESTORE",
                name="change_type_enum",
            ),
            nullable=False,
            comment="变更类型",
        ),
        sa.Column("definition_json", mysql.JSON(), nullable=False, comment="口径快照"),
        sa.Column("diff_json", mysql.JSON(), nullable=True, comment="结构化 diff"),
        sa.Column(
            "status",
            sa.Enum("DRAFT", "PENDING_REVIEW", "PUBLISHED", "ARCHIVED", name="version_status"),
            nullable=False,
            comment="版本状态",
        ),
        sa.Column("change_reason", sa.Text(), nullable=False, comment="变更原因"),
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("user.id", name="fk_metric_version_user"),
            nullable=False,
            comment="创建人 ID",
        ),
        sa.Column("published_at", sa.DateTime(), nullable=True, comment="发布时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("metric_id", "version", name="uk_metric_version"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    # ---- db_catalog ----
    op.create_table(
        "db_catalog",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("data_source.source_id", name="fk_db_catalog_source"),
            nullable=False,
            comment="数据源标识",
        ),
        sa.Column("entity_name", sa.String(256), nullable=False, comment="实体名（库.表）"),
        sa.Column(
            "entity_type",
            sa.Enum("table", "field", name="entity_type_enum"),
            nullable=False,
            comment="实体类型",
        ),
        sa.Column("schema_json", mysql.JSON(), nullable=False, comment="字段/类型/注释/索引"),
        sa.Column("etl_sql", sa.Text(), nullable=True, comment="源端 ETL SQL"),
        sa.Column(
            "sensitivity_level",
            sa.Enum("PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", name="sensitivity_enum"),
            nullable=False,
            comment="敏感级别",
        ),
        sa.Column(
            "owner_id",
            sa.BigInteger(),
            sa.ForeignKey("user.id", name="fk_db_catalog_owner"),
            nullable=True,
            comment="Owner ID（可空=孤儿资产）",
        ),
        sa.Column("upstream_signature", sa.String(64), nullable=False, comment="幂等键"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "entity_name", name="uk_db_catalog_entity"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_db_catalog_owner", "db_catalog", ["owner_id"], unique=False)
    op.create_index("idx_db_catalog_sens", "db_catalog", ["sensitivity_level"], unique=False)

    # ---- audit_log（WORM：无 deleted_at）----
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True, comment="主键 ID"),
        sa.Column(
            "actor_id",
            sa.BigInteger(),
            sa.ForeignKey("user.id", name="fk_audit_log_user"),
            nullable=False,
            comment="操作人 ID",
        ),
        sa.Column("action", sa.String(64), nullable=False, comment="操作类型"),
        sa.Column("entity_type", sa.String(64), nullable=False, comment="实体类型"),
        sa.Column("entity_id", sa.String(64), nullable=False, comment="实体 ID"),
        sa.Column("detail_json", mysql.JSON(), nullable=True, comment="操作详情"),
        sa.Column("ip", sa.String(64), nullable=False, comment="操作 IP"),
        sa.Column("trace_id", sa.String(64), nullable=False, comment="链路追踪 ID"),
        sa.Column("pii_access", sa.Boolean(), nullable=False, comment="是否涉及 PII 访问"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_audit_log_actor", "audit_log", ["actor_id"], unique=False)
    op.create_index("idx_audit_log_entity", "audit_log", ["entity_type", "entity_id"], unique=False)
    op.create_index("idx_audit_log_trace", "audit_log", ["trace_id"], unique=False)


def downgrade() -> None:
    # 子表先于父表删除；DROP TABLE 会自动清理索引与 FOREIGN KEY，
    # 故不显式 DROP INDEX（MySQL 下被外键使用的索引无法直接删除，报 1553）。
    op.drop_table("audit_log")
    op.drop_table("db_catalog")
    op.drop_table("metric_version")
    op.drop_table("metric")
    op.drop_table("data_source")
    op.drop_table("term")
    op.drop_table("user")
    op.drop_table("organization")
