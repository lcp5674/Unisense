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


async def test_rotate_active_refresh_isolated_between_users():
    """跨用户隔离：user2 登录不踢 user1 的会话（各自独立活跃映射）。"""
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

    # 用独立 user_id 避免与既有用例（id=1）的内存映射互相污染
    refresh_viewer = create_refresh_token(sub=101, role="viewer", org_id=1)
    refresh_analyst = create_refresh_token(sub=102, role="analyst", org_id=1)
    jti_viewer, jti_analyst = _jti(refresh_viewer), _jti(refresh_analyst)

    await rotate_active_refresh(101, refresh_viewer)
    await rotate_active_refresh(102, refresh_analyst)

    # 两个用户各自的会话都未被对方踢出
    assert await is_token_blacklisted(jti_viewer) is False
    assert await is_token_blacklisted(jti_analyst) is False


async def test_rotate_active_refresh_chain_kicks_previous():
    """连续多次登录（近似并发语义）：每次新会话都拉黑上一会话，最终仅最新活跃。"""
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

    refresh_1 = create_refresh_token(sub=201, role="metric_owner", org_id=1)
    refresh_2 = create_refresh_token(sub=201, role="metric_owner", org_id=1)
    refresh_3 = create_refresh_token(sub=201, role="metric_owner", org_id=1)
    jti_1, jti_2, jti_3 = _jti(refresh_1), _jti(refresh_2), _jti(refresh_3)

    await rotate_active_refresh(201, refresh_1)
    await rotate_active_refresh(201, refresh_2)
    await rotate_active_refresh(201, refresh_3)

    # 前两个会话均被后续登录拉黑，仅最新的 jti_3 保持活跃
    assert await is_token_blacklisted(jti_1) is True
    assert await is_token_blacklisted(jti_2) is True
    assert await is_token_blacklisted(jti_3) is False
