"""语义服务 REST API（P2: US13 指标模板 + 仪表盘 + 消费指南）。

提供：
1. 指标模板 CRUD：列表/详情/从模板创建指标。
2. 消费者仪表盘：按域/Owner 聚合指标统计。
3. 消费指南查询：获取指标的推荐使用方式。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.models.metric import Metric
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
    """创建新的指标模板。"""
    template = MetricTemplate(
        code=body.get("code", ""),
        name=body.get("name", ""),
        domain=body.get("domain", ""),
        description=body.get("description"),
        defaults_json=body.get("defaults_json", {}),
        required_fields=body.get("required_fields"),
        type=body.get("type"),
        granularity=body.get("granularity"),
        unit=body.get("unit"),
        aggregation=body.get("aggregation"),
        time_semantics=body.get("time_semantics"),
        freshness=body.get("freshness"),
        dw_layer=body.get("dw_layer"),
        serving_mode=body.get("serving_mode"),
        additivity=body.get("additivity"),
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
    """消费者仪表盘：按域/Owner 聚合指标统计。

    返回：总数、按状态分组、按分级分组、按域分组、PII 占比等。
    """
    base_q = select(Metric)
    if domain is not None:
        base_q = base_q.where(Metric.domain == domain)
    if owner_id is not None:
        base_q = base_q.where(Metric.owner_id == owner_id)

    # 总数
    count_q = select(func.count()).select_from(base_q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    # 按状态分组
    status_q2 = select(Metric.status, func.count()).group_by(Metric.status)
    if domain is not None:
        status_q2 = status_q2.where(Metric.domain == domain)
    if owner_id is not None:
        status_q2 = status_q2.where(Metric.owner_id == owner_id)
    status_rows = (await db.execute(status_q2)).all()
    by_status = {row[0]: row[1] for row in status_rows}

    # 按分级分组
    tier_q = select(Metric.metric_tier, func.count()).group_by(Metric.metric_tier)
    if domain is not None:
        tier_q = tier_q.where(Metric.domain == domain)
    if owner_id is not None:
        tier_q = tier_q.where(Metric.owner_id == owner_id)
    tier_rows = (await db.execute(tier_q)).all()
    by_tier = {row[0]: row[1] for row in tier_rows}

    # 按域分组
    domain_q = select(Metric.domain, func.count()).group_by(Metric.domain)
    if owner_id is not None:
        domain_q = domain_q.where(Metric.owner_id == owner_id)
    domain_rows = (await db.execute(domain_q)).all()
    by_domain = {row[0]: row[1] for row in domain_rows}

    # PII 占比
    pii_q = select(func.count()).select_from(Metric).where(Metric.pii_flag.is_(True))
    if domain is not None:
        pii_q = pii_q.where(Metric.domain == domain)
    if owner_id is not None:
        pii_q = pii_q.where(Metric.owner_id == owner_id)
    pii_count = (await db.execute(pii_q)).scalar_one()

    data = {
        "total": total,
        "by_status": by_status,
        "by_tier": by_tier,
        "by_domain": by_domain,
        "pii_count": pii_count,
        "pii_ratio": round(pii_count / max(total, 1), 4),
    }
    return ok(data=data, trace_id=get_trace_id(request))


# ----------------------------------------------------------------
# 3. 消费指南
# ----------------------------------------------------------------


@router.get(
    "/consumption-guide/{metric_id}",
    dependencies=_READ_DEPS,
    response_model=ApiResponse,
    summary="获取指标消费指南",
)
async def get_consumption_guide(
    request: Request,
    _user: CurrentUser,
    metric_id: int,
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """获取指定指标的消费指南。

    消费指南包含：推荐查询方式、适用场景、注意事项、关联指标等。
    如果指标没有预设 consumption_guide，则自动生成基础指南。
    """
    q = select(Metric).where(Metric.id == metric_id)
    result = await db.execute(q)
    metric = result.scalar_one_or_none()
    if metric is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError(f"指标不存在: {metric_id}")

    # 优先使用预设的消费指南
    if metric.consumption_guide:
        guide = metric.consumption_guide
    else:
        # 自动生成基础指南
        guide = {
            "metric_code": metric.metric_code,
            "name": metric.name,
            "domain": metric.domain,
            "type": metric.type,
            "granularity": metric.granularity,
            "unit": metric.unit,
            "aggregation": metric.aggregation,
            "time_semantics": metric.time_semantics,
            "serving_mode": metric.serving_mode,
            "recommended_usage": [
                f"适用 {metric.domain} 域 {metric.granularity} 粒度分析",
                f"聚合方式为 {metric.aggregation}，"
                f"注意{'不可' if metric.additivity == 'NON_ADDITIVE' else '可以'}跨维度聚合",
            ],
            "cautions": [],
            "related_metrics": [],
        }
        if metric.pii_flag:
            guide["cautions"].append("该指标包含 PII 数据，使用时需遵守数据合规要求")
        if metric.additivity == "SEMI_ADDITIVE":
            dims = metric.non_additive_dimensions or "未指定"
            guide["cautions"].append(f"半可加指标，不可加维度: {dims}")

    return ok(data=guide, trace_id=get_trace_id(request))
