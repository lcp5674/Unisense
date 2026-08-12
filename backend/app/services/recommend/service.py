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
from app.services.recommend.repository import RecommendRepository

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
        edges = await self._repo.related_edges(metric_id, limit)
        result: list[dict[str, Any]] = []
        for e in edges:
            other = e.target_node if e.source_node == metric_id else e.source_node
            result.append({"metric_id": other, "edge_type": e.edge_type, "from": e.source_node})
        return result

    async def recommend_metrics(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """协同过滤推荐 + 血缘冷启动兜底。

        策略：
        1. 基于 tracking_events 计算用户行为画像（查询/收藏/浏览的指标集合）
        2. 找出行为相似的用户（Jaccard 相似度）
        3. 推荐相似用户偏好但当前用户未交互过的指标
        4. 协同过滤无结果时，降级为原有血缘扩展推荐（冷启动兜底）
        """
        # 尝试协同过滤
        cf_results = await self._collaborative_filtering(user_id, limit)
        if cf_results:
            return cf_results

        # 冷启动兜底：原有血缘扩展推荐
        return await self._lineage_fallback(user_id, limit)

    async def _collaborative_filtering(
        self, user_id: int, limit: int
    ) -> list[dict[str, Any]]:
        """基于 tracking_events 的协同过滤推荐。"""
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

        # 4. 聚合相似用户的偏好指标，排除当前用户已交互的
        candidate_scores: dict[str, float] = defaultdict(float)
        for sim_uid, similarity in similar_users:
            sim_metrics = all_profiles.get(sim_uid, set())
            for metric_id in sim_metrics:
                if metric_id not in my_metrics:
                    candidate_scores[metric_id] += similarity

        # 5. 按得分排序取 Top-N
        ranked = sorted(candidate_scores.items(), key=lambda x: x[1], reverse=True)[:limit]
        recommendations: list[dict[str, Any]] = []
        for metric_id, score in ranked:
            recommendations.append({
                "metric_id": metric_id,
                "via": "collaborative_filtering",
                "score": round(score, 4),
                "edge_type": "CF_RECOMMEND",
            })

        return recommendations

    async def _get_user_metric_actions(self, user_id: str) -> set[str]:
        """从 tracking_events 获取用户交互过的指标集合。"""
        stmt = (
            select(TrackingEvent.target_id)
            .where(
                TrackingEvent.actor_id == user_id,
                TrackingEvent.target_type == "metric",
                TrackingEvent.target_id.isnot(None),
                TrackingEvent.event_type.in_(["query", "favorite", "browse", "search"]),
            )
            .distinct()
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {str(r) for r in rows if r}

    async def _get_all_user_profiles(self) -> dict[str, set[str]]:
        """获取所有用户的指标行为画像。"""
        stmt = (
            select(
                TrackingEvent.actor_id,
                TrackingEvent.target_id,
            )
            .where(
                TrackingEvent.target_type == "metric",
                TrackingEvent.target_id.isnot(None),
                TrackingEvent.event_type.in_(["query", "favorite", "browse", "search"]),
            )
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

    async def _lineage_fallback(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """血缘扩展推荐（冷启动兜底）。"""
        events = await self._repo.recent_user_events(user_id, limit * 5)
        seeds: set[str] = set()
        for ev in events:
            payload = getattr(ev, "payload", None) or {}
            mid = payload.get("metric_id") if isinstance(payload, dict) else None
            if mid:
                seeds.add(str(mid))
        recommendations: list[dict[str, Any]] = []
        seen: set[str] = set()
        for seed in seeds:
            edges = await self._repo.related_edges(seed, limit)
            for e in edges:
                other = e.target_node if e.source_node == seed else e.source_node
                if other in seeds or other in seen:
                    continue
                seen.add(other)
                recommendations.append({"metric_id": other, "via": seed, "edge_type": e.edge_type})
                if len(recommendations) >= limit:
                    return recommendations
        return recommendations

    async def recommend_terms(self, limit: int) -> list[Any]:
        return await self._repo.published_terms(limit)
