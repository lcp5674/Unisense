"""数据质量 API（TD §12.8 / FR-10，对应 §3.8a）。"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import write_audit
from app.core.exceptions import AuthError, NotFoundError
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.models.quality import QualityEventStatus, QualityRuleType, QualitySeverity
from app.services.quality.schemas import (
    BenchmarkBind,
    BenchmarkImport,
    QualityDetectRequest,
    QualityEventAck,
    QualityObservationRequest,
    QualityRuleCreate,
    QualityRuleUpdate,
    ReconciliationConfirm,
    ReconciliationRun,
)
from app.services.quality.service import QualityService

router = APIRouter(prefix="/quality", tags=["quality"])


async def _assert_metric_domain(
    db: AsyncSession,
    user: CurrentUser,
    *,
    metric_id: int | None = None,
    metric_code: str | None = None,
) -> None:
    """指标域归属校验（P1-3）：platform_admin 放行；其余角色指标须在本域。

    此前 create_rule/import_benchmark 不校验指标归属——域 A 的 metric_owner 可对
    域 B 指标注册规则/导入基准，污染对账与告警。
    """
    if user.has_role("platform_admin"):
        return
    from sqlalchemy import select

    from app.models.metric import Metric

    if metric_id is not None:
        stmt = select(Metric).where(Metric.id == metric_id, Metric.deleted_at.is_(None))
    elif metric_code is not None:
        stmt = select(Metric).where(Metric.metric_code == metric_code, Metric.deleted_at.is_(None))
    else:
        return
    metric = (await db.execute(stmt)).scalar_one_or_none()
    if metric is None:
        raise NotFoundError("指标不存在")
    if metric.domain != user.domain:
        raise AuthError(
            f"无权操作域外指标（当前域: {user.domain}，指标域: {metric.domain}）",
            error_code="FORBIDDEN",
        )

# 写/治理能力对齐前端 quality:config-rule / quality:run-check 基线（仅 platform_admin/
# domain_admin）——metric_owner/compliance_officer 无对应权限点，后端不额外放行。
_WRITE_ROLES = ("domain_admin", "platform_admin")
_GOV_ROLES = ("domain_admin", "platform_admin")
# 读对齐前端 quality:view 基线（全部内置角色）：补 reviewer/analyst。
_READ_ROLES = (
    "metric_owner", "domain_admin", "platform_admin", "compliance_officer",
    "viewer", "reviewer", "analyst",
)
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]
_GOV_DEPS = [Depends(require_roles(*_GOV_ROLES)), Depends(guard_against_injection)]


@router.post("/rules", status_code=201, dependencies=_WRITE_DEPS)
async def create_rule(
    payload: QualityRuleCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """注册质量规则（随指标 PUBLISHED 注册，按 tier/dw_layer 差异化）。"""
    await _assert_metric_domain(db, user, metric_id=payload.metric_id)
    resp = await QualityService(db).create_rule(payload, user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="quality_rule.create",
        entity_type="quality_rule",
        entity_id=str(resp.id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("/rules", dependencies=_READ_DEPS)
async def list_rules(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    metric_id: int | None = Query(None),
    rule_type: str | None = Query(None),
    severity: str | None = Query(None),
    enabled: bool | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    try:
        rt = QualityRuleType(rule_type) if rule_type else None
    except ValueError:
        from app.core.exceptions import ValidationError

        raise ValidationError(f"非法 rule_type: {rule_type}") from None
    try:
        sv = QualitySeverity(severity) if severity else None
    except ValueError:
        from app.core.exceptions import ValidationError

        raise ValidationError(f"非法 severity: {severity}") from None
    items, total = await QualityService(db).list_rules(
        metric_id,
        rt,
        sv,
        enabled,
        page,
        page_size,
        domain=user.domain,
        is_platform_admin=user.has_role("platform_admin"),
    )
    return ok(
        data={"items": items, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.get("/rules/{rule_id}", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_rule(
    rule_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(
        data=await QualityService(db).get_rule(
            rule_id,
            domain=user.domain,
            is_platform_admin=user.has_role("platform_admin"),
        ),
        trace_id=trace_id,
    )


@router.put("/rules/{rule_id}", dependencies=_WRITE_DEPS)
async def update_rule(
    rule_id: int,
    payload: QualityRuleUpdate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await QualityService(db).update_rule(
        rule_id,
        payload,
        domain=user.domain,
        is_platform_admin=user.has_role("platform_admin"),
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="quality_rule.update",
        entity_type="quality_rule",
        entity_id=str(rule_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.delete("/rules/{rule_id}", dependencies=_WRITE_DEPS)
async def delete_rule(
    rule_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    await QualityService(db).delete_rule(
        rule_id, domain=user.domain, is_platform_admin=user.has_role("platform_admin")
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="quality_rule.delete",
        entity_type="quality_rule",
        entity_id=str(rule_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"deleted": rule_id}, trace_id=trace_id)


@router.post(
    "/observe",
    status_code=201,
    dependencies=_WRITE_DEPS,
)
async def record_observation(
    payload: QualityObservationRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """写入一次质量观测样本（采集 / 产出分区就绪时调用），供动态基线 / 同环比 / 跨源检测复用。"""
    # 指标域归属校验（对齐规则/基准写路径）：域管理员不得向跨域指标写观测样本
    await _assert_metric_domain(db, user, metric_code=payload.metric_code)
    resp = await QualityService(db).record_observation(payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="quality_observation.record",
        entity_type="quality_observation",
        entity_id=str(resp.id),
        detail={"metric_code": resp.metric_code, "source_id": resp.source_id},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post("/events/detect", dependencies=_GOV_DEPS)
async def detect(
    payload: QualityDetectRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """质量检测引擎入口（一期：静态阈值评估；命中落异常事件并告警）。"""
    # 指标域归属校验：域管理员不得对跨域指标触发检测
    await _assert_metric_domain(db, user, metric_id=payload.metric_id)
    resp = await QualityService(db).detect(
        payload.metric_id,
        payload.rule_type,
        Decimal(str(payload.obs_value)),
        payload.rule_mode,
    )
    # PLAT-3: 质量检测命中落异常事件为治理写操作，须留痕；未命中（None）无写入不审计
    if resp is not None:
        await write_audit(
            db,
            actor_id=user.id,
            action="quality_event.detect",
            entity_type="quality_event",
            entity_id=str(resp.id),
            detail={
                "metric_id": resp.metric_id,
                "rule_type": resp.rule_type.value,
                "level": resp.level.value,
            },
            trace_id=trace_id,
        )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("/events", dependencies=_READ_DEPS)
async def list_events(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    metric_id: int | None = Query(None),
    status: str | None = Query(None),
    level: str | None = Query(None),
    # 个人工作台（待办中心）场景：非管理角色仅返回本人名下指标（Owner/副 Owner）
    # 的质量事件，避免把本域他人指标告警混入个人待办。
    mine_only: bool = Query(False, description="仅本人名下指标的质量事件"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    # P2：非法 status/level 返回 422 而非 500（list_rules 已有同样处理，此处补齐）
    try:
        st = QualityEventStatus(status) if status else None
    except ValueError:
        from app.core.exceptions import ValidationError

        raise ValidationError(f"非法 status: {status}") from None
    try:
        lv = QualitySeverity(level) if level else None
    except ValueError:
        from app.core.exceptions import ValidationError

        raise ValidationError(f"非法 level: {level}") from None
    items, total = await QualityService(db).list_events(
        metric_id,
        st,
        lv,
        page,
        page_size,
        domain=user.domain,
        is_platform_admin=user.has_role("platform_admin"),
        actor_id=user.id if mine_only else None,
    )
    return ok(
        data={"items": items, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.post(
    "/events/{event_id}/ack",
    dependencies=_GOV_DEPS,
)
async def ack_event(
    event_id: int,
    payload: QualityEventAck,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await QualityService(db).ack_event(
        event_id,
        payload.note,
        user.id,
        domain=user.domain,
        is_platform_admin=user.has_role("platform_admin"),
    )
    # PLAT-3: 审计须先于 commit，与业务同事务原子提交（避免业务落盘而审计随会话关闭丢失）
    await write_audit(
        db,
        actor_id=user.id,
        action="quality_event.ack",
        entity_type="quality_event",
        entity_id=str(event_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post(
    "/events/{event_id}/resolve",
    dependencies=_GOV_DEPS,
)
async def resolve_event(
    event_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await QualityService(db).resolve_event(
        event_id, user.id, domain=user.domain, is_platform_admin=user.has_role("platform_admin")
    )
    # PLAT-3: 审计先于 commit，同事务原子提交
    await write_audit(
        db,
        actor_id=user.id,
        action="quality_event.resolve",
        entity_type="quality_event",
        entity_id=str(event_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post(
    "/events/{event_id}/close",
    dependencies=_GOV_DEPS,
)
async def close_event(
    event_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await QualityService(db).close_event(
        event_id, user.id, domain=user.domain, is_platform_admin=user.has_role("platform_admin")
    )
    # PLAT-3: 审计先于 commit，同事务原子提交
    await write_audit(
        db,
        actor_id=user.id,
        action="quality_event.close",
        entity_type="quality_event",
        entity_id=str(event_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post(
    "/events/{event_id}/repair",
    dependencies=_GOV_DEPS,
)
async def confirm_repair(
    event_id: int,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """Owner 确认已线下修复（TD §4.8.5 闭环）：在修复建议中记录确认留痕。"""
    resp = await QualityService(db).confirm_repair(
        event_id, user.id, domain=user.domain, is_platform_admin=user.has_role("platform_admin")
    )
    # PLAT-3: 审计先于 commit，同事务原子提交
    await write_audit(
        db,
        actor_id=user.id,
        action="quality_event.confirm_repair",
        entity_type="quality_event",
        entity_id=str(event_id),
        detail={"confirmed_by": user.id},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


# --------------------------------------------------- 外部基准对账（TD §4.15.7）


@router.post(
    "/benchmarks/import",
    status_code=201,
    dependencies=_WRITE_DEPS,
)
async def import_benchmark(
    payload: BenchmarkImport,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """导入外部权威基准值（幂等）：同 key 重复导入视为更新。"""
    await _assert_metric_domain(db, user, metric_code=payload.metric_code)
    resp = await QualityService(db).import_benchmark(payload, user.id)
    # PLAT-3: 审计先于 commit，同事务原子提交
    await write_audit(
        db,
        actor_id=user.id,
        action="benchmark.import",
        entity_type="external_benchmark",
        entity_id=str(resp.id),
        detail={"metric_code": resp.metric_code, "provider": resp.provider},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("/benchmarks", dependencies=_READ_DEPS)
async def list_benchmarks(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    metric_code: str | None = Query(None),
    source_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    items, total = await QualityService(db).list_benchmarks(
        metric_code,
        source_id,
        page,
        page_size,
        domain=user.domain,
        is_platform_admin=user.has_role("platform_admin"),
    )
    return ok(
        data={"items": items, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.post(
    "/benchmarks/{benchmark_id}/bind",
    dependencies=_WRITE_DEPS,
)
async def bind_benchmark(
    benchmark_id: int,
    payload: BenchmarkBind,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """绑定基准到目标指标，声明比对口径 / 容忍率。"""
    await _assert_metric_domain(db, user, metric_code=payload.metric_code)
    resp = await QualityService(db).bind_benchmark(benchmark_id, payload, user.id)
    # PLAT-3: 审计先于 commit，同事务原子提交
    await write_audit(
        db,
        actor_id=user.id,
        action="benchmark.bind",
        entity_type="external_benchmark",
        entity_id=str(benchmark_id),
        detail={},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.post("/reconciliation/run", dependencies=_WRITE_DEPS)
async def run_reconciliation(
    payload: ReconciliationRun,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """执行一次对账：基准值 vs 平台观测值，自动判定差异状态（ALERT 触发告警）。"""
    resp = await QualityService(db).run_reconciliation(
        payload, user.id, domain=user.domain, is_platform_admin=user.has_role("platform_admin")
    )
    # PLAT-3: 审计先于 commit，同事务原子提交
    await write_audit(
        db,
        actor_id=user.id,
        action="reconciliation.run",
        entity_type="reconciliation_record",
        entity_id=str(resp.id),
        detail={
            "metric_code": resp.metric_code,
            "diff_pct": str(resp.diff_pct),
            "status": resp.status.value,
        },
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)


@router.get("/reconciliation-records", dependencies=_READ_DEPS)
async def list_reconciliation_records(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    status: str | None = Query(None),
    metric_code: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Any:
    items, total = await QualityService(db).list_reconciliations(
        status,
        metric_code,
        page,
        page_size,
        domain=user.domain,
        is_platform_admin=user.has_role("platform_admin"),
    )
    return ok(
        data={"items": items, "total": total, "page": page, "page_size": page_size},
        trace_id=trace_id,
    )


@router.post(
    "/reconciliation-records/{record_id}/confirm",
    dependencies=_GOV_DEPS,
)
async def confirm_reconciliation(
    record_id: int,
    payload: ReconciliationConfirm,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """Owner 确认差异（reasonable 合理 / caliber_error 口径有误→走变更）。"""
    resp = await QualityService(db).confirm_reconciliation(
        record_id,
        payload,
        user.id,
        domain=user.domain,
        is_platform_admin=user.has_role("platform_admin"),
    )
    # PLAT-3: 审计先于 commit，同事务原子提交
    await write_audit(
        db,
        actor_id=user.id,
        action="reconciliation.confirm",
        entity_type="reconciliation_record",
        entity_id=str(record_id),
        detail={"decision": payload.decision},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=resp, trace_id=trace_id)
