"""冲突领域可观测测试（对齐 gateways observability）。

覆盖：响应含 trace_id（全链路透传）；/metrics 暴露 RED 指标（prometheus 文本）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app


@pytest.fixture
async def analyst_client():
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=12, role="analyst")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_conflict_response_contains_trace_id(analyst_client):
    resp = await analyst_client.get("/api/v1/conflicts", params={"page": 1, "page_size": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert "trace_id" in body
    assert body["trace_id"]


async def test_metrics_endpoint_exposes_red():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
