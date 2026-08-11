"""新增 quality_observation 表（Epic 6：动态基线 / 同环比 / 跨源检测数据时序底座）。

非破坏性、可回滚：仅新建一张表与索引，不改动既有表。观测样本在采集 / 产出分区
就绪时写入，供质量引擎的三种高级模式（dynamic_baseline / yoy_woy / cross_source）
复用历史观测值，避免对源库重复下推。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_quality_observation"
down_revision = "0014_benchmark_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quality_observation",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column("metric_id", sa.BigInteger(), nullable=False, comment="关联指标 ID"),
        sa.Column("metric_code", sa.String(length=64), nullable=False, comment="指标编码"),

        sa.Column(
            "source_id",
            sa.String(length=64),
            nullable=True,
            comment="观测来源（跨源分组；空表示平台聚合值）",
        ),
        sa.Column(
            "obs_time",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="观测时间（分区就绪时刻）",
        ),
        sa.Column("value", sa.Numeric(precision=18, scale=4), nullable=False, comment="观测聚合值"),
        sa.Column("dims", sa.JSON(), nullable=True, comment="维度上下文（如 region/channel）"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_quality_obs_metric", "quality_observation", ["metric_id"], unique=False)
    op.create_index("idx_quality_obs_code", "quality_observation", ["metric_code"], unique=False)
    op.create_index("idx_quality_obs_source", "quality_observation", ["source_id"], unique=False)
    op.create_index("idx_quality_obs_time", "quality_observation", ["obs_time"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_quality_obs_time", table_name="quality_observation")
    op.drop_index("idx_quality_obs_source", table_name="quality_observation")
    op.drop_index("idx_quality_obs_code", table_name="quality_observation")
    op.drop_index("idx_quality_obs_metric", table_name="quality_observation")
    op.drop_table("quality_observation")
