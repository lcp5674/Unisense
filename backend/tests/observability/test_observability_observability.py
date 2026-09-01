"""observability 可观测测试（对齐 gateways observability，TD §14）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.api import observability as observability_api
from app.main import app
from app.services.observability.service import ObservabilityService


@pytest.fixture
def audit_sink(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []

    async def fake_write_audit(db: object, **kwargs: object) -> None:
        records.append(kwargs)

    monkeypatch.setattr(observability_api, "write_audit", fake_write_audit)
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


_FEEDBACK_BODY = {
    "user_id": 11,
    "target_type": "metric",
    "target_id": "M1",
    "rating": 5,
    "comment": "good",
}


@pytest.fixture
async def viewer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


@pytest.fixture
async def reader_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_submit_feedback_writes_audit_record(
    viewer_client: httpx.AsyncClient,
    audit_sink: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_submit(
        self: ObservabilityService, payload: object, actor_id: int | None = None
    ) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            id=1,
            user_id=actor_id or 11,
            target_type="metric",
            target_id=None,
            rating=5,
            comment="good",
            created_at=None,
        )

    monkeypatch.setattr(ObservabilityService, "submit_feedback", fake_submit)
    resp = await viewer_client.post("/api/v1/observability/feedback", json=_FEEDBACK_BODY)
    assert resp.status_code == 201
    assert len(audit_sink) == 1
    record = audit_sink[0]
    assert record["action"] == "feedback.submit"
    assert record["entity_type"] == "feedback"
    assert record["actor_id"] == 11
    assert record["trace_id"]


async def test_response_contains_trace_id(
    reader_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list(self: ObservabilityService, *args: object, **kwargs: object) -> object:
        # 与 service.list_feedback 返回结构对齐（items/total/page/page_size/target_names）
        return {"items": [], "total": 0, "page": 1, "page_size": 20, "target_names": {}}

    monkeypatch.setattr(ObservabilityService, "list_feedback", fake_list)
    resp = await reader_client.get("/api/v1/observability/feedback")
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
