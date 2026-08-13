"""术语库服务 Schemas（TD §12.14 / FR-08）。"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class TermStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"


class TermCreate(BaseModel):
    term_code: str | None = None  # 缺省由系统自动生成（domain_name slug）
    name: str
    definition: str
    domain: str
    synonyms: list[str] = []
    boundary: str | None = None
    # PLAT-2: owner_id 允许客户端省略，服务端以认证身份覆盖（防越权指定责任人）。
    owner_id: int | None = None


class TermUpdate(BaseModel):
    name: str | None = None
    definition: str | None = None
    domain: str | None = None
    synonyms: list[str] | None = None
    boundary: str | None = None


class TermResponse(BaseModel):
    id: int
    term_code: str
    name: str
    definition: str
    domain: str
    synonyms: list[Any] = []
    boundary: str | None = None
    status: TermStatus
    owner_id: int
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> TermResponse:
        return cls(
            id=m.id,
            term_code=m.term_code,
            name=m.name,
            definition=m.definition,
            domain=m.domain,
            synonyms=getattr(m, "synonyms", []) or [],
            boundary=getattr(m, "boundary", None),
            status=m.status,
            owner_id=m.owner_id,
            created_at=getattr(m, "created_at", None),
            updated_at=getattr(m, "updated_at", None),
        )


class GlossaryConflictResponse(BaseModel):
    id: int
    term_id: int
    conflict_type: str
    ref_term_id: int | None = None
    ref_metric_id: int | None = None
    status: str
    resolver: int | None = None
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> GlossaryConflictResponse:
        return cls(
            id=m.id,
            term_id=m.term_id,
            conflict_type=m.conflict_type,
            ref_term_id=getattr(m, "ref_term_id", None),
            ref_metric_id=getattr(m, "ref_metric_id", None),
            status=m.status,
            resolver=getattr(m, "resolver", None),
            created_at=getattr(m, "created_at", None),
        )


class TermVersionResponse(BaseModel):
    id: int
    term_id: int
    version: int
    changed_by: int
    change_note: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> TermVersionResponse:
        return cls(
            id=m.id,
            term_id=m.term_id,
            version=m.version,
            changed_by=m.changed_by,
            change_note=getattr(m, "change_note", None),
            created_at=getattr(m, "created_at", None),
        )


class TermRelationCreate(BaseModel):
    target_term_id: int
    relation_type: str
    declared_by: int | None = None
    source_type: str = "MANUAL"


class TermRelationResponse(BaseModel):
    id: int
    source_term_id: int
    target_term_id: int
    relation_type: str
    declared_by: int | None = None
    source_type: str
    confirmed_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> TermRelationResponse:
        return cls(
            id=m.id,
            source_term_id=m.source_term_id,
            target_term_id=m.target_term_id,
            relation_type=m.relation_type,
            declared_by=getattr(m, "declared_by", None),
            source_type=m.source_type,
            confirmed_at=getattr(m, "confirmed_at", None),
        )


class ConflictResolve(BaseModel):
    decision: str  # RESOLVED | IGNORED
    resolver_id: int | None = None  # PLAT-2: 以服务端认证身份为准，客户端可不传
