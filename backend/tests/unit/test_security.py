"""core/security 单测（补齐覆盖率至 core≥90% 门槛）。

覆盖：
- hash_password：bcrypt 哈希非明文、可被 verify_password 校验。
- verify_password：正确/错误密码、非法哈希格式（ValueError/TypeError）降级 False。
- create_access_token：默认过期时间、自定义过期时间、payload 字段（sub/role/org_id/iat/exp）、
  使用 settings.jwt_algorithm 签名。
"""

from __future__ import annotations

from datetime import UTC, datetime

import jwt
import pytest

from app.core.config import settings
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password,
)


class TestHashPassword:
    async def test_hash_is_not_plaintext(self) -> None:
        hashed = await hash_password("s3cret-pass")
        assert hashed != "s3cret-pass"
        assert hashed.startswith("$2")  # bcrypt 前缀

    async def test_hash_differs_for_same_password(self) -> None:
        # bcrypt 自动加盐：同一明文两次哈希结果不同
        assert await hash_password("same") != await hash_password("same")


class TestVerifyPassword:
    async def test_verify_correct_password(self) -> None:
        hashed = await hash_password("correct-horse")
        assert await verify_password("correct-horse", hashed) is True

    async def test_verify_wrong_password(self) -> None:
        hashed = await hash_password("right")
        assert await verify_password("wrong", hashed) is False

    async def test_verify_invalid_hash_returns_false(self) -> None:
        # 非法哈希格式（ValueError）→ 降级 False 而非抛异常
        assert await verify_password("x", "not-a-bcrypt-hash") is False

    async def test_verify_empty_hash_returns_false(self) -> None:
        # 空哈希（TypeError 路径）→ 降级 False
        assert await verify_password("x", "") is False


class TestCreateAccessToken:
    def test_token_uses_default_expire_minutes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "jwt_expire_minutes", 60)
        token = create_access_token(sub=7, role="platform_admin", org_id=1)
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        assert payload["sub"] == "7"
        assert payload["role"] == "platform_admin"
        assert payload["org_id"] == 1
        # exp - iat ≈ 60 分钟
        assert payload["exp"] - payload["iat"] == 60 * 60

    def test_token_uses_custom_expire_minutes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "jwt_expire_minutes", 60)
        token = create_access_token(sub=9, role="viewer", org_id=3, expire_minutes=5)
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        assert payload["exp"] - payload["iat"] == 5 * 60

    def test_token_payload_contains_iat_and_exp(self) -> None:
        before = int(datetime.now(UTC).timestamp())
        token = create_access_token(sub=1, role="analyst", org_id=2)
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        assert before <= payload["iat"] <= int(datetime.now(UTC).timestamp())
        assert payload["exp"] > payload["iat"]

    def test_token_roundtrip_with_security_module(self) -> None:
        # 保证 create_access_token 产出的 token 可由 security 自身依赖的 jwt 库解析
        token = create_access_token(sub="u-42", role="metric_owner", org_id=0)
        decoded = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        assert decoded["sub"] == "u-42"


class TestRevokeActiveRefresh:
    """登出吊销 refresh token（内存降级路径验证）。"""

    async def test_revoke_blacklists_and_clears_memory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.core.security as sec
        import app.db.redis as db_redis

        # 强制走内存降级（Redis 不可用）——security 内延迟导入 app.db.redis.get_redis
        def _boom(*args, **kwargs):  # noqa: ANN202
            raise RuntimeError("redis unavailable")

        monkeypatch.setattr(db_redis, "get_redis", _boom)
        # 预置活跃 refresh 映射
        user_id = 4242
        sec._memory_active_refresh[user_id] = ("jti-active-1", 9999999999)
        try:
            await sec.revoke_active_refresh(user_id)
            # 映射被清空
            assert user_id not in sec._memory_active_refresh
            # jti 已进黑名单
            assert await sec.is_token_blacklisted("jti-active-1") is True
        finally:
            sec._memory_active_refresh.pop(user_id, None)
            sec._memory_blacklist.pop("jti-active-1", None)

    async def test_revoke_no_active_refresh_is_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.core.security as sec
        import app.db.redis as db_redis

        def _boom(*args, **kwargs):  # noqa: ANN202
            raise RuntimeError("redis unavailable")

        monkeypatch.setattr(db_redis, "get_redis", _boom)
        # 无活跃 refresh（未预置）→ 不抛异常、黑名单不变
        await sec.revoke_active_refresh(999)
        assert "no-such-jti" not in sec._memory_blacklist
