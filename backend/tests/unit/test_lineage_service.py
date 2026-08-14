"""lineage service 单测（注入假 repo，覆盖解析落库、影响分析、what-if、缓存、PII、分页）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.services.lineage.schemas import (
    LineageEdgeResponse,
    LineageImpactParams,
    LineageParseRequest,
)
from app.services.lineage.service import LineageService, paginate_edges


def make_edge(
    i: int = 1,
    source: str = "table:a",
    target: str = "table:t",
    edge_type: str = "DERIVED_FROM",
    pii: bool = False,
) -> LineageEdgeResponse:
    """构造血缘边响应测试数据。"""
    granularity = "L2" if "field:" in source or "field:" in target else "L1"
    return LineageEdgeResponse(
        id=i,
        source_node=source,
        target_node=target,
        edge_type=edge_type,
        granularity=granularity,
        confidence=1.0,
        provenance="sqlglot",
        pii_inherited=pii,
    )


class FakeRepo:
    """内存假仓库：幂等 upsert + 按节点过滤的影响分析（对齐真实 BFS 读语义）。"""

    def __init__(self) -> None:
        self.edges: list[object] = []
        self.impact: list[LineageEdgeResponse] = []
        self.upsert_calls: list[dict[str, Any]] = []
        self.deleted_count = 0

    async def upsert_edge(self, **kwargs: object) -> SimpleNamespace:
        self.upsert_calls.append(kwargs)
        edge = SimpleNamespace(id=len(self.edges) + 1, **kwargs)
        self.edges.append(edge)
        return edge

    async def would_create_cycle(self, edge: object) -> bool:
        """环检假实现：默认不成环（供 parse_and_store 建边前调用）。"""
        return False

    async def query_impact(
        self, node: str, direction: str, max_hops: int, max_edges: int = 5000
    ) -> list[LineageEdgeResponse]:
        out: list[LineageEdgeResponse] = []
        for e in self.impact:
            if direction in ("downstream", "both") and e.source_node == node:
                out.append(e)
            if direction in ("upstream", "both") and e.target_node == node:
                out.append(e)
        return out

    async def soft_delete_by_node(self, node: str) -> int:
        return self.deleted_count


class FakeGraph:
    """模拟 Neo4j 图读；result=None 表示图不可用降级。"""

    def __init__(self, result: list[tuple[str, str, str]] | None | None = None) -> None:
        self.result = result
        self.calls: list[tuple[str, str, int, int]] = []

    async def query_impact(
        self, node: str, direction: str, max_hops: int, max_edges: int
    ) -> list[tuple[str, str, str]] | None:
        self.calls.append((node, direction, max_hops, max_edges))
        return self.result


class FakeRedis:
    """内存假 Redis（cache-aside 验证用）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.store[key] = value
        self.calls.append((key, value))
        return True


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
    svc._repo.impact = [make_edge(source="table:a", target="table:t")]
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert len(out) == 1
    assert out[0].target_node == "table:t"


async def test_query_impact_uses_graph_when_available() -> None:
    svc = LineageService(db=object())
    svc._repo = FakeRepo()
    svc._graph = FakeGraph(result=[("table:a", "table:g", "DERIVED_FROM")])
    svc._redis = FakeRedis()
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert len(out) == 1
    assert out[0].target_node == "table:g"
    assert out[0].provenance == "neo4j"
    assert out[0].granularity == "L1"


async def test_query_impact_falls_back_to_mysql_when_graph_none() -> None:
    svc = LineageService(db=object())
    repo = FakeRepo()
    repo.impact = [make_edge(source="table:a", target="table:t")]
    svc._repo = repo
    svc._graph = FakeGraph(result=None)
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert len(out) == 1
    assert out[0].target_node == "table:t"
    assert out[0].provenance == "sqlglot"


async def test_query_impact_falls_back_to_mysql_when_graph_empty() -> None:
    """图可达但查不到该节点（空列表）时回退 MySQL——否则仅写入 MySQL 的
    导入血缘（如 dp_csv）在前端永远不可见。"""
    svc = LineageService(db=object())
    repo = FakeRepo()
    repo.impact = [make_edge(source="table:a", target="table:t")]
    svc._repo = repo
    svc._graph = FakeGraph(result=[])
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert len(out) == 1
    assert out[0].target_node == "table:t"
    assert out[0].provenance == "sqlglot"


async def test_query_impact_skips_cache_without_redis() -> None:
    svc = LineageService(db=object())
    repo = FakeRepo()
    repo.impact = [make_edge(source="table:a", target="table:t")]
    svc._repo = repo
    svc._graph = FakeGraph(result=None)
    svc._redis = None
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert out[0].target_node == "table:t"


async def test_query_impact_reads_from_cache() -> None:
    svc = LineageService(db=object())
    svc._repo = FakeRepo()
    svc._redis = FakeRedis()
    await svc._impact_cache_set(
        "lineage:impact:table:a:downstream:5",
        [make_edge(source="table:a", target="table:cached")],
    )
    graph = FakeGraph(result=[("table:a", "table:graph", "DERIVED_FROM")])
    svc._graph = graph
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert out[0].target_node == "table:cached"
    assert graph.calls == [], "缓存命中时不应再访问图"


