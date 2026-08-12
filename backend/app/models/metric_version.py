"""PendingVersionConfirmation 模型（PENDING_VERSION 确认记录）。

对齐 TD §12.3 / spec FR-006~FR-009：破坏性变更 14 天消费方确认期。
每个消费方针对某个 (metric_id, version) 有一条确认记录。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class PendingVersionConfirmation(Base, BaseModel):
    """PENDING_VERSION 消费方确认记录。

    Attributes:
        metric_id: 指标 ID。
        version: 版本号。
        consumer_id: 消费方用户 ID。
        status: 确认状态（PENDING/CONFIRMED/REJECTED/TIMEOUT_ACCEPTED）。
        reason: 拒绝原因。
        extension_count: 延期次数（最多 1 次）。
        deadline: 确认截止时间（created_at + 14 天 + 延期）。
        confirmed_at: 确认/拒绝时间。
    """

    __tablename__ = "pending_version_confirmation"

    metric_id: Mapped[int] = mapped_column(
        ForeignKey("metric.id", name="fk_pending_confirm_metric"),
        nullable=False,
        comment="指标 ID",
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号")
    consumer_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="消费方用户 ID"
    )
    status: Mapped[str] = mapped_column(
        Enum("PENDING", "CONFIRMED", "REJECTED", "TIMEOUT_ACCEPTED", name="pending_confirm_status"),
        nullable=False,
        default="PENDING",
        comment="确认状态",
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="拒绝原因")
    extension_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="延期次数（最多 1 次）"
    )
    deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="确认截止时间"
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        nullable=True, comment="确认/拒绝时间"
    )

    __table_args__ = (
        UniqueConstraint("metric_id", "version", "consumer_id", name="uk_pending_confirm"),
        Index("idx_pending_deadline", "status", "deadline"),
    )
