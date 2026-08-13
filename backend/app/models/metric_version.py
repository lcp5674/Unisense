"""指标版本模型 + PendingVersionConfirmation 确认记录。

对齐 TD §4.1 metric_version 表 + TD §12.3 PENDING_VERSION 确认期。

MetricVersion 从 metric.py 拆出独立文件，避免 metric.py 膨胀。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

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
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.mysql import Base
from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.metric import Metric


class MetricVersion(Base, BaseModel):
    """指标版本（溯源）。

    对齐 TD §4.1 metric_version 表。
    含破坏性判定与结构化 diff。

    Attributes:
        metric_id: 指标 ID。
        version: 版本号。
        change_type: 变更类型。
        definition_json: 口径快照。
        diff_json: 结构化 diff（可空）。
        status: 版本状态。
        change_reason: 变更原因。
        created_by: 创建人 ID。
        published_at: 发布时间（可空）。
    """

    __tablename__ = "metric_version"

    metric_id: Mapped[int] = mapped_column(
        ForeignKey("metric.id", name="fk_metric_version_metric"),
        nullable=False,
        comment="指标 ID",
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号")
    change_type: Mapped[str] = mapped_column(
        Enum("CREATE", "UPDATE", "BREAKING", "DEPRECATE", "RESTORE", name="change_type_enum"),
        nullable=False,
        comment="变更类型",
    )
    definition_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="口径快照"
    )
    diff_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="结构化 diff"
    )
    status: Mapped[str] = mapped_column(
        Enum(
            "DRAFT",
            "PENDING_CONFIRMATION",
            "PUBLISHED",
            "EXPERIMENTAL",
            "ARCHIVED",
            "CANCELLED",
            name="version_status",
        ),
        nullable=False,
        default="DRAFT",
        comment="版本状态",
    )
    change_reason: Mapped[str] = mapped_column(Text, nullable=False, comment="变更原因")
    created_by: Mapped[int] = mapped_column(
        ForeignKey("user.id", name="fk_metric_version_user"), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(nullable=True, comment="发布时间")

    # ---- PENDING_VERSION 机制字段（对齐 TD §12.3 / FR-006~FR-009）----
    pending_deadline: Mapped[datetime | None] = mapped_column(
        nullable=True, comment="PENDING_VERSION 确认截止时间"
    )
    extension_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="延期次数（最多 1 次）"
    )
    effective_at: Mapped[datetime | None] = mapped_column(
        nullable=True, comment="实际生效时间（confirm 后记录）"
    )

    metric: Mapped["Metric"] = relationship(  # noqa: UP037 – forward ref required under annotations
        "Metric", back_populates="versions"
    )

    __table_args__ = (UniqueConstraint("metric_id", "version", name="uk_metric_version"),)


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
