"""组织（租户）管理 API（TD §4.1 organization 表，多租户闭环补齐）。

端点：

- GET    /organizations            组织列表（含用户数统计，分页 + 关键字/状态过滤）
- POST   /organizations            创建组织（platform_admin 专属）
- PATCH  /organizations/{id}       更新组织（名称 / 状态，platform_admin 专属）

鉴权：列表读 platform_admin + domain_admin；写操作仅 platform_admin。
审计：写操作落 ``audit_log``（action=ORG_CREATE / ORG_UPDATE）。

状态机：active / suspended / deleted。自我保护：
- 不能将「默认组织」（code=default）置为 suspended / deleted（防止锁死全平台默认租户）；
- 不能停用 / 删除当前登录管理员所属组织（防自锁）；
- 置 deleted 前校验组织下无用户（有用户须先迁移或回收，否则 409）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.models.subject_domain import SubjectDomain
from app.models.user import Organization, User


def _escape_like(text: str) -> str:
    """转义 LIKE 通配符（S5 审查修复）：用户输入 ``%``/``_`` 会放大匹配面/慢查询。

    用 ``/`` 作转义符（转义 //、/% 和 /_），配合 ``ilike(..., escape="/")`` 生效。
    """
    return text.replace("/", "//").replace("%", "/%").replace("_", "/_")

router = APIRouter(prefix="/organizations", tags=["组织管理"])

logger = logging.getLogger("unisense.organizations.api")

#: 管理依赖：写仅平台管理员 + 注入守卫；读平台管理员/域管理员。
_ADMIN_DEPS = [Depends(require_roles("platform_admin")), Depends(guard_against_injection)]
_READ_DEPS = [
    Depends(require_roles("platform_admin", "domain_admin")),
    Depends(guard_against_injection),
]

#: 默认组织编码（seed_admin.py 初始化，多租户保护：不可删除）。
DEFAULT_ORG_CODE = "default"


async def _assert_domain_active(db: AsyncSession, domain: str | None) -> None:
    """校验团队绑定域：若提供，必须是存在且 active 的主题域 code。

    与用户管理（``users.py:_assert_domain_active``）同口径——团队绑定域后其成员
    自动继承该域，须防止绕过 UI 写入任意域值（错误码 USER_DOMAIN_INVALID 兼容）。
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


class OrganizationCreate(BaseModel):
    """``POST /organizations`` 请求体。"""

    name: str = Field(min_length=1, max_length=128, description="组织名称")
    code: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="组织编码（唯一，小写字母/数字/下划线/连字符）",
    )
    domain: str | None = Field(
        default=None, max_length=64, description="所属业务域（可空=不限域，成员自动继承）"
    )


class OrganizationUpdate(BaseModel):
    """``PATCH /organizations/{id}`` 请求体（名称 / 状态 / 绑定域可独立更新）。

    ``domain`` 传空串表示清除团队绑定域（成员将不再自动继承域）。
    """

    name: str | None = Field(default=None, min_length=1, max_length=128, description="组织名称")
    status: Literal["active", "suspended", "deleted"] | None = Field(
        default=None, description="组织状态"
    )
    domain: str | None = Field(default=None, max_length=64, description="所属业务域（空串=清除）")


class OrganizationView(BaseModel):
    """组织管理视图（含用户数统计）。"""

    id: int
    name: str
    code: str
    status: str
    domain: str | None = None
    user_count: int = 0
    created_at: str | None = None


