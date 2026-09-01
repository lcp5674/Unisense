"""ai 可观测测试（对齐 gateways observability，TD §14）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import ai as ai_api
from app.api import deps
from app.main import app
from app.services.ai.service import AiService


@pytest.fixture
def audit_sink(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []

    async def fake_write_audit(db: object, **kwargs: object) -> None:
        records.append(kwargs)

    monkeypatch.setattr(ai_api, "write_audit", fake_write_audit)
    return records


def _session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
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


@pytest.fixture
async def viewer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_nl2sql_writes_audit_and_trace(
    viewer_client: httpx.AsyncClient,
    audit_sink: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ask(
        cls: AiService, nl_query: str, execute: bool = False, metric_scope: object = None
    ) -> dict:
        return {
            "anchored": [],
            "sql": "SELECT 1",
            "safe": True,
            "notes": [],
            "execute": False,
        }

    monkeypatch.setattr(AiService, "ask", fake_ask)
    resp = await viewer_client.post(
        "/api/v1/ai/nl2sql",
        json={"nl_query": "查询上月销售额", "metric_scope": [], "execute": False},
    )
    assert resp.status_code == 200
    assert len(audit_sink) == 1
    record = audit_sink[0]
    assert record["action"] == "ai.nl2sql"
    assert record["entity_type"] == "nl_query"
    assert record["actor_id"] == 11
    assert record["trace_id"]
    assert resp.json()["trace_id"] == resp.headers.get("X-Trace-Id")
