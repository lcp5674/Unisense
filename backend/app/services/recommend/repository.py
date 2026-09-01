"""推荐服务 Repository（TD §12.12 / FR-19）。

基于血缘（lineage_edge）与术语（term）的只读推荐；
用户行为画像取自 tracking_events（由 RecommendService 直接查询），
EventLog 无 user 列，不再承担按用户过滤的职责。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.lineage import LineageEdge
from app.models.metric import Metric
from app.models.term import Term
from app.models.tracking import TrackingEvent

#: 行为信号时间窗口（P11 性能审查）：tracking_event 无界增长，全量 GROUP BY/自连接
#: 随数据量线性恶化；推荐热度/协同过滤用近期行为更具业务语义（90 天）。
_BEHAVIOR_WINDOW_DAYS = 90

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

    async def related_by_behavior(self, metric_id: str, limit: int) -> list[tuple[str, int]]:
        """行为协同相关指标（people-also-viewed）。

        找到与 ``metric_id`` 有过交互（查看/查询/收藏等）的用户，聚合这些用户
        交互过的**其它**指标（排除自身），按共同用户数降序返回
        ``(metric_id, co_users)``。用于 MetricDetail「看过此指标的人还看了」卡片，
        行为驱动优先；无共同行为数据时由服务层回退血缘兜底。
        """
        te1 = aliased(TrackingEvent, name="te1")
        te2 = aliased(TrackingEvent, name="te2")
        # P11：自连接无时间窗口会随 tracking_event 无界增长恶化——两端均收敛到 90 天
        since = datetime.now(UTC) - timedelta(days=_BEHAVIOR_WINDOW_DAYS)
        stmt = (
            select(te2.target_id, func.count(func.distinct(te1.actor_id)).label("co_users"))
            .select_from(te1)
            .join(
                te2,
                (te1.actor_id == te2.actor_id)
                & (te2.target_id.isnot(None))
                & (te2.target_id != metric_id),
            )
            .where(
                te1.target_id == metric_id,
                te1.target_type == "metric",
                te2.target_type == "metric",
                te1.event_type.in_(METRIC_EVENT_TYPES),
                te2.event_type.in_(METRIC_EVENT_TYPES),
                te1.created_at >= since,
                te2.created_at >= since,
            )
            .group_by(te2.target_id)
            .order_by(func.count(func.distinct(te1.actor_id)).desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(str(r[0]), int(r[1])) for r in rows if r[0] is not None]

    async def dismissed_metrics(self, user_id: str) -> set[str]:
        """用户标记「不感兴趣」的指标集合（recommend_dismiss 事件）。

        负反馈持久化：前端「不感兴趣」上报 recommend_dismiss 事件，推荐各层级
        据此排除，保证「减少此类推荐」跨刷新真实生效。
        """
        stmt = (
            select(TrackingEvent.target_id)
            .where(
                TrackingEvent.actor_id == user_id,
                TrackingEvent.target_type == "metric",
                TrackingEvent.event_type == "recommend_dismiss",
                TrackingEvent.target_id.isnot(None),
            )
            .distinct()
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {str(r) for r in rows if r}

    async def published_terms(self, limit: int, domain: str | None = None) -> list[Term]:
        """已发布术语候选（软删过滤 + P1-5 域收敛）。

        ``domain`` 非 None 时仅取本域 PUBLISHED 术语——与 ``recent_published_metrics``
        的域收敛对齐：此前仅过滤 PUBLISHED 不校验域，A 域用户面板会出现他域术语
        （跨域信息泄漏）；同时补 ``deleted_at IS NULL`` 防作废术语进入推荐。
        """
        stmt = (
            select(Term)
            .where(Term.status == "PUBLISHED", Term.deleted_at.is_(None))
            .order_by(Term.id.desc())
            .limit(limit)
        )
        if domain:
            stmt = stmt.where(Term.domain == domain)
        return list((await self._session.execute(stmt)).scalars().all())

    async def popular_metrics(self, limit: int) -> list[tuple[str, int]]:
        """全站指标行为热度（冷启动兜底信号源）。

        聚合 tracking_events 中 ``target_type='metric'`` 且事件类型属于埋点词汇的事件，
        按出现次数降序返回 ``(metric_code, count)``。

        P11（性能审查）：加 90 天时间窗口——tracking_event 无界增长，全量 GROUP BY
        随数据量线性恶化，且推荐热度用近期行为更符合「当前热门」语义。
        """
        since = datetime.now(UTC) - timedelta(days=_BEHAVIOR_WINDOW_DAYS)
        stmt = (
            select(TrackingEvent.target_id, func.count().label("cnt"))
            .where(
                TrackingEvent.target_type == "metric",
                TrackingEvent.target_id.isnot(None),
                TrackingEvent.event_type.in_(METRIC_EVENT_TYPES),
                TrackingEvent.created_at >= since,
            )
            .group_by(TrackingEvent.target_id)
            .order_by(func.count().desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(str(r[0]), int(r[1])) for r in rows if r[0] is not None]

    async def recent_published_metrics(
        self, limit: int, domain: str | None = None
    ) -> list[str]:
        """最新发布的指标码（无任何行为信号时的最终兜底，保证面板永不空白）。

        ``domain`` 非 None 时仅取本域（P1-5 域收敛：推荐不产出他域指标码）。
        """
        stmt = (
            select(Metric.metric_code)
            .where(Metric.status == "PUBLISHED", Metric.deleted_at.is_(None))
            .order_by(Metric.created_at.desc())
            .limit(limit)
        )
        if domain:
            stmt = stmt.where(Metric.domain == domain)
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)
