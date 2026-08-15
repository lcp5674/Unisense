"""冲突服务 API（TD §12.4 / FR-09）。

端点：
- POST /conflicts/check            冲突检测（来自 semantic 注册）；硬冲突返回 409 CONFLICT
- GET  /conflicts                  冲突列表（过滤+分页）
- POST /conflicts/{id}/arbitrate   仲裁（GOV-2 裁决记录）
- POST /conflicts/{id}/escalate    升级（超时前人工升级）
- POST /conflicts/{id}/close       关闭（RULED → CLOSED）
- POST /conflicts/{id}/reopen      重新打开（CLOSED → OPEN，重新裁决）
- GET  /conflicts/{id}/rulings     裁决记录（知识库）
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.models.metric import Metric
from app.services.conflict.arbitration import apply_arbitration_impact
from app.services.conflict.events import ConflictEventPublisher
from app.services.conflict.llm_client import build_conflict_llm_client
from app.services.conflict.schemas import (
    ArbitrateRequest,
    ConflictCheckRequest,
    ConflictListParams,
    ConflictResponse,
    EscalateRequest,
    RulingRecordResponse,
)
from app.services.conflict.service import ConflictService
from app.services.semantic.service import MetricService

router = APIRouter(prefix="/conflicts", tags=["conflict"])

# P2-4: 前端 MetricCreate「冲突预检」对全部写角色可见，platform_admin/domain_admin 也须可调
_WRITE_ROLES = ("metric_owner", "platform_admin", "domain_admin")
_GOV_ROLES = ("compliance_officer", "domain_admin", "platform_admin")
_READ_ROLES = ALL_ROLES
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]
_GOV_DEPS = [Depends(require_roles(*_GOV_ROLES)), Depends(guard_against_injection)]


def _svc(db: AsyncSession, request: Request) -> ConflictService:
    notify_url = getattr(request.app.state, "notify_url", None)

    async def _clear_metric_conflict(metric_code: str) -> None:
        """清除指标表的 pending_conflict 冗余标记（跨服务一致性联动）。

        仅清除冲突标记，不动指标其他字段；用条件更新避免整行读写竞态。
        """
        await db.execute(
            update(Metric)
            .where(Metric.metric_code == metric_code, Metric.deleted_at.is_(None))
            .values(pending_conflict=False, pending_conflict_detail=None)
        )

    async def _mark_metric_conflict(metric_code: str, conflict: Any) -> None:
        """重新打开冲突后回置指标表的 pending_conflict 冗余标记。

        与清除对称：冲突重新打开为待处理，指标详情页须重新显示「口径冲突待处理」。
        pending_conflict_detail 记录重新打开来源与冲突快照，便于详情页定位。
        """
        codes = conflict.metric_codes or {}
        detail = {
            "status": "reopened",
            "conflict_id": conflict.conflict_id,
            "conflict_type": getattr(conflict.type, "value", None),
            "score": conflict.similarity_score,
            "existing_code": codes.get("existing"),
            "reason": "冲突重新打开，待重新裁决",
        }
        await db.execute(
            update(Metric)
            .where(Metric.metric_code == metric_code, Metric.deleted_at.is_(None))
            .values(pending_conflict=True, pending_conflict_detail=detail)
        )

    async def _apply_arbitration(
        conflict: Any, decision: str, canonical_code: str | None, actor_id: int
    ) -> None:
        """仲裁联动指标（TD §12.4）：落败方废弃/作废、胜方标记权威、共存标记。

        与 conflict 主流程同事务：本端点随后的 db.commit() 一并落库。
        """
        await apply_arbitration_impact(
            db,
            conflict,
            decision,
            canonical_code,
            actor_id,
            metric_svc=MetricService(db),
        )

    return ConflictService(
        db,
        events=ConflictEventPublisher(notify_url),
        llm=build_conflict_llm_client(),
        metric_conflict_clearer=_clear_metric_conflict,
        metric_conflict_marker=_mark_metric_conflict,
        arbitration_applier=_apply_arbitration,
    )


@router.post("/check", dependencies=_WRITE_DEPS)
async def check_conflict(
    payload: ConflictCheckRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """冲突检测；命中则落库 OPEN，硬冲突（同名不同义/PII）阻断发布返回 409。"""
    svc = _svc(db, request)
    result = await svc.check(payload.candidate, payload.existing)
    # PLAT-3: 命中冲突会落库 OPEN，属治理写操作须留痕；无命中（纯读）不审计
    if result.detections:
        await write_audit(
            db,
            actor_id=user.id,
            action="conflict.check",
            entity_type="conflict",
            entity_id=payload.candidate.metric_code,
            detail={
                "candidate": payload.candidate.metric_code,
                "domain": payload.candidate.domain,
                "detections": [
                    {
                        "conflict_type": d.conflict_type.value,
                        "existing_code": d.existing_code,
                        "severity": d.severity,
                        "block_publish": d.block_publish,
                    }
                    for d in result.detections
                ],
                "blocked": result.blocked,
            },
            ip=client_ip(request),
            trace_id=trace_id,
        )
    await db.commit()
    if result.blocked:
        raise HTTPException(
            status_code=409,
            detail={
                "trace_id": trace_id,
                "message": "检测到硬冲突，须协商或裁决后方可发布",
                "data": result.model_dump(),
            },
        )
    return ok(data=result.model_dump(), trace_id=trace_id)


@router.get("", dependencies=_READ_DEPS)
async def list_conflicts(
    params: Annotated[ConflictListParams, Depends()],
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    svc = _svc(db, request)
    rows, total = await svc.list_conflicts(params)
    return ok(
        data={
            "items": [ConflictResponse.from_model(r).model_dump() for r in rows],
            "total": total,
            "page": params.page,
            "page_size": params.page_size,
        },
        trace_id=trace_id,
    )


@router.post(
    "/{conflict_id}/arbitrate",
    dependencies=_GOV_DEPS,
)
async def arbitrate_conflict(
    conflict_id: str,
    payload: ArbitrateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    svc = _svc(db, request)
    # PLAT-2: 以服务端认证身份 user.id 作为权威归因，覆盖客户端请求体的 arbitrator_id
    conflict = await svc.arbitrate(conflict_id, payload, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="CONFLICT_ARBITRATE",
        entity_type="conflict",
        entity_id=conflict_id,
        detail={"decision": payload.decision, "canonical": payload.canonical_metric_code},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=ConflictResponse.from_model(conflict).model_dump(), trace_id=trace_id)


@router.post(
    "/{conflict_id}/escalate",
    dependencies=_WRITE_DEPS,
)
async def escalate_conflict(
    conflict_id: str,
    payload: EscalateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    svc = _svc(db, request)
    conflict = await svc.escalate(conflict_id, payload)
    await write_audit(
        db,
        actor_id=user.id,
        action="CONFLICT_ESCALATE",
        entity_type="conflict",
        entity_id=conflict_id,
        detail={"note": payload.note},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=ConflictResponse.from_model(conflict).model_dump(), trace_id=trace_id)


@router.post(
    "/{conflict_id}/close",
    dependencies=_GOV_DEPS,
)
async def close_conflict(
    conflict_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    svc = _svc(db, request)
    conflict = await svc.close(conflict_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="CONFLICT_CLOSE",
        entity_type="conflict",
        entity_id=conflict_id,
        detail={},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=ConflictResponse.from_model(conflict).model_dump(), trace_id=trace_id)


@router.post(
    "/{conflict_id}/reopen",
    dependencies=_GOV_DEPS,
)
async def reopen_conflict(
    conflict_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    svc = _svc(db, request)
    conflict = await svc.reopen(conflict_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="CONFLICT_REOPEN",
        entity_type="conflict",
        entity_id=conflict_id,
        detail={},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=ConflictResponse.from_model(conflict).model_dump(), trace_id=trace_id)


@router.get("/{conflict_id}/rulings", dependencies=_READ_DEPS)
async def list_rulings(
    conflict_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    svc = _svc(db, request)
    rulings = await svc.get_rulings(conflict_id)
    return ok(
        data=[RulingRecordResponse.model_validate(r).model_dump() for r in rulings],
        trace_id=trace_id,
    )
