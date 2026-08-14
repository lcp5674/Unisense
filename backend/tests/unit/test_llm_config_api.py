"""LLM 配置 API 测试（GET/PUT /ai/config + POST /ai/config/test）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app


def _make_session() -> MagicMock:
    """构造 mock 会话：execute 返回可配置的 Result mock。"""
    session = MagicMock()
    session.flush = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.fixture
async def llm_client() -> AsyncIterator[httpx.AsyncClient]:
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="platform_admin")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_get_config_default(llm_client: httpx.AsyncClient) -> None:
    with patch("app.services.llm.config_service.settings") as ms:
        ms.llm_base_url = ""
        ms.llm_api_key = ""
        resp = await llm_client.get("/api/v1/ai/config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["source"] == "none"
    assert data["has_api_key"] is False
    assert data["can_edit"] is True  # platform_admin 可编辑


async def test_get_config_can_edit_false_for_viewer() -> None:
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="viewer")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/ai/config")
    data = resp.json()["data"]
    assert data["can_edit"] is False
    app.dependency_overrides.clear()


async def test_put_config_saves(llm_client: httpx.AsyncClient) -> None:
    resp = await llm_client.put(
        "/api/v1/ai/config",
        json={
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": "sk-test",
            "timeout": 30,
            "enabled": True,
        },
    )
    assert resp.status_code == 200
    assert "id" in resp.json()["data"]


async def test_put_config_rejected_for_viewer() -> None:
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="viewer")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.put(
            "/api/v1/ai/config",
            json={"provider": "deepseek", "base_url": "https://x.com", "model": "m"},
        )
    assert resp.status_code == 403
    app.dependency_overrides.clear()


async def test_test_connection_success(llm_client: httpx.AsyncClient) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"model": "m1"}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
        resp = await llm_client.post(
            "/api/v1/ai/config/test",
            json={
                "base_url": "https://api.example.com",
                "api_key": "sk-x",
                "model": "m1",
                "timeout": 30,
            },
        )
    data = resp.json()["data"]
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["model"] == "m1"
