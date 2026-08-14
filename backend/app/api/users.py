"""用户管理 API（TD §3.4 用户管理，平台管理员专属）。

端点：

- GET    /users                       用户管理列表（含 email / 最后登录 / 创建时间）
- POST   /users                       创建用户
- POST   /users/batch-status          批量启用 / 禁用（207 语义：逐项标注失败，不影响其余）
- PUT    /users/{user_id}             编辑用户（显示名 / 邮箱 / 角色 / 域）
- PATCH  /users/{user_id}/status      启用 / 禁用
- POST   /users/{user_id}/reset-password  重置密码

鉴权：全部端点仅 ``platform_admin``（账号生命周期管理属平台级操作）。
审计：全部写操作落 ``audit_log``（action=USER_CREATE/USER_UPDATE/USER_STATUS/USER_STATUS_BATCH/
USER_RESET_PASSWORD）。
响应绝不暴露 ``password_hash``；错误码对齐 TD §5.4（CONFLICT / NOT_FOUND / VALIDATION_ERROR）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.guard import guard_against_injection
from app.core.security import hash_password
from app.db.mysql import get_db_session
from app.models.subject_domain import SubjectDomain
from app.models.user import User

router = APIRouter(prefix="/users", tags=["users"])

#: 平台可授予的全部内置角色（对齐 User.role Enum 7 值）。
UserRole = Literal[
    "platform_admin",
    "domain_admin",
    "metric_owner",
    "reviewer",
    "compliance_officer",
    "analyst",
    "viewer",
]

#: 管理端点依赖：仅平台管理员 + 注入守卫（纵深防御，ORM 参数化兜底之外拦截注入 payload）。
_ADMIN_DEPS = [Depends(require_roles("platform_admin")), Depends(guard_against_injection)]


class UserAdmin(BaseModel):
    """用户管理视图（管理端）。绝不暴露 ``password_hash``。

    Attributes:
        id: 用户 ID。
        username: 用户名。
        email: 邮箱。
        display_name: 显示名称。
        role: 角色。
        domain: 所属域。
        status: 状态（active/disabled/deleted）。
        last_login_at: 最后登录时间。
        created_at: 创建时间。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    display_name: str
    role: str
    domain: str | None
    status: str
    last_login_at: str | None = None
    created_at: str | None = None


class UserCreateRequest(BaseModel):
    """创建用户请求体。"""

    username: str = Field(
        ..., min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$", description="用户名"
    )
    email: str = Field(
        ...,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        max_length=128,
        description="邮箱（全局唯一）",
    )
    display_name: str = Field(..., min_length=1, max_length=128, description="显示名称")
    role: UserRole = Field(default="viewer", description="角色")
    domain: str | None = Field(default=None, max_length=64, description="所属域")
    password: str = Field(..., min_length=8, max_length=128, description="初始密码")


class UserUpdateRequest(BaseModel):
    """编辑用户请求体（全量覆盖：显示名 / 邮箱 / 角色 / 域）。

    ``domain`` 传空串或 null 表示清空归属域；不参与编辑的字段不应传入。
    """

    display_name: str = Field(..., min_length=1, max_length=128, description="显示名称")
    email: str = Field(
        ...,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        max_length=128,
        description="邮箱（全局唯一）",
    )
    role: UserRole = Field(..., description="角色")
    domain: str | None = Field(default=None, max_length=64, description="所属域（可空）")


class UserStatusRequest(BaseModel):
    """启用 / 禁用请求体。"""

    status: Literal["active", "disabled"] = Field(..., description="目标状态")


class UserBatchStatusItem(BaseModel):
    """批量启用 / 禁用单项结果（207 语义：逐项标注失败原因）。"""

    user_id: int
    username: str | None = None
    ok: bool
    error_code: str | None = None
    message: str | None = None


class UserBatchStatusResult(BaseModel):
    """批量启用 / 禁用汇总结果（对齐 BatchSourceResult 的 207 模式）。"""

    succeeded: list[UserBatchStatusItem]
    failed: list[UserBatchStatusItem]


class UserBatchStatusRequest(BaseModel):
    """批量启用 / 禁用请求体（上限 200，空列表由 min_length 拒绝）。"""

    user_ids: list[int] = Field(min_length=1, max_length=200, description="目标用户 ID 列表")
    status: Literal["active", "disabled"] = Field(..., description="目标状态")


class ResetPasswordRequest(BaseModel):
    """重置密码请求体。"""

    new_password: str = Field(..., min_length=8, max_length=128, description="新密码")


