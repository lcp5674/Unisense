"""维度管理 API（TD §12.15 / FR-05 / FR-09）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import write_audit
from app.core.exceptions import AuthError, NotFoundError
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.services.dimension.schemas import (
    DimensionCreate,
    DimensionMappingCreate,
    DimensionMappingResponse,
    DimensionMappingUpdate,
    DimensionMemberCreate,
    DimensionMemberResponse,
    DimensionMemberUpdate,
    DimensionMetricBinding,
    DimensionResponse,
    DimensionUpdate,
    MetricDimensionBind,
    MetricDimensionResponse,
    PreviewValuesRequest,
    PreviewValuesResponse,
    ReconciliationResponse,
    ReconciliationReview,
    ReconciliationSubmit,
)
from app.services.dimension.service import DimensionService

router = APIRouter(prefix="/dimensions", tags=["dimension"])

_WRITE_ROLES = ("metric_owner", "domain_admin", "platform_admin")
_GOV_ROLES = ("domain_admin", "platform_admin")
_READ_ROLES = ("metric_owner", "domain_admin", "platform_admin", "reviewer", "viewer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]
_GOV_DEPS = [Depends(require_roles(*_GOV_ROLES)), Depends(guard_against_injection)]

# 域作用域守卫（P1-10）：domain_admin/metric_owner 仅可操作本域资源，
# 防跨域越权——此前 API 仅校验角色、无 user.domain 作用域，域管理员可任意增删改他域维度。
_SCOPED_ROLES = ("domain_admin", "metric_owner")


def _assert_domain_scope(user: CurrentUser, resource_domain: str) -> None:
    if user.role in _SCOPED_ROLES and user.domain and resource_domain != user.domain:
        raise AuthError(
            f"无权限操作其他域的资源（资源域 {resource_domain}，当前域 {user.domain}）",
            error_code="FORBIDDEN",
        )


async def _scope_dimension(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
) -> None:
    """路径携带 dim_code 的写操作：加载维度并校验域作用域。"""
    dim = await DimensionService(db).get_dimension(dim_code)
    if dim is None:
        raise NotFoundError(f"维度不存在: {dim_code}")
    _assert_domain_scope(user, dim.domain)


async def _scope_dimension_by_code(
    code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
) -> None:
    """body 携带 dim_code 的写操作：解析维度域并校验作用域。"""
    dim = await DimensionService(db).get_dimension(code)
    if dim is None:
        raise NotFoundError(f"维度不存在: {code}")
    _assert_domain_scope(user, dim.domain)


async def _scope_mapping(
    mapping_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
) -> None:
    """映射按 id 写操作：解析源维度域并校验作用域。"""
    mapping = await DimensionService(db).get_mapping(mapping_id)
    if mapping is None:
        raise NotFoundError(f"映射不存在: {mapping_id}")
    dim = await DimensionService(db).get_dimension(mapping.source_dim_code)
    if dim is not None:
        _assert_domain_scope(user, dim.domain)


# 写操作 + 域作用域守卫（P1-10）：域管理员/指标 Owner 仅可操作本域资源
_WRITE_SCOPED_DEPS = _WRITE_DEPS + [Depends(_scope_dimension)]
_MAPPING_SCOPED_DEPS = _WRITE_DEPS + [Depends(_scope_mapping)]


@router.post("", status_code=201, dependencies=_WRITE_DEPS)
async def create_dimension(
    payload: DimensionCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # 域作用域守卫（P1-10）：域管理员仅可在本域建维度
    _assert_domain_scope(user, payload.domain)
    resp = await DimensionService(db).create_dimension(payload, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.create",
        entity_type="dimension",
        entity_id=str(payload.dim_code),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    # P0-3: 直接返回 ORM 对象会触发 FastAPI 序列化 500，须经 DimensionResponse 转换
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.get("", dependencies=_READ_DEPS)
async def list_dimensions(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    domain: str | None = Query(None),
    status: str | None = Query(None),
    keyword: str | None = Query(None, description="关键词：编码/名称/描述模糊匹配"),
    owner_id: int | None = Query(None, description="责任人（Owner）ID 过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    items, total = await DimensionService(db).list_dimensions(
        domain, status, keyword, owner_id, page=page, page_size=page_size
    )
    converted = []
    for dim, metric_count in items:
        resp = DimensionResponse.from_model(dim)
        resp.metric_count = metric_count
        converted.append(resp)
    return ok(
        data={"items": converted, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.post("/mappings", dependencies=_WRITE_DEPS)
async def create_mapping(
    payload: DimensionMappingCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # 域作用域守卫（P1-10）：映射源维度须在本域
    _scope_resource = await DimensionService(db).get_dimension(payload.source_dim_code)
    if _scope_resource is None:
        raise NotFoundError(f"维度不存在: {payload.source_dim_code}")
    _assert_domain_scope(user, _scope_resource.domain)
    resp = await DimensionService(db).create_mapping(payload, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_mapping.create",
        entity_type="dimension_mapping",
        entity_id=f"{payload.source_dim_code}:{payload.target_dim_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionMappingResponse.from_model(resp), trace_id=trace_id)


@router.get("/mappings", dependencies=_READ_DEPS)
async def list_mappings(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    source_dim_code: str | None = Query(None),
    # P10 服务端分页（对齐主表分页模式，防大映射集全量拉取）
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    items, total = await DimensionService(db).list_mappings(
        source_dim_code, page=page, page_size=page_size
    )
    converted = [DimensionMappingResponse.from_model(i) for i in items]
    return ok(
        data={"items": converted, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.put("/mappings/{mapping_id}", dependencies=_MAPPING_SCOPED_DEPS)
async def update_mapping(
    mapping_id: int,
    payload: DimensionMappingUpdate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).update_mapping(mapping_id, payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_mapping.update",
        entity_type="dimension_mapping",
        entity_id=str(mapping_id),
        detail=payload.model_dump(exclude_none=True),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionMappingResponse.from_model(resp), trace_id=trace_id)


@router.delete("/mappings/{mapping_id}", dependencies=_MAPPING_SCOPED_DEPS)
async def delete_mapping(
    mapping_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    await DimensionService(db).delete_mapping(mapping_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_mapping.delete",
        entity_type="dimension_mapping",
        entity_id=str(mapping_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@router.post("/reconciliations", dependencies=_WRITE_DEPS)
async def submit_reconciliation(
    payload: ReconciliationSubmit,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # 域作用域守卫（P1-10）：对账维度须在本域
    _scope_resource = await DimensionService(db).get_dimension(payload.dim_code)
    if _scope_resource is None:
        raise NotFoundError(f"维度不存在: {payload.dim_code}")
    _assert_domain_scope(user, _scope_resource.domain)
    resp = await DimensionService(db).submit_reconciliation(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="reconciliation.submit",
        entity_type="reconciliation",
        entity_id=f"metric:{payload.metric_id}:{payload.dim_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=ReconciliationResponse.from_model(resp), trace_id=trace_id)


@router.get("/reconciliations", dependencies=_READ_DEPS)
async def list_reconciliations(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    status: str | None = Query(None),
    # P10 服务端分页（防治理记录增长导致的全量拉取）
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    items, total = await DimensionService(db).list_reconciliations(
        status, page=page, page_size=page_size
    )
    converted = []
    for rec, metric in items:
        resp = ReconciliationResponse.from_model(rec)
        if metric is not None:
            resp.metric_code = metric.metric_code
            resp.metric_name = metric.name
        converted.append(resp)
    return ok(
        data={"items": converted, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.post(
    "/reconciliations/{rec_id}/review",
    dependencies=_GOV_DEPS,
)
async def review_reconciliation(
    rec_id: int,
    payload: ReconciliationReview,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).review_reconciliation(rec_id, payload, reviewer_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="reconciliation.review",
        entity_type="reconciliation",
        entity_id=str(rec_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=ReconciliationResponse.from_model(resp), trace_id=trace_id)


@router.post("/preview-values", dependencies=_WRITE_DEPS)
async def preview_dimension_values(
    payload: PreviewValuesRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """从数据源表列拉取去重枚举值（维度值自动获取的预览）。"""
    resp = await DimensionService(db).preview_column_values(
        source_id=payload.source_id,
        table=payload.table,
        column=payload.column,
        limit=payload.limit,
    )
    # 审计（P1-11）：数据探测动作留痕，防任意用户对任意源任意表列无痕 SELECT DISTINCT
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.preview_values",
        entity_type="data_source",
        entity_id=f"{payload.source_id}:{payload.table}.{payload.column}",
        detail={"row_count": resp.get("row_count")},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=PreviewValuesResponse(**resp), trace_id=trace_id)


@router.get("/{dim_code}", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_dimension(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).get_dimension(dim_code)
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.put("/{dim_code}", dependencies=_WRITE_DEPS + [Depends(_scope_dimension)])
async def update_dimension(
    dim_code: str,
    payload: DimensionUpdate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).update_dimension(dim_code, payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.update",
        entity_type="dimension",
        entity_id=dim_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.post("/{dim_code}/deprecate", dependencies=_WRITE_DEPS + [Depends(_scope_dimension)])
async def deprecate_dimension(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).deprecate_dimension(dim_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.deprecate",
        entity_type="dimension",
        entity_id=dim_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.post("/{dim_code}/publish", dependencies=_WRITE_DEPS + [Depends(_scope_dimension)])
async def publish_dimension(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).publish_dimension(dim_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension.publish",
        entity_type="dimension",
        entity_id=dim_code,
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionResponse.from_model(resp), trace_id=trace_id)


@router.post("/{dim_code}/members", dependencies=_WRITE_DEPS + [Depends(_scope_dimension)])
async def create_member(
    dim_code: str,
    payload: DimensionMemberCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).create_member(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_member.create",
        entity_type="dimension_member",
        entity_id=f"{payload.dim_code}:{payload.member_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionMemberResponse.from_model(resp), trace_id=trace_id)


@router.get("/{dim_code}/members", dependencies=_READ_DEPS)
async def list_members(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    dim_code: str,
) -> Any:
    items = await DimensionService(db).list_members(dim_code)
    return ok(
        data={"items": [DimensionMemberResponse.from_model(i) for i in items], "total": len(items)},
        trace_id=trace_id,
    )


@router.put("/{dim_code}/members/{member_code}", dependencies=_WRITE_SCOPED_DEPS)
async def update_member(
    dim_code: str,
    member_code: str,
    payload: DimensionMemberUpdate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).update_member(dim_code, member_code, payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_member.update",
        entity_type="dimension_member",
        entity_id=f"{dim_code}:{member_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=DimensionMemberResponse.from_model(resp), trace_id=trace_id)


@router.delete("/{dim_code}/members/{member_code}", dependencies=_WRITE_SCOPED_DEPS)
async def delete_member(
    dim_code: str,
    member_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # 服务端级联删除：删除父级连带整个子树（成员表无软删列，物理删除）
    deleted = await DimensionService(db).delete_member(dim_code, member_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_member.delete",
        entity_type="dimension_member",
        entity_id=f"{dim_code}:{member_code}",
        detail={"cascade_count": len(deleted)},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@router.post(
    "/{dim_code}/members/{member_code}/publish",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def publish_member(
    dim_code: str,
    member_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """发布维度成员（DRAFT → PUBLISHED），对齐维度主体状态机。"""
    resp = await DimensionService(db).publish_member(dim_code, member_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_member.publish",
        entity_type="dimension_member",
        entity_id=f"{dim_code}:{member_code}",
        detail={"status": resp.status},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@router.post(
    "/{dim_code}/members/batch-publish",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def publish_all_members(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """批量发布维度全部 DRAFT 成员（从表导入工作流闭环）。"""
    result = await DimensionService(db).publish_all_members(dim_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_member.batch_publish",
        entity_type="dimension_member",
        entity_id=dim_code,
        detail=result,
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@router.post(
    "/{dim_code}/members/{member_code}/deprecate",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def deprecate_member(
    dim_code: str,
    member_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """废弃维度成员（→ DEPRECATED）；存在子成员时拒绝（层级权威保护）。"""
    resp = await DimensionService(db).deprecate_member(dim_code, member_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="dimension_member.deprecate",
        entity_type="dimension_member",
        entity_id=f"{dim_code}:{member_code}",
        detail={"status": resp.status},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@router.post(
    "/{dim_code}/metrics",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def bind_metric_dimension(
    dim_code: str,
    payload: MetricDimensionBind,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await DimensionService(db).bind_metric_dimension(payload, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="metric.bind_dimension",
        entity_type="metric_dimension",
        entity_id=f"{payload.metric_id}:{payload.dim_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MetricDimensionResponse.from_model(resp), trace_id=trace_id)


@router.delete(
    "/{dim_code}/metrics/{metric_id}",
    dependencies=_WRITE_DEPS + [Depends(_scope_dimension)],
)
async def unbind_metric_dimension(
    dim_code: str,
    metric_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """解除指标-维度绑定（撤销误绑/改绑）：删除绑定记录 + 同步移除指标声明维度。"""
    await DimensionService(db).unbind_metric_dimension(metric_id, dim_code)
    await write_audit(
        db,
        actor_id=user.id,
        action="metric.unbind_dimension",
        entity_type="metric_dimension",
        entity_id=f"{metric_id}:{dim_code}",
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@router.get("/{dim_code}/metrics", dependencies=_READ_DEPS)
async def list_dimension_metrics(
    dim_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    # 治理追溯：查看该维度被哪些指标消费（绑定关系 + 指标信息）
    items = await DimensionService(db).list_dimension_metrics(dim_code)
    converted = [
        DimensionMetricBinding(
            metric_id=binding.metric_id,
            dim_code=binding.dim_code,
            role=binding.role,
            default_member=binding.default_member,
            metric_code=metric.metric_code,
            metric_name=metric.name,
            metric_status=metric.status,
        )
        for binding, metric in items
    ]
    return ok(data={"items": converted, "total": len(converted)}, trace_id=trace_id)


@router.get("/{metric_id}/metric-dimensions", dependencies=_READ_DEPS)
async def list_metric_dimensions(
    metric_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    items = await DimensionService(db).list_metric_dimensions(metric_id)
    return ok(
        data={"items": [MetricDimensionResponse.from_model(i) for i in items], "total": len(items)},
        trace_id=trace_id,
    )
