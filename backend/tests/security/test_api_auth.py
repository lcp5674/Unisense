"""鉴权 API 测试（ASGI）：登录签发 JWT、当前用户查询。

覆盖：登录成功（校验签发令牌与载荷）、密码错误、用户不存在（同码防枚举）、/me 映射。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import jwt
import pytest
from httpx import ASGITransport

from app.api import deps
from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    is_token_blacklisted,
)
from app.main import app
from app.models.user import User


@pytest.fixture
async def auth_client():
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, session
    app.dependency_overrides.clear()


async def _mock_user(password: str, **attrs: object) -> MagicMock:
    user = MagicMock(spec=User)
    user.password_hash = await hash_password(password)
    for k, v in attrs.items():
        setattr(user, k, v)
    return user


def _result_with(user: MagicMock | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    return result


async def test_login_success_issues_jwt(auth_client):
    c, session = auth_client
    session.execute.return_value = _result_with(
        await _mock_user("secret", id=1, role="platform_admin", org_id=1)
    )

    resp = await c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "OK"
    token = body["data"]["access_token"]
    assert body["data"]["token_type"] == "bearer"
    refresh = body["data"]["refresh_token"]
    assert refresh  # P0：登录同时签发 refresh token（7天），供 401 无感续期

    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert payload["sub"] == "1"
    assert payload["role"] == "platform_admin"
    assert payload["org_id"] == 1
    # refresh token 必须带 type=refresh 标记，且 sub 一致
    refresh_payload = jwt.decode(refresh, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert refresh_payload["type"] == "refresh"
    assert refresh_payload["sub"] == "1"
    session.commit.assert_awaited()  # 登录成功更新 last_login_at


async def test_login_wrong_password_returns_same_code(auth_client):
    c, session = auth_client
    session.execute.return_value = _result_with(await _mock_user("secret"))

    resp = await c.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})

    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_CREDENTIALS"


async def test_login_user_not_found_returns_same_code(auth_client):
    c, session = auth_client
    session.execute.return_value = _result_with(None)

    resp = await c.post("/api/v1/auth/login", json={"username": "ghost", "password": "x"})

    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_CREDENTIALS"


async def test_me_returns_current_user(auth_client):
    c, session = auth_client
    user = MagicMock()
    user.id = 7
    user.username = "alice"
    user.display_name = "Alice"
    user.role = "analyst"
    user.domain = "sales"
    user.org_id = 2
    user.must_change_password = False
    app.dependency_overrides[deps.get_current_user] = lambda: user

    # me 端点会查询 Organization 与 SubjectDomain 回填名称；mock 返回 None（无记录）即可。
    session.execute.side_effect = lambda _stmt: _result_with(None)

    resp = await c.get("/api/v1/auth/me", headers={"Authorization": "Bearer dummy"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["username"] == "alice"
    assert data["role"] == "analyst"
    assert data["org_id"] == 2
    assert data["domain"] == "sales"
    # S1：/me 必须回传 must_change_password（前端据此渲染全屏改密守卫，
    # 此前 UserInfo 缺该字段导致前端永远拿不到 true → 弹窗/守卫不触发，
    # 未改密用户被受保护端点 403 淹没显示"加载失败"）。
    assert data["must_change_password"] is False


async def test_me_returns_must_change_password_for_first_login(auth_client):
    """首次登录（或管理员重置密码）用户 /me 应返回 must_change_password=True。"""
    c, session = auth_client
    user = MagicMock()
    user.id = 8
    user.username = "bob"
    user.display_name = "Bob"
    user.role = "analyst"
    user.domain = None
    user.org_id = 2
    user.must_change_password = True
    app.dependency_overrides[deps.get_current_user] = lambda: user

    session.execute.side_effect = lambda _stmt: _result_with(None)

    resp = await c.get("/api/v1/auth/me", headers={"Authorization": "Bearer dummy"})

    assert resp.status_code == 200
    assert resp.json()["data"]["must_change_password"] is True


# ---- P0：令牌无感续期（refresh token 换发 + 轮换）----


async def test_refresh_rotates_and_issues_new_tokens(auth_client):
    """有效的 refresh token → 换发新 access + 新 refresh，旧 jti 进入黑名单（防重放）。"""
    c, session = auth_client
    session.execute.return_value = _result_with(
        await _mock_user("secret", id=1, role="platform_admin", org_id=1)
    )
    refresh_token = create_refresh_token(sub=1, role="platform_admin", org_id=1)
    old_jti = jwt.decode(refresh_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])[
        "jti"
    ]

    resp = await c.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["access_token"]
    assert data["refresh_token"]
    # 新 access token 载荷正确
    payload = jwt.decode(
        data["access_token"], settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    assert payload["sub"] == "1"
    assert payload["role"] == "platform_admin"
    # 新 refresh token 是 type=refresh，且 jti 不同于旧的（轮换）
    new_payload = jwt.decode(
        data["refresh_token"], settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )
    assert new_payload["type"] == "refresh"
    assert new_payload["jti"] != old_jti
    # 旧 refresh token 已入黑名单（防重放）
    assert await is_token_blacklisted(old_jti) is True


async def test_refresh_rejects_access_token(auth_client):
    """把 access token 当 refresh token 用 → 拒绝（type 校验）。"""
    c, _ = auth_client
    access_token = create_access_token(sub=1, role="platform_admin", org_id=1)

    resp = await c.post("/api/v1/auth/refresh", json={"refresh_token": access_token})

    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_INVALID"


async def test_refresh_rejects_blacklisted_token(auth_client):
    """已被撤销（黑名单）的 refresh token → 拒绝重放。"""
    c, session = auth_client
    session.execute.return_value = _result_with(
        await _mock_user("secret", id=1, role="platform_admin", org_id=1)
    )
    refresh_token = create_refresh_token(sub=1, role="platform_admin", org_id=1)
    jti = jwt.decode(refresh_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])["jti"]
    from app.core.security import blacklist_token

    await blacklist_token(jti, 600)

    resp = await c.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})

    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_REFRESH_REVOKED"


async def test_refresh_rejects_invalid_token(auth_client):
    """无效/乱写的 refresh token → 拒绝。"""
    c, _ = auth_client
    resp = await c.post("/api/v1/auth/refresh", json={"refresh_token": "garbage.token.value"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_INVALID"


async def test_refresh_user_not_found(auth_client):
    """refresh token 对应的用户已删除/禁用 → 拒绝。"""
    c, session = auth_client
    session.execute.return_value = _result_with(None)
    refresh_token = create_refresh_token(sub=99, role="analyst", org_id=1)

    resp = await c.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})

    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_INVALID"


async def test_login_kicks_previous_session(auth_client):
    """单端登录（TD §5）：同一用户二次登录后，旧会话 refresh 刷新被拒（互踢）。"""
    c, session = auth_client
    # 两次登录都查同一用户
    user = await _mock_user("secret", id=1, role="platform_admin", org_id=1)
    session.execute.return_value = _result_with(user)

    resp1 = await c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret"})
    assert resp1.status_code == 200
    old_refresh = resp1.json()["data"]["refresh_token"]

    # 第二次登录（另一处会话）→ 旧 refresh 应被拉黑
    resp2 = await c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret"})
    assert resp2.status_code == 200
    new_refresh = resp2.json()["data"]["refresh_token"]
    assert old_refresh != new_refresh

    # 用旧 refresh 刷新 → 应被拒（AUTH_REFRESH_REVOKED）
    resp3 = await c.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert resp3.status_code == 401
    assert resp3.json()["code"] == "AUTH_REFRESH_REVOKED"

    # 新 refresh 仍有效（不被自身误踢）
    resp4 = await c.post("/api/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert resp4.status_code == 200


async def test_login_different_users_do_not_kick_each_other(auth_client):
    """跨用户隔离（API 层）：viewer 登录后 analyst 登录，不踢 viewer 的会话。"""
    c, session = auth_client
    viewer = await _mock_user("secret", id=2, role="viewer", org_id=1)
    analyst = await _mock_user("secret", id=3, role="analyst", org_id=1)

    # 第 1 次查询返回 viewer、第 2 次返回 analyst，其后（refresh 查用户）返回 viewer
    results = [_result_with(viewer), _result_with(analyst)]

    def _side_effect(_stmt: object) -> MagicMock:
        return results.pop(0) if results else _result_with(viewer)

    session.execute.side_effect = _side_effect

    resp_viewer = await c.post("/api/v1/auth/login", json={"username": "v", "password": "secret"})
    assert resp_viewer.status_code == 200
    viewer_refresh = resp_viewer.json()["data"]["refresh_token"]

    resp_analyst = await c.post("/api/v1/auth/login", json={"username": "a", "password": "secret"})
    assert resp_analyst.status_code == 200

    # analyst 登录不应拉黑 viewer 的会话 → viewer 旧 refresh 仍可无感续期
    resp3 = await c.post("/api/v1/auth/refresh", json={"refresh_token": viewer_refresh})
    assert resp3.status_code == 200


# ---- 登录/登出审计（GB/T 35273 认证事件留痕）----


async def test_login_success_writes_auth_audit(auth_client):
    """登录成功落 auth.login 审计（谁、何时、从哪 IP）。"""
    c, session = auth_client
    session.execute.return_value = _result_with(
        await _mock_user("secret", id=1, role="platform_admin", org_id=1)
    )

    resp = await c.post("/api/v1/auth/login", json={"username": "admin", "password": "secret"})
    assert resp.status_code == 200

    assert session.add.called
    entries = [a.args[0] for a in session.add.call_args_list if a.args]
    assert any(getattr(e, "action", None) == "auth.login" for e in entries)


async def test_login_failure_writes_auth_failed_audit(auth_client):
    """登录失败落 auth.login_failed 审计（actor_id=NULL——无对应用户，X-4；
    entity_id 记录尝试用户名）。"""
    c, session = auth_client
    session.execute.return_value = _result_with(None)

    resp = await c.post("/api/v1/auth/login", json={"username": "ghost", "password": "x"})
    assert resp.status_code == 401

    assert session.add.called
    entries = [a.args[0] for a in session.add.call_args_list if a.args]
    failed = [e for e in entries if getattr(e, "action", None) == "auth.login_failed"]
    assert failed
    assert failed[0].actor_id is None
    assert failed[0].entity_id == "ghost"


async def test_logout_writes_auth_logout_audit(auth_client):
    """登出落 auth.logout 审计。"""
    c, session = auth_client
    user = MagicMock()
    user.id = 7
    user.username = "alice"
    app.dependency_overrides[deps.get_current_user] = lambda: user

    resp = await c.post(
        "/api/v1/auth/logout", headers={"Authorization": "Bearer dummy"}
    )
    assert resp.status_code == 200

    assert session.add.called
    entries = [a.args[0] for a in session.add.call_args_list if a.args]
    assert any(getattr(e, "action", None) == "auth.logout" for e in entries)
