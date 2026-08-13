"""血缘 Repository（MySQL 权威存储）。

对齐 DEV_GUIDE §9a：仅承载数据访问，不含业务规则；所有查询软删过滤。
历史快照 / 环检测 / 断链登记 / 指标级边均为数据访问层能力，策略由上层编排。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lineage import LineageEdge, LineageEdgeHistory


class LineageRepository:
    """血缘边数据访问。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _upsert(
        self,
        *,
        source_node: str,
        target_node: str,
        edge_type: str,
        granularity: str = "L3",
        confidence: float = 1.0,
        provenance: str = "sqlglot",
        pii_inherited: bool = False,
        change_reason: str = "manual",
        owner: str | None = None,
    ) -> LineageEdge:
        """按唯一键（source/target/edge_type/granularity）幂等写入血缘边。

        既有边值有变化时，先落一条变更前快照（record_edge_history）再覆盖，
        值未变化时不重复写历史（幂等）。
        """
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
        if existing is None:
            edge = LineageEdge(
                source_node=source_node,
                target_node=target_node,
                edge_type=edge_type,
                granularity=granularity,
                confidence=confidence,
                provenance=provenance,
                pii_inherited=pii_inherited,
                owner=owner,
            )
            self._db.add(edge)
        else:
            changes: dict[str, object] = {}
            if existing.confidence != confidence:
                changes["confidence"] = confidence
            if existing.provenance != provenance:
                changes["provenance"] = provenance
            if existing.pii_inherited != pii_inherited:
                changes["pii_inherited"] = pii_inherited
            if owner is not None and existing.owner != owner:
                changes["owner"] = owner
            if changes:
                await self.record_edge_history(existing, change_reason)
                for column, value in changes.items():
                    setattr(existing, column, value)
            edge = existing
        await self._db.flush()
        return edge

    async def upsert_edge(
        self,
        *,
        source_node: str,
        target_node: str,
        edge_type: str,
        granularity: str,
        confidence: float = 1.0,
        provenance: str = "sqlglot",
        pii_inherited: bool = False,
        change_reason: str = "reparse",
    ) -> LineageEdge:
        """幂等写入血缘边（按唯一键更新或插入）。"""
        return await self._upsert(
            source_node=source_node,
            target_node=target_node,
            edge_type=edge_type,
            granularity=granularity,
            confidence=confidence,
            provenance=provenance,
            pii_inherited=pii_inherited,
            change_reason=change_reason,
        )

    async def record_edge_history(
        self, edge: LineageEdge, change_reason: str
    ) -> LineageEdgeHistory:
        """把边当前值写入历史快照（供覆盖既有边前调用）。"""
        history = LineageEdgeHistory(
            source_node=edge.source_node,
            target_node=edge.target_node,
            edge_type=edge.edge_type,
            granularity=edge.granularity,
            confidence=edge.confidence,
            provenance=edge.provenance,
            pii_inherited=edge.pii_inherited,
            change_reason=change_reason,
        )
        self._db.add(history)
        await self._db.flush()
        return history

    async def would_create_cycle(self, edge: LineageEdge) -> bool:
        """检测新增 ``edge``（DERIVED_FROM）是否成环：source 已在 target 下游。

        BFS 沿 DERIVED_FROM 边从 target 向下游展开，可达 source 即视为成环；
        visited 集合防止环上的无限遍历。非 DERIVED_FROM 边不参与环检测。
        """
        if edge.edge_type != "DERIVED_FROM":
            return False
        if edge.source_node == edge.target_node:
            return True
        source_node = edge.source_node
        visited: set[str] = {edge.target_node}
        frontier: list[str] = [edge.target_node]
        while frontier:
            current = frontier.pop()
            for e in await self._edges_from(current):
                if e.edge_type != "DERIVED_FROM":
                    continue
                if e.target_node == source_node:
                    return True
                if e.target_node not in visited:
                    visited.add(e.target_node)
                    frontier.append(e.target_node)
        return False

    async def upsert_metric_edge(
        self,
        *,
        from_metric: str,
        to_metric: str,
        edge_type: str = "DERIVED_FROM",
        confidence: float = 1.0,
        provenance: str = "sqlglot",
        change_reason: str = "reparse",
    ) -> LineageEdge:
        """写入指标级血缘边（节点 id 用 ``metric:{code}`` 前缀，粒度 L3）。"""
        return await self._upsert(
            source_node=f"metric:{from_metric}",
            target_node=f"metric:{to_metric}",
            edge_type=edge_type,
            granularity="L3",
            confidence=confidence,
            provenance=provenance,
            change_reason=change_reason,
        )

    async def register_break(
        self,
        *,
        node: str,
        external_system: str,
        owner: str,
        direction: str = "downstream",
    ) -> LineageEdge:
        """登记断链：写入 EXTERNAL_BREAK 边，另一侧为 ``external:{system}`` 占位节点。

        direction=downstream 时 node 为上游（node -> external），
        direction=upstream 时 node 为下游（external -> node）；幂等。
        """
        external_node = f"external:{external_system}"
        if direction == "upstream":
            source_node, target_node = external_node, node
        else:
            source_node, target_node = node, external_node
        return await self._upsert(
            source_node=source_node,
            target_node=target_node,
            edge_type="EXTERNAL_BREAK",
            granularity="L1",
            confidence=1.0,
            provenance="manual",
            change_reason="manual",
            owner=owner,
        )

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
        """级联软删某节点相关的全部血缘边（影响分析失效时维护一致性）。

        置 ``deleted_at`` 而非物理删除：血缘边是审计/溯源对象，物理删除会连带
        丢失 ``lineage_edge_history`` 的关联上下文，且与全仓软删约定（所有查询
        以 ``deleted_at IS NULL`` 过滤）不一致。
        """
        stmt = (
            update(LineageEdge)
            .where(
                (LineageEdge.source_node == node) | (LineageEdge.target_node == node),
                LineageEdge.deleted_at.is_(None),
            )
            .values(deleted_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        result = await self._db.execute(stmt)
        await self._db.flush()
        return int(getattr(result, "rowcount", 0) or 0)
