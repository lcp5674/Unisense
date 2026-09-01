"""notify 可观测测试（对齐 gateways observability，TD §14）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.api import notify as notify_api
from app.main import app
from app.services.notify.service import NotifyService


@pytest.fixture
def audit_sink(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []

    async def fake_write_audit(db: object, **kwargs: object) -> None:
        records.append(kwargs)

    monkeypatch.setattr(notify_api, "write_audit", fake_write_audit)
    return records


def _session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    s.flush = AsyncMock()
    s.refresh = AsyncMock()
    s.execute = AsyncMock()
    return s


async def _client(uid: int, role: str) -> AsyncIterator[httpx.AsyncClient]:
    session = _session()

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=uid,
        role=role,
        domain="sales",
        roles_all=lambda: [role],
        has_role=lambda r: r == role,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


_EVENT_BODY = {
    "event_type": "metric.anomaly",
    "source": "quality",
    "payload": {"metric_id": "M1"},
    "level": "INFO",
}


@pytest.fixture
async def writer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(9, "metric_owner"):
        yield c


@pytest.fixture
async def reader_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_publish_event_writes_audit_record(
    writer_client: httpx.AsyncClient,
    audit_sink: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_publish(self: NotifyService, payload: object) -> object:
        return {"event_id": 1, "notifications": []}

    monkeypatch.setattr(NotifyService, "publish_event", fake_publish)
    resp = await writer_client.post("/api/v1/notify/events", json=_EVENT_BODY)
    assert resp.status_code == 201
    assert len(audit_sink) == 1
    record = audit_sink[0]
    assert record["action"] == "notification.publish"
    assert record["entity_type"] == "event_log"
    assert record["actor_id"] == 9
    assert record["trace_id"]


async def test_response_contains_trace_id(
    reader_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list_page(self: NotifyService, *args: object, **kwargs: object) -> object:
        return [], 0

    # API 层调用的是 list_notifications_page（此前误 mock 不存在的 list_notifications）
    monkeypatch.setattr(NotifyService, "list_notifications_page", fake_list_page)
    resp = await reader_client.get("/api/v1/notify/notifications", params={"subscriber_id": 11})
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
