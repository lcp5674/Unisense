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
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, ok
from app.core.audit import client_ip, write_audit
from app.core.config import settings
from app.core.exceptions import AuthError
from app.core.guard import guard_against_injection
from app.core.logging import get_logger
from app.core.login_throttle import (
    is_account_blocked,
    is_ip_blocked,
    is_login_blocked,
    record_account_failure,
    record_ip_failure,
    record_login_failure,
    reset_account_failures,
    reset_ip_failures,
    reset_login_failures,
)
from app.core.middleware import _client_key  # noqa: PLC2701 - 限流键解析（可信代理）复用
from app.core.security import (
    blacklist_token,
    create_access_token,
    create_refresh_token,
    is_token_blacklisted,
    revoke_active_refresh,
    rotate_active_refresh,
    verify_password,
)
from app.db.mysql import get_db_session
from app.models.user import User, UserRole

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger("unisense.auth.api")


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
    #: 首次登录/密码到期时需强制改密（前端据此在登录后渲染全屏改密守卫，
    #: 未改密前不进入业务路由；与登录响应 TokenResponse.must_change_password 同源）。
    must_change_password: bool = False


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
    # S5（审查修复）：双桶——组合桶（username+IP）防单账号爆破 + IP 级独立桶防换账号轰炸；
    # 客户端 IP 经 trusted_proxies 解析（反代后取真实 IP，避免所有用户同 IP 退化）。
    remote_ip = _client_key(request)
    throttle_key = f"{body.username}:{remote_ip}"
    # S9（审查修复）：三桶——组合桶（username+IP）+ IP 级桶 + 账号维度桶（纯
    # username 跨 IP 累计），杜绝换 IP 慢速撞库打同一账号。
    if (
        await is_login_blocked(throttle_key)
        or await is_ip_blocked(remote_ip)
        or await is_account_blocked(body.username)
    ):
        raise AuthError("登录失败次数过多，请稍后再试", error_code="AUTH_RATE_LIMITED")

    result = await db.execute(
        select(User).where(User.username == body.username, User.status == "active")
    )
    user = result.scalar_one_or_none()

    # 用户不存在与密码错误返回相同错误码，避免用户枚举。
    if user is None or not await verify_password(body.password, user.password_hash):
        await record_login_failure(throttle_key)
        await record_ip_failure(remote_ip)
        await record_account_failure(body.username)
        # 登录失败留痕（安全审计核心事件，GB/T 35273 认证事件要求）。
        # X-4：actor_id 置 None（无对应用户，audit_log.actor_id 已改可空）——
        # 此前 actor_id=0 触发 FK 违规，失败登录返回 500 且失败审计丢失。
        # 审计写入本身也做兜底：异常仅告警，绝不把认证失败变成 5xx。
        try:
            await write_audit(
                db,
                actor_id=None,
                action="auth.login_failed",
                entity_type="user",
                entity_id=body.username,
                detail={"reason": "invalid_credentials", "username": body.username},
                ip=client_ip(request),
            )
            await db.commit()
        except Exception:  # noqa: BLE001 - 审计失败不阻断 401（认证主流程优先）
            logger.warning("login_failed_audit_error", username=body.username, exc_info=True)
        raise AuthError("用户名或密码错误", error_code="AUTH_INVALID_CREDENTIALS")

    await reset_login_failures(throttle_key)
    await reset_ip_failures(remote_ip)
    await reset_account_failures(body.username)
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
            must_change_password=bool(getattr(user, "must_change_password", False)),
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
    多租户隔离：非平台管理员仅返回本组织用户（跨组织不枚举用户目录）。
    """
    stmt = select(User).order_by(User.id)
    # 多租户隔离（S1）：非平台管理员仅见本组织用户，防跨组织枚举用户目录
    if not user.has_role("platform_admin"):
        stmt = stmt.where(User.org_id == user.org_id)
    if role:
        # 方案 A 多角色：主角色或 user_role 扩展角色命中任一即计入。
        stmt = stmt.where(or_(User.role == role, User.role_items.any(UserRole.role == role)))
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
    """登出：吊销当前会话的 access + refresh token，并落登出审计。

    从请求 Bearer Token 中提取 jti（access token），计算剩余有效期，加入黑名单；
    同时吊销该用户当前活跃的 refresh token（``revoke_active_refresh``）——
    登出后被劫持/残留的 refresh 无法再续期 access token（会话吊销闭环）。
    登出事件（auth.logout）与业务同事务原子提交。
    """
    jti, remaining_ttl = _decode_jti_and_ttl(request)
    await blacklist_token(jti, remaining_ttl)
    await revoke_active_refresh(user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="auth.logout",
        entity_type="user",
        entity_id=str(user.id),
        detail={"username": user.username, "jti": jti[:12], "refresh_revoked": True},
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

    # S9（审查修复）：刷新须校验所属组织状态——停用/删除组织的用户不应能换发
    # 新 access（对齐 get_current_user 的 ORG_DISABLED 拦截；此前仅查用户 active，
    # 停用组织用户仍能续期，后续请求才被 deps 拒绝，属不一致）。
    from app.models.user import Organization

    org = (
        await db.execute(select(Organization).where(Organization.id == user.org_id))
    ).scalar_one_or_none()
    if org is None:
        raise AuthError("所属组织已停用，无法登录", error_code="ORG_DISABLED")
    org_status = str(org.status.value if hasattr(org.status, "value") else org.status)
    if org_status in ("suspended", "deleted"):
        raise AuthError("所属组织已停用，无法登录", error_code="ORG_DISABLED")

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
