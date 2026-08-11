"""assetmap 安全测试（对齐 gateways security_reverse，TD §13）。

assetmap 为只读服务，所有已认证角色可读，无写闸门；安全边界体现在
读端点的 SQL 注入守卫（纵深防御），且输入仅接受强类型参数。
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
async def writer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(9, "metric_owner"):
        yield c


@pytest.fixture
async def reader_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_summary_blocks_sql_injection_400(writer_client: httpx.AsyncClient) -> None:
    """资产地图读端点 SQL 注入被守卫拦截。"""
    resp = await writer_client.get("/api/v1/assetmap/summary", params={"status": "' OR '1'='1"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


async def test_summary_returns_200_for_reader(
    reader_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已认证读者可正常读取资产地图。"""

    async def fake(self: AssetMapService) -> dict:
        return {"total": 0, "by_domain": {}, "by_tier": {}, "by_layer": {}}

    monkeypatch.setattr(AssetMapService, "catalog_summary", fake)
    resp = await reader_client.get("/api/v1/assetmap/summary")
    assert resp.status_code == 200


async def test_tables_blocks_unauthorized_role_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAT-1: 不在 _READ_ROLES 中的角色被 RBAC 闸门拦截。"""
    from app.api import deps

    async def fake_db():
        yield MagicMock()

    app.dependency_overrides[deps.get_db_session] = fake_db
    # 角色 "guest" 不在 _READ_ROLES 白名单内
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=99, role="guest", domain="sales"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/assetmap/tables")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"
