"""语义服务 REST API（P2: US13 指标模板 + 仪表盘 + 消费指南）。

提供：
1. 指标模板 CRUD：列表/详情/从模板创建指标。
2. 消费者仪表盘：按域/Owner 聚合指标统计。
3. 消费指南查询：获取指标的推荐使用方式。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.models.metric_template import MetricTemplate
from app.services.semantic.service import MetricService

router = APIRouter(prefix="/semantics", tags=["semantics"])

_READ_ROLES = ALL_ROLES
_WRITE_ROLES = ("platform_admin", "domain_admin", "metric_owner")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]


# ----------------------------------------------------------------
# 1. 指标模板
# ----------------------------------------------------------------


@router.get(
    "/templates",
    dependencies=_READ_DEPS,
    response_model=ApiResponse,
    summary="列出指标模板",
)
async def list_templates(
    request: Request,
    _user: CurrentUser,
    domain: str | None = None,
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """列出指标模板，可按域和启用状态过滤。"""
    q = select(MetricTemplate)
    if domain is not None:
        q = q.where(MetricTemplate.domain == domain)
    if is_active is not None:
        q = q.where(MetricTemplate.is_active == is_active)
    q = q.order_by(MetricTemplate.domain, MetricTemplate.name)
    result = await db.execute(q)
    templates = result.scalars().all()
    return ok(data=[t.to_dict() for t in templates], trace_id=get_trace_id(request))


@router.get(
    "/templates/{template_id}",
    dependencies=_READ_DEPS,
    response_model=ApiResponse,
    summary="获取模板详情",
)
async def get_template(
    request: Request,
    _user: CurrentUser,
    template_id: int,
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """获取指定模板详情。"""
    q = select(MetricTemplate).where(MetricTemplate.id == template_id)
    result = await db.execute(q)
    template = result.scalar_one_or_none()
    if template is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError(f"模板不存在: {template_id}")
    return ok(data=template.to_dict(), trace_id=get_trace_id(request))


@router.post(
    "/templates",
    dependencies=_WRITE_DEPS,
    response_model=ApiResponse,
    summary="创建指标模板",
)
async def create_template(
    user: CurrentUser,
    request: Request,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """创建新的指标模板（含 Schema 校验）。"""
    from app.services.semantic.schemas import MetricTemplateCreateRequest

    # Schema 校验替代裸 dict
    validated = MetricTemplateCreateRequest(**body)
    template = MetricTemplate(
        code=validated.code,
        name=validated.name,
        domain=validated.domain,
        description=validated.description,
        defaults_json=validated.defaults_json,
        required_fields=validated.required_fields,
        type=validated.type,
        granularity=validated.granularity,
        unit=validated.unit,
        aggregation=validated.aggregation,
        time_semantics=validated.time_semantics,
        freshness=validated.freshness,
        dw_layer=validated.dw_layer,
        serving_mode=validated.serving_mode,
        additivity=validated.additivity,
        metric_tier=body.get("metric_tier"),
        created_by=user.id,
    )
    db.add(template)
    await write_audit(
        db,
        actor_id=user.id,
        action="template.create",
        entity_type="metric_template",
        entity_id=str(template.code),
        detail={"name": template.name, "domain": template.domain},
        ip=client_ip(request),
        trace_id=get_trace_id(request),
    )
    await db.commit()
    await db.refresh(template)
    return ok(data=template.to_dict(), trace_id=get_trace_id(request))


@router.post(
    "/templates/{template_id}/instantiate",
    dependencies=_WRITE_DEPS,
    response_model=ApiResponse,
    summary="从模板创建指标",
)
async def instantiate_template(
    user: CurrentUser,
    template_id: int,
    request: Request,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """从模板实例化创建指标。

    合并模板 defaults_json 与 body 中用户覆盖的字段，
    生成 MetricCreateRequest 并委托 MetricService.create_metric。
    """
    # 1. 获取模板
    q = select(MetricTemplate).where(
        MetricTemplate.id == template_id,
        MetricTemplate.is_active.is_(True),
    )
    result = await db.execute(q)
    template = result.scalar_one_or_none()
    if template is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError(f"模板不存在或已停用: {template_id}")

    # 2. 合并默认值 + 用户覆盖
    defaults = dict(template.defaults_json or {})
    # 模板预设字段作为默认
    for field in (
        "type",
        "granularity",
        "unit",
        "aggregation",
        "time_semantics",
        "freshness",
        "dw_layer",
        "serving_mode",
        "additivity",
        "metric_tier",
    ):
        val = getattr(template, field, None)
        if val is not None and field not in defaults:
            defaults[field] = val

    # 用户覆盖
    merged = {**defaults, **body}
    merged["template_id"] = template_id

    # 3. 必填字段校验
    required = template.required_fields or []
    missing = [f for f in required if f not in body or not body[f]]
    if missing:
        from app.core.exceptions import ValidationError

        raise ValidationError(f"必填字段缺失: {', '.join(missing)}")

    # 4. 委托 MetricService 创建指标
    from app.services.semantic.schemas import MetricCreateRequest

    create_req = MetricCreateRequest(**merged)
    svc = MetricService(db)
    metric = await svc.create_metric(create_req, owner_id=user.id)
    # PLAT-3: create_metric 仅 flush，补 commit 才算数；PLAT-1: 记模板实例化审计
    await write_audit(
        db,
        actor_id=user.id,
        action="template.instantiate",
        entity_type="metric_definition",
        entity_id=str(getattr(metric, "metric_code", "")),
        detail={"template_id": template_id},
        ip=client_ip(request),
        trace_id=get_trace_id(request),
    )
    await db.commit()

    data = metric.to_dict() if hasattr(metric, "to_dict") else metric
    return ok(data=data, trace_id=get_trace_id(request))


# ----------------------------------------------------------------
# 2. 消费者仪表盘
# ----------------------------------------------------------------


@router.get(
    "/dashboard",
    dependencies=_READ_DEPS,
    response_model=ApiResponse,
    summary="消费者仪表盘",
)
async def dashboard(
    request: Request,
    _user: CurrentUser,
    domain: str | None = None,
    owner_id: int | None = None,
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """消费者仪表盘：按域/Owner 聚合指标统计（单次聚合查询+deleted_at过滤）。"""
    from app.services.semantic.repository import MetricRepository

    repo = MetricRepository(db)
    data = await repo.aggregate_dashboard(domain=domain, owner_id=owner_id)
    return ok(data=data, trace_id=get_trace_id(request))


# ----------------------------------------------------------------
# 3. 消费指南
# ----------------------------------------------------------------


@router.get(
    "/consumption-guide/{metric_code}",
    dependencies=_READ_DEPS,
    response_model=ApiResponse,
    summary="获取指标消费指南",
)
async def get_consumption_guide(
    request: Request,
    _user: CurrentUser,
    metric_code: str,
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """获取指定指标的消费指南（Service层+缓存）。"""
    from app.services.semantic.service import MetricService

    svc = MetricService(db)
    guide = await svc.get_consumption_guide(metric_code)
    return ok(data=guide, trace_id=get_trace_id(request))
