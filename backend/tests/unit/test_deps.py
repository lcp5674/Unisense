"""公共依赖（deps.py）单测。

覆盖 get_current_user 的各类分支与 require_roles 角色校验。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest

from app.api import deps
from app.core.config import settings
from app.core.exceptions import AuthError
from app.models.user import Organization, User


def _make_user(uid: int = 1, role: str = "metric_owner") -> User:
    u = User(id=uid, username="u1", role=role, status="active", org_id=1)
    return u


def _valid_token(uid: int = 1, role: str = "metric_owner", expired: bool = False) -> str:
    now = datetime.now(UTC)
    exp = now - timedelta(minutes=5) if expired else now + timedelta(minutes=30)
    payload = {
        "sub": str(uid),
        "role": role,
        "org_id": 1,
        "jti": "test-jti-001",
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


class TestGetCurrentUser:
    async def test_missing_credentials_raises(self) -> None:
        db = MagicMock()
        with pytest.raises(AuthError):
            await deps.get_current_user(db, None)

    async def test_expired_token_raises(self) -> None:
        db = MagicMock()
        creds = MagicMock(credentials=_valid_token(expired=True))
        with pytest.raises(AuthError):
            await deps.get_current_user(db, creds)

    async def test_invalid_token_raises(self) -> None:
        db = MagicMock()
        creds = MagicMock(credentials="not-a-jwt")
        with pytest.raises(AuthError):
            await deps.get_current_user(db, creds)

    async def test_user_not_found_raises(self) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db = MagicMock()
        db.execute = AsyncMock(return_value=mock_result)
        creds = MagicMock(credentials=_valid_token(uid=999))
        with pytest.raises(AuthError):
            await deps.get_current_user(db, creds)

    async def test_valid_user_returns(self) -> None:
        user = _make_user()
        org = Organization(id=1, name="默认组织", code="default", status="active")
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = user
        org_result = MagicMock()
        org_result.scalar_one_or_none.return_value = org
        db = MagicMock()
        db.execute = AsyncMock(side_effect=[user_result, org_result])
        creds = MagicMock(credentials=_valid_token())
        result = await deps.get_current_user(db, creds)
        assert result.id == 1
        assert result.role == "metric_owner"

    async def test_org_suspended_blocks_login(self) -> None:
        """多租户隔离：所属组织 suspended → AuthError ORG_DISABLED（登录阻断）。"""
        user = _make_user()
        org = Organization(id=1, name="停用组织", code="sus", status="suspended")
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = user
        org_result = MagicMock()
        org_result.scalar_one_or_none.return_value = org
        db = MagicMock()
        db.execute = AsyncMock(side_effect=[user_result, org_result])
        creds = MagicMock(credentials=_valid_token())
        with pytest.raises(AuthError) as exc:
            await deps.get_current_user(db, creds)
        assert exc.value.error_code == "ORG_DISABLED"

    async def test_org_deleted_blocks_login(self) -> None:
        """多租户隔离：所属组织 deleted → AuthError ORG_DISABLED（登录阻断）。"""
        user = _make_user()
        org = Organization(id=1, name="已删组织", code="gone", status="deleted")
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = user
        org_result = MagicMock()
        org_result.scalar_one_or_none.return_value = org
        db = MagicMock()
        db.execute = AsyncMock(side_effect=[user_result, org_result])
        creds = MagicMock(credentials=_valid_token())
        with pytest.raises(AuthError) as exc:
            await deps.get_current_user(db, creds)
        assert exc.value.error_code == "ORG_DISABLED"

    async def test_blacklisted_token_raises_revoked(self) -> None:
        """登出撤销（jti 黑名单命中）的 token → AuthError AUTH_TOKEN_REVOKED（P0）。"""
        from app.core.security import blacklist_token

        await blacklist_token("test-jti-001", 600)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = _make_user()
        db = MagicMock()
        db.execute = AsyncMock(return_value=mock_result)
        creds = MagicMock(credentials=_valid_token())
        with pytest.raises(AuthError) as exc_info:
            await deps.get_current_user(db, creds)
        assert exc_info.value.error_code == "AUTH_TOKEN_REVOKED"
        db.execute.assert_not_awaited()  # 黑名单命中必须在查库前拦截


class TestRequireRoles:
    async def test_allowed_role_passes(self) -> None:
        user = _make_user(role="platform_admin")
        check = deps.require_roles("platform_admin", "domain_admin")
        result = await check(user)
        assert result is user

    async def test_denied_role_raises(self) -> None:
        user = _make_user(role="viewer")
        check = deps.require_roles("platform_admin", "domain_admin")
        with pytest.raises(AuthError):
            await check(user)
