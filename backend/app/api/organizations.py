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
from app.models.user import Organization, User

router = APIRouter(prefix="/organizations", tags=["组织管理"])

#: 管理依赖：写仅平台管理员 + 注入守卫；读平台管理员/域管理员。
_ADMIN_DEPS = [Depends(require_roles("platform_admin")), Depends(guard_against_injection)]
_READ_DEPS = [
    Depends(require_roles("platform_admin", "domain_admin")),
    Depends(guard_against_injection),
]

#: 默认组织编码（seed_admin.py 初始化，多租户保护：不可删除）。
DEFAULT_ORG_CODE = "default"


class OrganizationCreate(BaseModel):
    """``POST /organizations`` 请求体。"""

    name: str = Field(min_length=1, max_length=128, description="组织名称")
    code: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="组织编码（唯一，小写字母/数字/下划线/连字符）",
    )


class OrganizationUpdate(BaseModel):
    """``PATCH /organizations/{id}`` 请求体（名称与状态可独立更新）。"""

    name: str | None = Field(default=None, min_length=1, max_length=128, description="组织名称")
    status: Literal["active", "suspended", "deleted"] | None = Field(
        default=None, description="组织状态"
    )


class OrganizationView(BaseModel):
    """组织管理视图（含用户数统计）。"""

    id: int
    name: str
    code: str
    status: str
    user_count: int = 0
    created_at: str | None = None


async def _to_view(row: Organization, user_count: int) -> OrganizationView:
    return OrganizationView(
        id=row.id,
        name=row.name,
        code=row.code,
        status=row.status.value if hasattr(row.status, "value") else row.status,
        user_count=user_count,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


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
    if keyword:
        like = f"%{keyword}%"
        base = base.where(
            or_(Organization.name.ilike(like), Organization.code.ilike(like))
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
    row = Organization(name=payload.name, code=payload.code, status="active")
    db.add(row)
    await db.flush()
    await write_audit(
        db,
        actor_id=user.id,
        action="ORG_CREATE",
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

    if payload.name is not None:
        row.name = payload.name
    if payload.status is not None and payload.status != row.status:
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
        action="ORG_UPDATE",
        entity_type="organization",
        entity_id=str(row.id),
        detail={"name": row.name, "code": row.code, "status": row.status},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await db.refresh(row)
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
