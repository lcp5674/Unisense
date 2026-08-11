"""术语库 Repository（TD §12.14 / FR-08）。

持有域会话，提供读写原语；写入通过显式 commit/flush 落库。
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
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

    async def list_terms(
        self,
        domain: str | None,
        status: str | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> tuple[Iterable[Term], int]:
        conditions = []
        if domain:
            conditions.append(Term.domain == domain)
        if status:
            conditions.append(Term.status == status)
        if search:
            like = f"%{search}%"
            conditions.append(
                (Term.name.like(like)) | (Term.definition.like(like)) | (Term.term_code.like(like))
            )
        stmt = select(Term)
        if conditions:
            stmt = stmt.where(*conditions)
        count_stmt = select(Term)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = len((await self._session.execute(count_stmt)).scalars().all())
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
        stmt = select(TermVersion).where(TermVersion.term_id == term_id)
        return len((await self._session.execute(stmt)).scalars().all())

    async def save_term_relation(self, relation: TermRelation) -> TermRelation:
        self._session.add(relation)
        await self._session.flush()
        return relation

    async def commit(self) -> None:
        await self._session.commit()
