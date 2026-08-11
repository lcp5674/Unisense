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
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
