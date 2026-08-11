"""冲突与裁决领域模型（TD §12.4 / FR-09）。

四类冲突 + 仲裁状态机（GOV-2）：
- 同名字段不同义（硬冲突，阻断发布）
- 同义不同名（重复建设，建议合并）
- 粒度/单位冲突（软冲突）
- 跨域同口径异源（软冲突）
- 口径版本冲突（软冲突）
- PII 冲突（特殊路由，转交 governance.pii_review，不进普通仲裁）

裁决记录沉淀为规则知识库（PRD 4.7.5）。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Enum, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class ConflictType(enum.StrEnum):
    SAME_NAME_DIFF_DEF = "same_name_diff_def"
    SAME_DEF_DIFF_NAME = "same_def_diff_name"
    GRAIN_UNIT = "grain_unit"
    CROSS_DOMAIN_SAME_DEF = "cross_domain_same_def"
    VERSION_CONFLICT = "version_conflict"
    PII = "pii"


class ConflictStatus(enum.StrEnum):
    OPEN = "OPEN"
    NEGOTIATING = "NEGOTIATING"
    ESCALATED = "ESCALATED"
    RULED = "RULED"
    CLOSED = "CLOSED"


class Conflict(Base, BaseModel):
    __tablename__ = "conflict"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conflict_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    metric_a: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metric_b: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    type: Mapped[ConflictType] = mapped_column(
        Enum(ConflictType, values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    status: Mapped[ConflictStatus] = mapped_column(
        Enum(ConflictStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ConflictStatus.OPEN,
        index=True,
    )
    domain: Mapped[str | None] = mapped_column(String(64), nullable=True)
    similarity_score: Mapped[float] = mapped_column(Float, default=0.0)
    metric_codes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    arbitrator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RulingRecord(Base, BaseModel):
    __tablename__ = "ruling_record"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conflict_id: Mapped[str] = mapped_column(String(64), index=True)
    metric_codes: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    dispute_desc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    arbitrator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
