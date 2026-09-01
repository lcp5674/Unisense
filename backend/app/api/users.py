"""用户管理 API（TD §3.4 用户管理，平台管理员专属）。

端点：

- GET    /users                       用户管理列表（含 email / 最后登录 / 创建时间）
- POST   /users                       创建用户
- POST   /users/batch-status          批量启用 / 禁用（207 语义：逐项标注失败，不影响其余）
- POST   /users/me/password           自助改密（任意登录角色，校验旧密码 + 新密码复杂度）
- PUT    /users/{user_id}             编辑用户（显示名 / 邮箱 / 角色 / 域）
- PATCH  /users/{user_id}/status      启用 / 禁用
- POST   /users/{user_id}/reset-password  重置密码

鉴权：除自助改密（任意登录用户）外，全部端点仅 ``platform_admin``（账号生命周期管理属平台级操作）。
审计：全部写操作落 ``audit_log``（action=USER_CREATE/USER_UPDATE/USER_STATUS/USER_STATUS_BATCH/
USER_RESET_PASSWORD/USER_PASSWORD_CHANGE）。
响应绝不暴露 ``password_hash``；错误码对齐 TD §5.4（CONFLICT / NOT_FOUND / VALIDATION_ERROR）。
密码策略：创建/重置/自助改密统一校验复杂度（≥8 位且含大写/小写/数字/特殊字符至少 3 类，
错误码 PASSWORD_WEAK）；管理员创建/重置后置 ``must_change_password=True`` 强制首登改密。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.exceptions import AuthError, ConflictError, NotFoundError, ValidationError
from app.core.guard import guard_against_injection
from app.core.security import hash_password, verify_password
from app.core.totp import (
    decrypt_secret,
    encrypt_secret,
    generate_totp_secret,
    totp_uri,
    verify_totp,
)
from app.db.mysql import get_db_session
from app.models.subject_domain import SubjectDomain
from app.models.user import Organization, User, UserRole


def _escape_like(text: str) -> str:
    """转义 LIKE 通配符（S5 审查修复）：用户输入 ``%``/``_`` 会放大匹配面/慢查询。

    用 ``/`` 作转义符（转义 //、/% 和 /_），配合 ``ilike(..., escape="/")`` 生效
    （与 global_search/collector 的既有标准一致）。
    """
    return text.replace("/", "//").replace("%", "/%").replace("_", "/_")

router = APIRouter(prefix="/users", tags=["users"])

logger = logging.getLogger("unisense.users.api")

#: 平台可授予的全部内置角色（对齐 User.role 历史 7 值；自定义角色经 role 表校验）。
#: 注：``role`` 字段已放宽为 str（方案 A），内置角色名保持历史值不变。
BUILTIN_ROLES: tuple[str, ...] = (
    "platform_admin",
    "domain_admin",
    "metric_owner",
    "reviewer",
    "compliance_officer",
    "analyst",
    "viewer",
)

#: 管理端点依赖：仅平台管理员 + 注入守卫（纵深防御，ORM 参数化兜底之外拦截注入 payload）。
_ADMIN_DEPS = [Depends(require_roles("platform_admin")), Depends(guard_against_injection)]

#: 角色优先级（数字越小越优先）：主角色（user.role）取权限最高者，向后兼容
#: 所有既有单角色读取（Owner 责任链 / 评审指派 / PDP 主角色决策）。
#: 排序依据内置角色默认权限覆盖广度（ROLE_UI_ACTIONS）：域管理 > 指标负责 > 评审 > 合规。
#: 自定义角色无内置优先级，统一按最低（100）处理。
_ROLE_PRIORITY: dict[str, int] = {
    "platform_admin": 0,
    "domain_admin": 1,
    "metric_owner": 2,
    "reviewer": 3,
    "compliance_officer": 4,
    "analyst": 5,
    "viewer": 6,
}


def _resolve_primary_role(roles: list[str]) -> str:
    """从角色列表解析主角色：优先级最高（数字最小）者；自定义角色恒最后。"""
    return min(roles, key=lambda r: _ROLE_PRIORITY.get(r, 100))


def _normalize_roles(roles: list[str] | None, fallback: str) -> list[str]:
    """归一化角色列表：缺省回退为主角色；去重并保持首个出现顺序。"""
    source = roles if roles else [fallback]
    return list(dict.fromkeys(source))


class UserAdmin(BaseModel):
    """用户管理视图（管理端）。绝不暴露 ``password_hash``。

    Attributes:
        id: 用户 ID。
        username: 用户名。
        email: 邮箱。
        display_name: 显示名称。
        role: 主角色（权限最高者）。
        roles: 全部角色（主角色在前，含 user_role 扩展，方案 A 多角色）。
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
    roles: list[str] = Field(default_factory=list, description="全部角色（主角色在前）")
    domain: str | None
    org_id: int | None = None
    org_name: str | None = None
    status: str
    last_login_at: str | None = None
    created_at: str | None = None
    totp_enabled: bool = Field(default=False, description="是否已启用 TOTP 双因子认证")


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
    role: str = Field(default="viewer", max_length=32, description="主角色（内置或自定义角色名）")
    roles: list[str] | None = Field(
        default=None,
        max_length=16,
        description="全部角色（方案 A 多角色；缺省=[role]，主角色自动取权限最高者）",
    )
    #: 方案 B：所属域不再由用户直接维护，改由所属团队（org_id）自动继承——
    #: 团队绑定域则成员继承团队域，否则可显式指定（兼容旧客户端）；前端已合并为
    #: 「所属团队」单一下拉。后端按 ``org.domain or payload.domain`` 解析。
    domain: str | None = Field(
        default=None, max_length=64, description="所属域（由团队继承的兜底）"
    )
    org_id: int | None = Field(
        default=None, gt=0, description="所属团队 ID（缺省归入当前管理员团队）"
    )
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
    role: str = Field(..., max_length=32, description="主角色（内置或自定义角色名）")
    roles: list[str] | None = Field(
        default=None,
        max_length=16,
        description="全部角色（方案 A 多角色；缺省=[role]，主角色自动取权限最高者）",
    )
    #: 方案 B：所属域由所属团队自动继承（同创建）；org_id 缺省保持不变（不换团队）。
    domain: str | None = Field(
        default=None, max_length=64, description="所属域（由团队继承的兜底）"
    )
    org_id: int | None = Field(default=None, gt=0, description="所属团队 ID（缺省保持原团队）")


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


