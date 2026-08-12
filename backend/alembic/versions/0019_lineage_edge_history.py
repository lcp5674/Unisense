"""血缘增强：lineage_edge_history 历史快照表 + lineage_edge 枚举扩展 + owner 字段。

能力对齐（模块需求）：
1. 扩展 lineage_edge.edge_type 枚举，新增 EXTERNAL_BREAK（断链登记）。
2. 扩展 lineage_edge.granularity 枚举，新增 L3（指标级血缘）。
3. lineage_edge 新增 owner 字段（人工断链等人工边登记人）。
4. 新建 lineage_edge_history 表（WORM 历史快照，含 source/target 索引）。

可回滚：所有操作均为 ADD/ALTER/CREATE，downgrade 中反向操作，数据无损。
"""  # noqa: E501

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_lineage_edge_history"
down_revision = "0018_collector_drift_watermark"
branch_labels = None
depends_on = None

#: 与 app.models.lineage.LineageEdge / LineageEdgeHistory 声明保持一致的枚举取值。
_EDGE_TYPES_BEFORE = ("DERIVED_FROM", "LINEAGE_UP", "LINEAGE_DOWN", "CONSUMED_BY")
_EDGE_TYPES_AFTER = ("DERIVED_FROM", "LINEAGE_UP", "LINEAGE_DOWN", "CONSUMED_BY", "EXTERNAL_BREAK")
_GRANULARITIES_BEFORE = ("L1", "L2")
_GRANULARITIES_AFTER = ("L1", "L2", "L3")


def upgrade() -> None:
    # 1. lineage_edge.edge_type：新增 EXTERNAL_BREAK（断链登记）
    op.alter_column(
        "lineage_edge",
        "edge_type",
        existing_type=sa.Enum(*_EDGE_TYPES_BEFORE, name="lineage_edge_type"),
        type_=sa.Enum(*_EDGE_TYPES_AFTER, name="lineage_edge_type"),
        existing_nullable=False,
        comment="血缘边类型",
    )
    # 2. lineage_edge.granularity：新增 L3（指标级血缘）
    op.alter_column(
        "lineage_edge",
        "granularity",
        existing_type=sa.Enum(*_GRANULARITIES_BEFORE, name="lineage_granularity"),
        type_=sa.Enum(*_GRANULARITIES_AFTER, name="lineage_granularity"),
        existing_nullable=False,
        comment="L1=表级；L2=字段级；L3=指标级",
    )
    # 3. lineage_edge.owner：人工边登记人
    op.add_column(
        "lineage_edge",
        sa.Column(
            "owner",
            sa.String(64),
            nullable=True,
            comment="登记人（人工断链等人工边）",
        ),
    )
    # 4. lineage_edge_history 历史快照表（append-only，不接软删）
    op.create_table(
        "lineage_edge_history",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column("source_node", sa.String(512), nullable=False, comment="上游节点"),
        sa.Column("target_node", sa.String(512), nullable=False, comment="下游节点"),
        sa.Column(
            "edge_type",
            sa.Enum(*_EDGE_TYPES_AFTER, name="lineage_edge_type"),
            nullable=False,
            comment="血缘边类型",
        ),
        sa.Column(
            "granularity",
            sa.Enum(*_GRANULARITIES_AFTER, name="lineage_granularity"),
            nullable=False,
            comment="L1=表级；L2=字段级；L3=指标级",
        ),
        sa.Column("confidence", sa.Float(), nullable=False, comment="解析置信度"),
        sa.Column("provenance", sa.String(32), nullable=False, comment="来源通道"),
        sa.Column("pii_inherited", sa.Boolean(), nullable=False, comment="PII 是否沿血缘继承"),
        sa.Column(
            "change_reason",
            sa.String(32),
            nullable=False,
            comment="变更原因：schema_drift/reparse/manual/rename",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_lineage_edge_history_source", "lineage_edge_history", ["source_node"], unique=False
    )
    op.create_index(
        "ix_lineage_edge_history_target", "lineage_edge_history", ["target_node"], unique=False
    )


def downgrade() -> None:
    # 4. 删除历史快照表
    op.drop_index("ix_lineage_edge_history_source", table_name="lineage_edge_history")
    op.drop_index("ix_lineage_edge_history_target", table_name="lineage_edge_history")
    op.drop_table("lineage_edge_history")
    # 3. 移除 owner 字段
    op.drop_column("lineage_edge", "owner")
    # 2. granulariy 枚举回退
    op.alter_column(
        "lineage_edge",
        "granularity",
        existing_type=sa.Enum(*_GRANULARITIES_AFTER, name="lineage_granularity"),
        type_=sa.Enum(*_GRANULARITIES_BEFORE, name="lineage_granularity"),
        existing_nullable=False,
    )
    # 1. edge_type 枚举回退
    op.alter_column(
        "lineage_edge",
        "edge_type",
        existing_type=sa.Enum(*_EDGE_TYPES_AFTER, name="lineage_edge_type"),
        type_=sa.Enum(*_EDGE_TYPES_BEFORE, name="lineage_edge_type"),
        existing_nullable=False,
    )
