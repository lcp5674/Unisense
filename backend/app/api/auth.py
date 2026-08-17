"""鉴权 API：登录签发 JWT、登出撤销令牌、刷新令牌。

对齐 TD §5（鉴权）与 DEV_GUIDE §8b.1。
全局前缀由 main.py 注入（/api/v1），本路由前缀为 /auth。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, ok
from app.core.audit import client_ip, write_audit
from app.core.config import settings
from app.core.exceptions import AuthError
from app.core.guard import guard_against_injection
from app.core.login_throttle import is_login_blocked, record_login_failure, reset_login_failures
from app.core.security import (
    blacklist_token,
    create_access_token,
    create_refresh_token,
    is_token_blacklisted,
    rotate_active_refresh,
    verify_password,
)
from app.db.mysql import get_db_session
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """登录请求体。"""

    username: str = Field(..., min_length=1, description="用户名")
    password: str = Field(..., min_length=1, description="密码")


class TokenResponse(BaseModel):
    """登录成功响应（Bearer Token + 刷新令牌）。"""

    access_token: str = Field(..., description="JWT 访问令牌")
    token_type: str = Field(default="bearer", description="令牌类型")
    refresh_token: str = Field(default="", description="JWT 刷新令牌（7天有效，登录/刷新时签发）")
    must_change_password: bool = Field(default=False, description="首次登录/密码到期时需强制改密")


class RefreshRequest(BaseModel):
    """刷新令牌请求体。"""

    refresh_token: str = Field(..., min_length=1, description="刷新令牌")


class UserInfo(BaseModel):
    """当前用户基本信息。"""

    id: int
    username: str
    display_name: str
    role: str
    domain: str | None
    domain_name: str | None = None
    org_id: int
    org_name: str | None = None


class UserBrief(BaseModel):
    """只读用户摘要（Owner 责任链渲染用，绝不暴露 email/password_hash）。"""

    id: int
    username: str
    display_name: str
    role: str
    domain: str | None
    status: str


@router.post("/login", dependencies=[Depends(guard_against_injection)])
async def login(
    body: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[TokenResponse]:
    """用户名/密码登录，签发 JWT。

    Args:
        body: 登录凭证。
        request: 请求对象（取客户端 IP 参与限流键）。
        db: 数据库会话。

    Returns:
        包含 access_token 的统一信封。

    Raises:
        AuthError: 登录失败次数超限（AUTH_RATE_LIMITED）/ 用户不存在或密码错误
            （统一返回 AUTH_INVALID_CREDENTIALS）。
    """
    # 登录防撞库（TD §5）：按 username+IP 固定窗口限流失败次数，Redis 不可用降级内存，不阻断登录。
    remote_ip = request.client.host if request.client else ""
    throttle_key = f"{body.username}:{remote_ip}"
    if await is_login_blocked(throttle_key):
        raise AuthError("登录失败次数过多，请稍后再试", error_code="AUTH_RATE_LIMITED")

    result = await db.execute(
        select(User).where(User.username == body.username, User.status == "active")
    )
    user = result.scalar_one_or_none()

    # 用户不存在与密码错误返回相同错误码，避免用户枚举。
    if user is None or not await verify_password(body.password, user.password_hash):
        await record_login_failure(throttle_key)
        # 登录失败留痕（安全审计核心事件，GB/T 35273 认证事件要求）；
        # actor_id=0（无对应用户），entity_id 记录尝试的用户名，detail 区分凭据错误/锁定。
        await write_audit(
            db,
            actor_id=0,
            action="auth.login_failed",
            entity_type="user",
            entity_id=body.username,
            detail={"reason": "invalid_credentials", "username": body.username},
            ip=client_ip(request),
        )
        await db.commit()
        raise AuthError("用户名或密码错误", error_code="AUTH_INVALID_CREDENTIALS")

    await reset_login_failures(throttle_key)
    user.last_login_at = datetime.now(UTC)
    # 登录成功留痕：谁、何时、从哪 IP 登录（认证审计事件）。
    await write_audit(
        db,
        actor_id=user.id,
        action="auth.login",
        entity_type="user",
        entity_id=body.username,
        detail={"username": body.username},
        ip=client_ip(request),
    )
    await db.commit()

    token = create_access_token(sub=user.id, role=user.role, org_id=user.org_id)
    refresh = create_refresh_token(sub=user.id, role=user.role, org_id=user.org_id)
    # 单端登录（TD §5）：新登录使该用户旧会话的 refresh token 失效（互踢）。
    await rotate_active_refresh(user.id, refresh)
    return ok(
        TokenResponse(
            access_token=token,
            refresh_token=refresh,
            # 字段由用户模型提供（agent-users 并行接入），未就绪时默认 False，防御式读取。
            must_change_password=bool(getattr(user, "must_change_password", False)),
        )
    )


@router.get("/me")
async def me(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[UserInfo]:
    """查询当前登录用户基本信息（需 Bearer Token，含组织名 / 域名中文名回填）。"""
    from app.models.subject_domain import SubjectDomain
    from app.models.user import Organization

    org_name: str | None = None
    domain_name: str | None = None
    org = (
        await db.execute(select(Organization).where(Organization.id == user.org_id))
    ).scalar_one_or_none()
    if org is not None:
        org_name = org.name
    if user.domain:
        dom = (
            await db.execute(select(SubjectDomain).where(SubjectDomain.code == user.domain))
        ).scalar_one_or_none()
        if dom is not None:
            domain_name = dom.name
    return ok(
        UserInfo(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            domain=user.domain,
            domain_name=domain_name,
            org_id=user.org_id,
            org_name=org_name,
        )
    )


@router.get("/users")
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    role: str | None = Query(None, description="按角色过滤（可选）"),
    _: None = Depends(require_roles(*ALL_ROLES)),
) -> ApiResponse[list[UserBrief]]:
    """只读用户列表（Owner 责任链渲染用）。

    任意登录角色可读，仅返回基础字段（id/username/display_name/role/domain/status），
    不暴露 email 与 password_hash，避免敏感信息扩散。
    """
    stmt = select(User).order_by(User.id)
    if role:
        stmt = stmt.where(User.role == role)
    rows = (await db.execute(stmt)).scalars().all()
    return ok(
        [
            UserBrief(
                id=u.id,
                username=u.username,
                display_name=u.display_name,
                role=u.role,
                domain=u.domain,
                status=u.status,
            )
            for u in rows
        ]
    )


@router.post("/logout")
async def logout(
    user: CurrentUser,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[dict[str, str]]:
    """登出：将当前 JWT 的 jti 加入黑名单，并落登出审计。

    从请求 Bearer Token 中提取 jti，计算剩余有效期，加入黑名单。
    登出事件（auth.logout）与业务同事务原子提交。
    """
    jti, remaining_ttl = _decode_jti_and_ttl(request)
    await blacklist_token(jti, remaining_ttl)
    await write_audit(
        db,
        actor_id=user.id,
        action="auth.logout",
        entity_type="user",
        entity_id=str(user.id),
        detail={"username": user.username, "jti": jti[:12]},
        ip=client_ip(request),
    )
    await db.commit()
    return ok({"status": "logged_out", "jti": jti})


@router.post("/refresh", dependencies=[Depends(guard_against_injection)])
async def refresh(
    body: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[TokenResponse]:
    """刷新令牌：用 refresh token 换发新 access token + 新 refresh token（轮换）。

    校验 refresh token（type=refresh、未过期、未黑名单、用户存在且启用），
    将旧 refresh token 的 jti 加入黑名单实现轮换（防重放），
    签发新的 access token 与 refresh token。

    设计：access token 短效（jwt_expire_minutes），refresh token 长效（7天）。
    前端在访问令牌过期（401）时调用本端点无感续期，避免整页重登。
    """
    try:
        payload = jwt.decode(
            body.refresh_token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("刷新令牌已过期，请重新登录", error_code="AUTH_REFRESH_EXPIRED") from None
    except jwt.InvalidTokenError:
        raise AuthError("刷新令牌无效", error_code="AUTH_TOKEN_INVALID") from None

    # 仅接受 type=refresh 令牌，防止把 access token 当 refresh token 使用
    if payload.get("type") != "refresh":
        raise AuthError("刷新令牌无效", error_code="AUTH_TOKEN_INVALID")

    old_jti = str(payload.get("jti", ""))
    if await is_token_blacklisted(old_jti):
        raise AuthError("刷新令牌已失效，请重新登录", error_code="AUTH_REFRESH_REVOKED")

    user_id: int = int(payload.get("sub", 0))
    if user_id == 0:
        raise AuthError("刷新令牌无效", error_code="AUTH_TOKEN_INVALID")

    result = await db.execute(select(User).where(User.id == user_id, User.status == "active"))
    user = result.scalar_one_or_none()
    if user is None:
        raise AuthError("用户不存在或已禁用", error_code="AUTH_TOKEN_INVALID")

    # 轮换：旧 refresh token 加入黑名单（防重放），TTL = 剩余有效期
    remaining_ttl = max(int(payload.get("exp", 0)) - int(datetime.now(UTC).timestamp()), 0)
    await blacklist_token(old_jti, remaining_ttl)

    new_access = create_access_token(sub=user.id, role=user.role, org_id=user.org_id)
    new_refresh = create_refresh_token(sub=user.id, role=user.role, org_id=user.org_id)
    # 单端登录：刷新轮换后，把活跃 refresh 指向最新 jti（旧 jti 已被上方黑名单轮换拉黑）。
    await rotate_active_refresh(user_id, new_refresh)
    return ok(TokenResponse(access_token=new_access, refresh_token=new_refresh))


def _decode_jti_and_ttl(request: Request) -> tuple[str, int]:
    """从请求 Authorization header 中解码 JWT 提取 jti 和剩余 TTL 秒数。"""
    auth_header: str = request.headers.get("authorization", "")
    token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
    if not token:
        return "", settings.jwt_expire_minutes * 60

    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        jti: str = payload.get("jti", "")
        exp: int = payload.get("exp", 0)
        now = int(datetime.now(UTC).timestamp())
        remaining_ttl = max(exp - now, 0)
        return jti, remaining_ttl
    except jwt.InvalidTokenError:
        return "", settings.jwt_expire_minutes * 60
