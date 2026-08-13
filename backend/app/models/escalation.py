"""告警升级状态模型（TD §12.9 扩展：升级可重试、可确认、可逐级升级）。

记录每次告警升级的状态机数据，供周期任务 ``check_escalation_retries``
扫描驱动重试/升级：

- ``ESCALATED``：升级已发布，等待确认或到点重试。
- ``ACKNOWLEDGED``：已被人工确认（停止重试）。
- ``MAXED_OUT``：达到当前级别最大重试次数且已是最高级（P0），不再重试。

字段语义：
- ``attempts``：当前级别的累计触达次数（含首次）。
- ``max_attempts``：当前级别的最大触达次数（由策略决定，P0=6/P1=4/P2=0）。
- ``next_retry_at``：下次重试时刻；为 NULL 表示无需重试（P2 不重复）。
- ``last_payload``：最近一次升级事件的负载快照（审计/排查用）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class EscalationStatus:
    """升级状态常量（字符串存库，避免 DB 枚举迁移耦合）。"""

    ESCALATED = "ESCALATED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    MAXED_OUT = "MAXED_OUT"


class EscalationRecord(Base, BaseModel):
    """一次告警升级的状态记录。"""

    __tablename__ = "escalation_record"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="业务事件类型（如 quality.anomaly）"
    )
    source_ref: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="来源引用（如 quality_event.id）"
    )
    level: Mapped[str] = mapped_column(String(8), nullable=False, comment="当前严重级 P0/P1/P2")
    label: Mapped[str] = mapped_column(String(16), nullable=False, comment="级别中文标签")
    attempts: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, comment="当前级别累计触达次数"
    )
    max_attempts: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="当前级别最大触达次数"
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="下次重试时刻（NULL=不重复）"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EscalationStatus.ESCALATED, comment="状态"
    )
    last_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="最近一次升级负载快照"
    )
    actor_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="触发者（审计归因）"
    )
