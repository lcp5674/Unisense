"""健康检查与就绪探针单测（补齐覆盖率）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_ready_db_and_redis_ok(client: httpx.AsyncClient) -> None:
    db = MagicMock()
    db.execute = AsyncMock()
    redis = MagicMock()
    redis.ping = AsyncMock()

    async def fake_db():
        yield db

    async def fake_redis():
        yield redis

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_redis] = fake_redis
    try:
        resp = await client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in ("ok", "degraded")
        assert body["checks"]["db"] == "ok"
        assert body["checks"]["redis"] == "ok"
    finally:
        app.dependency_overrides.clear()


async def test_ready_db_failure(client: httpx.AsyncClient) -> None:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=Exception("db down"))

    async def fake_db():
        yield db

    app.dependency_overrides[deps.get_db_session] = fake_db
    # Redis 依赖：模拟可用，聚焦 DB 故障路径
    app.dependency_overrides[deps.get_redis] = lambda: MagicMock()
    try:
        resp = await client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "unavailable"
        assert resp.json()["checks"]["db"] == "fail"
    finally:
        app.dependency_overrides.clear()


async def test_ready_redis_none_skips(client: httpx.AsyncClient) -> None:
    db = MagicMock()
    db.execute = AsyncMock()

    async def fake_db():
        yield db

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_redis] = lambda: None
    try:
        resp = await client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["checks"]["redis"] == "skip"
    finally:
        app.dependency_overrides.clear()


async def test_metrics_endpoint(client: httpx.AsyncClient) -> None:
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    try:
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "http_requests_total" in resp.text
    finally:
        app.dependency_overrides.clear()


async def test_metrics_endpoint_requires_auth(client: httpx.AsyncClient) -> None:
    """运营端点（/metrics）需鉴权：匿名访问应 401（P0 缺口修复）。"""
    resp = await client.get("/metrics")
    assert resp.status_code == 401


async def test_degraded_overview(client: httpx.AsyncClient) -> None:
    """/health/degraded 返回统一降级面板摘要（OPS-05，需管理角色）。"""
    from app.core.degradation_registry import init_degradation_registry

    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    registry = init_degradation_registry()
    registry.register_degradation("redis", "probe failed")
    try:
        resp = await client.get("/health/degraded")
        assert resp.status_code == 200
        body = resp.json()
        assert body["overall_status"] == "degraded"
        assert body["degraded_count"] == 1
        assert body["degraded_components"][0]["component"] == "redis"
    finally:
        app.dependency_overrides.clear()
        init_degradation_registry()


async def test_ready_registers_degradation_in_registry(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    """可选依赖降级时 /ready 同步写入降级注册中心（OPS-05）。"""
    from app.core.degradation_registry import init_degradation_registry

    init_degradation_registry()
    db = MagicMock()
    db.execute = AsyncMock()
    redis = MagicMock()
    redis.ping = AsyncMock()

    async def fake_db():
        yield db

    async def fake_redis():
        yield redis

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_redis] = fake_redis

    async def _fake_status():
        return {"neo4j": False, "olap": True}

    monkeypatch.setattr("app.api.health.optional_dependency_status", _fake_status)
    try:
        resp = await client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "degraded"
        from app.core.degradation_registry import get_degradation_registry

        registry = get_degradation_registry()
        assert registry.is_degraded("neo4j") is True
    finally:
        app.dependency_overrides.clear()
        init_degradation_registry()
