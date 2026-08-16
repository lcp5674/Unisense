"""术语库领域模型（TD §12.14 / FR-08）。

包含术语冲突、术语版本快照、术语关系三类实体。
术语主实体见 `app.models.term.Term`（已存在，状态机 DRAFT→PUBLISHED→DEPRECATED）。
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class GlossaryConflictType(enum.StrEnum):
    ALIAS_OVERLAP = "alias_overlap"
    NAME_OVERLAP = "name_overlap"
    DEFINITION_OVERLAP = "definition_overlap"


class GlossaryConflictStatus(enum.StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    IGNORED = "IGNORED"


class TermRelationType(enum.StrEnum):
    SYNONYM_OF = "SYNONYM_OF"
    BROADER_THAN = "BROADER_THAN"
    NARROWER_THAN = "NARROWER_THAN"
    RELATED_TO = "RELATED_TO"
    # 增强（产品：覆盖业务反义/依赖/派生/实例语义）
    ANTONYM_OF = "ANTONYM_OF"
    DEPENDS_ON = "DEPENDS_ON"
    DERIVED_FROM = "DERIVED_FROM"
    INSTANCE_OF = "INSTANCE_OF"


class TermSourceType(enum.StrEnum):
    MANUAL = "MANUAL"
    LLM_SUGGESTED = "LLM_SUGGESTED"


class GlossaryConflict(Base, BaseModel):
    """术语冲突候选（同义词/别名重合率过高触发）。"""

    __tablename__ = "glossary_conflict"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    term_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    conflict_type: Mapped[str] = mapped_column(
        Enum(GlossaryConflictType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    ref_term_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ref_metric_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(GlossaryConflictStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=GlossaryConflictStatus.OPEN,
        index=True,
    )
    resolver: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )


class TermVersion(Base, BaseModel):
    """术语版本快照（每次变更留痕）。"""

    __tablename__ = "term_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    term_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    changed_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    change_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )


class TermRelation(Base, BaseModel):
    """术语间关系（PRD 4.6.5 R3-28）。"""

    __tablename__ = "term_relation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_term_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_term_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    relation_type: Mapped[str] = mapped_column(
        Enum(TermRelationType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    declared_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_type: Mapped[str] = mapped_column(
        Enum(TermSourceType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=TermSourceType.MANUAL,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        UniqueConstraint("source_term_id", "target_term_id", "relation_type", name="uk_term_pair"),
    )
