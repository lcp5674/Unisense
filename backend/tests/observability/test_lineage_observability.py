"""血缘领域可观测测试（对齐 gateways observability）。

覆盖：响应含 trace_id（全链路透传）；/metrics 暴露 RED 指标（prometheus 文本）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.api import lineage as lineage_api
from app.main import app


@pytest.fixture
async def owner_client():
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=5, role="metric_owner")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class _FakeLineageSvc:
    def __init__(self, db: object, **kw: object) -> None:
        pass

    async def query_impact(self, params: object) -> list:
        return []

    async def list_edges(self, node: object, direction: object) -> list:
        return []

    async def node_meta(self, node_ids: object) -> list:
        return []


async def test_lineage_response_contains_trace_id(owner_client, monkeypatch):
    monkeypatch.setattr(lineage_api, "LineageService", _FakeLineageSvc)
    resp = await owner_client.get(
        "/api/v1/lineage/impact",
        params={"node": "db.schema.t"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "trace_id" in body, "响应必须携带 trace_id（全链路透传）"
    assert body["trace_id"]


async def test_metrics_endpoint_exposes_red():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text  # RED 指标（prometheus 文本）
