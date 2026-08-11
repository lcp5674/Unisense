"""术语库服务（TD §12.14 / FR-08）。

职责：
1. 术语 CRUD + 状态机（DRAFT→PUBLISHED→DEPRECATED）。
2. 每次变更留存 `TermVersion` 快照（版本留痕）。
3. 同义词/别名重合率 > 80% 自动生成 `GlossaryConflict(OPEN)`，由 domain_admin 裁决。
4. 术语关系维护（SYNONYM_OF / BROADER_THAN / NARROWER_THAN / RELATED_TO）。
"""

from __future__ import annotations

import unicodedata
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, UnisenseError
from app.models.glossary import (
    GlossaryConflict,
    GlossaryConflictStatus,
    GlossaryConflictType,
    TermRelation,
    TermRelationType,
    TermVersion,
)
from app.models.term import Term
from app.services.glossary.repository import GlossaryRepository
from app.services.glossary.schemas import (
    TermCreate,
    TermRelationCreate,
    TermRelationResponse,
    TermResponse,
    TermStatus,
)


def _normalize(token: str) -> str:
    return unicodedata.normalize("NFKC", token.strip().lower())


def _overlap_ratio(a: list[str], b: list[str]) -> float:
    """两组词的归一化重叠率（Jaccard），用于同义词冲突判定。"""
    set_a = {_normalize(x) for x in a if x}
    set_b = {_normalize(x) for x in b if x}
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


class GlossaryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = GlossaryRepository(session)

    async def create_term(self, data: TermCreate, actor_id: int) -> TermResponse:
        existing = await self._repo.get_term(data.term_code)
        if existing is not None:
            raise ConflictError(f"术语编码已存在: {data.term_code}", error_code="TERM_EXISTS")
        term = Term(
            term_code=data.term_code,
            name=data.name,
            definition=data.definition,
            domain=data.domain,
            synonyms=list(data.synonyms),
            boundary=data.boundary,
            status=TermStatus.DRAFT.value,
            owner_id=data.owner_id,
        )
        term = await self._repo.save_term(term)
        await self._snapshot(term, actor_id, "create")
        await self._detect_conflicts(term)
        await self._repo.commit()
        return TermResponse.from_model(term)

    async def get_term(self, term_code: str) -> TermResponse:
        term = await self._repo.get_term(term_code)
        if term is None:
            raise NotFoundError(f"术语不存在: {term_code}")
        return TermResponse.from_model(term)

    async def list_terms(
        self,
        domain: str | None,
        status: str | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[TermResponse], int]:
        rows, total = await self._repo.list_terms(domain, status, search, limit, offset)
        return [TermResponse.from_model(t) for t in rows], total

    async def submit_term(self, term_code: str, actor_id: int) -> TermResponse:
        term = await self._require_term(term_code)
        if term.status != TermStatus.DRAFT.value:
            raise UnisenseError(
                f"仅 DRAFT 术语可提交，当前: {term.status}", error_code="INVALID_STATE"
            )
        term.status = TermStatus.PUBLISHED.value
        await self._snapshot(term, actor_id, "submit")
        await self._repo.commit()
        return TermResponse.from_model(term)

    async def update_term(self, term_code: str, data: Any, actor_id: int) -> TermResponse:
        term = await self._require_term(term_code)
        if data.name is not None:
            term.name = data.name
        if data.definition is not None:
            term.definition = data.definition
        if data.domain is not None:
            term.domain = data.domain
        if data.synonyms is not None:
            term.synonyms = list(data.synonyms)
        if data.boundary is not None:
            term.boundary = data.boundary
        await self._snapshot(term, actor_id, "update")
        await self._detect_conflicts(term)
        await self._repo.commit()
        return TermResponse.from_model(term)

    async def deprecate_term(self, term_code: str, actor_id: int) -> TermResponse:
        term = await self._require_term(term_code)
        if term.status == TermStatus.DEPRECATED.value:
            raise UnisenseError("术语已废弃", error_code="INVALID_STATE")
        term.status = TermStatus.DEPRECATED.value
        await self._snapshot(term, actor_id, "deprecate")
        await self._repo.commit()
        return TermResponse.from_model(term)

    async def list_conflicts(self, status: str | None) -> list[Any]:
        rows = await self._repo.list_conflicts(status)
        return [_conflict_to_resp(r) for r in rows]

    async def resolve_conflict(self, conflict_id: int, decision: str, resolver_id: int) -> Any:
        conflict = await self._repo.get_conflict(conflict_id)
        if conflict is None:
            raise NotFoundError(f"术语冲突不存在: {conflict_id}")
        if decision not in (
            GlossaryConflictStatus.RESOLVED.value,
            GlossaryConflictStatus.IGNORED.value,
        ):
            raise UnisenseError(f"未知裁决: {decision}", error_code="INVALID_DECISION")
        conflict.status = GlossaryConflictStatus(decision)
        conflict.resolver = resolver_id
        await self._repo.commit()
        return _conflict_to_resp(conflict)

    async def create_term_relation(
        self, term_code: str, data: TermRelationCreate
    ) -> TermRelationResponse:
        term = await self._require_term(term_code)
        relation = TermRelation(
            source_term_id=term.id,
            target_term_id=data.target_term_id,
            relation_type=TermRelationType(data.relation_type).value,
            declared_by=data.declared_by,
            source_type=data.source_type,
        )
        relation = await self._repo.save_term_relation(relation)
        await self._repo.commit()
        return TermRelationResponse.from_model(relation)

    # ---- 内部辅助 ----
    async def _require_term(self, term_code: str) -> Term:
        term = await self._repo.get_term(term_code)
        if term is None:
            raise NotFoundError(f"术语不存在: {term_code}")
        return term

    async def _snapshot(self, term: Term, actor_id: int, note: str) -> None:
        existing_count = await self._repo.count_term_versions(term.id)
        next_version = existing_count + 1
        snapshot = TermVersion(
            term_id=term.id,
            version=next_version,
            snapshot={
                "term_code": term.term_code,
                "name": term.name,
                "definition": term.definition,
                "domain": term.domain,
                "synonyms": list(getattr(term, "synonyms", []) or []),
                "boundary": getattr(term, "boundary", None),
                "status": term.status,
            },
            changed_by=actor_id,
            change_note=note,
        )
        await self._repo.save_term_version(snapshot)

    async def _detect_conflicts(self, term: Term) -> None:
        others = await self._repo.all_terms()
        term_tokens = {_normalize(term.name)} | {_normalize(s) for s in (term.synonyms or [])}
        for other in others:
            if other.id == term.id:
                continue
            other_synonyms = list(getattr(other, "synonyms", []) or [])
            # 名称精确冲突
            if _normalize(other.name) in term_tokens:
                await self._add_conflict(term, GlossaryConflictType.NAME_OVERLAP, other.id)
                continue
            # 同义词重叠率 > 0.8
            ratio = _overlap_ratio(term.synonyms or [], other_synonyms)
            if ratio > 0.8:
                await self._add_conflict(term, GlossaryConflictType.ALIAS_OVERLAP, other.id)

    async def _add_conflict(
        self, term: Term, ctype: GlossaryConflictType, ref_term_id: int
    ) -> None:
        conflict = GlossaryConflict(
            term_id=term.id,
            conflict_type=ctype.value,
            ref_term_id=ref_term_id,
            status=GlossaryConflictStatus.OPEN.value,
        )
        await self._repo.save_conflict(conflict)


def _conflict_to_resp(r: GlossaryConflict) -> Any:
    from app.services.glossary.schemas import GlossaryConflictResponse

    return GlossaryConflictResponse.from_model(r)
