"""用户管理 API 测试（GET/POST /users + PUT/PATCH/重置密码）。

覆盖：CRUD 全流程、唯一性冲突、自我保护（自降级/自禁用）、权限控制、不泄露哈希。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.models.user import User


def _make_session() -> MagicMock:
    """构造 mock 会话：execute/add/flush/commit。

    flush 副作用：为已 ``add`` 的 User 实例填充 id（模拟真实 flush 回填主键）。
    """
    session = MagicMock()

    def _flush_side_effect(*args: object, **kwargs: object) -> None:
        for call in session.add.call_args_list:
            obj = call.args[0] if call.args else None
            if isinstance(obj, User) and getattr(obj, "id", None) is None:
                obj.id = 2

    session.flush = AsyncMock(side_effect=_flush_side_effect)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    return session


def _make_user(**overrides: object) -> User:
    """构造 User ORM 实例（供 _get_user mock 返回）。"""
    base: dict[str, object] = {
        "id": 2,
        "org_id": 1,
        "username": "alice",
        "email": "alice@example.com",
        "password_hash": "hashed:xxx",
        "display_name": "爱丽丝",
        "role": "viewer",
        "domain": "finance",
        "status": "active",
    }
    base.update(overrides)
    return User(**base)  # type: ignore[arg-type]


@pytest.fixture
async def admin_client() -> AsyncIterator[httpx.AsyncClient]:
    """platform_admin 客户端 + mock 会话（默认无冲突、用户存在）。"""
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="platform_admin")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _request(role: str) -> httpx.AsyncClient:
    """按角色构造客户端（覆盖 get_current_user）。"""
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role=role)
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# 权限控制
# ---------------------------------------------------------------------------


async def test_create_requires_platform_admin() -> None:
    client = await _request("viewer")
    async with client:
        resp = await client.post("/api/v1/users", json={"username": "bob", "password": "secret123"})
    assert resp.status_code == 403
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


async def test_list_users_returns_paginated() -> None:
    """platform_admin 列表：返回分页结构且不含哈希。"""
    session = _make_session()
    u1 = _make_user(id=1, username="admin", display_name="管理员", role="platform_admin")
    u2 = _make_user(id=2, username="alice", display_name="爱丽丝")
    total_result = MagicMock()
    total_result.scalar.return_value = 2
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = [u1, u2]
    session.execute = AsyncMock(side_effect=[total_result, rows_result])

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="platform_admin")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/users?page=1&page_size=20")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][1]["username"] == "alice"
    assert data["items"][1]["email"] == "alice@example.com"
    assert "password_hash" not in resp.text


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------


async def test_create_user_success(admin_client: httpx.AsyncClient) -> None:
    with patch("app.api.users.hash_password", return_value="hashed:abc"), patch(
        "app.api.users._assert_unique", new=AsyncMock()
    ), patch("app.api.users._assert_domain_active", new=AsyncMock()):
        resp = await admin_client.post(
            "/api/v1/users",
            json={
                "username": "bob",
                "email": "bob@example.com",
                "display_name": "鲍勃",
                "role": "viewer",
                "domain": "finance",
                "password": "Secret123!",
            },
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["username"] == "bob"
    assert data["role"] == "viewer"
    assert "password_hash" not in resp.text
    assert "password" not in resp.text


async def test_create_user_conflict(admin_client: httpx.AsyncClient) -> None:
    from app.core.exceptions import ConflictError

    with patch("app.api.users._assert_unique", side_effect=ConflictError("用户名已被占用")):
        resp = await admin_client.post(
            "/api/v1/users",
            json={
                "username": "bob",
                "email": "bob@example.com",
                "display_name": "鲍勃",
                "password": "Secret123!",
            },
        )
    assert resp.status_code == 409


async def test_create_user_weak_password(admin_client: httpx.AsyncClient) -> None:
    resp = await admin_client.post(
        "/api/v1/users",
        json={"username": "bob", "email": "bob@example.com", "password": "short"},
    )
    assert resp.status_code == 422


async def test_create_user_invalid_domain_rejected(admin_client: httpx.AsyncClient) -> None:
    """domain 非 active 主题域 code → 422 USER_DOMAIN_INVALID（防绕过 UI 注入任意域值）。"""
    with patch("app.api.users._assert_unique", new=AsyncMock()):
        resp = await admin_client.post(
            "/api/v1/users",
            json={
                "username": "bob",
                "email": "bob@example.com",
                "display_name": "鲍勃",
                "role": "viewer",
                "domain": "ghost_domain",
                "password": "Secret123!",
            },
        )
    assert resp.status_code == 422
    assert "USER_DOMAIN_INVALID" in resp.text


async def test_update_user_invalid_domain_rejected(admin_client: httpx.AsyncClient) -> None:
    """编辑时把域改为不存在/未启用的主题域 → 422 USER_DOMAIN_INVALID。"""
    user = _make_user()
    with patch("app.api.users._get_user", return_value=user), patch(
        "app.api.users._assert_unique", new=AsyncMock()
    ):
        resp = await admin_client.put(
            "/api/v1/users/2",
            json={
                "display_name": "爱丽丝",
                "email": "alice@example.com",
                "role": "viewer",
                "domain": "ghost_domain",
            },
        )
    assert resp.status_code == 422
    assert "USER_DOMAIN_INVALID" in resp.text


# ---------------------------------------------------------------------------
# 编辑
# ---------------------------------------------------------------------------


async def test_update_user_success(admin_client: httpx.AsyncClient) -> None:
    user = _make_user()
    with patch("app.api.users._get_user", return_value=user), patch(
        "app.api.users._assert_unique", new=AsyncMock()
    ), patch("app.api.users._assert_domain_active", new=AsyncMock()):
        resp = await admin_client.put(
            "/api/v1/users/2",
            json={
                "display_name": "爱丽丝·新",
                "email": "alice@example.com",
                "role": "metric_owner",
                "domain": "sales",
            },
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["display_name"] == "爱丽丝·新"
    assert data["role"] == "metric_owner"
    assert data["domain"] == "sales"


async def test_update_user_self_demote_rejected(admin_client: httpx.AsyncClient) -> None:
    # 当前用户 id=1，编辑目标 id=1（自己），且降级非 platform_admin
    admin = _make_user(id=1, username="admin", role="platform_admin")
    with patch("app.api.users._get_user", return_value=admin), patch(
        "app.api.users._assert_unique", new=AsyncMock()
    ):
        resp = await admin_client.put(
            "/api/v1/users/1",
            json={
                "display_name": "管理员",
                "email": "admin@example.com",
                "role": "viewer",
                "domain": None,
            },
        )
    assert resp.status_code == 422
    assert "SELF_DEMOTE_FORBIDDEN" in resp.text


async def test_update_user_not_found(admin_client: httpx.AsyncClient) -> None:
    with patch("app.api.users._get_user", return_value=None):
        resp = await admin_client.put(
            "/api/v1/users/999",
            json={
                "display_name": "X",
                "email": "x@example.com",
                "role": "viewer",
                "domain": None,
            },
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 启用 / 禁用
# ---------------------------------------------------------------------------


async def test_disable_user_success(admin_client: httpx.AsyncClient) -> None:
    user = _make_user()
    with patch("app.api.users._get_user", return_value=user):
        resp = await admin_client.patch("/api/v1/users/2/status", json={"status": "disabled"})
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "disabled"


async def test_disable_self_rejected(admin_client: httpx.AsyncClient) -> None:
    admin = _make_user(id=1, username="admin", role="platform_admin")
    with patch("app.api.users._get_user", return_value=admin):
        resp = await admin_client.patch("/api/v1/users/1/status", json={"status": "disabled"})
    assert resp.status_code == 422
    assert "SELF_DISABLE_FORBIDDEN" in resp.text


# ---------------------------------------------------------------------------
# 批量启用 / 禁用
# ---------------------------------------------------------------------------


async def _batch_status_client(rows: list[User]) -> tuple[httpx.AsyncClient, MagicMock]:
    """构造批量状态测试客户端（id=1 平台管理员 + 指定用户行）。"""
    session = _make_session()
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = rows
    session.execute = AsyncMock(return_value=rows_result)

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="platform_admin")
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), session


async def test_batch_disable_success() -> None:
    """批量禁用：全部成功，返回 succeeded 2 条且行状态已更新。"""
    u2 = _make_user(id=2, username="alice")
    u3 = _make_user(id=3, username="bob")
    client, _ = await _batch_status_client([u2, u3])
    async with client:
        resp = await client.post(
            "/api/v1/users/batch-status", json={"user_ids": [2, 3], "status": "disabled"}
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["succeeded"]) == 2
    assert data["failed"] == []
    assert u2.status == "disabled"
    assert u3.status == "disabled"


async def test_batch_enable_success() -> None:
    """批量启用：全部成功，返回 succeeded 2 条。"""
    u2 = _make_user(id=2, username="alice", status="disabled")
    u3 = _make_user(id=3, username="bob", status="disabled")
    client, _ = await _batch_status_client([u2, u3])
    async with client:
        resp = await client.post(
            "/api/v1/users/batch-status", json={"user_ids": [2, 3], "status": "active"}
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["succeeded"]) == 2
    assert data["failed"] == []
    assert u2.status == "active"


async def test_batch_status_partial_failure() -> None:
    """部分失败：不存在 + 自我保护逐项标注，不影响其余更新（207 语义）。"""
    admin = _make_user(id=1, username="admin", role="platform_admin")
    u2 = _make_user(id=2, username="alice")
    client, _ = await _batch_status_client([admin, u2])
    async with client:
        resp = await client.post(
            "/api/v1/users/batch-status",
            json={"user_ids": [1, 2, 999], "status": "disabled"},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["succeeded"]) == 1
    assert data["succeeded"][0]["user_id"] == 2
    failed = {f["user_id"]: f for f in data["failed"]}
    assert failed[1]["error_code"] == "SELF_DISABLE_FORBIDDEN"
    assert failed[999]["error_code"] == "USER_NOT_FOUND"
    assert u2.status == "disabled"


async def test_batch_status_empty_rejected(admin_client: httpx.AsyncClient) -> None:
    """空 user_ids → 422（pydantic min_length=1）。"""
    resp = await admin_client.post(
        "/api/v1/users/batch-status", json={"user_ids": [], "status": "disabled"}
    )
    assert resp.status_code == 422


async def test_batch_status_over_quota_rejected(admin_client: httpx.AsyncClient) -> None:
    """超过 200 上限 → 422（pydantic max_length=200）。"""
    resp = await admin_client.post(
        "/api/v1/users/batch-status",
        json={"user_ids": list(range(1, 202)), "status": "disabled"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 重置密码
# ---------------------------------------------------------------------------


async def test_reset_password_success(admin_client: httpx.AsyncClient) -> None:
    user = _make_user()
    with patch("app.api.users._get_user", return_value=user), patch(
        "app.api.users.hash_password", return_value="hashed:new"
    ):
        resp = await admin_client.post(
            "/api/v1/users/2/reset-password", json={"new_password": "Newsecret123!"}
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["ok"] is True
    # 不返回明文新密码
    assert "newsecret123" not in resp.text


async def test_reset_password_not_found(admin_client: httpx.AsyncClient) -> None:
    with patch("app.api.users._get_user", return_value=None):
        resp = await admin_client.post(
            "/api/v1/users/999/reset-password", json={"new_password": "Newsecret123!"}
        )
    assert resp.status_code == 404