async def _to_view(row: Organization, user_count: int) -> OrganizationView:
    return OrganizationView(
        id=row.id,
        name=row.name,
        code=row.code,
        status=row.status.value if hasattr(row.status, "value") else row.status,
        domain=row.domain,
        user_count=user_count,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


async def _notify_org_members(
    db: AsyncSession,
    org: Organization,
    *,
    trace_id: str,
) -> None:
    """组织状态变更后向全部成员定向通知（best-effort，不阻断业务主流程）。

    与账号安全通知范式一致（``users.py:_notify_user`` / ``conflict.py:_notify_loser_owner``）：
    ``NotifyService(db).notify_user`` 内部会 commit，调用方必须在端点 ``await db.commit()``
    **之后**调用；组织被停用时成员可能已无法登录，通知仍尝试发送（IN_APP 站内信，登录即达）。
    查询不到成员 / 通知失败均仅记日志告警。
    """
    try:
        from app.services.notify.service import NotifyService

        members = (
            await db.execute(
                select(User).where(User.org_id == org.id, User.deleted_at.is_(None))
            )
        ).scalars().all()
        org_status = org.status.value if hasattr(org.status, "value") else org.status
        verb = "已停用" if org_status == "suspended" else "已启用"
        for member in members:
            await NotifyService(db).notify_user(
                user_id=member.id,
                event_type="org.status_changed",
                title=f"您所属的组织{verb}",
                body=f"您所属的组织「{org.name}」已被管理员{verb}，如有疑问请联系管理员。",
                payload={
                    "org_id": org.id,
                    "org_name": org.name,
                    "status": org_status,
                    "source": "org_admin",
                },
                channel="IN_APP",
            )
        logger.info(
            "org_status_notify_sent org_id=%s members=%s trace_id=%s",
            org.id,
            len(members),
            trace_id,
        )
    except Exception as exc:  # noqa: BLE001 - 通知降级，不阻断业务
        logger.warning("org_status_notify_failed org_id=%s err=%s", org.id, exc)


@router.get("", dependencies=_READ_DEPS)
async def list_organizations(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    keyword: str | None = Query(None, description="按名称/编码模糊"),
    status: str | None = Query(None, description="按状态过滤（active/suspended/deleted）"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页条数"),
) -> ApiResponse[dict[str, Any]]:
    """组织列表（含用户数统计，软删行排除）。"""
    base = select(Organization).where(Organization.deleted_at.is_(None))
    # 组织收敛：非平台管理员仅可见本组织（domain_admin 的组织治理范围是自己的组织，
    # 与 collector._resolve_org_scope 同语义；未绑定组织的用户看到空列表）。
    if "platform_admin" not in user.roles_all() and user.org_id is not None:
        base = base.where(Organization.id == user.org_id)
    if keyword:
        escaped = _escape_like(keyword)
        like = f"%{escaped}%"
        base = base.where(
            or_(
                Organization.name.ilike(like, escape="/"),
                Organization.code.ilike(like, escape="/"),
            )
        )
    if status:
        base = base.where(Organization.status == status)
    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    stmt = (
        base.order_by(Organization.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(stmt)).scalars().all()
    counts: dict[int, int] = {
        org_id: int(cnt)
        for org_id, cnt in (
            await db.execute(
                select(User.org_id, func.count())
                .where(User.deleted_at.is_(None))
                .group_by(User.org_id)
            )
        ).all()
    }
    items = [await _to_view(r, counts.get(r.id, 0)) for r in rows]
    return ok(
        {"total": int(total), "page": page, "page_size": page_size, "items": items},
        trace_id=trace_id,
    )


@router.post("", dependencies=_ADMIN_DEPS)
async def create_organization(
    payload: OrganizationCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[OrganizationView]:
    """创建组织（platform_admin 专属，编码全局唯一，落审计）。"""
    dup = (
        await db.execute(
            select(Organization).where(
                Organization.code == payload.code, Organization.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise ConflictError(
            f"组织编码已被占用: {payload.code}", error_code="ORG_EXISTS", ctx={"code": payload.code}
        )
    await _assert_domain_active(db, payload.domain)
    row = Organization(
        name=payload.name,
        code=payload.code,
        status="active",
        domain=payload.domain or None,
    )
    db.add(row)
    await db.flush()
    await write_audit(
        db,
        actor_id=user.id,
        action="organization.create",
        entity_type="organization",
        entity_id=str(row.id),
        detail={"name": row.name, "code": row.code},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await db.refresh(row)
    return ok(await _to_view(row, 0), trace_id=trace_id)


@router.patch("/{org_id}", dependencies=_ADMIN_DEPS)
async def update_organization(
    org_id: int,
    payload: OrganizationUpdate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[OrganizationView]:
    """更新组织（名称/状态）。

    自我保护：默认组织不可停用/删除；不可停用/删除当前管理员所属组织；
    置 deleted 前须组织下无用户（409）。
    """
    row = (
        await db.execute(
            select(Organization).where(Organization.id == org_id, Organization.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("组织不存在", error_code="ORG_NOT_FOUND", ctx={"org_id": org_id})

    status_changed = False
    domain_changed = False
    if payload.name is not None:
        row.name = payload.name
    if payload.domain is not None:
        # 空串 = 清除团队绑定域；非空须校验为 active 主题域
        new_domain = payload.domain.strip() or None
        await _assert_domain_active(db, new_domain)
        if row.domain != new_domain:
            row.domain = new_domain
            domain_changed = True
    if payload.status is not None and payload.status != row.status:
        status_changed = True
        if row.code == DEFAULT_ORG_CODE and payload.status in ("suspended", "deleted"):
            raise ValidationError(
                "默认组织不可停用或删除", error_code="ORG_PROTECTED", ctx={"code": row.code}
            )
        if payload.status in ("suspended", "deleted") and row.id == user.org_id:
            raise ValidationError(
                "不能停用/删除当前登录管理员所属组织（防自锁）",
                error_code="ORG_SELF_LOCK",
                ctx={"org_id": row.id},
            )
        if payload.status == "deleted":
            user_count = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(User)
                        .where(User.org_id == row.id, User.deleted_at.is_(None))
                    )
                ).scalar()
                or 0
            )
            if user_count > 0:
                raise ConflictError(
                    f"组织下仍有 {user_count} 个用户，无法删除（请先迁移或回收）",
                    error_code="ORG_HAS_USERS",
                    ctx={"org_id": row.id, "user_count": user_count},
                )
            row.deleted_at = datetime.now(UTC)
        row.status = payload.status

    await write_audit(
        db,
        actor_id=user.id,
        action="organization.update",
        entity_type="organization",
        entity_id=str(row.id),
        detail={
            "name": row.name,
            "code": row.code,
            "status": row.status,
            "domain": row.domain,
            "domain_changed": domain_changed,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    # 团队绑定域变更 → 实时传播到全部成员（user.domain 与权限域隔离保持一致；
    # 域是 PDP 同域判定的核心，成员域须跟随团队，否则权限隔离失真）
    if domain_changed:
        members = (
            await db.execute(
                select(User).where(User.org_id == row.id, User.deleted_at.is_(None))
            )
        ).scalars().all()
        for member in members:
            member.domain = row.domain
        if members:
            logger.info(
                "org_domain_propagated org_id=%s domain=%r members=%s trace_id=%s",
                row.id,
                row.domain,
                len(members),
                trace_id,
            )
    await db.commit()
    await db.refresh(row)
    # 组织状态变更（停用/启用）后向全部成员定向通知（best-effort，不阻断业务）
    if status_changed and row.status in ("active", "suspended"):
        await _notify_org_members(db, row, trace_id=trace_id)
    user_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(User.org_id == row.id, User.deleted_at.is_(None))
            )
        ).scalar()
        or 0
    )
    return ok(await _to_view(row, user_count), trace_id=trace_id)