async def _get_user(db: AsyncSession, user_id: int) -> User | None:
    """按 ID 取用户（含软删记录，便于管理端查看/恢复）。"""
    res = await db.execute(select(User).where(User.id == user_id))
    return res.scalar_one_or_none()


async def _assert_unique(
    db: AsyncSession,
    *,
    username: str,
    email: str,
    exclude_id: int | None = None,
) -> None:
    """校验用户名 / 邮箱唯一性（排除自身后），冲突抛 409。"""
    stmt = select(User).where(or_(User.username == username, User.email == email))
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    dup = (await db.execute(stmt)).scalar_one_or_none()
    if dup is not None:
        field = "用户名" if dup.username == username else "邮箱"
        raise ConflictError(
            f"{field}已被占用（{dup.username}）", error_code="USER_EXISTS", ctx={"user_id": dup.id}
        )


async def _assert_domain_active(db: AsyncSession, domain: str | None) -> None:
    """校验所属域：若提供，必须是存在且 active 的主题域 code。

    与前端「主题域管理」下拉数据源（``list_domain_tree(status=active)``）保持一致，
    防止绕过 UI 直接写接口注入任意域值（对齐 TD §3.4，错误码见 TD §5.4）。
    """
    if not domain:
        return
    row = (
        await db.execute(
            select(SubjectDomain).where(
                SubjectDomain.code == domain,
                SubjectDomain.status == "active",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValidationError(
            f"所属域不存在或未启用: {domain}",
            error_code="USER_DOMAIN_INVALID",
            ctx={"domain": domain},
        )


def _to_admin(row: User) -> UserAdmin:
    """ORM → 管理视图（created_at 序列化为 ISO 字符串）。"""
    return UserAdmin(
        id=row.id,
        username=row.username,
        email=row.email,
        display_name=row.display_name,
        role=row.role.value if hasattr(row.role, "value") else row.role,
        domain=row.domain,
        status=row.status,
        last_login_at=row.last_login_at.isoformat() if row.last_login_at else None,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


@router.get("", dependencies=_ADMIN_DEPS)
async def list_admin_users(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    role: str | None = Query(None, description="按角色过滤"),
    status: str | None = Query(None, description="按状态过滤（active/disabled/deleted）"),
    keyword: str | None = Query(None, description="按用户名/显示名/邮箱模糊"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页条数"),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, Any]]:
    """用户管理列表（分页 + 过滤，含邮箱与时间字段）。"""
    base = select(User)
    if role:
        base = base.where(User.role == role)
    if status:
        base = base.where(User.status == status)
    if keyword:
        like = f"%{keyword}%"
        base = base.where(
            or_(
                User.username.ilike(like),
                User.display_name.ilike(like),
                User.email.ilike(like),
            )
        )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    stmt = (
        base.order_by(User.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return ok(
        {
            "total": int(total),
            "page": page,
            "page_size": page_size,
            "items": [_to_admin(r) for r in rows],
        },
        trace_id=trace_id,
    )


@router.post("", dependencies=_ADMIN_DEPS)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[UserAdmin]:
    """创建用户（platform_admin 专属，全部落审计）。

    校验用户名/邮箱唯一，初始密码经 bcrypt 哈希落库。
    """
    await _assert_unique(db, username=payload.username, email=payload.email)
    await _assert_domain_active(db, payload.domain)
    row = User(
        org_id=user.org_id,
        username=payload.username,
        email=payload.email,
        display_name=payload.display_name,
        role=payload.role,
        domain=payload.domain or None,
        status="active",
        password_hash=await hash_password(payload.password),
    )
    db.add(row)
    await db.flush()
    await write_audit(
        db,
        actor_id=user.id,
        action="USER_CREATE",
        entity_type="user",
        entity_id=str(row.id),
        detail={
            "username": row.username,
            "display_name": row.display_name,
            "role": row.role,
            "domain": row.domain,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await db.refresh(row)
    return ok(_to_admin(row), trace_id=trace_id)


@router.post("/batch-status", dependencies=_ADMIN_DEPS)
async def batch_set_user_status(
    payload: UserBatchStatusRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[UserBatchStatusResult]:
    """批量启用 / 禁用用户（207 语义：单条失败逐项标注，不影响其余）。

    与单条 ``PATCH /users/{id}/status`` 同口径（存在校验 + 禁止自禁）；
    批量禁用包含当前登录账号时，该项单独标注失败，其余照常更新。
    一次提交统一落一条 ``USER_STATUS_BATCH`` 审计（含成败计数）。
    注意：本端点须注册在 ``/{{user_id}}`` 系列之前（FastAPI 按注册顺序匹配）。
    """
    rows = (await db.execute(select(User).where(User.id.in_(payload.user_ids)))).scalars().all()
    by_id = {r.id: r for r in rows}

    succeeded: list[UserBatchStatusItem] = []
    failed: list[UserBatchStatusItem] = []
    for uid in payload.user_ids:
        row = by_id.get(uid)
        if row is None:
            failed.append(
                UserBatchStatusItem(
                    user_id=uid, ok=False, error_code="USER_NOT_FOUND", message="用户不存在"
                )
            )
            continue
        if row.id == user.id and payload.status == "disabled":
            failed.append(
                UserBatchStatusItem(
                    user_id=uid,
                    username=row.username,
                    ok=False,
                    error_code="SELF_DISABLE_FORBIDDEN",
                    message="不能禁用当前登录的账号",
                )
            )
            continue
        row.status = payload.status
        succeeded.append(
            UserBatchStatusItem(
                user_id=uid,
                username=row.username,
                ok=True,
                message="已启用" if payload.status == "active" else "已禁用",
            )
        )

    await write_audit(
        db,
        actor_id=user.id,
        action="USER_STATUS_BATCH",
        entity_type="user",
        entity_id=f"items:{len(payload.user_ids)}",
        detail={"status": payload.status, "succeeded": len(succeeded), "failed": len(failed)},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(UserBatchStatusResult(succeeded=succeeded, failed=failed), trace_id=trace_id)


@router.put("/{user_id}", dependencies=_ADMIN_DEPS)
async def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[UserAdmin]:
    """编辑用户（显示名/邮箱/角色/域，全量覆盖）。

    自我保护：不能降级自己的平台管理员角色（防止自锁失去管理权）。
    """
    row = await _get_user(db, user_id)
    if row is None:
        raise NotFoundError("用户不存在", error_code="USER_NOT_FOUND")
    await _assert_unique(db, username=row.username, email=payload.email, exclude_id=row.id)
    await _assert_domain_active(db, payload.domain)
    if row.id == user.id and payload.role != "platform_admin":
        raise ValidationError(
            "不能降级当前登录的平台管理员角色", error_code="SELF_DEMOTE_FORBIDDEN"
        )

    row.display_name = payload.display_name
    row.email = payload.email
    row.role = payload.role
    row.domain = payload.domain or None
    await write_audit(
        db,
        actor_id=user.id,
        action="USER_UPDATE",
        entity_type="user",
        entity_id=str(row.id),
        detail={
            "username": row.username,
            "display_name": row.display_name,
            "role": row.role,
            "domain": row.domain,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await db.refresh(row)
    return ok(_to_admin(row), trace_id=trace_id)


@router.patch("/{user_id}/status", dependencies=_ADMIN_DEPS)
async def set_user_status(
    user_id: int,
    payload: UserStatusRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[UserAdmin]:
    """启用 / 禁用用户。

    自我保护：不能禁用当前登录账号（防止自锁）。
    """
    row = await _get_user(db, user_id)
    if row is None:
        raise NotFoundError("用户不存在", error_code="USER_NOT_FOUND")
    if row.id == user.id and payload.status == "disabled":
        raise ValidationError("不能禁用当前登录的账号", error_code="SELF_DISABLE_FORBIDDEN")

    row.status = payload.status
    await write_audit(
        db,
        actor_id=user.id,
        action="USER_STATUS",
        entity_type="user",
        entity_id=str(row.id),
        detail={"username": row.username, "status": row.status},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await db.refresh(row)
    return ok(_to_admin(row), trace_id=trace_id)


@router.post("/{user_id}/reset-password", dependencies=_ADMIN_DEPS)
async def reset_password(
    user_id: int,
    payload: ResetPasswordRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """重置用户密码（bcrypt 哈希落库，不返回明文）。"""
    row = await _get_user(db, user_id)
    if row is None:
        raise NotFoundError("用户不存在", error_code="USER_NOT_FOUND")

    row.password_hash = await hash_password(payload.new_password)
    await write_audit(
        db,
        actor_id=user.id,
        action="USER_RESET_PASSWORD",
        entity_type="user",
        entity_id=str(row.id),
        detail={"username": row.username},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok({"user_id": row.id, "ok": True}, trace_id=trace_id)
