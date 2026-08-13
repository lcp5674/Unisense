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
from app.core.security import hash_password
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

    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert payload["sub"] == "1"
    assert payload["role"] == "platform_admin"
    assert payload["org_id"] == 1
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
    c, _ = auth_client
    user = MagicMock()
    user.id = 7
    user.username = "alice"
    user.display_name = "Alice"
    user.role = "analyst"
    user.domain = "sales"
    user.org_id = 2
    app.dependency_overrides[deps.get_current_user] = lambda: user

    resp = await c.get("/api/v1/auth/me", headers={"Authorization": "Bearer dummy"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["username"] == "alice"
    assert data["role"] == "analyst"
    assert data["org_id"] == 2
    assert data["domain"] == "sales"
