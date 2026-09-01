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

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.guard import (
    guard_against_injection,
    guard_against_injection_exempt_paths,
)
from app.db.mysql import get_db_session
from app.models.conflict import Conflict
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
# 读角色：冲突仲裁数据含指标口径对比（含 DRAFT 指标码），仅治理相关角色可见——
# 与前端 review:view 权限点基线对齐（viewer/analyst 无 review:view，不应读冲突）。
_READ_ROLES = ("platform_admin", "domain_admin", "metric_owner", "reviewer", "compliance_officer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]
_GOV_DEPS = [Depends(require_roles(*_GOV_ROLES)), Depends(guard_against_injection)]
# 冲突预检端点（/check）：candidate/existing 的口径字段承载合法 SQL/伪 SQL 文本
# （definition 纯文本口径 + definition_json 的 sql/expression 等），仅经 sqlglot 纯函数
# 比对/相似度计算，不执行、不拼接进任何 DB 查询——合法 ETL 的 -- 行注释/UNION/块注释/
# 多语句会被注入正则误伤（对齐 metrics create/update 的 definition_json/raw_sql 豁免，
# 修复注册链路最后误伤点）。其余字段与 query 参数仍全量扫描，纵深防御不削弱。
_CHECK_DEPS = [
    Depends(require_roles(*_WRITE_ROLES)),
    Depends(
        guard_against_injection_exempt_paths(
            "candidate.definition",
            "candidate.definition_json",
            "existing[].definition",
            "existing[].definition_json",
        )
    ),
]

logger = logging.getLogger("unisense.conflict.api")


async def _notify_rename_owner(
    db: AsyncSession,
    rename_metric_code: str,
    conflict_id: str,
    trace_id: str,
) -> None:
    """仲裁「保留差异+指定一方改名」后定向通知被改名指标的 Owner。

    Owner 据此在指标详情页看到「仲裁要求改名」引导并执行改名（跨服务一致性闭环：
    仲裁 → 标记 rename_required → 通知 Owner → 详情页改名 → 清除标记）。best-effort：
    指标不存在/通知失败均不阻断仲裁主流程，留日志告警。
    """
    try:
        from app.services.notify.service import NotifyService
        from app.services.semantic.service import MetricService

        metric_svc = MetricService(db)
        metric = await metric_svc.get_metric_public(rename_metric_code)
        owner_id = getattr(metric, "owner_id", None)
        if not owner_id:
            logger.warning("rename_owner_missing metric_code=%s", rename_metric_code)
            return
        await NotifyService(db).notify_user(
            user_id=int(owner_id),
            event_type="metric.rename_required",
            title="指标需要改名（口径冲突裁决）",
            body=(
                f"指标 {rename_metric_code} 在冲突 {conflict_id} 仲裁中被指定改名，"
                "请在指标详情页完成改名以区分同名不同义口径。"
            ),
            payload={
                "metric_code": rename_metric_code,
                "conflict_id": conflict_id,
                "source": "conflict",
            },
            channel="IN_APP",
        )
        logger.info(
            "rename_owner_notified metric_code=%s conflict_id=%s owner_id=%s trace_id=%s",
            rename_metric_code,
            conflict_id,
            owner_id,
            trace_id,
        )
    except Exception as exc:  # noqa: BLE001 - 通知降级，不阻断仲裁
        logger.warning("rename_owner_notify_failed: %s", exc)


async def _notify_loser_owner(
    db: AsyncSession,
    loser_code: str,
    winner_code: str,
    conflict_id: str,
    trace_id: str,
) -> None:
    """仲裁落败方指标 Owner 定向通知（废弃/作废，best-effort）。

    判定以落败方指标实际落库状态为准（与 arbitration 联动同源，不重复判定逻辑）：
    - DEPRECATED → 「已废弃」（event=metric.deprecated，后继=胜方）
    - 软删（deleted_at 非空）→ 「已作废」（event=metric.voided，后继=胜方）
    - 其他（强韧性保护未处置/指标缺失/自我冲突）→ 不通知

    与 `_notify_rename_owner` 对称：IN_APP 定向通知，不依赖订阅偏好；
    失败仅告警，不阻断仲裁主流程。
    """
    try:
        from app.services.notify.service import NotifyService

        row = (
            await db.execute(select(Metric).where(Metric.metric_code == loser_code))
        ).scalar_one_or_none()
        if row is None or row.owner_id is None:
            logger.warning(
                "loser_owner_missing metric_code=%s conflict_id=%s", loser_code, conflict_id
            )
            return
        if row.status == "DEPRECATED":
            event_type, verb = "metric.deprecated", "已废弃"
        elif row.deleted_at is not None:
            event_type, verb = "metric.voided", "已作废"
        else:
            return  # 强韧性保护跳过/未处置：落败方未实际变化，不通知
        await NotifyService(db).notify_user(
            user_id=int(row.owner_id),
            event_type=event_type,
            title=f"指标{verb}（口径仲裁）",
            body=(
                f"指标 {loser_code} 在冲突 {conflict_id} 仲裁中落败，"
                f"已{verb}，后继口径为 {winner_code}。"
            ),
            payload={
                "metric_code": loser_code,
                "successor_code": winner_code,
                "conflict_id": conflict_id,
                "source": "conflict",
            },
            channel="IN_APP",
        )
        logger.info(
            "loser_owner_notified metric_code=%s conflict_id=%s owner_id=%s trace_id=%s",
            loser_code,
            conflict_id,
            row.owner_id,
            trace_id,
        )
    except Exception as exc:  # noqa: BLE001 - 通知降级，不阻断仲裁
        logger.warning("loser_owner_notify_failed: %s", exc)


async def _notify_reopen_owners(
    db: AsyncSession,
    conflict: Conflict,
    trace_id: str,
) -> None:
    """重新打开冲突后定向通知双方指标 Owner（best-effort，不阻断 reopen）。

    与 `_notify_rename_owner`/`_notify_loser_owner` 对称：IN_APP 定向通知，
    不依赖订阅偏好（owner 未订阅也能收到）；失败仅告警，不阻断主流程。

    为什么需要定向：`conflict_reopened` 事件虽已纳入 EventBus 白名单并走订阅扇出，
    但冲突重开是强相关场景——双方 Owner 必须知道要重新裁决，不应依赖其手动订阅。
    """
    try:
        from app.services.notify.service import NotifyService

        codes = conflict.metric_codes or {}
        for code in (codes.get("candidate"), codes.get("existing")):
            if not code:
                continue
            row = (
                await db.execute(select(Metric).where(Metric.metric_code == code))
            ).scalar_one_or_none()
            if row is None or row.owner_id is None:
                logger.warning(
                    "reopen_owner_missing metric_code=%s conflict_id=%s",
                    code,
                    conflict.conflict_id,
                )
                continue
            await NotifyService(db).notify_user(
                user_id=int(row.owner_id),
                event_type="conflict_reopened",
                title="口径冲突已重开",
                body=(
                    f"冲突 {conflict.conflict_id} 已重新打开，涉及指标 {code}，"
                    "请在冲突仲裁中重新裁决。"
                ),
                payload={
                    "metric_code": code,
                    "conflict_id": conflict.conflict_id,
                    "source": "conflict",
                },
                channel="IN_APP",
            )
            logger.info(
                "reopen_owner_notified metric_code=%s conflict_id=%s owner_id=%s trace_id=%s",
                code,
                conflict.conflict_id,
                row.owner_id,
                trace_id,
            )
    except Exception as exc:  # noqa: BLE001 - 通知降级，不阻断 reopen
        logger.warning("reopen_owner_notify_failed: %s", exc)


async def _notify_arbitration_owners(
    db: AsyncSession,
    conflict: Conflict,
    payload: ArbitrateRequest,
    trace_id: str,
) -> None:
    """仲裁后定向通知受影响指标 Owner（best-effort，不阻断仲裁）。

    覆盖 TD §12.4 两条通知链路：
    1. 「保留差异+指定一方改名」→ 通知被改名指标 Owner 去详情页改名。
       rename_code 以 service 层解析结果（decision_json）为准——前端传
       rename_target（角色）或 rename_metric_code 均已被 service 归一化。
    2. 选权威 → 通知落败方指标 Owner：指标已被废弃（DEPRECATED）或已作废（软删）。
    """
    decision_json = conflict.decision_json or {}
    rename_code = decision_json.get("rename_metric_code")
    if rename_code:
        await _notify_rename_owner(db, rename_code, conflict.conflict_id, trace_id)
    canonical = payload.canonical_metric_code
    if not canonical:
        return  # keep_diff：无落败方，仅改名通知
    codes = conflict.metric_codes or {}
    loser_code: str | None
    winner_code: str | None
    if canonical == codes.get("candidate"):
        loser_code, winner_code = codes.get("existing"), codes.get("candidate")
    elif canonical == codes.get("existing"):
        loser_code, winner_code = codes.get("candidate"), codes.get("existing")
    else:
        return  # 权威方不在冲突双方：不通知
    if not loser_code or loser_code == winner_code:
        return  # 自我冲突（双方同码）：无独立落败方
    await _notify_loser_owner(db, loser_code, winner_code or "", conflict.conflict_id, trace_id)


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
        conflict: Any,
        decision: str,
        canonical_code: str | None,
        actor_id: int,
        *,
        rename_code: str | None = None,
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
            rename_code=rename_code,
        )

    async def _is_metric_archived(metric_code: str) -> bool:
        """跨服务一致性：判断关联指标是否已被仲裁软删作废（deleted_at 置位）。

        供 ConflictService.reopen 前置校验使用——仲裁联动把落败方软删后，
        冲突状态与指标状态须同步，已作废指标无法再参与重新仲裁。
        """
        row = (
            await db.execute(
                select(Metric).where(
                    Metric.metric_code == metric_code, Metric.deleted_at.is_not(None)
                )
            )
        ).scalar_one_or_none()
        return row is not None

    return ConflictService(
        db,
        events=ConflictEventPublisher(notify_url),
        llm=build_conflict_llm_client(),
        metric_conflict_clearer=_clear_metric_conflict,
        metric_conflict_marker=_mark_metric_conflict,
        arbitration_applier=_apply_arbitration,
        metric_archived_checker=_is_metric_archived,
    )


