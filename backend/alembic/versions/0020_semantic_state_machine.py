"""语义模块工业级整改：状态机 + PENDING_VERSION + 健康度评分。

新增字段/表：
- metric 表新增 emergency_publish/emergency_reason/emergency_reviewed_at/gray_tenant_ids/pending_conflict/pending_conflict_detail
- metric_version 表 status 枚举扩展 PENDING_CONFIRMATION/EXPERIMENTAL/CANCELLED，新增 pending_deadline/extension_count/effective_at
- metric.status 枚举扩展 EXPERIMENTAL/DATA_SOURCE_DROPPED
- 新增 pending_version_confirmation 表
- 新增 metric_health_score 表

可回滚：所有操作为 ADD COLUMN / CREATE TABLE / ALTER ENUM，downgrade 中反向操作。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_semantic_state_machine"
down_revision = "0019_lineage_edge_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. metric.status 枚举扩展：新增 EXPERIMENTAL + DATA_SOURCE_DROPPED
    op.alter_column(
        "metric",
        "status",
        existing_type=sa.Enum("DRAFT", "REVIEW", "PUBLISHED", "EXPERIMENTAL", "DEPRECATED", "DATA_SOURCE_DROPPED", name="metric_status"),
        type_=sa.Enum("DRAFT", "REVIEW", "PUBLISHED", "EXPERIMENTAL", "DEPRECATED", "DATA_SOURCE_DROPPED", name="metric_status"),
        existing_nullable=False,
    )

    # 2. metric 表新增字段
    op.add_column(
        "metric",
        sa.Column("emergency_publish", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="紧急发布标记"),
    )
    op.add_column(
        "metric",
        sa.Column("emergency_reason", sa.Text(), nullable=True, comment="紧急发布原因"),
    )
    op.add_column(
        "metric",
        sa.Column("emergency_reviewed_at", sa.DateTime(), nullable=True, comment="紧急发布补审时间"),
    )
    op.add_column(
        "metric",
        sa.Column("gray_tenant_ids", sa.JSON(), nullable=True, comment="灰度白名单租户 ID 列表"),
    )
    op.add_column(
        "metric",
        sa.Column("pending_conflict", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="冲突预检标记"),
    )
    op.add_column(
        "metric",
        sa.Column("pending_conflict_detail", sa.JSON(), nullable=True, comment="冲突详情"),
    )

    # 3. metric_version.status 枚举扩展：新增 PENDING_CONFIRMATION/EXPERIMENTAL/CANCELLED
    op.alter_column(
        "metric_version",
        "status",
        existing_type=sa.Enum("DRAFT", "PENDING_REVIEW", "PUBLISHED", "ARCHIVED", name="version_status"),
        type_=sa.Enum("DRAFT", "PENDING_CONFIRMATION", "PUBLISHED", "EXPERIMENTAL", "ARCHIVED", "CANCELLED", name="version_status"),
        existing_nullable=False,
    )

    # 4. metric_version 表新增字段
    op.add_column(
        "metric_version",
        sa.Column("pending_deadline", sa.DateTime(), nullable=True, comment="PENDING_VERSION 确认截止时间"),
    )
    op.add_column(
        "metric_version",
        sa.Column("extension_count", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="延期次数（最多 1 次）"),
    )
    op.add_column(
        "metric_version",
        sa.Column("effective_at", sa.DateTime(), nullable=True, comment="实际生效时间"),
    )

    # 5. 创建 pending_version_confirmation 表
    op.create_table(
        "pending_version_confirmation",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("metric_id", sa.BigInteger(), sa.ForeignKey("metric.id", name="fk_pending_confirm_metric"), nullable=False, comment="指标 ID"),
        sa.Column("version", sa.Integer(), nullable=False, comment="版本号"),
        sa.Column("consumer_id", sa.BigInteger(), nullable=False, comment="消费方用户 ID"),
        sa.Column("status", sa.Enum("PENDING", "CONFIRMED", "REJECTED", "TIMEOUT_ACCEPTED", name="pending_confirm_status"), nullable=False, server_default="PENDING", comment="确认状态"),
        sa.Column("reason", sa.Text(), nullable=True, comment="拒绝原因"),
        sa.Column("extension_count", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="延期次数"),
        sa.Column("deadline", sa.DateTime(), nullable=False, comment="确认截止时间"),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True, comment="确认/拒绝时间"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now(), comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True, comment="软删除时间"),
        sa.UniqueConstraint("metric_id", "version", "consumer_id", name="uk_pending_confirm"),
    )
    op.create_index("idx_pending_deadline", "pending_version_confirmation", ["status", "deadline"])

    # 6. 创建 metric_health_score 表
    op.create_table(
        "metric_health_score",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("metric_id", sa.BigInteger(), nullable=False, unique=True, comment="指标 ID（唯一）"),
        sa.Column("score", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="综合评分 0-100"),
        sa.Column("level", sa.Enum("EXCELLENT", "GOOD", "WARNING", "CRITICAL", name="health_level_enum"), nullable=False, server_default="CRITICAL", comment="分级"),
        sa.Column("completeness_score", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="口径完整度 0-100"),
        sa.Column("activity_score", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="活跃度 0-100"),
        sa.Column("quality_score", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="质量 0-100"),
        sa.Column("owner_response_score", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="Owner 响应 0-100"),
        sa.Column("lineage_coverage_score", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="血缘覆盖 0-100"),
        sa.Column("missing_dimensions", sa.JSON(), nullable=True, comment="数据不足的维度列表"),
        sa.Column("calculated_at", sa.DateTime(), nullable=False, comment="评分计算时间"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now(), comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True, comment="软删除时间"),
    )
    op.create_index("idx_health_level", "metric_health_score", ["level"])
    op.create_index("idx_health_score", "metric_health_score", ["score"])


def downgrade() -> None:
    # 6. 删除 metric_health_score 表
    op.drop_index("idx_health_score", table_name="metric_health_score")
    op.drop_index("idx_health_level", table_name="metric_health_score")
    op.drop_table("metric_health_score")

    # 5. 删除 pending_version_confirmation 表
    op.drop_index("idx_pending_deadline", table_name="pending_version_confirmation")
    op.drop_table("pending_version_confirmation")

    # 4. 移除 metric_version 新增字段
    op.drop_column("metric_version", "effective_at")
    op.drop_column("metric_version", "extension_count")
    op.drop_column("metric_version", "pending_deadline")

    # 3. 还原 metric_version.status 枚举
    op.alter_column(
        "metric_version",
        "status",
        existing_type=sa.Enum("DRAFT", "PENDING_CONFIRMATION", "PUBLISHED", "EXPERIMENTAL", "ARCHIVED", "CANCELLED", name="version_status"),
        type_=sa.Enum("DRAFT", "PENDING_REVIEW", "PUBLISHED", "ARCHIVED", name="version_status"),
        existing_nullable=False,
    )

    # 2. 移除 metric 新增字段
    op.drop_column("metric", "pending_conflict_detail")
    op.drop_column("metric", "pending_conflict")
    op.drop_column("metric", "gray_tenant_ids")
    op.drop_column("metric", "emergency_reviewed_at")
    op.drop_column("metric", "emergency_reason")
    op.drop_column("metric", "emergency_publish")

    # 1. 还原 metric.status 枚举（MySQL 保留旧值，无需操作）
