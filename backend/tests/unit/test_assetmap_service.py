"""资产地图服务单元测试（TD §12.11 / FR-18）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.models.data_source import DBCatalog
from app.services.assetmap.service import AssetMapService


async def _svc() -> tuple[AssetMapService, MagicMock]:
    db = MagicMock()
    svc = AssetMapService(db)
    repo = MagicMock()
    repo.catalog_summary = AsyncMock(
        return_value={"total": 3, "by_entity_type": {}, "by_sensitivity": {}, "orphan_assets": 1}
    )
    repo.classification_summary = AsyncMock(return_value={"by_sensitivity": {"PII": 2}})
    repo.metric_summary = AsyncMock(
        return_value={"by_domain": {"sales": 1}, "by_status": {"PUBLISHED": 1}}
    )
    repo.list_tables = AsyncMock(
        return_value=[
            DBCatalog(source_id="s", entity_name="t", entity_type="table", schema_json={})
        ]
    )
    repo.orphan_assets = AsyncMock(return_value=[])
    svc._repo = repo  # noqa: SLF001
    return svc, repo


async def test_catalog_summary() -> None:
    svc, repo = await _svc()
    out = await svc.catalog_summary()
    assert out["total"] == 3
    assert out["orphan_assets"] == 1
    repo.catalog_summary.assert_awaited()


async def test_classification_summary() -> None:
    svc, repo = await _svc()
    out = await svc.classification_summary()
    assert out["by_sensitivity"]["PII"] == 2


async def test_list_tables() -> None:
    svc, repo = await _svc()
    items = await svc.list_tables(None, None, 100)
    assert len(items) == 1
    repo.list_tables.assert_awaited()


async def test_get_entity_detail_passthrough() -> None:
    """详情端点编排：透传 repository 结果；实体不存在返回 None。"""
    svc, repo = await _svc()
    repo.get_entity_detail = AsyncMock(
        return_value={"id": 1, "entity_name": "catalog.db.t", "lineage_count": 2}
    )
    out = await svc.get_entity_detail(1)
    assert out["lineage_count"] == 2
    repo.get_entity_detail.assert_awaited_once_with(1)

    repo.get_entity_detail = AsyncMock(return_value=None)
    assert await svc.get_entity_detail(999) is None


async def test_get_graph_falls_back_to_mysql_when_neo4j_unconfigured(
    monkeypatch,
) -> None:
    """Neo4j 不可达/熔断打开时，get_graph 降级到 MySQL 拼接（不抛异常）。"""
    svc, repo = await _svc()
    fake_breaker = type("B", (), {"allow": lambda self: False})
    monkeypatch.setattr("app.services.assetmap.service._NEO4J_BREAKER", fake_breaker())
    repo.graph_from_mysql = AsyncMock(return_value=([{"id": "metric:m1"}], []))
    out = await svc.get_graph(domain="sales", depth=3, pii_only=False)
    assert out["nodes"][0]["id"] == "metric:m1"
    repo.graph_from_mysql.assert_awaited_once_with("sales", False)


async def test_heatmap_passthrough() -> None:
    svc, repo = await _svc()
    repo.heatmap_aggregation = AsyncMock(
        return_value={"dimension": "domain", "buckets": [{"key": "sales", "total": 3}]}
    )
    out = await svc.get_heatmap(dimension="domain")
    assert out["buckets"][0]["total"] == 3
    repo.heatmap_aggregation.assert_awaited_once_with("domain")


async def test_owner_view_passthrough() -> None:
    svc, repo = await _svc()
    repo.owner_aggregation = AsyncMock(
        return_value={"owner_id": 9, "metrics": {"total": 4}, "catalogs": {"total": 7}}
    )
    out = await svc.get_owner_view(owner_id=9)
    assert out["metrics"]["total"] == 4
    repo.owner_aggregation.assert_awaited_once_with(9)


class _FakeRecord:
    def __init__(self, data: dict) -> None:
        self._data = data

    def __getitem__(self, key: str):
        return self._data[key]


class _FakeResult:
    def __init__(self, records: list) -> None:
        self._records = records

    def __aiter__(self):
        self._it = iter(self._records)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeSession:
    async def run(self, query: str, params: dict | None = None):
        if "RETURN n.id" in query:
            return _FakeResult(
                [
                    _FakeRecord(
                        {
                            "id": "metric:m1",
                            "type": "metric",
                            "label": "m1",
                            "pii": False,
                            "domain": "sales",
                            "owner": "1",
                        }
                    )
                ]
            )
        return _FakeResult(
            [_FakeRecord({"source": "a", "target": "b", "type": "DERIVED_FROM"})]
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeDriver:
    def session(self):
        return _FakeSession()


def _breaker(allow: bool) -> object:
    return type(
        "B",
        (),
        {
            "allow": lambda self: allow,
            "record_success": lambda self: None,
            "record_failure": lambda self: None,
        },
    )()


async def test_get_graph_neo4j_success(monkeypatch) -> None:
    """Neo4j 图读路径成功：Cypher 构建 + 节点/边迭代正确返回。"""
    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "neo4j_url", "bolt://localhost:7687")
    monkeypatch.setattr(cfg, "neo4j_password", "pw")
    monkeypatch.setattr("app.services.assetmap.service._get_neo4j_driver", lambda: _FakeDriver())
    monkeypatch.setattr("app.services.assetmap.service._NEO4J_BREAKER", _breaker(True))

    svc, _ = await _svc()
    out = await svc.get_graph(domain="sales", depth=3, pii_only=True)

    assert out["nodes"][0]["id"] == "metric:m1"
    assert out["nodes"][0]["label"] == "m1"
    assert out["edges"][0]["source"] == "a"
    assert out["edges"][0]["type"] == "DERIVED_FROM"


async def test_get_graph_neo4j_error_falls_back(monkeypatch) -> None:
    """Neo4j 查询异常：记失败并降级到 MySQL，不抛异常。"""
    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "neo4j_url", "bolt://localhost:7687")
    monkeypatch.setattr(cfg, "neo4j_password", "pw")

    def _boom():
        raise RuntimeError("conn refused")

    monkeypatch.setattr("app.services.assetmap.service._get_neo4j_driver", _boom)
    monkeypatch.setattr("app.services.assetmap.service._NEO4J_BREAKER", _breaker(True))

    svc, repo = await _svc()
    repo.graph_from_mysql = AsyncMock(return_value=([], []))
    out = await svc.get_graph(domain=None, depth=3, pii_only=False)

    assert out == {"nodes": [], "edges": []}
    repo.graph_from_mysql.assert_awaited_once()


def test_neo4j_driver_singleton_and_close(monkeypatch) -> None:
    """driver 惰性单例复用 + close 幂等（防每请求泄漏）。"""
    import app.services.assetmap.service as svc_mod
    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "neo4j_url", "bolt://localhost:7687")
    monkeypatch.setattr(cfg, "neo4j_password", "pw")
    monkeypatch.setattr(svc_mod, "_NEO4J_DRIVER", None)

    calls: list[str] = []

    def fake_driver(url: str, auth=None):
        calls.append(url)
        return type("D", (), {"close": lambda self: None})()

    monkeypatch.setattr("neo4j.AsyncGraphDatabase.driver", fake_driver)

    d1 = svc_mod._get_neo4j_driver()
    d2 = svc_mod._get_neo4j_driver()
    assert d1 is d2
    assert len(calls) == 1

    svc_mod._close_neo4j_driver()
    assert svc_mod._NEO4J_DRIVER is None
    # 幂等：再次 close 不抛
    svc_mod._close_neo4j_driver()
