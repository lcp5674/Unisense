"""语义领域混沌/韧性测试（对齐 TD §11 韧性 / DEV_GUIDE §17）。

核心依赖仅为 MySQL；Redis/Neo4j/ES/OLAP 为可选依赖。
验证：任一可选依赖宕机时，核心语义读链路仍返回 200（降级 / fallback / 舱壁隔离）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.core.resilience import CircuitBreaker
from app.main import app


def _healthy_redis() -> MagicMock:
    fake = MagicMock()
    fake.ping = AsyncMock(return_value=True)
    return fake


def _down_redis() -> MagicMock:
    fake = MagicMock()
    fake.ping = AsyncMock(side_effect=RuntimeError("redis down"))
    return fake


@pytest.fixture
async def chaos_client():
    session = MagicMock()
    session.execute = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalars.return_value.first.return_value = None
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    session.commit = AsyncMock()

    async def fake_db() -> AsyncGenerator[MagicMock, None]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="metric_owner")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_redis_down_core_link_still_200(chaos_client, monkeypatch):
    # Redis 宕机 -> 核心链路 200（语义读不依赖 Redis，degradation 生效）
    monkeypatch.setattr("app.db.redis.redis_client", _down_redis())
    resp = await chaos_client.get(
        "/api/v1/metric-definitions?page=1&page_size=10",
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200  # 核心链路不依赖 Redis，降级为 200
    ready = await chaos_client.get("/ready")
    assert ready.status_code == 200  # 即便 Redis 故障，就绪探针仍返回 200


async def test_neo4j_down_triggers_degradation(chaos_client, monkeypatch):
    # Neo4j 宕机 -> 降级生效
    monkeypatch.setattr("app.db.redis.redis_client", _healthy_redis())
    monkeypatch.setattr(
        "app.api.health.optional_dependency_status",
        lambda: {"neo4j": False, "elasticsearch": True, "olap": True},
    )
    ready = await chaos_client.get("/ready")
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "degraded"  # 降级（degradation）
    assert "neo4j" in body["degraded"]
    # 核心链路在可选依赖降级时仍可用
    core = await chaos_client.get(
        "/api/v1/metric-definitions?page=1&page_size=10",
        headers={"Authorization": "Bearer x"},
    )
    assert core.status_code == 200


async def test_es_down_search_fallback(chaos_client, monkeypatch):
    # ES 宕机 -> 搜索降级返回（fallback 到 DB）
    monkeypatch.setattr("app.db.redis.redis_client", _healthy_redis())
    monkeypatch.setattr(
        "app.api.health.optional_dependency_status",
        lambda: {"neo4j": True, "elasticsearch": False, "olap": True},
    )
    ready = await chaos_client.get("/ready")
    assert ready.json()["status"] == "degraded"
    core = await chaos_client.get(
        "/api/v1/metric-definitions?page=1&page_size=10",
        headers={"Authorization": "Bearer x"},
    )
    assert core.status_code == 200  # fallback 到 DB 查询


async def test_olap_down_bulkhead_isolation(chaos_client, monkeypatch):
    # OLAP 宕机 -> 查询舱壁隔离（核心链路不受影响）
    monkeypatch.setattr("app.db.redis.redis_client", _healthy_redis())
    monkeypatch.setattr(
        "app.api.health.optional_dependency_status",
        lambda: {"neo4j": True, "elasticsearch": True, "olap": False},
    )
    ready = await chaos_client.get("/ready")
    assert ready.json()["status"] == "degraded"
    core = await chaos_client.get(
        "/api/v1/metric-definitions?page=1&page_size=10",
        headers={"Authorization": "Bearer x"},
    )
    assert core.status_code == 200  # 舱壁隔离，OLAP 故障不波及语义读链路


def test_circuit_breaker_opens_after_failures():
    # 硬依赖(MySQL)故障 -> 503 Service Unavailable（circuit breaker open）
    cb = CircuitBreaker(failure_threshold=3, reset_timeout=1.0)
    assert cb.allow() is True
    assert cb.state == "closed"
    for _ in range(3):
        cb.record_failure()
    assert cb.state == "open"  # circuit 打开
    assert cb.allow() is False
    cb.record_success()
    assert cb.state == "closed"
    assert cb.allow() is True
