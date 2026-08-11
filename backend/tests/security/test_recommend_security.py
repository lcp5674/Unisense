"""recommend 安全测试（对齐 gateways security_reverse，TD §13）。

recommend 为只读服务，所有已认证角色可读；安全边界体现在读端点
SQL 注入守卫与强类型输入校验。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.services.recommend.service import RecommendService


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
async def writer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(9, "metric_owner"):
        yield c


@pytest.fixture
async def reader_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_terms_blocks_sql_injection_400(writer_client: httpx.AsyncClient) -> None:
    """推荐读端点 SQL 注入被守卫拦截。"""
    resp = await writer_client.get("/api/v1/recommend/terms", params={"status": "' OR '1'='1"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


async def test_terms_returns_200_for_reader(
    reader_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已认证读者可正常读取推荐。"""

    async def fake(self: RecommendService, limit: int = 20) -> list:
        return []

    monkeypatch.setattr(RecommendService, "recommend_terms", fake)
    resp = await reader_client.get("/api/v1/recommend/terms")
    assert resp.status_code == 200


async def test_recommend_metrics_blocks_unauthorized_role_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAT-1: 不在 _READ_ROLES 中的角色被 RBAC 闸门拦截。"""

    async def fake_db():
        yield MagicMock()

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=99, role="guest", domain="sales"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/recommend/metrics")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_recommend_metrics_ignores_client_user_id(
    writer_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PLAT-2: 即使 client 传入 user_id=999，服务端也只查当前认证用户(9)。"""
    import app.services.recommend.service as rs

    captured: dict[str, int] = {}

    async def fake(self, user_id: int, limit: int = 20):
        captured["user_id"] = user_id
        return []

    monkeypatch.setattr(rs.RecommendService, "recommend_metrics", fake)
    resp = await writer_client.get("/api/v1/recommend/metrics", params={"user_id": 999})
    assert resp.status_code == 200
    assert captured["user_id"] == 9