async def test_query_impact_writes_cache_on_miss() -> None:
    svc = LineageService(db=object())
    svc._repo = FakeRepo()
    graph = FakeGraph(result=[("table:a", "table:b", "DERIVED_FROM")])
    svc._graph = graph
    redis = FakeRedis()
    svc._redis = redis
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert out[0].target_node == "table:b"
    assert len(redis.store) == 1
    # 图宕机后二次读仍能从缓存命中
    graph.result = None
    out2 = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert out2[0].target_node == "table:b"
    assert len(graph.calls) == 1


async def test_query_impact_cache_corruption_falls_through() -> None:
    svc = LineageService(db=object())
    svc._repo = FakeRepo()
    redis = FakeRedis()
    redis.store["lineage:impact:table:a:downstream:5"] = "{not-json"
    svc._redis = redis
    svc._graph = FakeGraph(result=[("table:a", "table:b", "DERIVED_FROM")])
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert out[0].target_node == "table:b"


async def test_impact_preview_classifies_impact_and_risk() -> None:
    svc = LineageService(db=object())
    repo = FakeRepo()
    repo.impact = [
        make_edge(i=1, source="metric:gm", target="metric:m1"),
        make_edge(i=2, source="metric:gm", target="metric:m2"),
        make_edge(i=3, source="metric:gm", target="table:dw.rpt1"),
        make_edge(i=4, source="metric:gm", target="report:r1", edge_type="CONSUMED_BY"),
    ]
    svc._repo = repo
    svc._graph = None
    svc._redis = None
    result = await svc.impact_preview("gm", "UPDATE")
    assert [m.metric_code for m in result.affected_metrics] == ["m1", "m2"]
    assert all(m.change_type == "UPDATE" for m in result.affected_metrics)
    assert result.affected_tables == ["table:dw.rpt1"]
    assert result.affected_consumers == ["report:r1"]
    assert result.risk_level == "medium"


async def test_impact_preview_low_risk_when_no_impact() -> None:
    svc = LineageService(db=object())
    svc._repo = FakeRepo()
    svc._graph = None
    svc._redis = None
    result = await svc.impact_preview("ghost", "DROP")
    assert result.affected_metrics == []
    assert result.affected_tables == []
    assert result.affected_consumers == []
    assert result.risk_level == "low"


async def test_impact_preview_breaking_change_escalates_risk() -> None:
    svc = LineageService(db=object())
    repo = FakeRepo()
    repo.impact = [make_edge(source="metric:gm", target="metric:m1")]
    svc._repo = repo
    svc._graph = None
    svc._redis = None
    result = await svc.impact_preview("gm", "BREAKING")
    assert result.risk_level == "high"


async def test_propagate_pii_marks_derived_descendants() -> None:
    svc = LineageService(db=object())
    repo = FakeRepo()
    repo.impact = [
        make_edge(i=1, source="metric:source", target="metric:mid"),
        make_edge(i=2, source="metric:mid", target="metric:leaf"),
        make_edge(i=3, source="metric:mid", target="table:other", edge_type="CONSUMED_BY"),
    ]
    svc._repo = repo
    marked = await svc.propagate_pii("metric:source", depth=3)
    assert marked == 2
    pii_calls = [c for c in repo.upsert_calls if c.get("pii_inherited") is True]
    assert len(pii_calls) == 2
    assert {c["target_node"] for c in pii_calls} == {"metric:mid", "metric:leaf"}


async def test_propagate_pii_respects_depth() -> None:
    svc = LineageService(db=object())
    repo = FakeRepo()
    repo.impact = [
        make_edge(i=1, source="metric:a", target="metric:b"),
        make_edge(i=2, source="metric:b", target="metric:c"),
        make_edge(i=3, source="metric:c", target="metric:d"),
    ]
    svc._repo = repo
    marked = await svc.propagate_pii("metric:a", depth=2)
    assert marked == 2


async def test_list_edges_returns_direct_neighbors() -> None:
    svc = LineageService(db=object())
    repo = FakeRepo()
    repo.impact = [make_edge(source="table:a", target="table:t")]
    svc._repo = repo
    out = await svc.list_edges("table:a")
    assert len(out) == 1
    assert out[0].target_node == "table:t"
    assert out[0].pii_inherited is False


async def test_delete_by_node_delegates() -> None:
    svc = LineageService(db=object())
    repo = FakeRepo()
    repo.deleted_count = 3
    svc._repo = repo
    assert await svc.delete_by_node("table:a") == 3


def test_paginate_edges_slices_and_has_more() -> None:
    edges = [make_edge(i=i, target=f"table:t{i}") for i in range(1, 26)]
    page1 = paginate_edges(edges, 1, 10)
    assert page1["total"] == 25
    assert len(page1["items"]) == 10
    assert page1["has_more"] is True
    assert page1["items"][0]["id"] == 1

    last = paginate_edges(edges, 3, 10)
    assert len(last["items"]) == 5
    assert last["has_more"] is False

    empty = paginate_edges([], 1, 50)
    assert empty["total"] == 0
    assert empty["has_more"] is False


def test_risk_level_thresholds() -> None:
    assert LineageService._risk_level(0, "DROP") == "low"
    assert LineageService._risk_level(12, "UPDATE") == "high"
    assert LineageService._risk_level(25, "UPDATE") == "critical"
    assert LineageService._risk_level(4, "UPDATE") == "medium"
    assert LineageService._risk_level(4, "BREAKING") == "high"
