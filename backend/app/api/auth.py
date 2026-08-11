"""鉴权 API：登录签发 JWT、查询当前用户。

对齐 TD §5（鉴权）与 DEV_GUIDE §8b.1。
全局前缀由 main.py 注入（/api/v1），本路由前缀为 /auth。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.api.responses import ApiResponse, ok
from app.core.exceptions import AuthError
from app.core.security import create_access_token, verify_password
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


@router.post("/login")
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
