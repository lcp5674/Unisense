"""lineage service 单测（注入假 repo，验证解析落库与影响分析编排）。"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.lineage.schemas import (
    LineageEdgeResponse,
    LineageImpactParams,
    LineageParseRequest,
)
from app.services.lineage.service import LineageService


class FakeRepo:
    def __init__(self) -> None:
        self.edges: list[object] = []
        self.impact: list[LineageEdgeResponse] = []

    async def upsert_edge(self, **kwargs: object) -> SimpleNamespace:
        edge = SimpleNamespace(id=len(self.edges) + 1, **kwargs)
        self.edges.append(edge)
        return edge

    async def query_impact(
        self, node: str, direction: str, max_hops: int, max_edges: int = 5000
    ) -> list[LineageEdgeResponse]:
        return self.impact


async def test_parse_and_store_counts_no_graph() -> None:
    svc = LineageService(db=object())
    svc._repo = FakeRepo()
    res = await svc.parse_and_store(
        LineageParseRequest(sql="INSERT INTO t SELECT a.id FROM a"), actor_id=1
    )
    assert res.table_edges == 1
    assert res.graph_written is False
    assert len(svc._repo.edges) >= 1


async def test_query_impact_delegates_to_repo() -> None:
    svc = LineageService(db=object())
    svc._repo = FakeRepo()
    svc._repo.impact = [
        LineageEdgeResponse(
            id=1,
            source_node="table:a",
            target_node="table:t",
            edge_type="DERIVED_FROM",
            granularity="L1",
            confidence=1.0,
            provenance="sqlglot",
        )
    ]
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert len(out) == 1
    assert out[0].target_node == "table:t"
