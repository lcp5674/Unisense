"""语义领域读缓存 + 熔断舱壁测试（补实「CircuitBreaker 接入实时请求路径」）。

验证：
1. 缓存 set/get/invalidate 正常往返；
2. 缓存禁用（redis=None）时直接走 DB，不报错；
3. Redis 宕机 -> 熔断打开 -> get 直接降级（不再触碰 Redis）；
4. 服务层 get_metric_public：缓存不可用时回源 DB；缓存命中时跳过 DB；
5. 真实 HTTP 路径：Redis 宕机经熔断降级到 DB，详情接口仍 200。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
from httpx import ASGITransport

from app.api import deps
from app.core.resilience import CircuitBreaker
from app.main import app
from app.models.metric import Metric
from app.services.semantic.cache import MetricCache
from app.services.semantic.repository import MetricRepository
from app.services.semantic.service import MetricService


class _FakeRedis:
    """内存版 fake Redis（异步），用于验证缓存往返。"""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str):
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._store[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self._store.pop(key, None) is not None else 0


class _DownRedis:
    """始终抛错的 fake Redis，用于验证熔断降级。"""

    async def get(self, key: str):
        raise ConnectionError("redis down")

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        raise ConnectionError("redis down")

    async def delete(self, key: str) -> None:
        raise ConnectionError("redis down")


def _make_metric(code: str = "m1", pii: bool = False) -> Metric:
    now = datetime(2026, 1, 1, 0, 0, 0)
    return Metric(
        id=1,
        metric_code=code,
        name="测试指标",
        domain="finance",
        type="atomic",
        granularity="day",
        unit="次",
        aggregation="SUM",
        time_semantics="PERIOD",
        freshness="T1",
        dw_layer="ADS",
        metric_tier="T3",
        serving_mode="BATCH_ONLY",
        additivity="ADDITIVE",
        non_additive_dimensions=None,
        definition_json={"expression": "sum(x)"},
        version=1,
        row_version=1,
        status="DRAFT",
        owner_id=1,
        pii_flag=pii,
        compliance_reviewed=False,
        effective_version=None,
        consumption_guide=None,
        successor_code=None,
        deprecated_at=None,
        sunset_until=None,
        created_at=now,
        updated_at=now,
    )


async def test_cache_roundtrip_and_invalidate():
    cache = MetricCache(_FakeRedis())
    metric = _make_metric()
    await cache.set(metric)
    got = await cache.get(metric.metric_code)
    assert got is not None
    assert got["metric_code"] == metric.metric_code
    # 写后失效（版本缓存失效延迟 < 1s）
    await cache.invalidate(metric.metric_code)
    assert await cache.get(metric.metric_code) is None


async def test_cache_disabled_returns_none():
    cache = MetricCache(None)
    assert await cache.get("x") is None
    # 禁用时 set / invalidate 不抛错、不阻断主流程
    await cache.set(_make_metric())
    await cache.invalidate("x")


async def test_cache_degrades_via_circuit_breaker_on_redis_down():
    cache = MetricCache(_DownRedis())
    for _ in range(5):
        await cache.get("x")
    assert cache._breaker.state == "open"  # 连续失败 -> 熔断打开
    # 熔断打开后 get 直接降级，不再触碰 Redis
    assert await cache.get("x") is None


async def test_get_metric_public_falls_back_to_db_when_cache_down():
    # 缓存不可用 -> 回源 MySQL（舱壁隔离，核心链路不受影响）
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=1.0)
    cache = MetricCache(_DownRedis(), breaker=breaker)
    repo = MagicMock()
    repo.get_by_code = AsyncMock(return_value=_make_metric())
    service = MetricService(db=MagicMock(), cache=cache)
    service._repo = repo
    resp = await service.get_metric_public("m1")
    assert resp.metric_code == "m1"
    assert repo.get_by_code.await_count >= 1  # 缓存失效 -> 回源 DB


async def test_get_metric_public_cache_hit_skips_db():
    # 缓存命中 -> 跳过 DB（降低 MySQL 压力，证明缓存真实生效）
    cache = MetricCache(_FakeRedis())
    metric = _make_metric()
    await cache.set(metric)
    repo = MagicMock()
    repo.get_by_code = AsyncMock(return_value=metric)
    service = MetricService(db=MagicMock(), cache=cache)
    service._repo = repo
    resp = await service.get_metric_public("m1")
    assert resp.metric_code == "m1"
    assert repo.get_by_code.await_count == 0  # 命中缓存，未查 DB


async def test_get_metric_detail_degrades_via_breaker_when_redis_down(monkeypatch):
    # 真实 HTTP 路径：Redis 宕机经熔断降级到 DB，详情接口仍 200
    monkeypatch.setattr("app.db.redis.redis_client", _DownRedis())

    async def fake_get_by_code(self, code: str) -> Metric:
        return _make_metric(code=code, pii=True)

    monkeypatch.setattr(MetricRepository, "get_by_code", fake_get_by_code)

    session: MagicMock = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()

    async def fake_db() -> AsyncGenerator[MagicMock, None]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="metric_owner")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(
            "/api/v1/metric-definitions/m1",
            headers={"Authorization": "Bearer x"},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200  # 降级到 DB，详情仍可用
    assert resp.json()["data"]["metric_code"] == "m1"
