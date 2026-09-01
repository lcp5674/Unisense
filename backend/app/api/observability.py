"""可观测性 API（TD §12.10 / FR-16）。

P2 增强：NPS 采集 + 反馈采纳闭环。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, get_current_user, require_roles
from app.api.responses import get_trace_id, ok
from app.core.audit import write_audit
from app.core.guard import guard_against_injection
from app.db.mysql import get_db_session
from app.models.user import User
from app.services.observability.schemas import (
    FeedbackClarifyRequest,
    FeedbackCreate,
    FeedbackResponse,
)
from app.services.observability.service import ObservabilityService

router = APIRouter(prefix="/observability", tags=["observability"])

# 反馈/NPS 提交为用户自助（任何登录用户可提交建议），不按角色收窄——此前
# _WRITE_ROLES 排除 analyst/reviewer/compliance_officer，导致页面可点但 403。
_WRITE_ROLES = ALL_ROLES
# 运营统计/大盘（quality/api/notifications/lineage/overview/nps stats）为平台级
# OPS 遥测（依赖熔断/延迟/错误率、采集成功率、PII 待复核/授权到期风险雷达、审计动作
# 计数），仅平台/域管理员可读——对齐前端 observability:view 基线，杜绝 viewer/
# reviewer/metric_owner 绕过菜单直调 API 拉取全局运营数据。
_READ_ROLES = ("platform_admin", "domain_admin")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 反馈列表读：对齐前端 feedback:view 基线（平台/域管理员、指标负责人、评审、合规、
# 分析师），viewer 不可见他人反馈。
_FEEDBACK_READ_DEPS = [
    Depends(
        require_roles(
            "platform_admin", "domain_admin", "metric_owner",
            "reviewer", "compliance_officer", "analyst",
        )
    ),
    Depends(guard_against_injection),
]
# 指标健康度摘要：总览仪表「指标可信度」卡片数据源，全员可读（quality 类元数据，
# 对齐 quality:view 基线）；敏感 OPS 遥测（system/risks/审计计数）不在此端点。
_HEALTH_READ_DEPS = [Depends(require_roles(*ALL_ROLES)), Depends(guard_against_injection)]
# 写端点统一挂注入守卫（纵深防御：ORM 参数化兜底之外拦截注入 payload）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]


class NpsSubmitRequest(BaseModel):
    """NPS 提交请求。"""

    score: int = Field(..., ge=0, le=10, description="NPS 分数（0-10）")
    comment: str | None = Field(None, description="可选评论")
    target_type: str = Field("platform", description="目标类型")
    target_id: str | None = Field(None, description="目标 ID")


class FeedbackStatusUpdateRequest(BaseModel):
    """反馈状态更新请求。"""

    status: str = Field(
        ..., pattern="^(adopted|rejected|in_progress|clarifying)$", description="新状态"
    )
    resolution_note: str | None = Field(None, description="处理说明")


@router.post("/feedback", status_code=201, dependencies=_WRITE_DEPS)
async def submit_feedback(
    payload: FeedbackCreate,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    resp = await ObservabilityService(db).submit_feedback(payload, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="feedback.submit",
        entity_type="feedback",
        entity_id=str(resp.id),
        detail={},
        trace_id=trace_id,
    )
    # PLAT-3: 审计与业务同一事务原子提交
    await db.commit()
    return ok(data=FeedbackResponse.from_model(resp), trace_id=trace_id)


@router.get("/feedback", dependencies=_FEEDBACK_READ_DEPS)
async def list_feedback(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    target_type: str | None = Query(None),
    status: str | None = Query(
        None, description="过滤：adopted/rejected/in_progress/pending"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Any:
    """反馈列表（分页 + 状态过滤）。

    平台管理员全组织可见（org_id=None）；其余角色按反馈人所属组织隔离
    （防跨组织反馈/处理意见泄露给任意 viewer，对齐 /overview 的 org 语义）。
    """
    org_id = None if user.has_role("platform_admin") else getattr(user, "org_id", None)
    data = await ObservabilityService(db).list_feedback(
        target_type, status, page, page_size, org_id=org_id
    )
    target_names = data.get("target_names", {})
    items = []
    for i in data["items"]:
        resp = FeedbackResponse.from_model(i)
        resp.target_name = target_names.get(i.id)
        items.append(resp)
    return ok(
        data={
            "items": items,
            "total": data["total"],
            "page": data["page"],
            "page_size": data["page_size"],
        },
        trace_id=trace_id,
    )


@router.get("/metrics/quality", dependencies=_READ_DEPS)
async def quality_metrics(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await ObservabilityService(db).quality_stats(), trace_id=trace_id)


@router.get("/metrics/health", dependencies=_HEALTH_READ_DEPS)
async def metric_health(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """指标健康度摘要（总览仪表「指标可信度」卡片，全员可读）。

    与 /overview 的 quality.metric_health 同源；非管理角色按 P0-3 可见性收敛，
    管理角色全量。独立端点避免仪表盘经 /overview 拉取全局 OPS 遥测。
    """
    scope: dict[str, Any] = {}
    if user.role not in ("platform_admin", "domain_admin"):
        scope = {"actor_id": user.id, "role": user.role, "user_domain": user.domain}
    return ok(
        data=await ObservabilityService(db).metric_health_stats(**scope),
        trace_id=trace_id,
    )


@router.get("/quality-events", dependencies=_READ_DEPS)
async def quality_events_list(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    limit: int = Query(20, ge=1, le=100),
) -> Any:
    """最近质量事件明细：level/status/metric_id/created_at。"""
    items = await ObservabilityService(db).quality_events(limit)
    return ok(data={"items": items, "total": len(items)}, trace_id=trace_id)


@router.get("/metrics/api", dependencies=_READ_DEPS)
async def api_metrics(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await ObservabilityService(db).api_stats(), trace_id=trace_id)


@router.get("/metrics/notifications", dependencies=_READ_DEPS)
async def notification_metrics(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await ObservabilityService(db).notification_stats(), trace_id=trace_id)


@router.get("/metrics/lineage", dependencies=_READ_DEPS)
async def lineage_metrics(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    return ok(data=await ObservabilityService(db).lineage_stats(), trace_id=trace_id)


@router.get("/overview", dependencies=_READ_DEPS)
async def overview_metrics(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """平台运营总览：数据源健康 / 治理积压 / 资产规模 / 消费接入 一次拉齐。

    平台管理员全组织可见（org_id=None）；其余角色 PII 待复核数按本组织隔离。
    """
    org_id = None if user.has_role("platform_admin") else getattr(user, "org_id", None)
    return ok(
        data=await ObservabilityService(db).overview_stats(org_id=org_id),
        trace_id=trace_id,
    )


# ----------------------------------------------------------------
# P2 Enhancement: NPS 采集 + 反馈采纳闭环
# ----------------------------------------------------------------


@router.post("/nps", status_code=201, dependencies=_WRITE_DEPS)
async def submit_nps(
    payload: NpsSubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """NPS 采集：用户提交 0-10 推荐度评分。"""
    resp = await ObservabilityService(db).submit_nps(
        user_id=user.id,
        score=payload.score,
        comment=payload.comment,
        target_type=payload.target_type,
        target_id=payload.target_id,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="feedback.submit_nps",
        entity_type="feedback",
        entity_id=str(resp.id),
        detail={"score": payload.score},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=FeedbackResponse.from_model(resp), trace_id=trace_id)


@router.get("/nps/stats", dependencies=_FEEDBACK_READ_DEPS)
async def nps_stats(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """NPS 分布统计：total/promoters/passives/detractors/score。"""
    return ok(data=await ObservabilityService(db).nps_stats(), trace_id=trace_id)


@router.patch(
    "/feedback/{feedback_id}/status",
    dependencies=[
        Depends(require_roles("platform_admin", "domain_admin")),
        Depends(guard_against_injection),
    ],
)
async def update_feedback_status(
    feedback_id: int,
    payload: FeedbackStatusUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> Any:
    """反馈采纳闭环：更新反馈状态（adopted/rejected/in_progress）。"""
    resp = await ObservabilityService(db).update_feedback_status(
        feedback_id=feedback_id,
        status=payload.status,
        resolver_id=user.id,
        resolution_note=payload.resolution_note,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="feedback.update_status",
        entity_type="feedback",
        entity_id=str(feedback_id),
        detail={"status": payload.status, "note": payload.resolution_note},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=FeedbackResponse.from_model(resp), trace_id=trace_id)


@router.post("/feedback/{feedback_id}/clarify", dependencies=[Depends(guard_against_injection)])
async def clarify_feedback(
    feedback_id: int,
    payload: FeedbackClarifyRequest,
    db: AsyncSession = Depends(get_db_session),
    user: User = Depends(get_current_user),
    trace_id: Annotated[str, Depends(get_trace_id)] = "",
) -> Any:
    """质疑闭环：反馈提交人在 clarifying（待澄清）状态补充口径分歧说明。

    仅反馈提交人本人可澄清（PLAT-2 服务端校验）；澄清后状态回到 in_progress
    继续由处理人修订/采纳/驳回。
    """
    resp = await ObservabilityService(db).clarify_feedback(
        feedback_id, payload.clarification, user.id
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="feedback.clarify",
        entity_type="feedback",
        entity_id=str(feedback_id),
        detail={"clarification": payload.clarification[:200]},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=FeedbackResponse.from_model(resp), trace_id=trace_id)
