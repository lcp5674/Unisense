"""采集模块工业级修复：SchemaDriftLog + CollectionWatermark 表 + DataSource/DBCatalog 新增字段。

可回滚：所有操作为 CREATE TABLE 或 ADD COLUMN，downgrade 中反向操作。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_collector_drift_watermark"
down_revision = "0017_p2_audit_template_pii"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 创建 schema_drift_log 表
    op.create_table(
        "schema_drift_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.String(64), sa.ForeignKey("data_source.source_id", name="fk_drift_log_source"), nullable=False, comment="数据源标识"),
        sa.Column("entity_name", sa.String(256), nullable=False, comment="实体名"),
        sa.Column(
            "change_type",
            sa.Enum("ADD_COLUMN", "DROP_COLUMN", "TYPE_CHANGE", "SCHEMA_CHANGED", name="drift_change_type_enum"),
            nullable=False,
            comment="变更类型",
        ),
        sa.Column("before_signature", sa.String(64), nullable=True, comment="变更前内容指纹"),
        sa.Column("after_signature", sa.String(64), nullable=False, comment="变更后内容指纹"),
        sa.Column("before_schema", sa.JSON(), nullable=True, comment="变更前 schema"),
        sa.Column("after_schema", sa.JSON(), nullable=False, comment="变更后 schema"),
        sa.Column("diff_json", sa.JSON(), nullable=True, comment="差异详情"),
        sa.Column("detected_at", sa.DateTime(), nullable=False, comment="检测时间"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now(), comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True, comment="软删除时间"),
    )
    op.create_index("idx_drift_source_entity", "schema_drift_log", ["source_id", "entity_name"])
    op.create_index("idx_drift_detected_at", "schema_drift_log", ["detected_at"])

    # 2. 创建 collection_watermark 表
    op.create_table(
        "collection_watermark",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.String(64), sa.ForeignKey("data_source.source_id", name="fk_watermark_source"), nullable=False, unique=True, comment="数据源标识（唯一）"),
        sa.Column("last_collected_at", sa.DateTime(), nullable=False, comment="最后采集时间"),
        sa.Column(
            "mode",
            sa.Enum("FULL", "INCREMENTAL", name="watermark_mode_enum"),
            nullable=False,
            server_default="FULL",
            comment="采集模式",
        ),
        sa.Column("scanned_count", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="采集表数"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="失败表数"),
        sa.Column("content_fingerprints", sa.JSON(), nullable=False, comment="实体级指纹映射"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now(), comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True, comment="软删除时间"),
    )
    op.create_index("idx_watermark_source", "collection_watermark", ["source_id"])

    # 3. DataSource 新增 schedule_cron / collection_mode 字段
    op.add_column(
        "data_source",
        sa.Column(
            "schedule_cron",
            sa.String(100),
            nullable=True,
            comment="定时调度 cron 表达式",
        ),
    )
    op.add_column(
        "data_source",
        sa.Column(
            "collection_mode",
            sa.String(16),
            nullable=False,
            server_default="FULL",
            comment="采集模式（FULL/INCREMENTAL）",
        ),
    )

    # 4. DBCatalog 新增 content_signature / schema_incomplete 字段
    op.add_column(
        "db_catalog",
        sa.Column(
            "content_signature",
            sa.String(64),
            nullable=True,
            comment="内容指纹 SHA-256(canonical_schema_json)",
        ),
    )
    op.add_column(
        "db_catalog",
        sa.Column(
            "schema_incomplete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="空 schema 标记",
        ),
    )

    # 5. 修改 entity_type_enum：新增 VIEW/FIELD 值（MySQL ALTER ENUM 需重建）
    # 注意：MySQL 不支持 ALTER ENUM 直接加值，需修改列定义
    # 由于旧枚举仅含 table/field，新枚举为 TABLE/VIEW/FIELD
    op.alter_column(
        "db_catalog",
        "entity_type",
        existing_type=sa.Enum("table", "field", name="entity_type_enum"),
        type_=sa.Enum("TABLE", "VIEW", "FIELD", name="entity_type_enum"),
        existing_nullable=False,
    )

    # 6. 修改 sensitivity_enum：新增 NEEDS_REVIEW 值
    op.alter_column(
        "db_catalog",
        "sensitivity_level",
        existing_type=sa.Enum("PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", name="sensitivity_enum"),
        type_=sa.Enum("PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "NEEDS_REVIEW", name="sensitivity_enum"),
        existing_nullable=False,
    )

    # 7. 修改 source_type_enum：更新枚举值
    op.alter_column(
        "data_source",
        "source_type",
        existing_type=sa.Enum(
            "mysql", "postgres", "hive", "doris", "starrocks", "clickhouse", "maxcompute",
            name="source_type_enum",
        ),
        type_=sa.Enum(
            "mysql", "postgres", "hive", "doris", "clickhouse", "kafka", "starrocks",
            name="source_type_enum",
        ),
        existing_nullable=False,
    )


def downgrade() -> None:
    # 7. 还原 source_type_enum
    op.alter_column(
        "data_source",
        "source_type",
        existing_type=sa.Enum(
            "mysql", "postgres", "hive", "doris", "clickhouse", "kafka", "starrocks",
            name="source_type_enum",
        ),
        type_=sa.Enum(
            "mysql", "postgres", "hive", "doris", "starrocks", "clickhouse", "maxcompute",
            name="source_type_enum",
        ),
        existing_nullable=False,
    )

    # 6. 还原 sensitivity_enum
    op.alter_column(
        "db_catalog",
        "sensitivity_level",
        existing_type=sa.Enum("PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "NEEDS_REVIEW", name="sensitivity_enum"),
        type_=sa.Enum("PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", name="sensitivity_enum"),
        existing_nullable=False,
    )

    # 5. 还原 entity_type_enum
    op.alter_column(
        "db_catalog",
        "entity_type",
        existing_type=sa.Enum("TABLE", "VIEW", "FIELD", name="entity_type_enum"),
        type_=sa.Enum("table", "field", name="entity_type_enum"),
        existing_nullable=False,
    )

    # 4. 移除 DBCatalog 新增字段
    op.drop_column("db_catalog", "schema_incomplete")
    op.drop_column("db_catalog", "content_signature")

    # 3. 移除 DataSource 新增字段
    op.drop_column("data_source", "collection_mode")
    op.drop_column("data_source", "schedule_cron")

    # 2. 删除 collection_watermark 表
    op.drop_index("idx_watermark_source", table_name="collection_watermark")
    op.drop_table("collection_watermark")

    # 1. 删除 schema_drift_log 表
    op.drop_index("idx_drift_detected_at", table_name="schema_drift_log")
    op.drop_index("idx_drift_source_entity", table_name="schema_drift_log")
    op.drop_table("schema_drift_log")
