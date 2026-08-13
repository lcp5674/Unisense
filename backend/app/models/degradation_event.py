"""降级事件模型（对齐 TD §4.13 degradation_event 表 + §5.2 降级矩阵审计）。

记录每个可选依赖（LLM/OLAP/GRAPH/ES/DATASOURCE/NOTIFICATION）降级开始 / 恢复事件，
供运营看板与审计查询。TD §4.13 明确：降级开始/恢复事件入审计与看板 —— 本表即降级类
事件的权威审计表（与通用 audit_log 互补，避免对 user.id 的外键耦合，系统事件 actor_id=0）。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import TimestampMixin

# 依赖类型取值（对齐 TD §4.13 degradation_event.dependency_type ENUM）
DEPENDENCY_TYPES = ("LLM", "OLAP", "GRAPH", "ES", "DATASOURCE", "NOTIFICATION")
# 降级状态：DEGRADED=降级开始，HEALTHY=恢复
DEGRADATION_STATES = ("DEGRADED", "HEALTHY")


class DegradationEvent(Base, TimestampMixin):
    """降级事件（WORM：只写不删，运营看板与审计查询源）。"""

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
    )
