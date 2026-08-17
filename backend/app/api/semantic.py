"""语义服务 REST API（P2: US13 指标模板 + 仪表盘 + 消费指南）。

提供：
1. 指标模板 CRUD：列表/详情/从模板创建指标。
2. 消费者仪表盘：按域/Owner 聚合指标统计。
3. 消费指南查询：获取指标的推荐使用方式。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
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
    keyword: str | None = Query(None, description="关键词：编码/名称/描述模糊匹配"),
    owner_id: int | None = Query(None, description="责任人（Owner）ID 过滤"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=200, description="每页条数"),
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """列出指标模板，可按域、启用状态、责任人和关键词过滤（分页）。"""
    q = select(MetricTemplate)
    if domain is not None:
        q = q.where(MetricTemplate.domain == domain)
    if is_active is not None:
        q = q.where(MetricTemplate.is_active == is_active)
    if owner_id is not None:
        q = q.where(MetricTemplate.owner_id == owner_id)
    if keyword:
        # 参数化 LIKE + 通配符转义（对齐 FR-035：% / _ 须转义，防模糊放大）。
        # 转义符 / 配合 escape="/"（修复前 \\% 无 ESCAPE 子句致转义失效）
        escaped = keyword.replace("/", "//").replace("%", "/%").replace("_", "/_")
        q = q.where(
            or_(
                MetricTemplate.code.like(f"%{escaped}%", escape="/"),
                MetricTemplate.name.like(f"%{escaped}%", escape="/"),
                MetricTemplate.description.like(f"%{escaped}%", escape="/"),
            )
        )
    total_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(total_q)).scalar_one() or 0
    q = (
        q.order_by(MetricTemplate.domain, MetricTemplate.name, MetricTemplate.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(q)
    templates = result.scalars().all()
    return ok(
        data={
            "items": [t.to_dict() for t in templates],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
        trace_id=get_trace_id(request),
    )


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


async def _generate_template_code(
    db: AsyncSession,
    validated: Any,
) -> str:
    """自动生成唯一模板编码：``tpl_{domain}_{name_slug}``，冲突自增后缀。"""
    from app.core.codegen import generate_unique_code, slugify_code

    name_slug = slugify_code(validated.name)
    base = f"tpl_{validated.domain}_{name_slug}" if name_slug else f"tpl_{validated.domain}"

    async def _exists(code: str) -> bool:
        res = await db.execute(select(MetricTemplate).where(MetricTemplate.code == code))
        return res.scalar_one_or_none() is not None

    return await generate_unique_code(base, _exists)


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
    # 编码自动生成（FR-010：缺省时由系统生成 tpl_{domain}_{name}，避免人为创造）
    if not validated.code:
        validated.code = await _generate_template_code(db, validated)
    # 编码唯一性检查
    dup = await db.execute(select(MetricTemplate).where(MetricTemplate.code == validated.code))
    if dup.scalar_one_or_none() is not None:
        from app.core.exceptions import ConflictError

        raise ConflictError(f"模板编码已存在: {validated.code}", error_code="TPL_EXISTS")
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
        owner_id=validated.owner_id,
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


@router.patch(
    "/templates/{template_id}/owner",
    dependencies=_WRITE_DEPS,
    response_model=ApiResponse,
    summary="指派/解除模板责任人",
)
async def update_template_owner(
    user: CurrentUser,
    template_id: int,
    request: Request,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """指派或解除指标模板责任人（owner_id=None 解除归属）。

    责任人用于总览仪表 Owner 责任分布的跨资产统计（模板纳入责任维度）。
    """
    from app.models.user import User

    q = select(MetricTemplate).where(MetricTemplate.id == template_id)
    result = await db.execute(q)
    template = result.scalar_one_or_none()
    if template is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError(f"模板不存在: {template_id}")
    owner_id = body.get("owner_id")
    if owner_id is not None:
        if not isinstance(owner_id, int) or owner_id < 1:
            from fastapi import HTTPException

            raise HTTPException(status_code=422, detail="owner_id 必须为正整数")
        exists = await db.execute(select(User.id).where(User.id == owner_id))
        if exists.scalar_one_or_none() is None:
            from app.core.exceptions import NotFoundError

            raise NotFoundError(f"目标用户不存在: {owner_id}")
    template.owner_id = owner_id
    await write_audit(
        db,
        actor_id=user.id,
        action="template.assign_owner",
        entity_type="metric_template",
        entity_id=str(template.code),
        detail={"owner_id": owner_id},
        ip=client_ip(request),
        trace_id=get_trace_id(request),
    )
    await db.commit()
    await db.refresh(template)
    return ok(data=template.to_dict(), trace_id=get_trace_id(request))


@router.patch(
    "/templates/{template_id}/active",
    dependencies=_WRITE_DEPS,
    response_model=ApiResponse,
    summary="启用/停用指标模板",
)
async def update_template_active(
    user: CurrentUser,
    template_id: int,
    request: Request,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """启用或停用指标模板（is_active=False 停止新实例化，保留存量）。

    停用模板不可再被实例化（instantiate 端点校验 is_active），但列表/详情仍可查。
    """
    q = select(MetricTemplate).where(MetricTemplate.id == template_id)
    result = await db.execute(q)
    template = result.scalar_one_or_none()
    if template is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError(f"模板不存在: {template_id}")
    is_active = body.get("is_active")
    if not isinstance(is_active, bool):
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="is_active 必须为布尔值")
    template.is_active = is_active
    await write_audit(
        db,
        actor_id=user.id,
        action="template.set_active",
        entity_type="metric_template",
        entity_id=str(template.code),
        detail={"is_active": is_active},
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

    # definition_json 特殊处理：body 传空对象（前端实例化弹窗未填口径）时
    # 不覆盖模板默认口径——否则 `{**defaults, **body}` 会把空对象顶替掉模板口径，
    # 实例化出"空心"指标（无 L3 血缘、口径丢失）。
    if not merged.get("definition_json") and defaults.get("definition_json"):
        merged["definition_json"] = defaults["definition_json"]

    # 3. 必填字段校验（对齐 merged：模板默认值亦满足必填——
    #    仅查 body 会误拒"模板默认已提供"的必填字段）
    required = template.required_fields or []
    missing = [f for f in required if f not in merged or not merged[f]]
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
    """消费者仪表盘：按域/Owner 聚合指标统计 + 全资产计数（单次聚合查询+deleted_at过滤）。

    ``assets`` 覆盖指标/数据表/数据源/维度/术语/指标模板/数据字典（DB 聚合），
    采集任务为运行时 JobStore 数据，由采集服务聚合后并入，避免 semantic 仓储耦合 collector。
    """
    from app.services.semantic.repository import MetricRepository

    repo = MetricRepository(db)
    data = await repo.aggregate_dashboard(domain=domain, owner_id=owner_id)
    # 采集任务：运行时数据（Redis/内存 JobStore），采集服务聚合；失败不阻断仪表盘
    try:
        from app.services.collector.service import CollectorService

        job_stats = await CollectorService(db).count_jobs_by_status()
        data["assets"]["collection_task"] = {
            "total": sum(job_stats.values()),
            "by_status": job_stats,
        }
    except Exception:  # noqa: BLE001 —— 采集服务异常不影响指标/资产读数
        data["assets"]["collection_task"] = {"total": 0, "by_status": {}}
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


# ----------------------------------------------------------------
# 4. QuickBI 嵌入票据（FR-12 / TD §12.3）
# ----------------------------------------------------------------


async def _issue_quickbi_ticket(
    body: dict[str, Any],
    db: AsyncSession,
    actor: Any,
    request: Request,
) -> dict[str, Any]:
    """签发 QuickBI 嵌入票据（共享处理，供主路径与兼容路径复用）。"""
    from app.core.exceptions import AuthError, ValidationError
    from app.core.feature_flags import is_feature_enabled_or_default

    # OPS-09 特性开关：QuickBI 嵌入能力可被平台管理员灰度关闭（默认开启，非破坏）
    if not is_feature_enabled_or_default("quickbi"):
        raise AuthError(
            "QuickBI 报表嵌入能力已被平台管理员关闭",
            error_code="FEATURE_DISABLED",
            ctx={"feature_flag": "quickbi"},
        )

    report_id = str(body.get("reportId") or body.get("report_id") or "").strip()
    if not report_id:
        raise ValidationError("reportId 必填", ctx={"field": "reportId"})
    dashboard_id = body.get("dashboardId") or body.get("dashboard_id")
    params = body.get("params")
    if params is not None and not isinstance(params, dict):
        raise ValidationError("params 必须为对象", ctx={"field": "params"})

    from app.services.semantic.quickbi import QuickBiService

    # 绑定签发者身份声明（user_id/role/domain），供网关侧按用户收敛报表访问：
    # 此前票据不绑定身份，任意调用者可签任意 report_id 嵌入（含 PII 报表）。
    role_val = actor.role.value if hasattr(actor.role, "value") else actor.role
    actor_claim = {
        "user_id": actor.id,
        "role": str(role_val) if role_val is not None else None,
        "domain": getattr(actor, "domain", None),
    }
    data = QuickBiService().issue_ticket(
        report_id=report_id,
        dashboard_id=str(dashboard_id) if dashboard_id else None,
        params=params if isinstance(params, dict) else None,
        actor=actor_claim,
    )
    # FR-16: 票据签发为消费侧操作，留痕便于审计报表访问
    await write_audit(
        db,
        actor_id=actor.id,
        action="quickbi.get_ticket",
        entity_type="quickbi_report",
        entity_id=report_id,
        detail={"dashboard_id": str(dashboard_id) if dashboard_id else None},
        ip=client_ip(request),
        trace_id=get_trace_id(request),
    )
    await db.commit()
    return data


@router.post(
    "/quickbi/ticket",
    dependencies=_READ_DEPS,
    response_model=ApiResponse,
    summary="获取 QuickBI 嵌入票据",
)
async def issue_quickbi_ticket(
    request: Request,
    _user: CurrentUser,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """签发 QuickBI 嵌入票据（FR-12）。返回短期签名票据与嵌入地址。"""
    data = await _issue_quickbi_ticket(body, db, _user, request)
    return ok(data=data, trace_id=get_trace_id(request))


# 兼容路径：前端 QuickBI 组件调用 /api/v1/semantic/quickbi/ticket（单数 semantic，
# 与 router prefix /semantics 多一字母差异）。提供独立 router 保持双路径可用。
quickbi_compat_router = APIRouter(prefix="/semantic", tags=["semantics"])


@quickbi_compat_router.post(
    "/quickbi/ticket",
    dependencies=_READ_DEPS,
    response_model=ApiResponse,
    summary="获取 QuickBI 嵌入票据（兼容路径）",
)
async def issue_quickbi_ticket_compat(
    request: Request,
    _user: CurrentUser,
    body: dict[str, Any],
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """兼容前端 /api/v1/semantic/quickbi/ticket（单数 semantic）。"""
    data = await _issue_quickbi_ticket(body, db, _user, request)
    return ok(data=data, trace_id=get_trace_id(request))
