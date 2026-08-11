"""推荐服务（TD §12.12 / FR-19）。

核心能力：
1. 指标关联推荐：基于血缘（lineage_edge）给出与某指标相关的上下游指标。
2. 个性化推荐：依据用户事件行为（event_log）找到其关注指标，并通过血缘扩展推荐。
3. 术语推荐：返回已发布的术语候选。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.recommend.repository import RecommendRepository


class RecommendService:
    def __init__(self, session: AsyncSession) -> None:
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
