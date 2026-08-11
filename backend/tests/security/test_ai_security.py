"""ai 安全测试（对齐 gateways security_reverse，TD §13）。

ai 问数写接口对所有已认证角色开放，安全边界在于对危险 SQL 语义的
拦截（禁止 DDL/DML，防止通过自然语言越权改写数据）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app


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


async def test_nl2sql_rejects_dangerous_query(viewer_client: httpx.AsyncClient) -> None:
    """自然语言中包含 DDL/DML 危险语义必须被拒绝。"""
    resp = await viewer_client.post(
        "/api/v1/ai/nl2sql",
        json={"nl_query": "DROP TABLE users", "metric_scope": [], "execute": False},
    )
    assert resp.status_code != 200
    assert resp.json()["code"] == "UNSAFE_QUERY"
