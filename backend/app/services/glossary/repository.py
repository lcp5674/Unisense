"""术语库 Repository（TD §12.14 / FR-08）。

持有域会话，提供读写原语；写入通过显式 commit/flush 落库。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.glossary import GlossaryConflict, TermRelation, TermVersion
from app.models.term import Term


class GlossaryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_term(self, term: Term) -> Term:
        self._session.add(term)
        await self._session.flush()
        return term

    async def get_term(self, term_code: str) -> Term | None:
        stmt = select(Term).where(Term.term_code == term_code)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_term_by_id(self, term_id: int) -> Term | None:
        stmt = select(Term).where(Term.id == term_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_terms(
        self,
        domain: str | None,
        status: str | None,
        search: str | None,
        limit: int,
        offset: int,
        owner_id: int | None = None,
    ) -> tuple[Iterable[Term], int]:
        conditions = [Term.deleted_at.is_(None)]
        if domain:
            conditions.append(Term.domain == domain)
        if status:
            conditions.append(Term.status == status)
        if owner_id is not None:
            conditions.append(Term.owner_id == owner_id)
        if search:
            # 参数化 LIKE + 通配符转义（对齐 FR-035：% / _ 须转义，防模糊放大）
            escaped = search.replace("/", "//").replace("%", "/%").replace("_", "/_")
            like = f"%{escaped}%"
            conditions.append(
                (Term.name.like(like, escape="/"))
                | (Term.definition.like(like, escape="/"))
                | (Term.term_code.like(like, escape="/"))
            )
        stmt = select(Term)
        if conditions:
            stmt = stmt.where(*conditions)
        count_stmt = select(func.count()).select_from(Term)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = int((await self._session.execute(count_stmt)).scalar() or 0)
        rows = (
            (await self._session.execute(stmt.order_by(Term.id.desc()).limit(limit).offset(offset)))
            .scalars()
            .all()
        )
        return rows, total

    async def delete_term(self, term: Term) -> None:
        await self._session.delete(term)
        await self._session.flush()

    async def all_terms(self) -> list[Term]:
        stmt = select(Term)
        return list((await self._session.execute(stmt)).scalars().all())

    async def save_conflict(self, conflict: GlossaryConflict) -> GlossaryConflict:
        self._session.add(conflict)
        await self._session.flush()
        return conflict

    async def list_conflicts(self, status: str | None) -> list[GlossaryConflict]:
        stmt = select(GlossaryConflict)
        if status:
            stmt = stmt.where(GlossaryConflict.status == status)
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_conflict(self, conflict_id: int) -> GlossaryConflict | None:
        stmt = select(GlossaryConflict).where(GlossaryConflict.id == conflict_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def save_term_version(self, version: TermVersion) -> TermVersion:
        self._session.add(version)
        await self._session.flush()
        return version

    async def count_term_versions(self, term_id: int) -> int:
        stmt = select(func.count()).select_from(TermVersion).where(TermVersion.term_id == term_id)
        return int((await self._session.execute(stmt)).scalar() or 0)

    async def save_term_relation(self, relation: TermRelation) -> TermRelation:
        self._session.add(relation)
        await self._session.flush()
        return relation

    async def list_term_relations(self, term_id: int) -> list[dict[str, Any]]:
        """查某术语的全部关系（作为源或目标），带对端术语信息（名称/编码）。

        Returns:
            每个元素 ``{relation, peer_term, relation_type}``——``peer_term`` 为对端
            ``Term``（源时对端是 target，目标时对端是 source）；``relation_type`` 为
            关系方向归一化（source→target 时原样，target←source 时记录 ``REVERSED`` 语义）。
        """
        stmt = select(TermRelation).where(
            or_(
                TermRelation.source_term_id == term_id,
                TermRelation.target_term_id == term_id,
            ),
            TermRelation.deleted_at.is_(None),
        )
        relations = list((await self._session.execute(stmt)).scalars().all())
        if not relations:
            return []
        # 收集对端术语 ID → 批量取术语名/编码
        peer_ids = {
            rel.target_term_id if rel.source_term_id == term_id else rel.source_term_id
            for rel in relations
        }
        peers: dict[int, Term] = {}
        if peer_ids:
            peers_stmt = select(Term).where(Term.id.in_(peer_ids))
            peers = {t.id: t for t in (await self._session.execute(peers_stmt)).scalars().all()}
        out: list[dict[str, Any]] = []
        for rel in relations:
            peer_id = rel.target_term_id if rel.source_term_id == term_id else rel.source_term_id
            peer = peers.get(peer_id)
            out.append(
                {
                    "relation": rel,
                    "relation_type": rel.relation_type,
                    "peer": peer,
                }
            )
        return out

    async def commit(self) -> None:
        await self._session.commit()
