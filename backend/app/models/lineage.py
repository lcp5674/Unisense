"""血缘领域模型。

对齐 TD §12.2（血缘解析）。血缘边以 MySQL 为权威存储（lineage_edge），
Neo4j 作为图存储用于影响分析（best-effort，可降级）。

边类型（edge_type）：
- DERIVED_FROM：A 由 B 派生（字段级 L2 / 表级 L1 / 指标级 L3 通用）
- LINEAGE_UP / LINEAGE_DOWN / CONSUMED_BY：方向性血缘关系
- EXTERNAL_BREAK：断链登记（源或目标一侧为 external:{system} 占位节点）

粒度（granularity）：
- L1：表级血缘
- L2：字段级血缘
- L3：指标级血缘
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Index,
    String,
)
from sqlalchemy import (
    Enum as SQLEnum,
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
            "DERIVED_FROM",
            "LINEAGE_UP",
            "LINEAGE_DOWN",
            "CONSUMED_BY",
            "EXTERNAL_BREAK",
            name="lineage_edge_type",
        ),
        nullable=False,
        comment="血缘边类型",
    )
    granularity: Mapped[str] = mapped_column(
        SQLEnum("L1", "L2", "L3", name="lineage_granularity"),
        nullable=False,
        comment="L1=表级；L2=字段级；L3=指标级",
    )
    confidence: Mapped[float] = mapped_column(
        Float, default=1.0, nullable=False, comment="解析置信度"
    )
    provenance: Mapped[str] = mapped_column(
        String(32), default="sqlglot", nullable=False, comment="来源通道"
    )
    pii_inherited: Mapped[bool] = mapped_column(
        default=False, nullable=False, comment="PII 是否沿血缘继承"
    )
    owner: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None, comment="登记人（人工断链等人工边）"
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


class LineageEdgeHistory(Base):
    """血缘边历史快照（WORM，仅追加）。

    每次既有边发生值变更时，在覆盖前写入一份变更前的快照，并记录变更原因
    （change_reason：schema_drift / reparse / manual / rename），便于追溯
    血缘边为什么会变化。

    对齐 TD §12.2 血缘解析；历史表不参与软删过滤（append-only）。
    """

    __tablename__ = "lineage_edge_history"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键 ID"
    )
    source_node: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="上游节点，如 table:db.orders"
    )
    target_node: Mapped[str] = mapped_column(String(512), nullable=False, comment="下游节点")
    edge_type: Mapped[str] = mapped_column(
        SQLEnum(
            "DERIVED_FROM",
            "LINEAGE_UP",
            "LINEAGE_DOWN",
            "CONSUMED_BY",
            "EXTERNAL_BREAK",
            name="lineage_edge_type",
        ),
        nullable=False,
        comment="血缘边类型",
    )
    granularity: Mapped[str] = mapped_column(
        SQLEnum("L1", "L2", "L3", name="lineage_granularity"),
        nullable=False,
        comment="L1=表级；L2=字段级；L3=指标级",
    )
    confidence: Mapped[float] = mapped_column(
        Float, default=1.0, nullable=False, comment="解析置信度"
    )
    provenance: Mapped[str] = mapped_column(
        String(32), default="sqlglot", nullable=False, comment="来源通道"
    )
    pii_inherited: Mapped[bool] = mapped_column(
        default=False, nullable=False, comment="PII 是否沿血缘继承"
    )
    change_reason: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="变更原因：schema_drift/reparse/manual/rename",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="创建时间（UTC）",
    )

    __table_args__ = (
        Index("ix_lineage_edge_history_source", "source_node"),
        Index("ix_lineage_edge_history_target", "target_node"),
    )
