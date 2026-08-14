"""血缘 Repository（MySQL 权威存储）。

对齐 DEV_GUIDE §9a：仅承载数据访问，不含业务规则；所有查询软删过滤。
历史快照 / 环检测 / 断链登记 / 指标级边均为数据访问层能力，策略由上层编排。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lineage import LineageEdge, LineageEdgeHistory, LineageIngestRun


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
        edge, _ = await self._upsert_with_created(
            source_node=source_node,
            target_node=target_node,
            edge_type=edge_type,
            granularity=granularity,
            confidence=confidence,
            provenance=provenance,
            pii_inherited=pii_inherited,
            change_reason=change_reason,
            owner=owner,
        )
        return edge

    async def _upsert_with_created(
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
    ) -> tuple[LineageEdge, bool]:
        """幂等写入血缘边，返回 ``(edge, created)``（created 标记本次是否新建）。

        created=True 表示新插入（用于增量采集的 added 计数）；created=False
        表示命中既有边并覆盖值（updated 计数）。既有边值有变化时先落快照。
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
            created = True
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
            created = False
        await self._db.flush()
        return edge, created

    async def upsert_edge_with_status(
        self,
        *,
        source_node: str,
        target_node: str,
        edge_type: str,
        granularity: str,
        confidence: float = 1.0,
        provenance: str = "sqlglot",
        pii_inherited: bool = False,
        change_reason: str = "ingest",
    ) -> tuple[LineageEdge, bool]:
        """幂等写入血缘边，返回 ``(edge, created)``（增量采集变更计数用）。"""
        return await self._upsert_with_created(
            source_node=source_node,
            target_node=target_node,
            edge_type=edge_type,
            granularity=granularity,
            confidence=confidence,
            provenance=provenance,
            pii_inherited=pii_inherited,
            change_reason=change_reason,
        )

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

    # ---- 增量采集与失效管理（TD §12.2 血缘采集通道）----

    async def _source_l1_edges(self, source: str) -> list[LineageEdge]:
        """取某来源通道全部未删除的表级（L1/DERIVED_FROM）血缘边。"""
        return list(
            (
                await self._db.execute(
                    select(LineageEdge).where(
                        LineageEdge.provenance == source,
                        LineageEdge.edge_type == "DERIVED_FROM",
                        LineageEdge.granularity == "L1",
                        LineageEdge.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    async def mark_seen(self, source: str, seen_pairs: set[tuple[str, str]]) -> tuple[int, int]:
        """把本次采集确认存在的边标记为「已见」。

        对 ``(source_node, target_node)`` 命中 ``seen_pairs`` 的边刷新
        ``last_seen_at`` 并清零 ``missing_count``；此前处于失效队列（stale=True）
        的边一并恢复（stale=False、stale_since=None）。

        Args:
            source: 来源通道标识（如 ``dp_csv``）。
            seen_pairs: 本次采集确认存在的 ``(source_node, target_node)`` 集合。

        Returns:
            ``(confirmed_count, restored_count)``：确认边数、恢复的失效边数。
        """
        rows = await self._source_l1_edges(source)
        confirmed = 0
        restored = 0
        now = datetime.now(UTC)
        for edge in rows:
            if (edge.source_node, edge.target_node) not in seen_pairs:
                continue
            edge.last_seen_at = now
            if edge.missing_count != 0:
                edge.missing_count = 0
            if edge.stale:
                edge.stale = False
                edge.stale_since = None
                restored += 1
            confirmed += 1
        if confirmed:
            await self._db.flush()
        return confirmed, restored

    async def mark_missing(
        self,
        source: str,
        seen_pairs: set[tuple[str, str]],
        threshold: int,
    ) -> tuple[int, int]:
        """增量采集的失效检测：对未再出现的边累加观察期计数。

        仅处理此前至少被确认过一次（``last_seen_at`` 非空）的边；本次仍在
        ``seen_pairs`` 中的边跳过。``missing_count`` 达到 ``threshold`` 时标记
        进入失效队列（stale=True、stale_since=now），避免单次未采到误删真实血缘。

        Args:
            source: 来源通道标识。
            seen_pairs: 本次采集确认存在的 ``(source_node, target_node)`` 集合。
            threshold: 连续未出现轮次阈值（观察期）。

        Returns:
            ``(missing_count, stale_flagged_count)``：未再出现边数、新失效边数。
        """
        rows = await self._source_l1_edges(source)
        missing = 0
        stale_flagged = 0
        now = datetime.now(UTC)
        for edge in rows:
            if edge.last_seen_at is None:
                continue
            if (edge.source_node, edge.target_node) in seen_pairs:
                continue
            edge.missing_count += 1
            missing += 1
            if edge.missing_count >= threshold and not edge.stale:
                edge.stale = True
                edge.stale_since = now
                stale_flagged += 1
        if missing:
            await self._db.flush()
        return missing, stale_flagged

    async def begin_ingest_run(self, source: str) -> LineageIngestRun:
        """开始一次增量采集运行（写入 running 状态记录）。"""
        run = LineageIngestRun(source=source, status="running")
        self._db.add(run)
        await self._db.flush()
        return run

    async def finish_ingest_run(
        self,
        run: LineageIngestRun,
        *,
        status: str,
        total_edges: int = 0,
        added: int = 0,
        updated: int = 0,
        missing: int = 0,
        stale_flagged: int = 0,
        restored: int = 0,
        error: str | None = None,
    ) -> None:
        """结束增量采集运行，回写变更摘要与状态。"""
        run.status = status
        run.total_edges = total_edges
        run.added_count = added
        run.updated_count = updated
        run.missing_count = missing
        run.stale_flagged_count = stale_flagged
        run.restored_count = restored
        run.error = error
        await self._db.flush()

    async def latest_ingest_run(self, source: str) -> LineageIngestRun | None:
        """取某来源通道最近一次运行记录（无记录返回 None）。"""
        return (
            await self._db.execute(
                select(LineageIngestRun)
                .where(LineageIngestRun.source == source)
                .order_by(LineageIngestRun.run_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def list_ingest_runs(self, source: str, limit: int = 20) -> list[LineageIngestRun]:
        """取某来源通道最近的运行历史（按时间倒序）。"""
        return list(
            (
                await self._db.execute(
                    select(LineageIngestRun)
                    .where(LineageIngestRun.source == source)
                    .order_by(LineageIngestRun.run_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def list_channels(self) -> list[dict[str, Any]]:
        """血缘采集通道总览：按来源聚合边数/节点数/失效边数/最近运行。

        Returns:
            ``[{source, edge_count, node_count, stale_count, last_run}]``。
        """
        # 边数 + 失效边数按来源聚合
        edge_rows = (
            await self._db.execute(
                select(
                    LineageEdge.provenance,
                    func.count(LineageEdge.id),
                    func.sum(case((LineageEdge.stale.is_(True), 1), else_=0)),
                )
                .where(LineageEdge.deleted_at.is_(None))
                .group_by(LineageEdge.provenance)
            )
        ).all()
        # 节点数（源节点 ∪ 目标节点 去重）按来源聚合
        src_q = select(LineageEdge.provenance.label("p"), LineageEdge.source_node.label("n")).where(
            LineageEdge.deleted_at.is_(None)
        )
        tgt_q = select(LineageEdge.provenance.label("p"), LineageEdge.target_node.label("n")).where(
            LineageEdge.deleted_at.is_(None)
        )
        union = src_q.union(tgt_q).subquery()
        node_rows = (
            await self._db.execute(
                select(union.c.p, func.count(func.distinct(union.c.n))).group_by(union.c.p)
            )
        ).all()
        node_counts = {str(p): int(c or 0) for p, c in node_rows}

        channels: list[dict[str, Any]] = []
        for provenance, edge_count, stale_count in edge_rows:
            source = str(provenance)
            channels.append(
                {
                    "source": source,
                    "edge_count": int(edge_count or 0),
                    "node_count": int(node_counts.get(source, 0)),
                    "stale_count": int(stale_count or 0),
                    "last_run": await self.latest_ingest_run(source),
                }
            )
        channels.sort(key=lambda c: c["edge_count"], reverse=True)
        return channels

    async def list_stale_edges(
        self, source: str | None = None, limit: int = 200
    ) -> list[LineageEdge]:
        """失效队列：stale=True 且未删除的边（按进入失效时间倒序）。"""
        stmt = (
            select(LineageEdge)
            .where(LineageEdge.stale.is_(True), LineageEdge.deleted_at.is_(None))
            .order_by(LineageEdge.stale_since.desc())
            .limit(limit)
        )
        if source:
            stmt = stmt.where(LineageEdge.provenance == source)
        return list((await self._db.execute(stmt)).scalars().all())

    async def get_edge(self, edge_id: int) -> LineageEdge | None:
        """按主键取未删除的血缘边。"""
        return (
            await self._db.execute(
                select(LineageEdge).where(
                    LineageEdge.id == edge_id, LineageEdge.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()

    async def confirm_stale(self, edge: LineageEdge) -> None:
        """确认失效边：软删（置 deleted_at），不再参与血缘查询。"""
        edge.deleted_at = datetime.now(UTC)
        await self._db.flush()

    async def restore_stale(self, edge: LineageEdge) -> None:
        """恢复失效边：清除失效标记与观察期计数，重新参与血缘查询。"""
        edge.stale = False
        edge.stale_since = None
        edge.missing_count = 0
        await self._db.flush()
