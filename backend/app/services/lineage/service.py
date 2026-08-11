"""血缘服务（领域编排）。

对齐 TD §12.2（血缘解析）与 DEV_GUIDE §9a（编排层在 Service 内聚合 Repository/图/事件）。
解析器为纯函数（services/lineage/parser.py）；边以 MySQL 为权威存储，Neo4j 为可选图存储。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.lineage.events import LineageEventPublisher
from app.services.lineage.graph import LineageGraphClient
from app.services.lineage.parser import (
    extract_field_lineage,
    extract_table_lineage,
    node_field,
    node_table,
)
from app.services.lineage.repository import LineageRepository
from app.services.lineage.schemas import (
    LineageEdgeResponse,
    LineageImpactParams,
    LineageParseRequest,
    LineageParseResponse,
)


class LineageService:
    """血缘解析与影响分析服务。"""

    def __init__(
        self,
        db: AsyncSession,
        *,
        graph: LineageGraphClient | None = None,
        events: LineageEventPublisher | None = None,
    ) -> None:
        self._repo = LineageRepository(db)
        self._graph = graph
        self._events = events

    async def parse_and_store(
        self, req: LineageParseRequest, actor_id: int
    ) -> LineageParseResponse:
        """解析 SQL 并持久化血缘边（表级 + 字段级）。"""
        table_edges = extract_table_lineage(req.sql, req.dialect)
        field_edges = extract_field_lineage(req.sql, req.dialect)

        stored_table = 0
        stored_field = 0
        graph_edges: list[tuple[str, str, str]] = []

        for e in table_edges:
            sn = node_table(e.source)
            tn = node_table(e.target)
            await self._repo.upsert_edge(
                source_node=sn,
                target_node=tn,
                edge_type="DERIVED_FROM",
                granularity="L1",
                provenance=req.provenance,
            )
            stored_table += 1
            graph_edges.append((sn, tn, "DERIVED_FROM"))

        for fe in field_edges:
            if not (fe.source_table and fe.source_column and fe.target_table and fe.target_column):
                continue
            sn = node_field(fe.source_table, fe.source_column)
            tn = node_field(fe.target_table, fe.target_column)
            await self._repo.upsert_edge(
                source_node=sn,
                target_node=tn,
                edge_type="DERIVED_FROM",
                granularity="L2",
                provenance=req.provenance,
            )
            stored_field += 1
            graph_edges.append((sn, tn, "DERIVED_FROM"))

        graph_written = False
        if self._graph is not None:
            graph_written = await self._graph.write_edges(graph_edges)
        if self._events is not None:
            await self._events.publish(
                "lineage_parsed",
                {"table_edges": stored_table, "field_edges": stored_field},
            )
        return LineageParseResponse(
            table_edges=stored_table,
            field_edges=stored_field,
            graph_written=graph_written,
        )

    async def query_impact(self, params: LineageImpactParams) -> list[LineageEdgeResponse]:
        """影响分析：返回从给定节点出发、按方向展开的全部血缘边。"""
        edges = await self._repo.query_impact(
            params.node, params.direction, params.max_hops, max_edges=5000
        )
        return [LineageEdgeResponse.model_validate(e) for e in edges]

    async def list_edges(self, node: str, direction: str = "both") -> list[LineageEdgeResponse]:
        """列出与某节点相关的全部血缘边。"""
        edges = await self._repo.query_impact(node, direction, max_hops=1, max_edges=5000)
        return [LineageEdgeResponse.model_validate(e) for e in edges]

    async def delete_by_node(self, node: str) -> int:
        """级联软删某节点相关的全部血缘边（数据源删除时维护一致性）。"""
        return await self._repo.soft_delete_by_node(node)
