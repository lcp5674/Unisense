"""通知服务领域模型（TD §12.9 / FR-16 / FR-17）。

包含通知记录、事件日志、订阅偏好三类实体。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class NotifyChannel(enum.StrEnum):
    EMAIL = "EMAIL"
    SMS = "SMS"
    WEBHOOK = "WEBHOOK"
    IN_APP = "IN_APP"


class NotifyStatus(enum.StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class EventLevel(enum.StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class Notification(Base, BaseModel):
    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subscriber_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="订阅人 ID", index=True
    )
    channel: Mapped[str] = mapped_column(
        Enum(NotifyChannel, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        comment="通知渠道",
    )
    template_code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="模板编码")
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="标题")
    body: Mapped[str | None] = mapped_column(Text, nullable=True, comment="正文")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="扩展负载")
    status: Mapped[str] = mapped_column(
        Enum(NotifyStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=NotifyStatus.PENDING.value,
        comment="状态",
        index=True,
    )
    send_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="计划发送时间"
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="实际发送时间"
    )
    ref_type: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="关联类型")
    ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="关联 ID")


class EventLog(Base, BaseModel):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="事件类型", index=True
    )
    source: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="事件来源")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="事件负载")
    level: Mapped[str] = mapped_column(
        Enum(EventLevel, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=EventLevel.INFO.value,
        comment="事件级别",
    )
    notified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否已通知"
    )


class SubscriptionPref(Base, BaseModel):
    __tablename__ = "subscription_pref"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="用户 ID", index=True)
    channel: Mapped[str] = mapped_column(
        Enum(NotifyChannel, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        comment="通知渠道",
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, comment="事件类型")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    threshold: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="阈值")

    __table_args__ = (UniqueConstraint("user_id", "channel", "event_type", name="uk_sub_pref"),)
