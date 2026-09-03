"""LLM 配置 API 测试（GET/POST/PUT/DELETE /ai/config + POST /ai/config/test）。

多实例轮询路由：GET 返回 items 列表 + 路由策略 + 生效配置；POST/PUT/DELETE 管理实例。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.core.secrets import SecretManager
from app.main import app


def _make_session(rows: list[object] | None = None) -> MagicMock:
    """构造 mock 会话：execute 返回可配置的 Result mock（scalars().all() 供 list 查询）。"""
    session = MagicMock()
    session.flush = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.return_value = list(rows or [])
    session.execute = AsyncMock(return_value=result)
    return session


def _row(**overrides: object) -> MagicMock:
    """构造一个 llm_config 行 mock（含 name/priority 路由字段）。"""
    row = MagicMock()
    cfg = {
        "id": 1,
        "name": "主用",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key_enc": SecretManager.encrypt({"api_key": "sk-test"}),
        "timeout": 30,
        "max_tokens": 2048,
        "temperature": None,
        "enabled": True,
        "priority": 0,
        "updated_by": 1,
        "updated_at": None,
        "deleted_at": None,
    }
    cfg.update(overrides)
    for k, v in cfg.items():
        setattr(row, k, v)
    return row


@pytest.fixture
async def llm_client() -> AsyncIterator[httpx.AsyncClient]:
    session = _make_session([_row()])

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_get_config_list(llm_client: httpx.AsyncClient) -> None:
    with patch("app.services.llm.config_service.settings") as ms:
        ms.llm_base_url = ""
        ms.llm_api_key = ""
        resp = await llm_client.get("/api/v1/ai/config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["strategy"] == "round_robin"
    assert data["can_edit"] is True  # platform_admin 可编辑
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["name"] == "主用"
    assert item["base_url"] == "https://api.deepseek.com"
    assert item["has_api_key"] is True  # 脱敏：只返回标记，不含明文
    assert "api_key" not in item or not item.get("api_key")
    assert data["effective"]["source"] == "db"


async def test_get_config_can_edit_false_for_reviewer() -> None:
    """reviewer 有 ai:view 可访问 AI 配置（can_edit=false）；viewer 基线无 ai:view 被拒。

    此前 /ai/config 依赖曾用 _WRITE_ROLES（含 viewer），viewer 可读；现对齐 ai:view
    基线——reviewer/compliance/analyst 可进页面只读，viewer 403。
    """
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="reviewer", roles_all=lambda: ["reviewer"], has_role=lambda r: r == "reviewer"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/ai/config")
    data = resp.json()["data"]
    assert data["can_edit"] is False
    assert len(data["items"]) == 0
    app.dependency_overrides.clear()


async def test_get_config_denied_for_viewer() -> None:
    """viewer 基线无 ai:view：AI 配置读被拒（403）。"""
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="viewer", roles_all=lambda: ["viewer"], has_role=lambda r: r == "viewer"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/ai/config")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


async def test_get_config_secret_returns_plaintext() -> None:
    session = _make_session()
    session.execute.return_value.scalar_one_or_none.return_value = _row()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/ai/config/1/secret")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["id"] == 1
    assert data["api_key"] == "sk-test"
    app.dependency_overrides.clear()


async def test_get_config_secret_404_when_missing() -> None:
    session = _make_session()
    session.execute.return_value.scalar_one_or_none.return_value = None

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/ai/config/99/secret")
    assert resp.status_code == 404
    app.dependency_overrides.clear()


async def test_get_config_secret_404_when_no_key() -> None:
    session = _make_session()
    session.execute.return_value.scalar_one_or_none.return_value = _row(api_key_enc="")

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/ai/config/1/secret")
    assert resp.status_code == 404
    app.dependency_overrides.clear()


async def test_get_config_secret_forbidden_for_viewer() -> None:
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="viewer", roles_all=lambda: ["viewer"], has_role=lambda r: r == "viewer"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/v1/ai/config/1/secret")
    assert resp.status_code == 403
    app.dependency_overrides.clear()


async def test_post_config_creates(llm_client: httpx.AsyncClient) -> None:
    resp = await llm_client.post(
        "/api/v1/ai/config",
        json={
            "name": "备用",
            "provider": "qwen",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode",
            "model": "qwen-turbo",
            "api_key": "sk-test",
            "timeout": 30,
            "enabled": True,
            "priority": 1,
        },
    )
    assert resp.status_code == 201
    assert "id" in resp.json()["data"]


async def test_post_config_requires_api_key(llm_client: httpx.AsyncClient) -> None:
    resp = await llm_client.post(
        "/api/v1/ai/config",
        json={
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": "",
            "timeout": 30,
            "enabled": True,
        },
    )
    assert resp.status_code == 422


async def test_put_config_updates() -> None:
    session = _make_session()
    session.execute.return_value.scalar_one_or_none.return_value = _row()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.put(
            "/api/v1/ai/config/1",
            json={
                "name": "主用-改",
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "api_key": "",
                "timeout": 60,
                "enabled": True,
                "priority": 0,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == 1
    app.dependency_overrides.clear()


async def test_delete_config() -> None:
    session = _make_session()
    session.execute.return_value.scalar_one_or_none.return_value = _row()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.delete("/api/v1/ai/config/1")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True
    app.dependency_overrides.clear()


async def test_write_rejected_for_viewer() -> None:
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="viewer", roles_all=lambda: ["viewer"], has_role=lambda r: r == "viewer"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/ai/config",
            json={
                "provider": "deepseek",
                "base_url": "https://x.com",
                "model": "m",
                "api_key": "sk-x",
            },
        )
    assert resp.status_code == 403
    app.dependency_overrides.clear()


async def test_test_connection_success(llm_client: httpx.AsyncClient) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"choices": [{"message": {"content": "pong"}}], "model": "m1"}
    # 两步探测：先 GET /models 快速探测（返回 200 通过），再 POST 真实推理
    mock_models_resp = MagicMock()
    mock_models_resp.status_code = 200
    mock_models_resp.json.return_value = {"data": [{"id": "m1"}]}
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_models_resp
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
    assert data["chat"] is True


async def test_test_instance_by_id() -> None:
    session = _make_session()
    session.execute.return_value.scalar_one_or_none.return_value = _row()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "pong"}}],
        "model": "deepseek-chat",
    }
    # 两步探测：GET /models 快速探测返回 200，再 POST 真实推理
    mock_models_resp = MagicMock()
    mock_models_resp.status_code = 200
    mock_models_resp.json.return_value = {"data": [{"id": "deepseek-chat"}]}
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_models_resp
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        with patch("app.services.llm.config_service.httpx.AsyncClient", return_value=mock_client):
            resp = await c.post("/api/v1/ai/config/test", json={"instance_id": 1})
    assert resp.status_code == 200
    assert resp.json()["data"]["ok"] is True  # 用落库密钥成功探测
    app.dependency_overrides.clear()
