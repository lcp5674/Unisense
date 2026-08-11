"""公共依赖（FastAPI Depends）。

提供数据库会话、Redis、当前用户、角色校验等公共依赖。
对齐 DEV_GUIDE §8b.1（API 层仅解析请求/调 service/格式化响应）。
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from typing import Annotated

import redis.asyncio as aioredis
import structlog
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthError
from app.db.mysql import get_db_session
from app.db.redis import get_redis
from app.models.user import User

#: 全部已定义角色：任何已登录用户均可读参考/目录类数据（列表与查询端点统一授权）。
ALL_ROLES = (
    "platform_admin",
    "domain_admin",
    "metric_owner",
    "reviewer",
    "compliance_officer",
    "analyst",
    "viewer",
)

logger = structlog.get_logger("unisense.deps")

security = HTTPBearer(auto_error=False)

DBSession = Annotated[AsyncSession, Depends(get_db_session)]
RedisClient = Annotated[aioredis.Redis, Depends(get_redis)]


async def get_current_user(
    db: DBSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> User:
    """从 JWT Bearer Token 解析当前用户。

    Args:
        db: 数据库会话。
        credentials: Bearer Token 凭证。

    Returns:
        当前登录用户。

    Raises:
        AuthError: Token 缺失/过期/无效或用户不存在。
    """
    import jwt

    from app.core.config import settings

    if credentials is None:
        raise AuthError("请求未携带 Bearer Token", error_code="AUTH_TOKEN_MISSING")

    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        user_id: int = int(payload.get("sub", 0))
        if user_id == 0:
            raise AuthError("Token 无效", error_code="AUTH_TOKEN_INVALID")
    except jwt.ExpiredSignatureError:
        raise AuthError("Token 已过期", error_code="AUTH_TOKEN_EXPIRED") from None
    except jwt.InvalidTokenError:
        raise AuthError("Token 无效", error_code="AUTH_TOKEN_INVALID") from None

    result = await db.execute(select(User).where(User.id == user_id, User.status == "active"))
    user = result.scalar_one_or_none()
    if user is None:
        raise AuthError("用户不存在或已禁用", error_code="AUTH_TOKEN_INVALID")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*allowed_roles: str) -> Callable[[CurrentUser], Awaitable[User]]:
    """角色校验依赖工厂。

    Args:
        allowed_roles: 允许的角色列表。

    Returns:
        依赖函数。

    Examples:
        >>> @router.post(
        ...     "/metrics",
        ...     dependencies=[Depends(require_roles("platform_admin", "domain_admin"))],
        ... )
    """

    async def _check_role(user: CurrentUser) -> User:
        role_val = user.role.value if isinstance(user.role, enum.Enum) else user.role
        if role_val not in allowed_roles:
            raise AuthError(
                f"无权操作，需要角色: {', '.join(allowed_roles)}",
                error_code="FORBIDDEN",
                ctx={"user_role": user.role},
            )
        return user

    return _check_role
