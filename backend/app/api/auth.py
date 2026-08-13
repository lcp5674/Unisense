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
from app.core.config import settings
from app.core.exceptions import AuthError
from app.core.guard import guard_against_injection
from app.core.security import (
    blacklist_token,
    create_access_token,
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
    """登录成功响应（Bearer Token）。"""

    access_token: str = Field(..., description="JWT 访问令牌")
    token_type: str = Field(default="bearer", description="令牌类型")


class UserInfo(BaseModel):
    """当前用户基本信息。"""

    id: int
    username: str
    display_name: str
    role: str
    domain: str | None
    org_id: int


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
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[TokenResponse]:
    """用户名/密码登录，签发 JWT。

    Args:
        body: 登录凭证。
        db: 数据库会话。

    Returns:
        包含 access_token 的统一信封。

    Raises:
        AuthError: 用户不存在/已禁用/密码错误（统一返回 AUTH_INVALID_CREDENTIALS）。
    """
    result = await db.execute(
        select(User).where(User.username == body.username, User.status == "active")
    )
    user = result.scalar_one_or_none()

    # 用户不存在与密码错误返回相同错误码，避免用户枚举。
    if user is None or not await verify_password(body.password, user.password_hash):
        raise AuthError("用户名或密码错误", error_code="AUTH_INVALID_CREDENTIALS")

    user.last_login_at = datetime.now(UTC)
    await db.commit()

    token = create_access_token(sub=user.id, role=user.role, org_id=user.org_id)
    return ok(TokenResponse(access_token=token))


@router.get("/me")
async def me(user: CurrentUser) -> ApiResponse[UserInfo]:
    """查询当前登录用户基本信息（需 Bearer Token）。"""
    return ok(
        UserInfo(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            domain=user.domain,
            org_id=user.org_id,
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
) -> ApiResponse[dict[str, str]]:
    """登出：将当前 JWT 的 jti 加入黑名单。

    从请求 Bearer Token 中提取 jti，计算剩余有效期，加入黑名单。
    """
    jti, remaining_ttl = _decode_jti_and_ttl(request)
    await blacklist_token(jti, remaining_ttl)
    return ok({"status": "logged_out", "jti": jti})


@router.post("/refresh")
async def refresh(
    user: CurrentUser,
    request: Request,
) -> ApiResponse[TokenResponse]:
    """刷新令牌：黑名单旧 jti，签发新 JWT。

    接受当前有效 JWT，将其 jti 加入黑名单，签发新令牌。
    """
    old_jti, remaining_ttl = _decode_jti_and_ttl(request)
    await blacklist_token(old_jti, remaining_ttl)

    new_token = create_access_token(sub=user.id, role=user.role, org_id=user.org_id)
    return ok(TokenResponse(access_token=new_token))


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
