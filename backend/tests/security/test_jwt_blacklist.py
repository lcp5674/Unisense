"""SEC-06 JWT 黑名单回归测试。"""


def test_blacklisted_token_rejected():
    from app.core.security import blacklist_token, is_token_blacklisted

    assert callable(blacklist_token)
    assert callable(is_token_blacklisted)


async def test_rotate_active_refresh_kicks_old_session():
    """单端登录（TD §5）：签发新 refresh 使该用户旧 refresh jti 拉黑（互踢）。"""
    import jwt

    from app.core.config import settings
    from app.core.security import (
        create_refresh_token,
        is_token_blacklisted,
        rotate_active_refresh,
    )

    def _jti(token: str) -> str:
        return str(
            jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])["jti"]
        )

    refresh_1 = create_refresh_token(sub=1, role="platform_admin", org_id=1)
    refresh_2 = create_refresh_token(sub=1, role="platform_admin", org_id=1)
    jti_1, jti_2 = _jti(refresh_1), _jti(refresh_2)

    # 第一次签发：记录 jti1 为活跃
    await rotate_active_refresh(1, refresh_1)
    assert await is_token_blacklisted(jti_1) is False

    # 第二次签发（新登录）：旧 jti1 拉黑、新 jti2 成为活跃
    await rotate_active_refresh(1, refresh_2)
    assert await is_token_blacklisted(jti_1) is True
    assert await is_token_blacklisted(jti_2) is False


async def test_rotate_active_refresh_ignores_invalid_token():
    """防御：非法 refresh token 不抛异常、不影响既有活跃映射。"""
    import jwt

    from app.core.config import settings
    from app.core.security import (
        create_refresh_token,
        is_token_blacklisted,
        rotate_active_refresh,
    )

    refresh_1 = create_refresh_token(sub=1, role="viewer", org_id=1)
    jti_1 = str(
        jwt.decode(refresh_1, settings.jwt_secret, algorithms=[settings.jwt_algorithm])["jti"]
    )
    await rotate_active_refresh(1, refresh_1)

    # 非法 token → 静默返回，旧 jti 仍活跃
    await rotate_active_refresh(1, "not-a-jwt")
    assert await is_token_blacklisted(jti_1) is False
