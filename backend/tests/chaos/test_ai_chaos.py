"""ai 混沌测试（对齐 gateways chaos，TD §15）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.core.exceptions import ExternalDependencyError
from app.main import app
from app.services.ai.service import AiService


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
async def viewer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_nl2sql_returns_503_on_dependency_failure(
    viewer_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(
        cls: AiService, nl_query: str, execute: bool = False, metric_scope: object = None
    ) -> object:
        raise ExternalDependencyError("语义库不可达")

    monkeypatch.setattr(AiService, "ask", boom)
    resp = await viewer_client.post(
        "/api/v1/ai/nl2sql",
        json={"nl_query": "查询上月销售额", "metric_scope": [], "execute": False},
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "EXTERNAL_DEPENDENCY_ERROR"
