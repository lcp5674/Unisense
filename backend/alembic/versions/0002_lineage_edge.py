"""lineage_edge（血缘边，MySQL 权威存储）

Revision ID: 0002_lineage_edge
Revises: 0001_initial
Create Date: 2026-08-07

对齐 TD §12.2 与 DEV_GUIDE §9（up + down 均可执行、数据无损）。
仅新增血缘边表；Neo4j 图存储由应用层 best-effort 写入，不在此迁移中维护。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_lineage_edge"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lineage_edge",
        sa.Column("id", sa.BigInteger(), nullable=False, comment="主键 ID"),
        sa.Column("source_node", sa.String(512), nullable=False, comment="上游节点"),
        sa.Column("target_node", sa.String(512), nullable=False, comment="下游节点"),
        sa.Column(
            "edge_type",
            sa.Enum(
                "DERIVED_FROM",
                "LINEAGE_UP",
                "LINEAGE_DOWN",
                "CONSUMED_BY",
                name="lineage_edge_type",
            ),
            nullable=False,
            comment="血缘边类型",
        ),
        sa.Column(
            "granularity",
            sa.Enum("L1", "L2", name="lineage_granularity"),
            nullable=False,
            comment="L1=表级；L2=字段级",
        ),
        sa.Column("confidence", sa.Float(), nullable=False, comment="解析置信度"),
        sa.Column("provenance", sa.String(32), nullable=False, comment="来源通道"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, comment="更新时间"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    # 唯一键：源/目标节点为 utf8mb4 VARCHAR(512)，全字段索引超 3072 字节上限，
    # 故对 source_node/target_node 取 255 前缀建唯一索引（前缀冲突概率极低）。
    op.create_index(
        "uq_lineage_edge",
        "lineage_edge",
        ["source_node", "target_node", "edge_type", "granularity"],
        unique=True,
        mysql_length={"source_node": 255, "target_node": 255},
    )
    op.create_index("ix_lineage_edge_source", "lineage_edge", ["source_node"], unique=False)
    op.create_index("ix_lineage_edge_target", "lineage_edge", ["target_node"], unique=False)


def downgrade() -> None:
    op.drop_index("uq_lineage_edge", table_name="lineage_edge")
    op.drop_table("lineage_edge")
