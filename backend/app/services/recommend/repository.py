"""推荐服务 Repository（TD §12.12 / FR-19）。

基于血缘（lineage_edge）与术语（term）的只读推荐；
用户行为画像取自 tracking_events（由 RecommendService 直接查询），
EventLog 无 user 列，不再承担按用户过滤的职责。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lineage import LineageEdge
from app.models.term import Term


class RecommendRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
