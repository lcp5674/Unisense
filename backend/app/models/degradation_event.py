"""降级事件模型（对齐 TD §4.13 degradation_event 表 + §5.2 降级矩阵审计）。

记录每个可选依赖（LLM/OLAP/GRAPH/ES/DATASOURCE/NOTIFICATION）降级开始 / 恢复事件，
供运营看板与审计查询。TD §4.13 明确：降级开始/恢复事件入审计与看板 —— 本表即降级类
事件的权威审计表（与通用 audit_log 互补，避免对 user.id 的外键耦合，系统事件 actor_id=0）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import TimestampMixin

# 依赖类型取值（对齐 TD §4.13 degradation_event.dependency_type ENUM）
DEPENDENCY_TYPES = ("LLM", "OLAP", "GRAPH", "ES", "DATASOURCE", "NOTIFICATION")
# 降级状态：DEGRADED=降级开始，HEALTHY=恢复
DEGRADATION_STATES = ("DEGRADED", "HEALTHY")
# 事件类型（对齐 TD §4.13 degradation_event.event_type ENUM）
EVENT_TYPES = (
    "DEGRADED",
    "UNAVAILABLE",
    "RECOVERED",
    "CIRCUIT_OPENED",
    "CIRCUIT_HALF_OPEN",
    "CIRCUIT_CLOSED",
)
# 严重程度（对齐 TD §4.13 degradation_event.severity ENUM）：轻降级(功能减退)/重降级(能力关停)
SEVERITIES = ("LIGHT", "HEAVY")


class DegradationEvent(Base, TimestampMixin):
    """降级事件（WORM：只写不删，运营看板与审计查询源）。

    在 TD §4.13 基础表（dependency_type / dependency_id / state / reason / actor_id）之上，
    补齐降级度量字段：event_type / severity / affected_capabilities / affected_user_count /
    started_at / recovered_at / duration_seconds / trigger_reason / resolution_action，
    使运营看板可计算降级频次·时长·影响用户数（Gap #4，FR-17）。
    """

    __tablename__ = "degradation_event"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键 ID"
    )
    dependency_type: Mapped[str] = mapped_column(
        SAEnum(*DEPENDENCY_TYPES, name="degradation_dep_type"),
        nullable=False,
        comment="依赖类型（LLM/OLAP/GRAPH/ES/DATASOURCE/NOTIFICATION）",
    )
    dependency_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="依赖实例标识（如 olap / neo4j / redis）"
    )
    # ---- TD §4.13 度量字段（Gap #4 补齐）----
    event_type: Mapped[str] = mapped_column(
        SAEnum(*EVENT_TYPES, name="degradation_event_type"),
        nullable=False,
        default="DEGRADED",
        comment="事件类型（DEGRADED/UNAVAILABLE/RECOVERED/CIRCUIT_OPENED/CIRCUIT_HALF_OPEN/CIRCUIT_CLOSED）",
    )
    severity: Mapped[str] = mapped_column(
        SAEnum(*SEVERITIES, name="degradation_severity"),
        nullable=False,
        default="LIGHT",
        comment="严重程度：LIGHT=轻降级(功能减退) / HEAVY=重降级(能力关停)",
    )
    affected_capabilities: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="受影响能力列表（如 ['ai_prefill','nl2sql']）"
    )
    affected_user_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="预估受影响用户数"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="降级开始时间（UTC）"
    )
    recovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="恢复时间（UTC），NULL=仍在降级中"
    )
    duration_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="降级持续秒数（恢复后回填）"
    )
    trigger_reason: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="触发原因（如 'LLM 连续5次超时 > 30s'）"
    )
    resolution_action: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="恢复动作（如 '自动探测恢复' / '人工重启'）"
    )
    # ---- 基础审计字段 ----
    state: Mapped[str] = mapped_column(
        SAEnum(*DEGRADATION_STATES, name="degradation_state"),
        nullable=False,
        comment="DEGRADED=降级开始 / HEALTHY=恢复",
    )
    reason: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="降级/恢复原因（如 circuit_open / circuit_recovered）"
    )
    actor_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="触发方（0=系统自动）"
    )

    __table_args__ = (
        Index("idx_degradation_dep_time", "dependency_type", "created_at"),
        Index("idx_degradation_state_time", "state", "created_at"),
        Index("idx_degradation_started", "started_at"),
    )
