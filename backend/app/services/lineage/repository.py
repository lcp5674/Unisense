"""血缘 Repository（MySQL 权威存储）。

对齐 DEV_GUIDE §9a：仅承载数据访问，不含业务规则；所有查询软删过滤。
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lineage import LineageEdge


class LineageRepository:
    """血缘边数据访问。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def upsert_edge(
        self,
        *,
        source_node: str,
        target_node: str,
        edge_type: str,
        granularity: str,
        confidence: float = 1.0,
        provenance: str = "sqlglot",
    ) -> LineageEdge:
        """幂等写入血缘边（按唯一键更新或插入）。"""
        existing = (
            await self._db.execute(
                select(LineageEdge).where(
                    LineageEdge.source_node == source_node,
                    LineageEdge.target_node == target_node,
                    LineageEdge.edge_type == edge_type,
                    LineageEdge.granularity == granularity,
                    LineageEdge.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.confidence = confidence
            existing.provenance = provenance
            edge = existing
        else:
            edge = LineageEdge()
            edge.source_node = source_node
            edge.target_node = target_node
            edge.edge_type = edge_type
            edge.granularity = granularity
            edge.confidence = confidence
            edge.provenance = provenance
            self._db.add(edge)
        await self._db.flush()
        return edge

    async def _edges_from(self, node: str) -> list[LineageEdge]:
        return list(
            (
                await self._db.execute(
                    select(LineageEdge).where(
                        LineageEdge.source_node == node, LineageEdge.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _edges_to(self, node: str) -> list[LineageEdge]:
        return list(
            (
                await self._db.execute(
                    select(LineageEdge).where(
                        LineageEdge.target_node == node, LineageEdge.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )

    async def query_impact(
        self, node: str, direction: str = "downstream", max_hops: int = 5, max_edges: int = 5000
    ) -> list[LineageEdge]:
        """基于 BFS 的影响分析（按跳数展开，最多 max_hops 跳，结果上限 max_edges）。"""
        visited: set[str] = {node}
        frontier: list[str] = [node]
        result: list[LineageEdge] = []
        seen_edges: set[int] = set()
        hops = 0
        while frontier and hops < max_hops:
            next_frontier: list[str] = []
            for n in frontier:
                if direction in ("downstream", "both"):
                    for e in await self._edges_from(n):
                        if e.id not in seen_edges:
                            seen_edges.add(e.id)
                            result.append(e)
                        if e.target_node not in visited:
                            visited.add(e.target_node)
                            next_frontier.append(e.target_node)
                if direction in ("upstream", "both"):
                    for e in await self._edges_to(n):
                        if e.id not in seen_edges:
                            seen_edges.add(e.id)
                            result.append(e)
                        if e.source_node not in visited:
                            visited.add(e.source_node)
                            next_frontier.append(e.source_node)
            frontier = next_frontier
            hops += 1
            if len(result) >= max_edges:
                break
        return result

    async def soft_delete_by_node(self, node: str) -> int:
        """级联软删某节点相关的全部血缘边（影响分析失效时维护一致性）。"""
        stmt = (
            delete(LineageEdge)
            .where(
                (LineageEdge.source_node == node) | (LineageEdge.target_node == node),
                LineageEdge.deleted_at.is_(None),
            )
            .execution_options(synchronize_session=False)
        )
        result = await self._db.execute(stmt)
        await self._db.flush()
        return int(getattr(result, "rowcount", 0) or 0)
