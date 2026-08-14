"""推荐服务（TD §12.12 / FR-19）。

核心能力：
1. 指标关联推荐：基于血缘（lineage_edge）给出与某指标相关的上下游指标。
2. 个性化推荐：依据用户事件行为（event_log）找到其关注指标，并通过血缘扩展推荐。
3. 术语推荐：返回已发布的术语候选。
4. 协同过滤推荐：基于 tracking_events 用户行为(查询/收藏/浏览)计算相似用户，
   推荐相似用户偏好指标；保留 related_metrics 作为冷启动兜底。

P3: 继承 BaseService Protocol。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.models.tracking import TrackingEvent
from app.services.glossary.schemas import TermResponse
from app.services.recommend.repository import METRIC_EVENT_TYPES, RecommendRepository

logger = logging.getLogger(__name__)

# 协同过滤参数
_SIMILAR_USER_LIMIT = 10
_CANDIDATE_METRIC_LIMIT = 50
_MIN_OVERLAP = 2  # 最小行为重叠数


class RecommendService(BaseService):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._session = session
        self._repo = RecommendRepository(session)

    async def related_metrics(self, metric_id: str, limit: int) -> list[dict[str, Any]]:
        """推荐「与某指标相关」的指标——行为协同优先，血缘兜底。

        FR-16 语义（MetricDetail「看过此指标的人还看了」卡片消费本接口）：
        1. 行为协同：找到与 ``metric_id`` 有过交互的用户，推荐他们看的其它指标
           （people-also-viewed，行为驱动）；
        2. 血缘兜底：无任何共同行为数据时，回退到血缘上下游指标，保证有内容。
        """
        behavior = await self._repo.related_by_behavior(metric_id, limit)
        if behavior:
            return [
                {
                    "metric_id": str(mid),
                    "via": "behavior",
                    "edge_type": "CO_VIEWED",
                    "reason": "看过此指标的人还看了",
                    "score": float(cnt),
                }
                for mid, cnt in behavior
            ]
        edges = await self._repo.related_edges(metric_id, limit)
        result: list[dict[str, Any]] = []
        for e in edges:
            other = e.target_node if e.source_node == metric_id else e.source_node
            result.append(
                {
                    "metric_id": other,
                    "via": "lineage",
                    "edge_type": e.edge_type,
                    "from": e.source_node,
                    "reason": "血缘相关指标",
                }
            )
        return result

    async def recommend_metrics(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """分层推荐策略（协同过滤 → 血缘扩展 → 全局热门 → 最新发布）。

        1. 协同过滤：基于 tracking_events 计算相似用户，推荐其偏好但当前用户未交互的指标；
        2. 血缘扩展：以用户交互过的指标为种子，经 lineage_edge 扩展上下游指标；
        3. 全局热门：无相似用户时，回退到全站行为热度（按埋点事件聚合）；
        4. 最新发布：系统无任何行为信号时的终极兜底，保证面板永不空白。

        每个推荐项均携带 ``reason`` 字段，便于前端向用户解释「为何推荐」。
        所有层级统一排除用户「不感兴趣」（recommend_dismiss）的指标，负反馈跨刷新生效。
        """
        dismissed = await self._repo.dismissed_metrics(str(user_id))
        # 1. 协同过滤（个性化最强，优先）
        cf_results = await self._collaborative_filtering(user_id, limit, dismissed)
        if cf_results:
            return cf_results

        # 2. 血缘扩展兜底
        lineage = await self._lineage_fallback(user_id, limit, dismissed)
        if lineage:
            return lineage

        # 3 & 4. 冷启动兜底：全局热门（排除已交互+已排除）→ 最新发布，保证面板非空
        seeds = await self._get_user_metric_actions(str(user_id))
        exclude = seeds | dismissed
        popular = await self._global_popular(limit, exclude)
        if popular:
            return popular
        return await self._latest_published(limit, exclude)

    async def _collaborative_filtering(
        self, user_id: int, limit: int, dismissed: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """基于 tracking_events 的协同过滤推荐。"""
        dismissed = dismissed or set()
        # 1. 获取当前用户行为画像
        my_metrics = await self._get_user_metric_actions(str(user_id))
        if not my_metrics:
            return []

        # 2. 获取所有活跃用户的行为画像
        all_profiles = await self._get_all_user_profiles()
        if len(all_profiles) < 2:
            return []

        # 3. 计算相似用户（Jaccard）
        similar_users = self._find_similar_users(
            my_metrics, all_profiles, exclude_user=str(user_id)
        )
        if not similar_users:
            return []

        # 4. 聚合相似用户的偏好指标，排除当前用户已交互的与「不感兴趣」的
        candidate_scores: dict[str, float] = defaultdict(float)
        for sim_uid, similarity in similar_users:
            sim_metrics = all_profiles.get(sim_uid, set())
            for metric_id in sim_metrics:
                if metric_id not in my_metrics and metric_id not in dismissed:
                    candidate_scores[metric_id] += similarity

        # 5. 按得分排序取 Top-N
        ranked = sorted(candidate_scores.items(), key=lambda x: x[1], reverse=True)[:limit]
        recommendations: list[dict[str, Any]] = []
        for metric_id, score in ranked:
            recommendations.append(
                {
                    "metric_id": metric_id,
                    "via": "collaborative_filtering",
                    "score": round(score, 4),
                    "edge_type": "CF_RECOMMEND",
                    "reason": "与你行为相似的同事也关注",
                }
            )

        return recommendations

    async def _get_user_metric_actions(self, user_id: str) -> set[str]:
        """从 tracking_events 获取用户交互过的指标集合。"""
        stmt = (
            select(TrackingEvent.target_id)
            .where(
                TrackingEvent.actor_id == user_id,
                TrackingEvent.target_type == "metric",
                TrackingEvent.target_id.isnot(None),
                TrackingEvent.event_type.in_(METRIC_EVENT_TYPES),
            )
            .distinct()
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {str(r) for r in rows if r}

    async def _get_all_user_profiles(self) -> dict[str, set[str]]:
        """获取所有用户的指标行为画像。"""
        stmt = select(
            TrackingEvent.actor_id,
            TrackingEvent.target_id,
        ).where(
            TrackingEvent.target_type == "metric",
            TrackingEvent.target_id.isnot(None),
            TrackingEvent.event_type.in_(METRIC_EVENT_TYPES),
        )
        rows = (await self._session.execute(stmt)).all()
        profiles: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            if row.actor_id and row.target_id:
                profiles[str(row.actor_id)].add(str(row.target_id))
        return dict(profiles)

    @staticmethod
    def _find_similar_users(
        my_metrics: set[str],
        all_profiles: dict[str, set[str]],
        exclude_user: str,
    ) -> list[tuple[str, float]]:
        """计算 Jaccard 相似度，返回相似用户列表。"""
        scores: list[tuple[str, float]] = []
        for uid, metrics in all_profiles.items():
            if uid == exclude_user:
                continue
            intersection = my_metrics & metrics
            if len(intersection) < _MIN_OVERLAP:
                continue
            union = my_metrics | metrics
            jaccard = len(intersection) / len(union) if union else 0.0
            scores.append((uid, jaccard))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:_SIMILAR_USER_LIMIT]

    async def _lineage_fallback(
        self, user_id: int, limit: int, dismissed: set[str] | None = None
    ) -> list[dict[str, Any]]:
        """血缘扩展推荐（冷启动兜底）。

        种子 = 当前用户在 tracking_events 中交互过的指标（与协同过滤同源，
        避免依赖 EventLog 按用户过滤——EventLog 无 user 列，旧实现 ``source ==
        str(user_id)`` 永不命中导致兜底恒空），再经血缘边扩展上下游指标。
        扩展结果排除用户「不感兴趣」的指标（dismissed）。
        """
        dismissed = dismissed or set()
        seeds = await self._get_user_metric_actions(str(user_id))
        recommendations: list[dict[str, Any]] = []
        seen: set[str] = set(seeds) | dismissed
        for seed in seeds:
            edges = await self._repo.related_edges(seed, limit)
            for e in edges:
                other = e.target_node if e.source_node == seed else e.source_node
                if other in seen:
                    continue
                seen.add(other)
                recommendations.append({"metric_id": other, "via": seed, "edge_type": e.edge_type})
                if len(recommendations) >= limit:
                    return recommendations
        return recommendations

    async def _global_popular(self, limit: int, exclude: set[str]) -> list[dict[str, Any]]:
        """全局热门指标（冷启动兜底，基于全站 tracking_events 聚合）。

        排除当前用户已交互过的指标（exclude），避免重复推荐其已看过的指标。
        """
        rows = await self._repo.popular_metrics(limit + len(exclude))
        result: list[dict[str, Any]] = []
        seen: set[str] = set(exclude)
        for metric_id, _cnt in rows:
            metric_id = str(metric_id)
            if metric_id in seen:
                continue
            seen.add(metric_id)
            result.append(
                {
                    "metric_id": metric_id,
                    "via": "global_hot",
                    "edge_type": "POPULAR",
                    "reason": "全站热门指标",
                }
            )
            if len(result) >= limit:
                break
        return result

    async def _latest_published(self, limit: int, exclude: set[str]) -> list[dict[str, Any]]:
        """最新发布指标（终极兜底，保证面板永不空白）。

        当系统完全没有任何行为信号时，回退到最新发布的指标，作为用户的探索起点；
        同样排除当前用户已交互过的指标。
        """
        codes = await self._repo.recent_published_metrics(limit + len(exclude))
        result: list[dict[str, Any]] = []
        seen: set[str] = set(exclude)
        for code in codes:
            code = str(code)
            if code in seen:
                continue
            seen.add(code)
            result.append(
                {
                    "metric_id": code,
                    "via": "latest_published",
                    "edge_type": "RECENT",
                    "reason": "最新发布指标",
                }
            )
            if len(result) >= limit:
                break
        return result

    async def recommend_terms(self, limit: int) -> list[TermResponse]:
        """返回已发布术语候选（ORM → TermResponse 转换，避免 Pydantic 序列化 500）。"""
        terms = await self._repo.published_terms(limit)
        return [TermResponse.from_model(t) for t in terms]
