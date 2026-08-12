"""埋点事件模型（对齐 US9 / FR-16）。

存储用户行为事件（搜索/查询/审核/浏览），支撑驾驶舱和推荐算法。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base


class TrackingEvent(Base):
    """埋点事件模型。

    记录用户行为事件，包括事件类型、操作人、目标对象、上下文等。
    """

    __tablename__ = "tracking_event"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, comment="UUID 主键"
    )
    event_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="事件类型(search/query/approve/browse/nps)"
    )
    actor_id: Mapped[str] = mapped_column(
        String(36), nullable=False, comment="操作人 ID", index=True
    )
    target_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, comment="目标对象 ID(指标/术语等)"
    )
    target_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="目标类型(metric/term/glossary)"
    )
    context_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="事件上下文(搜索关键词/查询参数等)"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
        comment="事件时间",
    )

    __table_args__ = (
        Index("ix_tracking_event_type_created", "event_type", "created_at"),
        Index("ix_tracking_actor_type", "actor_id", "event_type"),
    )
