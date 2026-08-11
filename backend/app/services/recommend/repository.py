"""推荐服务 Repository（TD §12.12 / FR-19）。

基于事件行为（event_log）、血缘（lineage_edge）与术语（term）的只读推荐。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lineage import LineageEdge
from app.models.notify import EventLog
from app.models.term import Term


class RecommendRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def recent_user_events(self, user_id: int, limit: int) -> list[EventLog]:
        stmt = (
            select(EventLog)
            .where(EventLog.source == str(user_id))
            .order_by(EventLog.id.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def related_edges(self, node: str, limit: int) -> list[LineageEdge]:
        stmt = (
            select(LineageEdge)
            .where((LineageEdge.source_node == node) | (LineageEdge.target_node == node))
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def published_terms(self, limit: int) -> list[Term]:
        stmt = select(Term).where(Term.status == "PUBLISHED").order_by(Term.id.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())
