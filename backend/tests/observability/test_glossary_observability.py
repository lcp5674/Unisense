"""glossary 可观测测试（对齐 gateways observability，TD §14）。

覆盖：
① 写操作落审计（action/entity_type/actor/trace_id 正确）；
② 读端点响应体与响应头均带 trace_id（全链路透传）；
③ /metrics 暴露 RED 指标。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.api import glossary as glossary_api
from app.main import app
from app.services.glossary.service import GlossaryService


@pytest.fixture
def audit_sink(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []

    async def fake_write_audit(db: object, **kwargs: object) -> None:
        records.append(kwargs)

    monkeypatch.setattr(glossary_api, "write_audit", fake_write_audit)
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


_TERM_BODY = {
    "term_code": "T_GROSS_PROFIT",
    "name": "毛利",
    "definition": "收入减去成本",
    "domain": "finance",
    "owner_id": 9,
}


@pytest.fixture
async def writer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(9, "metric_owner"):
        yield c


@pytest.fixture
async def reader_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_create_term_writes_audit_record(
    writer_client: httpx.AsyncClient,
    audit_sink: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """术语创建写操作必须落审计。"""

    async def fake_create(self: GlossaryService, payload: object, user_id: int) -> object:
        m = MagicMock()
        m.code = payload.term_code  # type: ignore[attr-defined]
        return m

    monkeypatch.setattr(GlossaryService, "create_term", fake_create)
    resp = await writer_client.post("/api/v1/terms", json=_TERM_BODY)
    assert resp.status_code == 201
    assert len(audit_sink) == 1
    record = audit_sink[0]
    assert record["action"] == "term.create"
    assert record["entity_type"] == "term"
    assert record["actor_id"] == 9
    assert record["trace_id"]


async def test_response_contains_trace_id(
    reader_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读端点响应体与头均透传 trace_id。"""

    async def fake_list(self: GlossaryService, *args: object, **kwargs: object) -> object:
        return ([], 0)

    monkeypatch.setattr(GlossaryService, "list_terms", fake_list)
    resp = await reader_client.get("/api/v1/terms")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"]
    assert resp.headers.get("X-Trace-Id")
    assert body["trace_id"] == resp.headers["X-Trace-Id"]


async def test_metrics_endpoint_exposes_red() -> None:
    """/metrics 暴露 RED 指标。"""
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/metrics")
    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
