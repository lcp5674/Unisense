"""资产地图服务单元测试（TD §12.11 / FR-18）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundError, UnisenseError
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
    repo.heatmap_aggregation = AsyncMock(return_value={})
    repo.health_summary = AsyncMock(return_value={"healthy": 0})
    # 写能力相关方法默认 AsyncMock（避免 MagicMock 无法 await / 无 assert_awaited）
    repo.get_catalog_entity = AsyncMock(return_value=None)
    repo.list_catalog_entities = AsyncMock(return_value=[])
    repo.user_exists = AsyncMock(return_value=False)
    repo.assign_owner = AsyncMock(return_value=None)
    repo.reclassify_sensitivity = AsyncMock(return_value=None)
    repo.batch_assign_owner = AsyncMock(return_value=0)
    repo.batch_reclassify = AsyncMock(return_value=0)
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


async def test_heatmap_rejects_invalid_dimension() -> None:
    """非法聚合维度必须显式拒绝，禁止静默回退到 domain 并以非法键缓存。"""
    svc, repo = await _svc()
    with pytest.raises(UnisenseError) as exc:
        await svc.get_heatmap(dimension="bogus")
    assert exc.value.error_code == "INVALID_HEATMAP_DIMENSION"
    repo.heatmap_aggregation.assert_not_awaited()


async def test_heatmap_matrix_passthrough() -> None:
    svc, repo = await _svc()
    repo.heatmap_matrix = AsyncMock(
        return_value={
            "cells": [{"domain": "sales", "sensitivity": "PII", "count": 3, "pii_count": 3}],
            "columns": ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "NEEDS_REVIEW"],
        }
    )
    out = await svc.heatmap_matrix()
    assert out["cells"][0]["count"] == 3
    assert out["columns"] == ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "NEEDS_REVIEW"]
    repo.heatmap_matrix.assert_awaited_once()


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
        return _FakeResult([_FakeRecord({"source": "a", "target": "b", "type": "DERIVED_FROM"})])

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

    def fake_driver(url: str, auth=None, **kwargs):
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


def test_neo4j_driver_sets_connection_timeouts(monkeypatch) -> None:
    """外部 Neo4j 调用必须设连接/获取超时，防止无响应时无限挂起（熔断不兜挂起）。"""
    import app.services.assetmap.service as svc_mod
    from app.core.config import settings as cfg

    monkeypatch.setattr(cfg, "neo4j_url", "bolt://localhost:7687")
    monkeypatch.setattr(cfg, "neo4j_password", "pw")
    monkeypatch.setattr(svc_mod, "_NEO4J_DRIVER", None)

    captured: dict = {}

    def fake_driver(url: str, auth=None, **kwargs):
        captured.update(kwargs)
        return type("D", (), {"close": lambda self: None})()

    monkeypatch.setattr("neo4j.AsyncGraphDatabase.driver", fake_driver)

    svc_mod._get_neo4j_driver()
    try:
        assert captured["connection_timeout"] == 5.0
        assert captured["connection_acquisition_timeout"] == 5.0
        assert captured["max_connection_pool_size"] == 10
    finally:
        svc_mod._close_neo4j_driver()


# ---- 产品补充（FR-18 生产化）：搜索 / 健康 / PII / 变更 / 我的资产 / 导出 ----


async def test_search_assets_passthrough() -> None:
    svc, repo = await _svc()
    repo.search_assets = AsyncMock(
        return_value=[{"type": "metric", "name": "sales_gmv_amount_day"}]
    )
    out = await svc.search_assets("sales", entity_type="metric", limit=20)
    assert out[0]["name"] == "sales_gmv_amount_day"
    repo.search_assets.assert_awaited_once_with("sales", "metric", 20)


async def test_health_summary_passthrough() -> None:
    svc, repo = await _svc()
    repo.health_summary = AsyncMock(return_value={"orphan_assets": 3})
    out = await svc.health_summary()
    assert out["orphan_assets"] == 3
    repo.health_summary.assert_awaited_once()


async def test_pii_overview_passthrough() -> None:
    svc, repo = await _svc()
    repo.pii_overview = AsyncMock(return_value={"pii_metric_count": 5})
    out = await svc.pii_overview()
    assert out["pii_metric_count"] == 5
    repo.pii_overview.assert_awaited_once()


async def test_recent_changes_passthrough() -> None:
    svc, repo = await _svc()
    repo.recent_changes = AsyncMock(return_value={"catalogs": [], "metrics": [], "days": 7})
    out = await svc.recent_changes(days=7, limit=50)
    assert out["days"] == 7
    repo.recent_changes.assert_awaited_once_with(7, 50)


async def test_my_assets_passthrough() -> None:
    svc, repo = await _svc()
    repo.my_assets = AsyncMock(return_value={"owner_id": 9, "catalogs": [], "metrics": []})
    out = await svc.my_assets(owner_id=9, limit=50)
    assert out["owner_id"] == 9
    repo.my_assets.assert_awaited_once_with(9, 50)


async def test_export_tables_builds_rows() -> None:
    """导出：透传 list_tables 并返回 to_dict 列表（敏感字段剥离）。"""
    svc, repo = await _svc()
    repo.list_tables = AsyncMock(
        return_value=[
            DBCatalog(source_id="s", entity_name="t", entity_type="table", schema_json={})
        ]
    )
    items = await svc.export_tables(None, None)
    assert len(items) == 1
    assert items[0]["entity_name"] == "t"
    # to_dict 剥离 schema_json（敏感字段黑名单）
    assert "schema_json" not in items[0]
    repo.list_tables.assert_awaited_once()


# ---- 写能力（FR-18 资产工作台）：认领/转让、重分类、批量 ----


async def test_assign_owner_success() -> None:
    """认领/转让归属：实体存在 + 用户存在 → 更新并返回。"""
    svc, repo = await _svc()
    entity = DBCatalog(id=1, entity_name="catalog.sales.orders", entity_type="table")
    repo.get_catalog_entity = AsyncMock(return_value=entity)
    repo.user_exists = AsyncMock(return_value=True)
    repo.assign_owner = AsyncMock(
        return_value=DBCatalog(id=1, entity_name="catalog.sales.orders", owner_id=9)
    )
    out = await svc.assign_owner(1, owner_id=9)
    assert out["entity_id"] == 1
    assert out["owner_id"] == 9
    repo.assign_owner.assert_awaited_once_with(entity, 9)


async def test_assign_owner_release() -> None:
    """解除归属：owner_id=None 不校验用户，直接清除。"""
    svc, repo = await _svc()
    entity = DBCatalog(id=1, entity_name="catalog.sales.orders")
    repo.get_catalog_entity = AsyncMock(return_value=entity)
    repo.assign_owner = AsyncMock(return_value=DBCatalog(id=1, owner_id=None))
    out = await svc.assign_owner(1, owner_id=None)
    assert out["owner_id"] is None
    repo.user_exists.assert_not_awaited()
    repo.assign_owner.assert_awaited_once_with(entity, None)


async def test_assign_owner_entity_missing_raises() -> None:
    """认领不存在的资产 → NotFoundError。"""
    svc, repo = await _svc()
    repo.get_catalog_entity = AsyncMock(return_value=None)
    try:
        await svc.assign_owner(999, owner_id=1)
    except NotFoundError:
        pass
    else:  # pragma: no cover - 断言异常必抛
        raise AssertionError("应抛 NotFoundError")
    repo.user_exists.assert_not_awaited()


async def test_assign_owner_user_missing_raises() -> None:
    """认领到不存在的用户 → NotFoundError（防孤儿归属脏数据）。"""
    svc, repo = await _svc()
    repo.get_catalog_entity = AsyncMock(
        return_value=DBCatalog(id=1, entity_name="catalog.sales.orders")
    )
    repo.user_exists = AsyncMock(return_value=False)
    try:
        await svc.assign_owner(1, owner_id=999)
    except NotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("应抛 NotFoundError")


async def test_reclassify_sensitivity_success() -> None:
    """重分类敏感级：实体存在 → 更新并返回新级别。"""
    svc, repo = await _svc()
    repo.get_catalog_entity = AsyncMock(
        return_value=DBCatalog(id=1, entity_name="catalog.sales.orders")
    )
    repo.reclassify_sensitivity = AsyncMock(
        return_value=DBCatalog(id=1, entity_name="catalog.sales.orders", sensitivity_level="PII")
    )
    out = await svc.reclassify_sensitivity(1, "PII")
    assert out["sensitivity_level"] == "PII"
    repo.reclassify_sensitivity.assert_awaited_once()


async def test_reclassify_sensitivity_missing_raises() -> None:
    """重分类不存在的资产 → NotFoundError。"""
    svc, repo = await _svc()
    repo.get_catalog_entity = AsyncMock(return_value=None)
    try:
        await svc.reclassify_sensitivity(999, "PII")
    except NotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("应抛 NotFoundError")


async def test_batch_assign_owner_success() -> None:
    """批量认领：实体存在 → 同事务更新并返回影响数。"""
    svc, repo = await _svc()
    entities = [
        DBCatalog(id=1, entity_name="catalog.sales.orders"),
        DBCatalog(id=2, entity_name="catalog.sales.items"),
    ]
    repo.user_exists = AsyncMock(return_value=True)
    repo.list_catalog_entities = AsyncMock(return_value=entities)
    repo.batch_assign_owner = AsyncMock(return_value=2)
    out = await svc.batch_assign_owner([1, 2], owner_id=9)
    assert out["affected"] == 2
    assert out["total"] == 2
    repo.batch_assign_owner.assert_awaited_once_with(entities, 9)


async def test_batch_assign_owner_empty_raises() -> None:
    """批量认领：指定实体均不存在 → NotFoundError。"""
    svc, repo = await _svc()
    repo.list_catalog_entities = AsyncMock(return_value=[])
    try:
        await svc.batch_assign_owner([1, 2], owner_id=9)
    except NotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("应抛 NotFoundError")


async def test_batch_reclassify_success() -> None:
    """批量重分类：返回影响数与目标级别。"""
    svc, repo = await _svc()
    entities = [DBCatalog(id=1, entity_name="catalog.sales.orders")]
    repo.list_catalog_entities = AsyncMock(return_value=entities)
    repo.batch_reclassify = AsyncMock(return_value=1)
    out = await svc.batch_reclassify([1], "CONFIDENTIAL")
    assert out["affected"] == 1
    assert out["sensitivity_level"] == "CONFIDENTIAL"
    repo.batch_reclassify.assert_awaited_once_with(entities, "CONFIDENTIAL")


async def test_batch_reclassify_empty_raises() -> None:
    """批量重分类：指定实体均不存在 → NotFoundError。"""
    svc, repo = await _svc()
    repo.list_catalog_entities = AsyncMock(return_value=[])
    try:
        await svc.batch_reclassify([1], "PII")
    except NotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("应抛 NotFoundError")


# ---- 聚合缓存（大规模优化）：cache-aside + 熔断降级 ----


async def _fake_redis(monkeypatch, *, get_value=None, get_side_effect=None) -> MagicMock:
    import app.db.redis as redis_mod

    fake = MagicMock()
    fake.get = AsyncMock(
        return_value=get_value,
        side_effect=get_side_effect,
    )
    fake.set = AsyncMock()
    monkeypatch.setattr(redis_mod, "get_redis", lambda: fake)
    return fake


async def test_agg_cached_hit_skips_loader(monkeypatch) -> None:
    """缓存命中：不调用 loader，直接返回缓存数据。"""
    import app.services.assetmap.service as svc_mod

    monkeypatch.setattr(svc_mod, "_CACHE_BREAKER", _breaker(True))
    await _fake_redis(
        monkeypatch,
        get_value='{"total": 3, "by_entity_type": {}}',
    )
    loader = AsyncMock(return_value={"total": 99})
    out = await svc_mod._agg_cached("catalog_summary", loader)
    assert out["total"] == 3
    loader.assert_not_awaited()


async def test_agg_cached_miss_loads_and_sets(monkeypatch) -> None:
    """缓存未命中：调 loader 回源，并写缓存（TTL 30s）。"""
    import app.services.assetmap.service as svc_mod

    monkeypatch.setattr(svc_mod, "_CACHE_BREAKER", _breaker(True))
    fake = await _fake_redis(monkeypatch, get_value=None)
    loader = AsyncMock(return_value={"total": 5})
    out = await svc_mod._agg_cached("catalog_summary", loader)
    assert out["total"] == 5
    loader.assert_awaited_once()
    fake.set.assert_awaited_once()
    # key 带前缀；TTL=30
    key = fake.set.await_args.args[0]
    assert key == "assetmap:agg:catalog_summary"
    assert fake.set.await_args.kwargs["ex"] == 30


async def test_agg_cached_redis_down_falls_back(monkeypatch) -> None:
    """Redis 不可用：记熔断失败并回源，不抛异常、不阻塞主链路。"""
    import app.services.assetmap.service as svc_mod

    monkeypatch.setattr(svc_mod, "_CACHE_BREAKER", _breaker(True))
    await _fake_redis(monkeypatch, get_side_effect=ConnectionError("redis down"))
    loader = AsyncMock(return_value={"total": 7})
    out = await svc_mod._agg_cached("catalog_summary", loader)
    assert out["total"] == 7
    loader.assert_awaited_once()


async def test_agg_cached_breaker_open_skips_cache(monkeypatch) -> None:
    """熔断打开：跳过缓存读写，直接回源（防雪崩）。"""
    import app.services.assetmap.service as svc_mod
    from app.db import redis as redis_mod

    monkeypatch.setattr(svc_mod, "_CACHE_BREAKER", _breaker(False))
    spy = MagicMock()
    monkeypatch.setattr(redis_mod, "get_redis", spy)
    loader = AsyncMock(return_value={"total": 11})
    out = await svc_mod._agg_cached("catalog_summary", loader)
    assert out["total"] == 11
    spy.assert_not_called()  # 熔断打开时完全不触碰 Redis
    loader.assert_awaited_once()


async def test_agg_cached_bad_json_falls_back(monkeypatch) -> None:
    """缓存坏数据（非 JSON）：回源且不抛异常。"""
    import app.services.assetmap.service as svc_mod

    monkeypatch.setattr(svc_mod, "_CACHE_BREAKER", _breaker(True))
    await _fake_redis(monkeypatch, get_value="not-json{")
    loader = AsyncMock(return_value={"total": 13})
    out = await svc_mod._agg_cached("catalog_summary", loader)
    assert out["total"] == 13
    loader.assert_awaited_once()