@router.post("/check", dependencies=_CHECK_DEPS)
async def check_conflict(
    payload: ConflictCheckRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """冲突检测；命中则落库 OPEN，硬冲突（同名不同义/PII）阻断发布返回 409。

    P0-A 修复：前端"冲突预检"按钮只传 candidate（existing 为空）时，服务端
    自动加载活动指标作对比对象——否则 ``check`` 遍历空列表永远返回"未检测到
    冲突"，用户主动求证的入口形同虚设。复用 MetricService.load_conflict_existing
    （P1-F/G 已修复 DEPRECATED 参与比对与 1000 条截断漏检）。
    """
    svc = _svc(db, request)
    existing = payload.existing
    if not existing:
        existing = await MetricService(db).load_conflict_existing()
    # P1-1：单条预检对自动加载的整批 existing 逐对 LLM 判定可能数百次——加批级
    # 预算（limit=10 对齐批量创建），耗尽降级纯词法，防刷耗 LLM 额度。
    llm_budget = {"used": 0, "limit": 10}
    result = await svc.check(payload.candidate, existing, llm_budget=llm_budget)
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
    # C-3（第七轮）：手动预检命中 → 同步给 existing 侧指标挂 pending_conflict 标记，
    # 消除「仲裁台有 OPEN 记录、指标目录无标记」的不一致；候选未创建无 metric_code 可挂，
    # 仅挂已存在的 existing 指标（existing_metric_id 非空即库中真实指标）。
    for det in result.detections:
        if det.existing_metric_id is not None and det.existing_code:
            await db.execute(
                update(Metric)
                .where(Metric.metric_code == det.existing_code, Metric.deleted_at.is_(None))
                .values(
                    pending_conflict=True,
                    pending_conflict_detail={
                        "status": "manual_precheck",
                        "conflict_id": getattr(det, "conflict_id", None),
                        "conflict_type": getattr(det.conflict_type, "value", None),
                        "score": getattr(det, "score", None),
                        "existing_code": det.existing_code,
                        "candidate_code": payload.candidate.metric_code,
                        "severity": det.severity,
                        "reason": "手动冲突预检命中，待协商或裁决",
                    },
                )
            )
    await db.commit()
    if result.blocked:
        from app.core.exceptions import ConflictError

        raise ConflictError(
            "检测到硬冲突，须协商或裁决后方可发布",
            ctx={"data": result.model_dump()},
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
        action="conflict.arbitrate",
        entity_type="conflict",
        entity_id=conflict_id,
        detail={
            "decision": payload.decision,
            "canonical": payload.canonical_metric_code,
            "rename_metric_code": payload.rename_metric_code,
        },
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    # 仲裁后定向通知受影响指标 Owner（best-effort，不阻断仲裁）：
    # 「保留差异+指定改名」→ 通知被改名方 Owner 去详情页改名；
    # 「选权威」→ 通知落败方 Owner 指标已废弃/作废（后继=胜方）。
    await _notify_arbitration_owners(db, conflict, payload, trace_id)
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
        action="conflict.escalate",
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
        action="conflict.close",
        entity_type="conflict",
        entity_id=conflict_id,
        detail={},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=ConflictResponse.from_model(conflict).model_dump(), trace_id=trace_id)


@router.post(
    "/{conflict_id}/force-close",
    dependencies=_GOV_DEPS,
)
async def force_close_conflict(
    conflict_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """强制关闭未决冲突（悬空处置：关联指标已删，仲裁失去对象）。"""
    svc = _svc(db, request)
    conflict = await svc.force_close(conflict_id, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="conflict.force_close",
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
        action="conflict.reopen",
        entity_type="conflict",
        entity_id=conflict_id,
        detail={},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    # 冲突重开强相关：定向通知双方指标 Owner（IN_APP，不依赖订阅偏好）
    await _notify_reopen_owners(db, conflict, trace_id)
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
