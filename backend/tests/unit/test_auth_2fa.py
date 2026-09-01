"""双因子登录（TOTP）API 测试：第一步挑战标记 + 第二步动态码签发令牌。

覆盖：启用 2FA 账号登录返回 totp_required 且不发令牌；正确动态码签发令牌；
错误动态码 401；未启用账号直接签发令牌（原有流程不回退）。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from httpx import ASGITransport

from app.api import deps
from app.core.totp import _base32_decode, _hotp, encrypt_secret, generate_totp_secret
from app.main import app
from app.models.user import User


def _make_user(**overrides: object) -> User:
    base: dict[str, object] = {
        "id": 2,
        "org_id": 1,
        "username": "alice",
        "email": "alice@example.com",
        "password_hash": "$2b$12$abcdefghijklmnopqrstuv",  # 仅占位，测试中 verify_password 被 mock
        "display_name": "爱丽丝",
        "role": "viewer",
        "domain": "finance",
        "status": "active",
        "totp_enabled": False,
        "totp_secret": None,
    }
    base.update(overrides)
    return User(**base)  # type: ignore[arg-type]


def _code_for(secret: str) -> str:
    return _hotp(_base32_decode(secret), int(time.time() // 30))


@asynccontextmanager
async def _client_with(user: User, password_ok: bool = True) -> AsyncIterator[httpx.AsyncClient]:
    """构造登录客户端：mock 会话返回指定 user，verify_password 可 mock。"""
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
    with patch("app.api.auth.verify_password", return_value=password_ok):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    app.dependency_overrides.clear()


async def test_login_returns_totp_required_when_enabled() -> None:
    secret = generate_totp_secret()
    user = _make_user(totp_enabled=True, totp_secret=encrypt_secret(secret))
    async with _client_with(user) as client:
        resp = await client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "p@ss"}
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["totp_required"] is True
    assert body["access_token"] == ""  # 第一步不发令牌


async def test_login_2fa_with_valid_code_issues_tokens() -> None:
    secret = generate_totp_secret()
    user = _make_user(totp_enabled=True, totp_secret=encrypt_secret(secret))
    async with _client_with(user) as client:
        resp = await client.post(
            "/api/v1/auth/login/2fa",
            json={"username": "alice", "password": "p@ss", "totp_code": _code_for(secret)},
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["totp_required"] is False
    assert body["access_token"]
    assert body["refresh_token"]


async def test_login_2fa_rejects_wrong_code() -> None:
    secret = generate_totp_secret()
    user = _make_user(totp_enabled=True, totp_secret=encrypt_secret(secret))
    async with _client_with(user) as client:
        resp = await client.post(
            "/api/v1/auth/login/2fa",
            json={"username": "alice", "password": "p@ss", "totp_code": "000000"},
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_TOTP_INVALID"


async def test_login_2fa_rejects_account_without_2fa() -> None:
    user = _make_user(totp_enabled=False, totp_secret=None)
    async with _client_with(user) as client:
        resp = await client.post(
            "/api/v1/auth/login/2fa",
            json={"username": "alice", "password": "p@ss", "totp_code": "123456"},
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_TOTP_NOT_ENABLED"


async def test_login_without_2fa_still_issues_tokens() -> None:
    user = _make_user(totp_enabled=False, totp_secret=None)
    async with _client_with(user) as client:
        resp = await client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "p@ss"}
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["totp_required"] is False
    assert body["access_token"]
