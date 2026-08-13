"""依赖实时健康态模型（对齐 TD §4.13 dependency_health 表，PRD 4.13.6）。

记录每个可选依赖（LLM/OLAP/GRAPH/ES/DATASOURCE/NOTIFICATION）的**实时**健康态：
熔断态（CLOSED/OPEN/HALF_OPEN）、连续失败次数、最近探测时间、P95 延迟、错误率等。
与 ``degradation_event``（只写不删的审计明细）形成「明细 + 实时快照」双表：
看板/运维直接查本表即得各依赖当前健康，无需回放降级事件历史。

设计取舍：与 ``degradation_event`` 通过 (dependency_type, dependency_id) 逻辑关联，
不建硬外键（避免对 data_source 的耦合，DATASOURCE 仅为依赖类型之一）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import TimestampMixin

# 依赖类型（对齐 TD §4.13 dependency_health.dependency_type ENUM）
DEP_HEALTH_DEP_TYPES = ("LLM", "OLAP", "GRAPH", "ES", "DATASOURCE", "NOTIFICATION")
# 健康状态：HEALTHY=健康 / DEGRADED=降级 / UNAVAILABLE=不可用
DEP_HEALTH_STATES = ("HEALTHY", "DEGRADED", "UNAVAILABLE")
# 熔断器状态（对齐 TD §4.13 circuit_state ENUM）
DEP_HEALTH_CIRCUIT = ("CLOSED", "OPEN", "HALF_OPEN")


class DependencyHealth(Base, TimestampMixin):
    """依赖实时健康态（按 (dependency_type, dependency_id) 唯一，UPSERT 维护）。"""

    __tablename__ = "dependency_health"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键 ID"
    )
    dependency_type: Mapped[str] = mapped_column(
        SAEnum(*DEP_HEALTH_DEP_TYPES, name="dependency_health_dep_type"),
        nullable=False,
        comment="依赖类型（LLM/OLAP/GRAPH/ES/DATASOURCE/NOTIFICATION）",
    )
    dependency_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="依赖实例标识（如 olap / neo4j / redis）"
    )
    status: Mapped[str] = mapped_column(
        SAEnum(*DEP_HEALTH_STATES, name="dependency_health_status"),
        nullable=False,
        default="HEALTHY",
        comment="HEALTHY=健康 / DEGRADED=降级 / UNAVAILABLE=不可用",
    )
    last_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近一次探测时间（UTC）"
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="连续失败次数（达阈值→UNAVAILABLE）"
    )
    latency_p95_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="近5分钟 P95 延迟（ms）"
    )
    error_rate_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, comment="近5分钟错误率（%）"
    )
    circuit_state: Mapped[str] = mapped_column(
        SAEnum(*DEP_HEALTH_CIRCUIT, name="dependency_health_circuit"),
        nullable=False,
        default="CLOSED",
        comment="熔断器状态（CLOSED/OPEN/HALF_OPEN）",
    )
    circuit_opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="熔断开启时间（UTC），未开启为 NULL"
    )
    # 注意：列名用 ``meta`` 而非 TD §4.13 的 ``metadata``——``metadata`` 是 SQLAlchemy
    # Table/Base 的保留属性（指向 MetaData 注册表），作列名会在 values()/upsert 解析时冲突。
    meta: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="扩展信息（如熔断阈值/活跃连接数）"
    )

    __table_args__ = (
        UniqueConstraint(
            "dependency_type",
            "dependency_id",
            name="uk_dependency_health_dep",
        ),
        Index("idx_dep_type", "dependency_type"),
        Index("idx_dep_id", "dependency_id"),
    )
