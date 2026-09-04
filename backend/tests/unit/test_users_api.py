"""用户管理 API 测试（GET/POST /users + PUT/PATCH/重置密码）。

覆盖：CRUD 全流程、唯一性冲突、自我保护（自降级/自禁用）、权限控制、不泄露哈希。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy.sql import Delete

from app.api import deps
from app.main import app
from app.models.user import Organization, User


def _make_org(**overrides: object) -> Organization:
    """构造 Organization ORM 实例（方案 B：团队绑定域继承数据源）。"""
    base: dict[str, object] = {
        "id": 1,
        "name": "默认团队",
        "code": "default",
        "status": "active",
        "domain": None,
    }
    base.update(overrides)
    return Organization(**base)  # type: ignore[arg-type]


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


async def _request(role: str) -> httpx.AsyncClient:
    """按角色构造客户端（覆盖 get_current_user）。"""
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role=role, roles_all=lambda: [role], has_role=lambda r: r == role
    )
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
    org_result = MagicMock()
    org_result.all.return_value = [(1, "默认团队")]
    session.execute = AsyncMock(side_effect=[total_result, rows_result, org_result])

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
    with (
        patch("app.api.users.hash_password", return_value="hashed:abc"),
        patch("app.api.users._assert_unique", new=AsyncMock()),
        patch("app.api.users._assert_domains_active", new=AsyncMock()),
        patch(
            "app.api.users._assert_org_active",
            new=AsyncMock(return_value=_make_org()),
        ),
    ):
        resp = await admin_client.post(
            "/api/v1/users",
            json={
                "username": "bob",
                "email": "bob@example.com",
                "display_name": "鲍勃",
                "role": "viewer",
                "domain": "finance",
                "org_id": 1,
                "password": "Secret123!",
            },
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["username"] == "bob"
    assert data["role"] == "viewer"
    assert "password_hash" not in resp.text
    assert "password" not in resp.text


async def test_create_user_with_multiple_roles(admin_client: httpx.AsyncClient) -> None:
    """方案 A 多角色：POST roles 多选，主角色取权限最高者，user_role 全部落表。"""
    with (
        patch("app.api.users.hash_password", return_value="hashed:abc"),
        patch("app.api.users._assert_unique", new=AsyncMock()),
        patch("app.api.users._assert_domains_active", new=AsyncMock()),
        patch(
            "app.api.users._assert_org_active",
            new=AsyncMock(return_value=_make_org()),
        ),
    ):
        resp = await admin_client.post(
            "/api/v1/users",
            json={
                "username": "carol",
                "email": "carol@example.com",
                "display_name": "卡罗尔",
                "role": "reviewer",
                "roles": ["domain_admin", "reviewer"],
                "org_id": 1,
                "password": "Secret123!",
            },
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # 主角色自动重算为权限最高者（domain_admin > reviewer）
    assert data["role"] == "domain_admin"
    assert set(data["roles"]) == {"domain_admin", "reviewer"}


async def test_update_user_replaces_roles(admin_client: httpx.AsyncClient) -> None:
    """方案 A 多角色：PUT 整表替换 roles（含主角色重算 + 自我保护）。"""
    row = _make_user(id=2, role="viewer")
    session = _make_session()
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = row
    org_result = MagicMock()
    org_result.scalar_one_or_none.return_value = None
    # 第 1 次：_get_user 查用户；第 2 次：org 查询；第 3 次：显式删除旧 user_role（delete 语句）
    session.execute = AsyncMock(side_effect=[user_result, org_result, MagicMock()])

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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("app.api.users._assert_unique", new=AsyncMock()),
            patch("app.api.users._assert_domains_active", new=AsyncMock()),
            patch(
                "app.api.users._assert_org_active",
                new=AsyncMock(return_value=_make_org()),
            ),
        ):
            resp = await client.put(
                "/api/v1/users/2",
                json={
                    "display_name": "卡罗尔",
                    "email": "carol@example.com",
                    "role": "reviewer",
                    "roles": ["metric_owner", "reviewer"],
                },
            )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()["data"]
    # metric_owner 优先级高于 reviewer → 主角色 metric_owner
    assert data["role"] == "metric_owner"
    assert set(data["roles"]) == {"metric_owner", "reviewer"}
    # 显式删除旧 user_role 行（delete-orphan 失效时的确定性兜底，防唯一键冲突）
    delete_call = session.execute.await_args_list[2]
    assert isinstance(delete_call.args[0], Delete)
    # role_items 整表重建为请求的角色集合
    assert {ur.role for ur in row.role_items} == {"metric_owner", "reviewer"}


async def test_update_self_platform_admin_removal_forbidden(
    admin_client: httpx.AsyncClient,
) -> None:
    """自我保护：当前登录平台管理员不能通过多角色编辑移除自己的 platform_admin。"""
    row = _make_user(id=1, role="platform_admin")  # 编辑的是自己
    session = _make_session()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session.execute = AsyncMock(return_value=result)

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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("app.api.users._assert_unique", new=AsyncMock()),
            patch("app.api.users._assert_domains_active", new=AsyncMock()),
        ):
            resp = await client.put(
                "/api/v1/users/1",
                json={
                    "display_name": "平台管理员",
                    "email": "admin@example.com",
                    "role": "viewer",
                    "roles": ["viewer"],
                },
            )
    app.dependency_overrides.clear()
    assert resp.status_code == 422  # SELF_DEMOTE_FORBIDDEN → ValidationError
    body = resp.json()
    assert body["code"] == "SELF_DEMOTE_FORBIDDEN"


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
    with (
        patch("app.api.users._assert_unique", new=AsyncMock()),
        patch("app.api.users._assert_org_active", new=AsyncMock(return_value=_make_org())),
    ):
        resp = await admin_client.post(
            "/api/v1/users",
            json={
                "username": "bob",
                "email": "bob@example.com",
                "display_name": "鲍勃",
                "role": "viewer",
                "domain": "ghost_domain",
                "org_id": 1,
                "password": "Secret123!",
            },
        )
    assert resp.status_code == 422
    assert "USER_DOMAIN_INVALID" in resp.text


async def test_update_user_invalid_domain_rejected(admin_client: httpx.AsyncClient) -> None:
    """编辑时把域改为不存在/未启用的主题域 → 422 USER_DOMAIN_INVALID。"""
    user = _make_user()
    with (
        patch("app.api.users._get_user", return_value=user),
        patch("app.api.users._assert_unique", new=AsyncMock()),
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
    with (
        patch("app.api.users._get_user", return_value=user),
        patch("app.api.users._assert_unique", new=AsyncMock()),
        patch("app.api.users._assert_domains_active", new=AsyncMock()),
    ):
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
    with (
        patch("app.api.users._get_user", return_value=admin),
        patch("app.api.users._assert_unique", new=AsyncMock()),
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
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
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
    with (
        patch("app.api.users._get_user", return_value=user),
        patch("app.api.users.hash_password", return_value="hashed:new"),
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


async def test_reset_password_managed_admin_forbidden(admin_client: httpx.AsyncClient) -> None:
    """部署托管账号 admin：管理员重置密码一律 403，且不触碰哈希。"""
    admin = _make_user(id=1, username="admin", role="platform_admin")
    with (
        patch("app.api.users._get_user", return_value=admin),
        patch("app.api.users.hash_password", new=AsyncMock()) as hash_pw,
    ):
        resp = await admin_client.post(
            "/api/v1/users/1/reset-password", json={"new_password": "Newsecret123!"}
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "ACCOUNT_MANAGED_BY_DEPLOYMENT"
    hash_pw.assert_not_called()


async def test_change_my_password_managed_admin_forbidden() -> None:
    """部署托管账号 admin 自助改密同样 403（在校验旧密码之前拦截）。"""
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, username="admin", role="platform_admin", password_hash="hashed:old"
    )
    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            with patch("app.api.users.verify_password", new=AsyncMock()) as verify_pw:
                resp = await c.post(
                    "/api/v1/users/me/password",
                    json={"current_password": "Oldsecret123!", "new_password": "Newsecret123!"},
                )
        assert resp.status_code == 403
        assert resp.json()["code"] == "ACCOUNT_MANAGED_BY_DEPLOYMENT"
        verify_pw.assert_not_called()  # 托管账号在验密前即拒绝
    finally:
        app.dependency_overrides.clear()


async def test_is_managed_account_env_list() -> None:
    """托管账号集合默认含 admin，可经 UNISENSE_MANAGED_ACCOUNTS 扩展。"""
    from app.api import users
    from app.core.config import settings

    with patch.object(settings, "managed_accounts", "admin, opsbot, deploy"):
        assert users._is_managed_account("admin") is True
        assert users._is_managed_account("opsbot") is True
        assert users._is_managed_account("alice") is False
        assert users._is_managed_account(None) is False
    with patch.object(settings, "managed_accounts", "admin"):
        assert users._is_managed_account("admin") is True
        assert users._is_managed_account("opsbot") is False


# ---------------------------------------------------------------------------
# 账号安全事件定向通知（轨道D：NotifyService.notify_user best-effort）
# ---------------------------------------------------------------------------


async def test_create_user_sends_created_notification(admin_client: httpx.AsyncClient) -> None:
    """创建用户成功后定向通知新用户本人 user.created，通知体不含明文密码。"""
    with (
        patch("app.api.users.hash_password", return_value="hashed:abc"),
        patch("app.api.users._assert_unique", new=AsyncMock()),
        patch("app.api.users._assert_domains_active", new=AsyncMock()),
        patch("app.api.users._assert_org_active", new=AsyncMock(return_value=_make_org())),
        patch("app.services.notify.service.NotifyService") as ns,
    ):
        ns.return_value.notify_user = AsyncMock()
        resp = await admin_client.post(
            "/api/v1/users",
            json={
                "username": "bob",
                "email": "bob@example.com",
                "display_name": "鲍勃",
                "role": "viewer",
                "org_id": 1,
                "password": "Secret123!",
            },
        )
    assert resp.status_code == 200
    ns.return_value.notify_user.assert_awaited_once()
    kwargs = ns.return_value.notify_user.await_args.kwargs
    assert kwargs["user_id"] == 2  # flush 回填的新用户 id
    assert kwargs["event_type"] == "user.created"
    assert kwargs["title"] == "账号已创建"
    assert kwargs["channel"] == "IN_APP"
    assert "bob" in kwargs["body"]
    assert "密码" in kwargs["body"]
    assert "Secret123!" not in kwargs["body"]  # 初始密码线下交付，不入通知体


async def test_create_user_notify_failure_does_not_block(admin_client: httpx.AsyncClient) -> None:
    """通知失败（如 Redis 不可用）不阻断创建主流程（best-effort）。"""
    with (
        patch("app.api.users.hash_password", return_value="hashed:abc"),
        patch("app.api.users._assert_unique", new=AsyncMock()),
        patch("app.api.users._assert_domains_active", new=AsyncMock()),
        patch("app.api.users._assert_org_active", new=AsyncMock(return_value=_make_org())),
        patch("app.services.notify.service.NotifyService") as ns,
    ):
        ns.return_value.notify_user = AsyncMock(side_effect=RuntimeError("redis down"))
        resp = await admin_client.post(
            "/api/v1/users",
            json={
                "username": "bob",
                "email": "bob@example.com",
                "display_name": "鲍勃",
                "role": "viewer",
                "org_id": 1,
                "password": "Secret123!",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["username"] == "bob"
    ns.return_value.notify_user.assert_awaited_once()


async def test_disable_user_sends_status_notification(admin_client: httpx.AsyncClient) -> None:
    """单条禁用 → 定向通知 user.status_changed（标题「账号已禁用」）。"""
    user = _make_user()
    with (
        patch("app.api.users._get_user", return_value=user),
        patch("app.services.notify.service.NotifyService") as ns,
    ):
        ns.return_value.notify_user = AsyncMock()
        resp = await admin_client.patch("/api/v1/users/2/status", json={"status": "disabled"})
    assert resp.status_code == 200
    ns.return_value.notify_user.assert_awaited_once()
    kwargs = ns.return_value.notify_user.await_args.kwargs
    assert kwargs["user_id"] == 2
    assert kwargs["event_type"] == "user.status_changed"
    assert kwargs["title"] == "账号已禁用"
    assert kwargs["channel"] == "IN_APP"
    assert "禁用" in kwargs["body"]


async def test_enable_user_sends_status_notification(admin_client: httpx.AsyncClient) -> None:
    """单条启用 → 定向通知 user.status_changed（标题「账号已启用」）。"""
    user = _make_user(status="disabled")
    with (
        patch("app.api.users._get_user", return_value=user),
        patch("app.services.notify.service.NotifyService") as ns,
    ):
        ns.return_value.notify_user = AsyncMock()
        resp = await admin_client.patch("/api/v1/users/2/status", json={"status": "active"})
    assert resp.status_code == 200
    kwargs = ns.return_value.notify_user.await_args.kwargs
    assert kwargs["event_type"] == "user.status_changed"
    assert kwargs["title"] == "账号已启用"
    assert "启用" in kwargs["body"]


async def test_batch_status_notifies_each_succeeded_user() -> None:
    """批量禁用：仅对 succeeded 用户逐人定向通知 user.status_changed。"""
    u2 = _make_user(id=2, username="alice")
    u3 = _make_user(id=3, username="bob")
    client, _ = await _batch_status_client([u2, u3])
    async with client:
        with patch("app.services.notify.service.NotifyService") as ns:
            ns.return_value.notify_user = AsyncMock()
            resp = await client.post(
                "/api/v1/users/batch-status", json={"user_ids": [2, 3], "status": "disabled"}
            )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert ns.return_value.notify_user.await_count == 2
    call_ids = [c.kwargs["user_id"] for c in ns.return_value.notify_user.await_args_list]
    assert call_ids == [2, 3]
    for c in ns.return_value.notify_user.await_args_list:
        assert c.kwargs["event_type"] == "user.status_changed"
        assert c.kwargs["title"] == "账号已禁用"
        assert c.kwargs["channel"] == "IN_APP"


async def test_batch_status_skips_notification_for_failed_items() -> None:
    """批量禁用部分失败：仅通知成功项，失败项（自禁/不存在）不通知。"""
    admin = _make_user(id=1, username="admin", role="platform_admin")
    u2 = _make_user(id=2, username="alice")
    client, _ = await _batch_status_client([admin, u2])
    async with client:
        with patch("app.services.notify.service.NotifyService") as ns:
            ns.return_value.notify_user = AsyncMock()
            resp = await client.post(
                "/api/v1/users/batch-status",
                json={"user_ids": [1, 2, 999], "status": "disabled"},
            )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert len(resp.json()["data"]["failed"]) == 2
    ns.return_value.notify_user.assert_awaited_once()
    assert ns.return_value.notify_user.await_args.kwargs["user_id"] == 2


async def test_reset_password_sends_notification(admin_client: httpx.AsyncClient) -> None:
    """重置密码 → 定向通知 user.password_reset，提示用临时密码登录，不含明文。"""
    user = _make_user()
    with (
        patch("app.api.users._get_user", return_value=user),
        patch("app.api.users.hash_password", return_value="hashed:new"),
        patch("app.services.notify.service.NotifyService") as ns,
    ):
        ns.return_value.notify_user = AsyncMock()
        resp = await admin_client.post(
            "/api/v1/users/2/reset-password", json={"new_password": "Newsecret123!"}
        )
    assert resp.status_code == 200
    ns.return_value.notify_user.assert_awaited_once()
    kwargs = ns.return_value.notify_user.await_args.kwargs
    assert kwargs["user_id"] == 2
    assert kwargs["event_type"] == "user.password_reset"
    assert kwargs["title"] == "密码已被重置"
    assert kwargs["channel"] == "IN_APP"
    assert "临时密码" in kwargs["body"]
    assert "Newsecret123!" not in kwargs["body"]


# ---------------------------------------------------------------------------
# 方案 B：团队绑定域继承（user.domain 由 org.domain 自动继承）
# ---------------------------------------------------------------------------


async def test_create_user_inherits_team_domain(admin_client: httpx.AsyncClient) -> None:
    """创建用户：团队绑定域（org.domain=sales）时，用户自动继承团队域（不传 domain）。"""
    with (
        patch("app.api.users.hash_password", return_value="hashed:abc"),
        patch("app.api.users._assert_unique", new=AsyncMock()),
        patch("app.api.users._assert_domains_active", new=AsyncMock()),
        patch(
            "app.api.users._assert_org_active",
            new=AsyncMock(return_value=_make_org(domain="sales")),
        ),
    ):
        resp = await admin_client.post(
            "/api/v1/users",
            json={
                "username": "bob",
                "email": "bob@example.com",
                "display_name": "鲍勃",
                "role": "viewer",
                "org_id": 1,
                "password": "Secret123!",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["domain"] == "sales"
    assert resp.json()["data"]["org_id"] == 1


async def test_create_user_domains_union_with_team_domain(admin_client: httpx.AsyncClient) -> None:
    """创建用户：权限域 = 团队继承 ∪ 显式指定（并集），主域取显式首个。"""
    with (
        patch("app.api.users.hash_password", return_value="hashed:abc"),
        patch("app.api.users._assert_unique", new=AsyncMock()),
        patch("app.api.users._assert_domains_active", new=AsyncMock()),
        patch(
            "app.api.users._assert_org_active",
            new=AsyncMock(return_value=_make_org(domain="sales")),
        ),
    ):
        resp = await admin_client.post(
            "/api/v1/users",
            json={
                "username": "bob",
                "email": "bob@example.com",
                "display_name": "鲍勃",
                "role": "domain_admin",
                "org_id": 1,
                "domains": ["medical_fee", "online_consultation"],
                "password": "Secret123!",
            },
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # 主域 = 显式首个；domains = 团队域 ∪ 显式指定（去重并集）
    assert data["domain"] == "medical_fee"
    assert data["domains"] == ["medical_fee", "online_consultation", "sales"]


async def test_update_user_change_team_inherits_new_domain(admin_client: httpx.AsyncClient) -> None:
    """编辑用户换团队：新团队绑定域（domain=finance）时，用户域自动切换为新团队域。"""
    user = _make_user(org_id=1, domain="sales")
    with (
        patch("app.api.users._get_user", return_value=user),
        patch("app.api.users._assert_unique", new=AsyncMock()),
        patch("app.api.users._assert_domains_active", new=AsyncMock()),
        patch(
            "app.api.users._assert_org_active",
            new=AsyncMock(
                return_value=_make_org(id=2, name="金融团队", code="fin", domain="finance")
            ),
        ),
    ):
        resp = await admin_client.put(
            "/api/v1/users/2",
            json={
                "display_name": "爱丽丝·新",
                "email": "alice@example.com",
                "role": "metric_owner",
                "org_id": 2,
            },
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["org_id"] == 2
    assert data["org_name"] == "金融团队"
    assert data["domain"] == "finance"


# ---------------------------------------------------------------------------
# 方案 A 多角色：require_roles 命中任一角色即放行
# ---------------------------------------------------------------------------


async def test_multi_role_user_with_platform_admin_can_access() -> None:
    """扩展角色为 platform_admin 的用户可访问平台管理端点（主角色 reviewer 亦可）。"""
    session = _make_session()
    total_result = MagicMock()
    total_result.scalar.return_value = 0
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = []
    org_result = MagicMock()
    org_result.all.return_value = []
    session.execute = AsyncMock(side_effect=[total_result, rows_result, org_result])

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=2,
        role="reviewer",
        roles_all=lambda: ["reviewer", "platform_admin"],
        has_role=lambda r: r in ("reviewer", "platform_admin"),
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/users?page=1&page_size=20")
    app.dependency_overrides.clear()
    assert resp.status_code == 200


async def test_multi_role_user_without_platform_admin_forbidden() -> None:
    """扩展角色不含 platform_admin → 平台管理端点仍拒绝（fail-closed）。"""
    session = _make_session()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=2,
        role="reviewer",
        roles_all=lambda: ["reviewer", "metric_owner"],
        has_role=lambda r: r in ("reviewer", "metric_owner"),
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/users")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_create_user_loads_roles_before_flush() -> None:
    """回归守卫：create 路径必须在 ``db.add/flush`` **之前**装载 ``role_items``。

    原因（真实环境 500，非 mock 可覆盖）：新建对象在 ``flush()`` 后首次赋值
    ``row.role_items = [...]`` 时，SQLAlchemy 为计算 delete-orphan 会 emit
    ``SELECT user_role WHERE user_id = :id`` 触发 lazy load，async 上下文报
    ``MissingGreenlet`` → ``POST /api/v1/users`` 返回 500。

    本测试用**源码顺序契约**而非 mock session 断言——mock session 不会 emit SQL，
    因此对该缺陷天然假绿（与 update 路径 delete-orphan 失效同一族教训）。
    """
    import inspect

    from app.api import users as users_api

    src = inspect.getsource(users_api.create_user)
    add_pos = src.find("db.add(row)")
    assign_pos = src.find("row.role_items")
    assert add_pos != -1, "未找到 db.add(row)"
    assert assign_pos != -1, "未找到 row.role_items 装载"
    assert assign_pos < add_pos, (
        "row.role_items 必须在 db.add(row)/flush 之前装载（对象仍为 pending），"
        "否则 delete-orphan 会触发 lazy load 导致 MissingGreenlet 500"
    )


# ---------------------------------------------------------------------------
# 双因子认证（TOTP）自服务 + 管理员重置（P2 加固）
# ---------------------------------------------------------------------------


async def _me_2fa_client(user: User) -> httpx.AsyncClient:
    """以指定 User 作为当前登录用户构造客户端（自服务端点）。"""
    session = MagicMock()

    async def _execute(*args: object, **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        return result

    session.execute = AsyncMock(side_effect=_execute)
    session.commit = AsyncMock()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _totp_user(secret: str | None = None, enabled: bool = False) -> User:
    """构造带加密 TOTP 密钥的用户（默认已生成密钥、未启用；enabled=True 表示已启用）。"""
    from app.core.totp import encrypt_secret, generate_totp_secret

    return _make_user(
        totp_secret=encrypt_secret(secret if secret is not None else generate_totp_secret()),
        totp_enabled=enabled,
    )


def _code_for(secret: str) -> str:
    import time

    from app.core.totp import _base32_decode, _hotp

    return _hotp(_base32_decode(secret), int(time.time() // 30))


async def test_setup_2fa_wrong_password_rejected() -> None:
    user = _totp_user()
    client = await _me_2fa_client(user)
    async with client:
        with patch("app.api.users.verify_password", return_value=False):
            resp = await client.post(
                "/api/v1/users/me/2fa/setup", json={"current_password": "wrong"}
            )
    assert resp.status_code == 401  # PASSWORD_INCORRECT 映射为 401
    assert resp.json()["code"] == "PASSWORD_INCORRECT"
    app.dependency_overrides.clear()


async def test_setup_2fa_returns_secret_and_uri() -> None:
    user = _totp_user()
    client = await _me_2fa_client(user)
    async with client:
        with patch("app.api.users.verify_password", return_value=True):
            resp = await client.post(
                "/api/v1/users/me/2fa/setup", json={"current_password": "p@ss"}
            )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["secret"] and len(data["secret"]) == 32
    assert data["otpauth_uri"].startswith("otpauth://totp/")
    assert data["enabled"] is False
    assert user.totp_secret  # 密钥已加密落库
    app.dependency_overrides.clear()


async def test_confirm_2fa_enables_with_valid_code() -> None:
    from app.core.totp import decrypt_secret

    user = _totp_user()
    secret = user.totp_secret
    assert secret
    client = await _me_2fa_client(user)
    async with client:
        resp = await client.post(
            "/api/v1/users/me/2fa/confirm",
            json={"totp_code": _code_for(decrypt_secret(secret) or "")},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True
    assert user.totp_enabled is True
    app.dependency_overrides.clear()


async def test_confirm_2fa_wrong_code_rejected() -> None:
    user = _totp_user()
    client = await _me_2fa_client(user)
    async with client:
        resp = await client.post("/api/v1/users/me/2fa/confirm", json={"totp_code": "000000"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_TOTP_INVALID"
    assert user.totp_enabled is False
    app.dependency_overrides.clear()


async def test_disable_2fa_requires_code_and_disables() -> None:
    from app.core.totp import decrypt_secret

    user = _totp_user(enabled=True)
    secret = user.totp_secret
    assert secret
    client = await _me_2fa_client(user)
    async with client:
        resp = await client.post(
            "/api/v1/users/me/2fa/disable",
            json={"totp_code": _code_for(decrypt_secret(secret) or "")},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False
    assert user.totp_enabled is False
    assert user.totp_secret is None
    app.dependency_overrides.clear()


async def test_admin_reset_2fa_forces_disable() -> None:
    user = _totp_user(enabled=True)
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/users/2/2fa/reset")
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False
    assert user.totp_enabled is False
    assert user.totp_secret is None
    app.dependency_overrides.clear()


async def test_resolve_user_names_by_ids_cross_org() -> None:
    """by-ids 跨组织解析：非管理员也能按已知 id 拿到其他组织用户的真实中文名。

    这是「展示已知 id → 中文名」的权威解析通道（Owner 责任链/责任人列），
    与 /auth/users（本组织列表）互补——跨组织 id 在此可按精确 id 反查名字，
    但仅返回基础字段（不暴露 email/password_hash），且无法借此枚举目录。
    """
    session = _make_session()
    # execute 返回两个不同组织的用户（跨组织场景）
    alice = _make_user(
        id=1, org_id=1, username="alice", display_name="爱丽丝", role="viewer", domain="finance"
    )
    bob = _make_user(
        id=2,
        org_id=2,
        username="bob",
        display_name="鲍勃",
        role="metric_owner",
        domain="outpatient",
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [alice, bob]
    session.execute = AsyncMock(return_value=result)

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=3,
        role="viewer",
        roles_all=lambda: ["viewer"],
        has_role=lambda r: False,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/auth/users/by-ids?ids=1,2")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [u["id"] for u in data] == [1, 2]
    assert data[0]["display_name"] == "爱丽丝"
    assert data[1]["display_name"] == "鲍勃"
    # 最小信息：绝不暴露 email/password_hash
    assert "email" not in data[0]
    assert "password_hash" not in data[0]
    app.dependency_overrides.clear()


async def test_resolve_user_names_unknown_and_empty_ids() -> None:
    """by-ids 边界：查无的 id 跳过（不 404）、空/非数字 id 返回空列表。"""
    session = _make_session()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="viewer",
        roles_all=lambda: ["viewer"],
        has_role=lambda r: False,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 查无该 id（用户已删）→ 空列表而非 404
        resp = await client.get("/api/v1/auth/users/by-ids?ids=999")
        assert resp.status_code == 200
        assert resp.json()["data"] == []
        # 非数字/空 → 空列表
        resp2 = await client.get("/api/v1/auth/users/by-ids?ids=abc,")
        assert resp2.status_code == 200
        assert resp2.json()["data"] == []
    app.dependency_overrides.clear()


def test_user_domains_all_union_with_team() -> None:
    """User.domains_all()：权限域 = 主域 ∪ domains 扩展 ∪ 团队域（动态继承，去重）。"""
    user = _make_user(domain="medical_fee", domains=["online_consultation", "medical_fee"])
    # 团队域经认证层挂载 _org_domain（动态继承）
    user._org_domain = "sales"
    assert user.domains_all() == ["medical_fee", "online_consultation", "sales"]

    # 无 domains 扩展、无团队域 → 仅主域
    u2 = _make_user(domain="finance")
    assert u2.domains_all() == ["finance"]

    # 主域为空、仅扩展域 → 扩展域生效
    u3 = _make_user(domain=None, domains=["medical_fee"])
    assert u3.domains_all() == ["medical_fee"]

    # 完全无域 → 空
    u4 = _make_user(domain=None)
    assert u4.domains_all() == []
