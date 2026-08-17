"""lineage Neo4j 定时对账任务（app/services/lineage/neo4j_sync.py）单测。

覆盖：
- ``run_sync``：加载权威数据 → 补全节点属性 + 写表级/指标边（M2 对账核心）；
- ``sync_neo4j_assets_task``：arq 任务装配（自建会话 + 图连接 + 异常降级）；
- ``load_table_edges``：权威库表级边加载（修复 ingest_batch 历史未写图漂移）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.services.lineage import neo4j_sync


class FakeSyncGraph:
    """对账用假图：记录 upsert/write 调用。"""

    def __init__(self) -> None:
        self.asset_ids = ["table:t1", "table:existing", "field:a.x"]
        self.upserted: list[dict[str, object]] = []
        self.written: list[tuple[str, str, str]] = []

    async def list_asset_ids(self, limit: int = 100000) -> list[str]:
        return self.asset_ids

    async def upsert_assets(self, assets: list[dict[str, object]]) -> bool:
        self.upserted.extend(assets)
        return True

    async def write_edges(self, edges: list[tuple[str, str, str]]) -> bool:
        self.written.extend(edges)
        return True

    async def dispose(self) -> None:
        pass


async def test_run_sync_writes_assets_and_edges() -> None:
    """M2: 对账核心补全节点属性并写入表级/指标血缘边。"""
    db = AsyncMock()
    graph = FakeSyncGraph()
    with (
        patch.object(
            neo4j_sync,
            "load_catalog_attrs",
            AsyncMock(
                return_value={
                    "t1": {
                        "type": "table",
                        "label": "t1",
                        "pii": False,
                        "domain": "d",
                        "owner": None,
                    }
                }
            ),
        ),
        patch.object(
            neo4j_sync,
            "load_metric_attrs",
            AsyncMock(
                return_value={
                    "m1": {
                        "type": "metric",
                        "label": "m1",
                        "pii": False,
                        "domain": None,
                        "owner": None,
                    }
                }
            ),
        ),
        patch.object(
            neo4j_sync,
            "load_table_edges",
            AsyncMock(return_value=[("table:t1", "table:t2", "DERIVED_FROM")]),
        ),
        patch.object(
            neo4j_sync,
            "load_metric_edges",
            AsyncMock(return_value=[("table:t1", "metric:m1", "DERIVED_FROM")]),
        ),
    ):
        stats = await neo4j_sync.run_sync(db, graph)

    # 表级边全量写图（修复 ingest_batch 历史漂移）
    assert ("table:t1", "table:t2", "DERIVED_FROM") in graph.written
    # 指标边表端已在图内 → 保留
    assert ("table:t1", "metric:m1", "DERIVED_FROM") in graph.written
    # 图内既有节点补全属性（table:existing 未匹配目录 → 用 id 推导降级属性）
    assert any(a["id"] == "table:existing" for a in graph.upserted)
    assert stats["written_edges"] is True


async def test_run_sync_metric_edge_with_unknown_table_dropped() -> None:
    """对账：指标边表端不在图内时丢弃（避免引入无属性孤立表节点）。"""
    db = AsyncMock()
    graph = FakeSyncGraph()
    with (
        patch.object(neo4j_sync, "load_catalog_attrs", AsyncMock(return_value={})),
        patch.object(neo4j_sync, "load_metric_attrs", AsyncMock(return_value={})),
        patch.object(neo4j_sync, "load_table_edges", AsyncMock(return_value=[])),
        patch.object(
            neo4j_sync,
            "load_metric_edges",
            AsyncMock(return_value=[("table:ghost", "metric:m1", "DERIVED_FROM")]),
        ),
    ):
        stats = await neo4j_sync.run_sync(db, graph)

    assert ("table:ghost", "metric:m1", "DERIVED_FROM") not in graph.written
    assert stats["metric_edges"] == 0


async def test_sync_task_self_builds_session_and_graph() -> None:
    """arq 任务：自建会话与图连接，委托 run_sync。"""
    db = AsyncMock()
    graph = FakeSyncGraph()

    class _Ctx:
        def __init__(self) -> None:
            self.db = db

        async def __aenter__(self) -> AsyncMock:
            return self.db

        async def __aexit__(self, *args: object) -> bool:
            return False

    with (
        patch("app.db.mysql.async_session_factory", return_value=_Ctx()),
        patch(
            "app.services.lineage.neo4j_sync.LineageGraphClient", return_value=graph
        ) as m_graph,
        patch.object(
            neo4j_sync,
            "run_sync",
            AsyncMock(return_value={"written_nodes": True, "written_edges": True}),
        ) as m_run,
    ):
        stats = await neo4j_sync.sync_neo4j_assets_task({})

    m_graph.assert_called_once_with()
    m_run.assert_awaited_once_with(db, graph)
    assert stats["written_nodes"] is True


async def test_sync_task_failure_degrades() -> None:
    """arq 任务：DB/图不可达时降级返回统计，不抛错中断 worker。"""
    with (
        patch(
            "app.db.mysql.async_session_factory",
            side_effect=RuntimeError("db down"),
        ),
        patch(
            "app.services.lineage.neo4j_sync.LineageGraphClient",
            return_value=AsyncMock(),
        ),
    ):
        stats = await neo4j_sync.sync_neo4j_assets_task({})

    assert stats["written_nodes"] is False
    assert "error" in stats


async def test_load_table_edges_returns_active_l1_edges() -> None:
    """权威库表级边加载：只取活跃 DERIVED_FROM/L1（软删过滤由查询完成）。"""
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(
            source_node="table:a", target_node="table:b", edge_type="DERIVED_FROM"
        ),
        SimpleNamespace(
            source_node="table:c", target_node="table:d", edge_type="DERIVED_FROM"
        ),
    ]

    class _FakeExecuteResult:
        def all(self) -> list[SimpleNamespace]:
            return rows

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_FakeExecuteResult())
    edges = await neo4j_sync.load_table_edges(db)
    assert edges == [
        ("table:a", "table:b", "DERIVED_FROM"),
        ("table:c", "table:d", "DERIVED_FROM"),
    ]
