"""血缘增量采集与失效管理：lineage_edge 增补失效字段 + 新建 lineage_ingest_run。

背景：血缘视图「采集通道」需要感知各来源（dp_csv/quickbi/数据接口）的运行状态、
变更摘要与失效边队列。本迁移：
1. lineage_edge 增加 last_seen_at/missing_count/stale/stale_since 四列——
   支撑增量采集的「最近确认时间」「观察期计数」「失效队列标记」；
2. 新建 lineage_ingest_run 表——每次增量采集写一条运行记录（新增/更新/未再出现/
   新失效/恢复边数），供前端展示来源新鲜度与变更摘要。

可逆：downgrade 删除 lineage_ingest_run 表并回退 lineage_edge 四列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_lineage_ingest"
down_revision = "0037_llm_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) lineage_edge 增补增量采集与失效管理字段
    op.add_column(
        "lineage_edge",
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一次被采集通道确认存在的时间（UTC）",
        ),
    )
    op.add_column(
        "lineage_edge",
        sa.Column(
            "missing_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="连续未被采集通道确认的轮次（观察期计数）",
        ),
    )
    op.add_column(
        "lineage_edge",
        sa.Column(
            "stale",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="是否进入失效队列（等待确认删除或恢复）",
        ),
    )
    op.add_column(
        "lineage_edge",
        sa.Column(
            "stale_since",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="进入失效队列的时间（UTC）",
        ),
    )

    # 2) 新建采集通道运行记录表
    op.create_table(
        "lineage_ingest_run",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            comment="来源通道，如 dp_csv",
        ),
        sa.Column(
            "run_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            comment="运行时间（UTC）",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            comment="running/success/failed",
        ),
        sa.Column(
            "total_edges",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="本次采集确认的边总数",
        ),
        sa.Column(
            "added_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="本次新增边数",
        ),
        sa.Column(
            "updated_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="本次更新边数",
        ),
        sa.Column(
            "missing_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="本次未再出现的边数（观察期累加）",
        ),
        sa.Column(
            "stale_flagged_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="本次新进入失效队列的边数",
        ),
        sa.Column(
            "restored_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="本次恢复的失效边数",
        ),
        sa.Column("error", sa.Text(), nullable=True, comment="失败原因（status=failed 时）"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            comment="创建时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        comment="血缘采集通道运行记录（增量采集审计）",
    )
    op.create_index(
        "ix_lineage_ingest_run_source",
        "lineage_ingest_run",
        ["source"],
    )


def downgrade() -> None:
    op.drop_index("ix_lineage_ingest_run_source", table_name="lineage_ingest_run")
    op.drop_table("lineage_ingest_run")
    op.drop_column("lineage_edge", "stale_since")
    op.drop_column("lineage_edge", "stale")
    op.drop_column("lineage_edge", "missing_count")
    op.drop_column("lineage_edge", "last_seen_at")
