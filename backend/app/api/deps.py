"""公共依赖（FastAPI Depends）。

提供数据库会话、Redis、当前用户、角色校验等公共依赖。
对齐 DEV_GUIDE §8b.1（API 层仅解析请求/调 service/格式化响应）。
"""

from __future__ import annotations

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
from app.models.user import Organization, User

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
    from app.core.security import is_token_blacklisted

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
        # 越权审查修复：JWT type 校验——refresh token（type=refresh，7 天有效）
        # 不得当作 access token 使用（否则 refresh 泄露 = 7 天全权访问，绕过
        # access 15 分钟短效设计）。access/consume token 无 type 字段（向后兼容）。
        if payload.get("type") == "refresh":
            raise AuthError(
                "刷新令牌不能直接访问业务接口，请通过 /auth/refresh 换取访问令牌",
                error_code="AUTH_TOKEN_TYPE_INVALID",
            )
    except jwt.ExpiredSignatureError:
        raise AuthError("Token 已过期", error_code="AUTH_TOKEN_EXPIRED") from None
    except jwt.InvalidTokenError:
        raise AuthError("Token 无效", error_code="AUTH_TOKEN_INVALID") from None

    # 登出撤销检查：jti 已进黑名单的 token 即便未过期也拒绝（P0）。
    jti = str(payload.get("jti", ""))
    if jti and await is_token_blacklisted(jti):
        raise AuthError("Token 已撤销，请重新登录", error_code="AUTH_TOKEN_REVOKED")

    result = await db.execute(
        select(User).where(User.id == user_id, User.status == "active")
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise AuthError("用户不存在或已禁用", error_code="AUTH_TOKEN_INVALID")

    # 多租户隔离（TD §4.1 organization）：所属组织停用/删除后禁止登录。
    org = (
        await db.execute(
            select(Organization).where(Organization.id == user.org_id)
        )
    ).scalar_one_or_none()
    if org is None:
        raise AuthError("所属组织已停用，无法登录", error_code="ORG_DISABLED")
    org_status = str(org.status.value if hasattr(org.status, "value") else org.status)
    if org_status in ("suspended", "deleted"):
        raise AuthError("所属组织已停用，无法登录", error_code="ORG_DISABLED")

    # 权限域动态继承（方案 B 增强）：挂载团队业务域，供 User.domains_all()
    # 取「团队继承 ∪ 显式指定」并集——团队改绑业务域后成员无需重新保存即自动生效。
    user._org_domain = org.domain
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*allowed_roles: str) -> Callable[..., Awaitable[User]]:
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
        ...

    Notes:
        自定义角色（方案 A：``user.role`` 为自定义角色名）在「任意登录用户可读」
        门禁（``allowed_roles == ALL_ROLES``）下放行——即自定义角色用户与内置角色
        一样可访问只读/参考类端点；管理类写端点仍须显式列出内置角色，自定义角色
        不会被默认放行（fail-closed，防提权）。
    """

    #: 任意登录用户可读门禁：仅当放行集合恰为 ALL_ROLES 时对自定义角色放行。
    is_any_active_gate = set(allowed_roles) == set(ALL_ROLES)

    async def _check_role(
        user: CurrentUser,
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> User:
        # S1（审查修复）：首次登录强制改密未完成前，禁止访问一切受角色门禁保护的
        # 端点（含写操作与业务读）。改密端点 /me/password 与 /me 走 CurrentUser，
        # 不受影响——前端据此引导跳转强制改密页。
        # 严格 is True：测试 mock 用户（MagicMock）无该字段时不应触发拦截。
        if getattr(user, "must_change_password", False) is True:
            raise AuthError(
                "首次登录须先修改初始密码，请前往修改密码后再继续操作",
                error_code="PASSWORD_CHANGE_REQUIRED",
            )
        # 方案 A 多角色：命中用户任意角色（主角色 user.role ∪ user_role 表）即放行。
        if any(r in allowed_roles for r in user.roles_all()):
            return user
        if is_any_active_gate:
            for r in user.roles_all():
                if await _is_custom_role(db, r):
                    return user
        raise AuthError(
            f"无权操作，需要角色: {', '.join(allowed_roles)}",
            error_code="FORBIDDEN",
            ctx={"user_role": user.roles_all()},
        )

    return _check_role


async def _is_custom_role(db: AsyncSession, role: str) -> bool:
    """判断角色名是否为已登记的自定义角色（``role`` 表 ``is_custom=True``）。

    仅用于「任意登录用户可读」门禁的放行判定；管理类写门禁不调用本函数。
    """
    from sqlalchemy import select

    from app.models.governance import Role

    row = (
        await db.execute(
            select(Role).where(
                Role.name == role,
                Role.is_custom.is_(True),
                Role.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return row is not None
