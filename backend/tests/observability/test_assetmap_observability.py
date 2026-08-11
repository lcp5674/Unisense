"""assetmap 可观测测试（对齐 gateways observability，TD §14）。

只读服务无写审计；覆盖读端点 trace_id 透传与 /metrics RED 指标暴露。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.services.assetmap.service import AssetMapService


def _session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.commit = MagicMock()
    s.rollback = MagicMock()
    s.flush = MagicMock()
    s.refresh = MagicMock()
    s.execute = MagicMock()
    return s


async def _client(uid: int, role: str) -> AsyncIterator[httpx.AsyncClient]:
    session = _session()

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=uid, role=role, domain="sales"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def reader_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_response_contains_trace_id(
    reader_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(self: AssetMapService) -> dict:
        return {"total": 0, "by_domain": {}, "by_tier": {}, "by_layer": {}}

    monkeypatch.setattr(AssetMapService, "catalog_summary", fake)
    resp = await reader_client.get("/api/v1/assetmap/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"]
    assert resp.headers.get("X-Trace-Id")
    assert body["trace_id"] == resp.headers["X-Trace-Id"]


async def test_metrics_endpoint_exposes_red() -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
