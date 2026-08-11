"""血缘领域模型。

对齐 TD §12.2（血缘解析）。血缘边以 MySQL 为权威存储（lineage_edge），
Neo4j 作为图存储用于影响分析（best-effort，可降级）。

边类型（edge_type）：
- DERIVED_FROM：A 由 B 派生（字段级 L2 / 表级 L1 通用）
- LINEAGE_UP / LINEAGE_DOWN / CONSUMED_BY：方向性血缘关系

粒度（granularity）：
- L1：表级血缘
- L2：字段级血缘
"""

from __future__ import annotations

from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy import (
    Float,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class LineageEdge(Base, BaseModel):
    """血缘边（权威存储在 MySQL）。"""

    __tablename__ = "lineage_edge"

    source_node: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="上游节点，如 table:db.orders"
    )
    target_node: Mapped[str] = mapped_column(String(512), nullable=False, comment="下游节点")
    edge_type: Mapped[str] = mapped_column(
        SQLEnum(
            "DERIVED_FROM", "LINEAGE_UP", "LINEAGE_DOWN", "CONSUMED_BY", name="lineage_edge_type"
        ),
        nullable=False,
        comment="血缘边类型",
    )
    granularity: Mapped[str] = mapped_column(
        SQLEnum("L1", "L2", name="lineage_granularity"),
        nullable=False,
        comment="L1=表级；L2=字段级",
    )
    confidence: Mapped[float] = mapped_column(
        Float, default=1.0, nullable=False, comment="解析置信度"
    )
    provenance: Mapped[str] = mapped_column(
        String(32), default="sqlglot", nullable=False, comment="来源通道"
    )
    # created_at / updated_at / deleted_at 由 BaseModel 提供，不重复声明

    __table_args__ = (
        Index(
            "uq_lineage_edge",
            "source_node",
            "target_node",
            "edge_type",
            "granularity",
            unique=True,
            mysql_length={"source_node": 255, "target_node": 255},
        ),
        Index("ix_lineage_edge_source", "source_node"),
        Index("ix_lineage_edge_target", "target_node"),
    )
