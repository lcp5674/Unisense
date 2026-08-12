"""P2 增量：audit_log archived 字段 + audit_archive_log 表 + metric_template 表 + metric.template_id 字段 + lineage_edge.pii_inherited 字段。

可回滚：所有操作为 ADD COLUMN 或 CREATE TABLE，downgrade 中反向操作。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_p2_audit_template_pii"
down_revision = "0016_quality_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. audit_log: 添加 archived 字段
    op.add_column(
        "audit_log",
        sa.Column(
            "archived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="是否已归档",
        ),
    )
    op.create_index(
        "idx_audit_archived_created",
        "audit_log",
        ["archived", "created_at"],
    )

    # 2. audit_archive_log 表
    op.create_table(
        "audit_archive_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("archive_date", sa.Date(), nullable=False, comment="归档日期"),
        sa.Column("rows_archived", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="归档行数"),
        sa.Column("s3_key", sa.String(512), nullable=True, comment="MinIO/S3 对象键"),
        sa.Column("s3_size_bytes", sa.BigInteger(), nullable=True, comment="对象大小(bytes)"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending", comment="归档状态"),
        sa.Column("error_message", sa.Text(), nullable=True, comment="错误信息"),
        sa.Column("completed_at", sa.DateTime(), nullable=True, comment="完成时间"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now(), comment="更新时间"),
    )

    # 3. metric_template 表
    op.create_table(
        "metric_template",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True, comment="模板编码"),
        sa.Column("name", sa.String(128), nullable=False, comment="模板名称"),
        sa.Column("domain", sa.String(64), nullable=False, comment="适用域"),
        sa.Column("description", sa.Text(), nullable=True, comment="模板说明"),
        sa.Column("defaults_json", sa.JSON(), nullable=False, comment="预填字段默认值"),
        sa.Column("required_fields", sa.JSON(), nullable=True, comment="必填字段列表"),
        sa.Column("type", sa.String(32), nullable=True, comment="指标类型预设"),
        sa.Column("granularity", sa.String(64), nullable=True, comment="粒度预设"),
        sa.Column("unit", sa.String(32), nullable=True, comment="单位预设"),
        sa.Column("aggregation", sa.String(32), nullable=True, comment="聚合方式预设"),
        sa.Column("time_semantics", sa.String(32), nullable=True, comment="时间语义预设"),
        sa.Column("freshness", sa.String(32), nullable=True, comment="数据新鲜度预设"),
        sa.Column("dw_layer", sa.String(32), nullable=True, comment="数仓分层预设"),
        sa.Column("serving_mode", sa.String(32), nullable=True, comment="服务模式预设"),
        sa.Column("additivity", sa.String(32), nullable=True, comment="可加性预设"),
        sa.Column("metric_tier", sa.String(8), nullable=True, comment="指标分级预设"),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1"), comment="模板版本号"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1"), comment="是否启用"),
        sa.Column("created_by", sa.Integer(), nullable=True, comment="创建人 ID"),
        sa.Column("published_at", sa.DateTime(), nullable=True, comment="发布时间"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now(), comment="更新时间"),
    )
    op.create_index("idx_template_domain", "metric_template", ["domain"])
    op.create_index("idx_template_active", "metric_template", ["is_active"])

    # 4. metric: 添加 template_id 字段
    op.add_column(
        "metric",
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("metric_template.id", name="fk_metric_template"),
            nullable=True,
            comment="关联模板 ID",
        ),
    )

    # 5. lineage_edge: 添加 pii_inherited 字段
    op.add_column(
        "lineage_edge",
        sa.Column(
            "pii_inherited",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="PII 是否沿血缘继承",
        ),
    )


def downgrade() -> None:
    # 5. 移除 lineage_edge.pii_inherited
    op.drop_column("lineage_edge", "pii_inherited")

    # 4. 移除 metric.template_id
    op.drop_column("metric", "template_id")

    # 3. 删除 metric_template 表
    op.drop_index("idx_template_active", table_name="metric_template")
    op.drop_index("idx_template_domain", table_name="metric_template")
    op.drop_table("metric_template")

    # 2. 删除 audit_archive_log 表
    op.drop_table("audit_archive_log")

    # 1. 移除 audit_log.archived 字段 + 索引
    op.drop_index("idx_audit_archived_created", table_name="audit_log")
    op.drop_column("audit_log", "archived")