class UserChangePasswordRequest(BaseModel):
    """自助改密请求体（新密码复杂度由 _validate_password_complexity 校验）。"""

    current_password: str = Field(..., min_length=1, description="当前密码")
    new_password: str = Field(..., min_length=1, max_length=128, description="新密码")


class Setup2faRequest(BaseModel):
    """发起 TOTP 双因子设置请求体（重验密码防会话劫持启用 2FA 反锁账号）。"""

    current_password: str = Field(..., min_length=1, description="当前密码")


class TotpCodeRequest(BaseModel):
    """TOTP 动态码请求体（启用确认 / 关闭验证）。"""

    totp_code: str = Field(..., min_length=1, description="身份验证器动态码（6位）")


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


async def _assert_org_active(db: AsyncSession, org_id: int) -> Organization:
    """校验组织存在且 active（多租户：停用/删除组织不可新建用户）。

    Returns:
        校验通过的组织行（供团队绑定域继承）。
    Raises:
        NotFoundError: 组织不存在（ORG_NOT_FOUND）。
        ValidationError: 组织未启用（ORG_DISABLED）。
    """
    org = (
        await db.execute(
            select(Organization).where(Organization.id == org_id, Organization.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if org is None:
        raise NotFoundError("组织不存在", error_code="ORG_NOT_FOUND", ctx={"org_id": org_id})
    org_status = str(org.status.value if hasattr(org.status, "value") else org.status)
    if org_status != "active":
        raise ValidationError(
            "组织未启用，不能创建用户",
            error_code="ORG_DISABLED",
            ctx={"org_id": org_id, "status": org_status},
        )
    return org


def _resolve_team_domain(org: Organization, fallback: str | None) -> str | None:
    """方案 B：用户所属域由所属团队（组织）继承。

    团队绑定业务域则成员自动继承团队域；团队不限域时允许显式兜底域（兼容旧客户端），
    否则为 None（不限定域，需经 grants 授权才能操作指标）。
    """
    return org.domain or fallback or None


async def _assert_role_valid(db: AsyncSession, role: str) -> None:
    """校验角色：内置七角色 或 已登记的自定义角色（方案 A）。

    自定义角色名必须存在于 ``role`` 表（``is_custom=True``），防止绕过 UI
    直接写接口注入任意角色名（与主题域强校验同款 fail-closed 设计）。

    Raises:
        ValidationError: 未知角色（USER_ROLE_INVALID）。
    """
    if role in BUILTIN_ROLES:
        return
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
    if row is None:
        raise ValidationError(
            f"未知角色: {role}（内置角色或已登记的自定义角色）",
            error_code="USER_ROLE_INVALID",
            ctx={"role": role},
        )


def _password_category_count(password: str) -> int:
    """统计密码命中的字符类别数（大写/小写/数字/特殊字符，每类至多 1 分）。"""
    return sum(
        (
            any(c.isupper() for c in password),
            any(c.islower() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        )
    )


def _validate_password_complexity(password: str) -> None:
    """校验密码复杂度：长度 ≥8 且含大写/小写/数字/特殊字符中至少 3 类。

    Args:
        password: 明文密码。

    Raises:
        ValidationError: 密码不满足复杂度要求（error_code=PASSWORD_WEAK）。
    """
    if len(password) < 8:
        raise ValidationError("密码长度至少 8 位", error_code="PASSWORD_WEAK")
    if _password_category_count(password) < 3:
        raise ValidationError(
            "密码须至少包含大写字母/小写字母/数字/特殊字符中的 3 类",
            error_code="PASSWORD_WEAK",
        )


def _to_admin(row: User, org_name: str | None = None) -> UserAdmin:
    """ORM → 管理视图（created_at 序列化为 ISO 字符串，可带团队名）。"""
    return UserAdmin(
        id=row.id,
        username=row.username,
        email=row.email,
        display_name=row.display_name,
        role=row.role.value if hasattr(row.role, "value") else row.role,
        roles=row.roles_all(),
        domain=row.domain,
        org_id=row.org_id,
        org_name=org_name,
        status=row.status,
        last_login_at=row.last_login_at.isoformat() if row.last_login_at else None,
        created_at=row.created_at.isoformat() if row.created_at else None,
        totp_enabled=bool(getattr(row, "totp_enabled", False)),
    )


async def _notify_user(
    db: AsyncSession,
    *,
    user_id: int,
    event_type: str,
    title: str,
    body: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """账号安全事件定向通知（best-effort，不阻断业务主流程）。

    与冲突仲裁通知范式一致（``conflict.py:_notify_loser_owner``）：
    ``NotifyService(db).notify_user`` 内部会 commit，调用方必须在端点
    ``await db.commit()`` **之后**调用本函数；通知失败仅记日志告警。
    """
    try:
        from app.services.notify.service import NotifyService

        await NotifyService(db).notify_user(
            user_id=user_id,
            event_type=event_type,
            title=title,
            body=body,
            payload=payload,
            channel="IN_APP",
        )
        logger.info("user_notify_sent event_type=%s user_id=%s", event_type, user_id)
    except Exception as exc:  # noqa: BLE001 - 通知降级，不阻断业务
        logger.warning(
            "user_notify_failed event_type=%s user_id=%s err=%s", event_type, user_id, exc
        )


@router.get("", dependencies=_ADMIN_DEPS)
async def list_admin_users(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    role: list[str] | None = Query(None, description="按角色过滤（可重复，命中任一）"),
    status: str | None = Query(None, description="按状态过滤（active/disabled/deleted）"),
    keyword: str | None = Query(None, description="按用户名/显示名/邮箱模糊"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页条数"),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> ApiResponse[dict[str, Any]]:
    """用户管理列表（分页 + 过滤，含邮箱与时间字段）。"""
    base = select(User)
    if role:
        # 方案 A 多角色：任一选中角色命中（主角色或 user_role 扩展）即计入。
        conds = [
            or_(User.role == r, User.role_items.any(UserRole.role == r))
            for r in role
        ]
        base = base.where(or_(*conds))
    if status:
        base = base.where(User.status == status)
    if keyword:
        escaped = _escape_like(keyword)
        like = f"%{escaped}%"
        base = base.where(
            or_(
                User.username.ilike(like, escape="/"),
                User.display_name.ilike(like, escape="/"),
                User.email.ilike(like, escape="/"),
            )
        )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    stmt = (
        base.order_by(User.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(stmt)).scalars().all()
    org_ids = {r.org_id for r in rows if r.org_id}
    org_names: dict[int, str] = {}
    if org_ids:
        org_rows = (
            await db.execute(
                select(Organization.id, Organization.name).where(Organization.id.in_(org_ids))
            )
        ).all()
        org_names = dict(org_rows)
    return ok(
        {
            "total": int(total),
            "page": page,
            "page_size": page_size,
            "items": [_to_admin(r, org_names.get(r.org_id)) for r in rows],
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

    校验用户名/邮箱唯一，初始密码经 bcrypt 哈希落库；管理员设置的初始密码
    强制首登改密（must_change_password=True）。
    """
    _validate_password_complexity(payload.password)
    await _assert_unique(db, username=payload.username, email=payload.email)
    roles = _normalize_roles(payload.roles, payload.role)
    for r in roles:
        await _assert_role_valid(db, r)
    primary_role = _resolve_primary_role(roles)
    org_id = payload.org_id or user.org_id
    org = await _assert_org_active(db, org_id)
    # 方案 B：所属域由所属团队继承（团队绑定域则自动继承，否则用显式兜底）
    domain = _resolve_team_domain(org, payload.domain)
    await _assert_domain_active(db, domain)
    row = User(
        org_id=org_id,
        username=payload.username,
        email=payload.email,
        display_name=payload.display_name,
        role=primary_role,
        domain=domain,
        status="active",
        must_change_password=True,
        password_hash=await hash_password(payload.password),
    )
    # 方案 A 多角色：全部角色（含主角色）落 user_role 权威表，供跨请求角色解析。
    # 注：必须在 db.add/flush 之前（对象仍为 pending）装载集合——pending 对象赋值
    # 不会触发 lazy load；若 flush 之后再赋值，SQLAlchemy 为计算 delete-orphan 会
    # emit SELECT user_role，async 上下文报 MissingGreenlet（真实环境 500）。
    row.role_items = [UserRole(role=r) for r in roles]
    db.add(row)
    await db.flush()
    await write_audit(
        db,
        actor_id=user.id,
        action="user.create",
        entity_type="user",
        entity_id=str(row.id),
        detail={
            "username": row.username,
            "display_name": row.display_name,
            "role": row.role,
            "roles": roles,
            "domain": row.domain,
            "org_id": row.org_id,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await db.refresh(row)
    # 定向通知新用户本人「账号已创建」；初始密码由管理员线下交付，通知体不含明文密码
    await _notify_user(
        db,
        user_id=row.id,
        event_type="user.created",
        title="账号已创建",
        body=(
            f"您的账号 {row.username} 已创建。初始密码由管理员线下交付，"
            "请尽快登录并立即修改密码。"
        ),
        payload={"user_id": row.id, "username": row.username, "source": "user_admin"},
    )
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
        action="user.batch_update_status",
        entity_type="user",
        entity_id=f"items:{len(payload.user_ids)}",
        detail={"status": payload.status, "succeeded": len(succeeded), "failed": len(failed)},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    # 批量启用/禁用：对每个成功变更的用户逐人定向通知（best-effort）
    status_cn = "已启用" if payload.status == "active" else "已禁用"
    for item in succeeded:
        target = by_id.get(item.user_id)
        if target is None:
            continue
        await _notify_user(
            db,
            user_id=item.user_id,
            event_type="user.status_changed",
            title="账号已启用" if payload.status == "active" else "账号已禁用",
            body=f"您的账号 {target.username} 已被管理员{status_cn}。",
            payload={
                "user_id": item.user_id,
                "username": target.username,
                "status": payload.status,
                "source": "user_admin",
            },
        )
    return ok(UserBatchStatusResult(succeeded=succeeded, failed=failed), trace_id=trace_id)


@router.post("/me/password", dependencies=[Depends(guard_against_injection)])
async def change_my_password(
    payload: UserChangePasswordRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """自助修改当前登录用户密码（任意登录角色，不校验 platform_admin）。

    校验旧密码正确性（错误码 PASSWORD_INCORRECT）+ 新密码复杂度；
    成功后清除首登强制改密标记（must_change_password=False）并落审计。
    注意：本静态路径注册在 ``/{user_id}`` 系列之前（FastAPI 按注册顺序匹配）。
    """
    _validate_password_complexity(payload.new_password)
    if not await verify_password(payload.current_password, user.password_hash):
        raise AuthError("当前密码错误", error_code="PASSWORD_INCORRECT")
    if payload.new_password == payload.current_password:
        raise ValidationError("新密码不能与当前密码相同", error_code="PASSWORD_SAME")

    user.password_hash = await hash_password(payload.new_password)
    user.must_change_password = False
    await write_audit(
        db,
        actor_id=user.id,
        action="user.change_password",
        entity_type="user",
        entity_id=str(user.id),
        detail={"username": user.username},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    # S2（审查修复）：改密后吊销该用户活跃 refresh token——被劫持的 refresh
    # 在剩余 7 天有效期内不再能续期 access，强制重新登录。
    from app.core.security import revoke_active_refresh

    await revoke_active_refresh(user.id)
    return ok({"user_id": user.id, "ok": True}, trace_id=trace_id)


@router.post("/me/2fa/setup", dependencies=[Depends(guard_against_injection)])
async def setup_my_2fa(
    payload: Setup2faRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """发起 TOTP 双因子设置：生成密钥并加密落库（未启用），返回 otpauth URI。

    重验当前密码（防会话劫持者恶意启用 2FA 反锁账号）；已启用时幂等返回当前
    otpauth URI（基于已存密钥重新生成）。确认启用走 ``/me/2fa/confirm``。
    """
    if not await verify_password(payload.current_password, user.password_hash):
        raise AuthError("当前密码错误", error_code="PASSWORD_INCORRECT")

    secret = None
    if user.totp_secret:
        secret = decrypt_secret(user.totp_secret)
    if not secret:
        secret = generate_totp_secret()
        user.totp_secret = encrypt_secret(secret)
    account = user.username or f"user-{user.id}"
    await write_audit(
        db,
        actor_id=user.id,
        action="user.setup_2fa",
        entity_type="user",
        entity_id=str(user.id),
        detail={"username": user.username},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        {"secret": secret, "otpauth_uri": totp_uri(secret, account), "enabled": user.totp_enabled},
        trace_id=trace_id,
    )


@router.post("/me/2fa/confirm", dependencies=[Depends(guard_against_injection)])
async def confirm_my_2fa(
    payload: TotpCodeRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """确认启用 TOTP 双因子：校验动态码与待启用密钥匹配后置 enabled=True。

    需先经 ``/me/2fa/setup`` 生成密钥；未设置密钥或动态码错误均拒绝启用。
    """
    if not user.totp_secret:
        raise ValidationError(
            "尚未生成双因子密钥，请先完成设置", error_code="AUTH_TOTP_NOT_SETUP"
        )
    secret = decrypt_secret(user.totp_secret)
    if secret is None or not verify_totp(secret, payload.totp_code):
        raise AuthError("动态验证码错误，请重新输入", error_code="AUTH_TOTP_INVALID")
    if not user.totp_enabled:
        user.totp_enabled = True
        await write_audit(
            db,
            actor_id=user.id,
            action="user.enable_2fa",
            entity_type="user",
            entity_id=str(user.id),
            detail={"username": user.username},
            ip=client_ip(request),
            trace_id=trace_id,
        )
    await db.commit()
    return ok({"enabled": True}, trace_id=trace_id)


@router.post("/me/2fa/disable", dependencies=[Depends(guard_against_injection)])
async def disable_my_2fa(
    payload: TotpCodeRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """关闭 TOTP 双因子：须校验当前动态码（证明持有验证器，防他人代关）。

    设备丢失无法提供动态码时，由平台管理员经 ``/{user_id}/2fa/reset`` 强制重置。
    """
    if not user.totp_enabled:
        raise ValidationError("该账号未启用双因子认证", error_code="AUTH_TOTP_NOT_ENABLED")
    secret = decrypt_secret(user.totp_secret) if user.totp_secret else None
    if secret is None or not verify_totp(secret, payload.totp_code):
        raise AuthError("动态验证码错误，请重新输入", error_code="AUTH_TOTP_INVALID")
    user.totp_secret = None
    user.totp_enabled = False
    await write_audit(
        db,
        actor_id=user.id,
        action="user.disable_2fa",
        entity_type="user",
        entity_id=str(user.id),
        detail={"username": user.username},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok({"enabled": False}, trace_id=trace_id)


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
    roles = _normalize_roles(payload.roles, payload.role)
    for r in roles:
        await _assert_role_valid(db, r)
    if row.id == user.id and "platform_admin" not in roles:
        raise ValidationError(
            "不能降级当前登录的平台管理员角色", error_code="SELF_DEMOTE_FORBIDDEN"
        )
    primary_role = _resolve_primary_role(roles)

    # 方案 B：换团队（org_id 提供时）或保持原团队，域由团队继承
    org_name: str | None = None
    if payload.org_id is not None:
        org = await _assert_org_active(db, payload.org_id)
        row.org_id = org.id
        org_name = org.name
        domain = _resolve_team_domain(org, payload.domain)
    else:
        cur_org = (
            await db.execute(
                select(Organization).where(
                    Organization.id == row.org_id, Organization.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if cur_org is not None:
            org_name = cur_org.name
        domain = _resolve_team_domain(cur_org, payload.domain) if cur_org else payload.domain
    await _assert_domain_active(db, domain)

    row.display_name = payload.display_name
    row.email = payload.email
    row.role = primary_role
    row.domain = domain
    # 方案 A 多角色：整表替换 user_role（含主角色）。
    # 注：实测 SQLAlchemy 2.0 下本 relationship 的 delete-orphan 不触发
    # （clear()/remove()/整体赋值均不产生 DELETE，session.deleted 为空），
    # 若仅 `row.role_items = [...]` 会与存量行唯一键 uk_user_role_user_role 冲突（真实环境 500）。
    # 因此先显式 Core delete 旧行，再装载新集合。
    await db.execute(delete(UserRole).where(UserRole.user_id == row.id))
    row.role_items = []
    row.role_items = [UserRole(user_id=row.id, role=r) for r in roles]
    await write_audit(
        db,
        actor_id=user.id,
        action="user.update",
        entity_type="user",
        entity_id=str(row.id),
        detail={
            "username": row.username,
            "display_name": row.display_name,
            "role": row.role,
            "roles": roles,
            "domain": row.domain,
            "org_id": row.org_id,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await db.refresh(row)
    return ok(_to_admin(row, org_name), trace_id=trace_id)


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
        action="user.update_status",
        entity_type="user",
        entity_id=str(row.id),
        detail={"username": row.username, "status": row.status},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await db.refresh(row)
    # 单条启用/禁用：定向通知被操作用户本人（best-effort）
    await _notify_user(
        db,
        user_id=row.id,
        event_type="user.status_changed",
        title="账号已启用" if row.status == "active" else "账号已禁用",
        body=f"您的账号 {row.username} 已被管理员{'启用' if row.status == 'active' else '禁用'}。",
        payload={
            "user_id": row.id,
            "username": row.username,
            "status": row.status,
            "source": "user_admin",
        },
    )
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
    """重置用户密码（bcrypt 哈希落库，不返回明文；重置后强制首登改密）。"""
    row = await _get_user(db, user_id)
    if row is None:
        raise NotFoundError("用户不存在", error_code="USER_NOT_FOUND")

    _validate_password_complexity(payload.new_password)
    row.password_hash = await hash_password(payload.new_password)
    row.must_change_password = True
    await write_audit(
        db,
        actor_id=user.id,
        action="user.reset_password",
        entity_type="user",
        entity_id=str(row.id),
        detail={"username": row.username},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    # S2（审查修复）：管理员重置密码后吊销该用户活跃 refresh token——
    # 若旧 refresh 被泄露，攻击者不能再续期 access 继续使用旧会话。
    from app.core.security import revoke_active_refresh

    await revoke_active_refresh(row.id)
    # 定向通知被重置用户（安全感知，防"被重置"）；不含明文密码
    await _notify_user(
        db,
        user_id=row.id,
        event_type="user.password_reset",
        title="密码已被重置",
        body=(
            f"您的账号 {row.username} 的密码已被管理员重置。"
            "请用管理员下发的临时密码登录并立即修改密码。"
        ),
        payload={"user_id": row.id, "username": row.username, "source": "user_admin"},
    )
    return ok({"user_id": row.id, "ok": True}, trace_id=trace_id)


@router.post("/{user_id}/2fa/reset", dependencies=_ADMIN_DEPS)
async def reset_user_2fa(
    user_id: int,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[dict[str, Any]]:
    """管理员强制关闭某用户的双因子认证（设备丢失/动态码失联的应急通道）。

    清除密钥与启用标记，后续该用户登录不再要求动态码（可重新设置）。
    落审计 + 定向通知（安全感知，防"被静默关闭 2FA"）。
    """
    row = await _get_user(db, user_id)
    if row is None:
        raise NotFoundError("用户不存在", error_code="USER_NOT_FOUND")
    if not row.totp_enabled:
        raise ValidationError("该用户未启用双因子认证", error_code="AUTH_TOTP_NOT_ENABLED")
    row.totp_secret = None
    row.totp_enabled = False
    await write_audit(
        db,
        actor_id=user.id,
        action="user.reset_2fa",
        entity_type="user",
        entity_id=str(row.id),
        detail={"username": row.username, "operator": user.username},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await _notify_user(
        db,
        user_id=row.id,
        event_type="user.twofa_reset",
        title="双因子认证已被管理员重置",
        body=(
            f"您的账号 {row.username} 的双因子认证已被管理员关闭。"
            "如非本人操作请联系平台管理员；可登录后在个人中心重新开启。"
        ),
        payload={"user_id": row.id, "username": row.username, "source": "user_admin"},
    )
    return ok({"user_id": row.id, "enabled": False}, trace_id=trace_id)
