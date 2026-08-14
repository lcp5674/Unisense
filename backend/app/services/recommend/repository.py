"""推荐服务 Repository（TD §12.12 / FR-19）。

基于血缘（lineage_edge）与术语（term）的只读推荐；
用户行为画像取自 tracking_events（由 RecommendService 直接查询），
EventLog 无 user 列，不再承担按用户过滤的职责。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lineage import LineageEdge
from app.models.metric import Metric
from app.models.term import Term
from app.models.tracking import TrackingEvent

# 与前端埋点事件类型对齐（frontend/src/utils/enums.ts:288-299）。
# 这些事件以 target_type="metric" 记录用户对指标的行为，是协同过滤与热门聚合的信号源。
# 注意：metric_search 仅携带 keyword，target_id 为 undefined，不计入指标画像。
METRIC_EVENT_TYPES = (
    "metric_detail_view",
    "metric_search",
    "consume_query",
    "consume_dry_run",
    "consume_semantic",
    "consumption_guide_view",
)


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

    async def popular_metrics(self, limit: int) -> list[tuple[str, int]]:
        """全站指标行为热度（冷启动兜底信号源）。

        聚合 tracking_events 中 ``target_type='metric'`` 且事件类型属于埋点词汇的事件，
        按出现次数降序返回 ``(metric_code, count)``。
        """
        stmt = (
            select(TrackingEvent.target_id, func.count().label("cnt"))
            .where(
                TrackingEvent.target_type == "metric",
                TrackingEvent.target_id.isnot(None),
                TrackingEvent.event_type.in_(METRIC_EVENT_TYPES),
            )
            .group_by(TrackingEvent.target_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).all())

    async def recent_published_metrics(self, limit: int) -> list[str]:
        """最新发布的指标码（无任何行为信号时的最终兜底，保证面板永不空白）。"""
        stmt = (
            select(Metric.metric_code)
            .where(Metric.status == "PUBLISHED", Metric.deleted_at.is_(None))
            .order_by(Metric.created_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)
