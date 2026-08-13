"""特性开关管理 API 安全测试（OPS-09: 特性开关框架）。

覆盖：
① 匿名/非管理角色访问被 401/403 拦截（仅 platform_admin 可读写）；
② platform_admin 可列出/更新开关（200）；
③ 更新不存在的开关返回 404；
④ 关闭存量开关后 is_feature_enabled_or_default 返回 False（闸门生效）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app


def _session() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    return session


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
async def admin_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(1, "platform_admin"):
        yield c


@pytest.fixture
async def analyst_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(2, "analyst"):
        yield c


async def test_anonymous_401() -> None:
    """匿名访问被拒（401）。"""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/feature-flags")
    assert resp.status_code == 401


async def test_analyst_403(analyst_client: httpx.AsyncClient) -> None:
    """非管理角色（analyst）访问被拒（403）。"""
    resp = await analyst_client.get("/api/v1/feature-flags")
    assert resp.status_code == 403
    resp_put = await analyst_client.put(
        "/api/v1/feature-flags/quickbi", json={"enabled": False}
    )
    assert resp_put.status_code == 403


async def test_admin_list_200(admin_client: httpx.AsyncClient) -> None:
    """平台管理员可列出开关。"""
    resp = await admin_client.get("/api/v1/feature-flags")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "items" in data


async def test_admin_update_200(admin_client: httpx.AsyncClient) -> None:
    """平台管理员可更新开关（即时生效）。"""
    from app.core.feature_flags import get_feature_flag_manager

    manager = get_feature_flag_manager()
    manager.register_flag("flag_api_test", enabled=True)
    try:
        resp = await admin_client.put(
            "/api/v1/feature-flags/flag_api_test", json={"enabled": False}
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["enabled"] is False
        # 闸门生效：已注册且关闭 → is_feature_enabled_or_default 返回 False
        from app.core.feature_flags import is_feature_enabled_or_default

        assert is_feature_enabled_or_default("flag_api_test") is False
    finally:
        manager._flags.pop("flag_api_test", None)


async def test_admin_update_missing_404(admin_client: httpx.AsyncClient) -> None:
    """更新不存在的开关返回 404。"""
    resp = await admin_client.put(
        "/api/v1/feature-flags/nonexistent_flag", json={"enabled": True}
    )
    assert resp.status_code == 404
