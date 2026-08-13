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

from app.core.base_service import BaseService
from app.core.config import settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    UnisenseError,
    ValidationError,
)
from app.models.glossary import (
    GlossaryConflict,
    GlossaryConflictStatus,
    GlossaryConflictType,
    TermRelation,
    TermRelationType,
    TermSourceType,
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


#: 合法关系类型 / 来源类型取值（DB Enum 列，非法值须在服务层转 4xx，而非 DB 500）。
_VALID_RELATION_TYPES = {e.value for e in TermRelationType}
_VALID_SOURCE_TYPES = {e.value for e in TermSourceType}


def _overlap_ratio(a: list[str], b: list[str]) -> float:
    """两组词的归一化重叠率（Jaccard），用于同义词冲突判定。"""
    set_a = {_normalize(x) for x in a if x}
    set_b = {_normalize(x) for x in b if x}
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _get_synonym_threshold() -> float:
    """获取同义词冲突阈值（可配置，默认 0.8）。

    通过 settings.glossary_synonym_threshold 热更新，对齐 OPS-03 配置热更新。
    """
    return getattr(settings, "glossary_synonym_threshold", 0.8)


class GlossaryService(BaseService):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._session = session
        self._repo = GlossaryRepository(session)

    async def _generate_term_code(self, data: TermCreate) -> str:
        """自动生成唯一术语编码。

        规则：``{domain_slug}_{name_slug}``；纯中文等无 ASCII 名回退 ``term``；
        冲突追加 ``_2/_3/...`` 后缀（上限 100 次）。
        """
        from app.core.codegen import generate_unique_code, slugify_code

        domain_slug = slugify_code(data.domain)
        name_slug = slugify_code(data.name)
        if domain_slug and name_slug:
            base = f"{domain_slug}_{name_slug}"
        elif name_slug:
            base = f"term_{name_slug}"
        elif domain_slug:
            base = f"term_{domain_slug}"
        else:
            base = "term"

        async def _exists(code: str) -> bool:
            return await self._repo.get_term(code) is not None

        return await generate_unique_code(base, _exists)

    async def create_term(self, data: TermCreate, actor_id: int | None = None) -> TermResponse:
        # 编码自动生成（FR-010：缺省时由系统生成，非人为创造）
        if not data.term_code:
            data.term_code = await self._generate_term_code(data)
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
            # PLAT-2: 认证身份优先，client 传入的 owner_id 仅作降级
            owner_id=actor_id if actor_id is not None else data.owner_id,
        )
        term = await self._repo.save_term(term)
        await self._snapshot(term, actor_id or 0, "create")
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
        # 目标术语存在性校验（防孤儿关系落到库）
        target = await self._repo.get_term_by_id(data.target_term_id)
        if target is None:
            raise NotFoundError(
                f"目标术语不存在: {data.target_term_id}",
                error_code="TERM_TARGET_NOT_FOUND",
                ctx={"target_term_id": data.target_term_id},
            )
        # enum 显式校验：非法值须转 4xx，而非触达 DB Enum 抛 500
        if data.relation_type not in _VALID_RELATION_TYPES:
            raise ValidationError(
                f"未知术语关系类型: {data.relation_type}",
                error_code="INVALID_RELATION_TYPE",
                ctx={"relation_type": data.relation_type},
            )
        if data.source_type not in _VALID_SOURCE_TYPES:
            raise ValidationError(
                f"未知术语来源类型: {data.source_type}",
                error_code="INVALID_SOURCE_TYPE",
                ctx={"source_type": data.source_type},
            )
        # 自引用关系防护（防自环）
        if target.id == term.id:
            raise ConflictError(
                "术语不能与自身建立关系",
                error_code="SELF_RELATION",
                ctx={"term_code": term_code, "term_id": term.id},
            )
        relation = TermRelation(
            source_term_id=term.id,
            target_term_id=target.id,
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
            # 同义词重叠率超阈值
            ratio = _overlap_ratio(term.synonyms or [], other_synonyms)
            threshold = _get_synonym_threshold()
            if ratio > threshold:
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
