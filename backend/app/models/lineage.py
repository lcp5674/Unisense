"""血缘领域模型。

对齐 TD §12.2（血缘解析）。血缘边以 MySQL 为权威存储（lineage_edge），
Neo4j 作为图存储用于影响分析（best-effort，可降级）。

边类型（edge_type）：
- DERIVED_FROM：A 由 B 派生（字段级 L2 / 表级 L1 / 指标级 L3 通用）
- LINEAGE_UP / LINEAGE_DOWN / CONSUMED_BY：方向性血缘关系
- EXTERNAL_BREAK：断链登记（源或目标一侧为 external:{system} 占位节点）
- USES_DIMENSION：指标 ↔ 维度（L3，指标基于维度分析，dimension:{code} 节点）
- READS_COLUMN：指标 ↔ 字段（L3，指标来源于表的具体字段，column:{db}.{tbl}.{col} 节点）
- BASED_ON：派生指标 ↔ 基础原子指标（L3，OneData 派生 = 基础原子 + 业务限定 +
  时间周期——标识"哪个原子指标是此派生的基底"，区别于 DERIVED_FROM 的普通上游引用）

粒度（granularity）：
- L1：表级血缘
- L2：字段级血缘
- L3：指标级血缘
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
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
            "USES_DIMENSION",
            "READS_COLUMN",
            "BASED_ON",
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
    # ---- 增量采集与失效管理（TD §12.2 血缘采集通道）----
    # last_seen_at：最近一次被任何采集通道确认存在的时间（未确认过为 NULL）。
    # missing_count：连续未被采集通道确认的轮次（观察期计数，达到阈值进入失效队列）。
    # stale：是否进入失效队列（不再参与影响分析，等待人工确认删除或恢复）。
    # stale_since：进入失效队列的时间。
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="最近一次被采集通道确认存在的时间（UTC）",
    )
    missing_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="连续未被采集通道确认的轮次（观察期计数）",
    )
    stale: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="是否进入失效队列（等待确认删除或恢复）",
    )
    stale_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        comment="进入失效队列的时间（UTC）",
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
            "USES_DIMENSION",
            "READS_COLUMN",
            "BASED_ON",
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


class LineageIngestRun(Base):
    """血缘采集通道运行记录（增量采集审计）。

    每次增量采集（dp_csv / quickbi / 数据接口 / SQL 解析）写一条运行记录，
    记录本次运行的新增/更新/未再出现/新失效/恢复边数，用于「采集通道」视图
    展示来源新鲜度与变更摘要（TD §12.2 增量血缘运维）。

    对齐 TD §12.2 血缘采集；运行记录为追加型审计数据，不参与软删过滤。
    """

    __tablename__ = "lineage_ingest_run"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键 ID"
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, comment="来源通道，如 dp_csv"
    )
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="运行时间（UTC）",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="running/success/failed"
    )
    total_edges: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="本次采集确认的边总数"
    )
    added_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="本次新增边数"
    )
    updated_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="本次更新边数"
    )
    missing_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="本次未再出现的边数（观察期累加）"
    )
    stale_flagged_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="本次新进入失效队列的边数"
    )
    restored_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="本次恢复的失效边数"
    )
    error: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None, comment="失败原因（status=failed 时）"
    )
    detail_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
        comment="本次运行详情快照（JSON）：SQL 原文/方言/落点/边明细 或 批量变更边明细",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        comment="创建时间（UTC）",
    )
