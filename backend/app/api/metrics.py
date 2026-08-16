"""指标语义定义 REST API（FR-05/06/07）。

全部成功响应套用统一信封 ``{code, message, data, trace_id}``（见 app.api.responses）。
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ALL_ROLES, CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.core.exceptions import ConflictError
from app.core.guard import guard_against_injection
from app.core.logging import get_logger
from app.db.mysql import get_db_session
from app.db.redis import get_redis
from app.services.collector.infer_guard import InferInflightGuard
from app.services.semantic.schemas import (
    MetricApproveRequest,
    MetricBatchApproveRequest,
    MetricBatchDeprecateRequest,
    MetricBatchItemResult,
    MetricBatchRegisterRequest,
    MetricBatchRejectRequest,
    MetricBatchResponse,
    MetricBatchSubmitRequest,
    MetricCompareRequest,
    MetricCreateRequest,
    MetricDeprecateRequest,
    MetricDescriptionUpdateRequest,
    MetricEmergencyPublishRequest,
    MetricHealthResponse,
    MetricListParams,
    MetricListResponse,
    MetricPublishRequest,
    MetricRejectRequest,
    MetricResponse,
    MetricSourceDroppedRequest,
    MetricSubmitRequest,
    MetricUpdateRequest,
    MetricVersionResponse,
    VersionConfirmRequest,
    VersionExtendRequest,
    VersionRejectRequest,
)
from app.services.semantic.service import MetricService, redact_definition
from app.services.subject_domain.service import SubjectDomainService

router = APIRouter(prefix="/metric-definitions", tags=["metric-definitions"])

logger = get_logger("unisense.api.metrics")


async def _register_metric_l3_lineage(db: AsyncSession, metric: Any) -> None:
    """指标创建/更新后注册 L3 指标血缘边（``metric:{code} ↔ table:{t}``，幂等）。

    让指标节点进入血缘体系，与 DP 血缘（dp_csv）/ SQL 解析（sqlglot）表级血缘
    衔接成「源表 → 指标 → 落地表」完整链路。注册失败仅记日志、不阻断主流程
    （血缘为辅助能力，可事后用 ``scripts/register_metric_lineage.py`` 补注册）。
    """
    try:
        from app.services.lineage.service import LineageService

        await LineageService(db).register_metric_from_definition(metric, commit=False)
    except Exception:
        logger.exception("metric_lineage_register_failed", metric_code=metric.metric_code)


@contextlib.asynccontextmanager
async def _metric_infer_inflight(metric_code: str) -> AsyncIterator[None]:
    """指标描述 LLM 推断 in-flight 去重（复用 collector 的 InferInflightGuard）。

    Redis 可用时 SET NX EX 跨进程去重；不可用降级为进程内去重。
    已有推断进行中时抛 409（LLM_INFER_IN_PROGRESS），前端据此提示「正在进行中」。
    关键场景：首次并发点击推断（都还没有描述）时避免双调 LLM。
    """
    owner_id = f"infer-metric-{uuid.uuid4().hex[:8]}"
    redis = None
    with contextlib.suppress(RuntimeError):
        redis = get_redis()  # Redis 不可用时降级为进程内去重
    guard = InferInflightGuard(redis)
    acquired = await guard.acquire("metric", metric_code, owner=owner_id)
    if not acquired:
        raise ConflictError(
            "该指标的 LLM 推断正在进行中，请稍后重试",
            error_code="LLM_INFER_IN_PROGRESS",
        )
    try:
        yield
    finally:
        await guard.release("metric", metric_code, owner=owner_id)


# 语义定义写操作允许的角色（对齐 RBAC：平台/域管理员 + 指标 Owner）
_WRITE_ROLES = ("platform_admin", "domain_admin", "metric_owner")
# 评审角色（TD §13 评审指派）：除管理角色外，被指派的评审员（reviewer 角色）
# 也可通过/打回指标——具体能否评审由 service 层按指派校验，此处仅放开入口
_REVIEW_ROLES = ("platform_admin", "domain_admin", "reviewer")
# PII 合规复核须由合规/域管理员执行，禁止指标 Owner 自审
# （对齐治理 COMPL-2 / governance._COMPLIANCE_ROLES）
_PII_REVIEW_ROLES = ("platform_admin", "domain_admin", "compliance_officer")
_READ_ROLES = ALL_ROLES
# PII 指标口径可读角色：仅管理/合规可见完整口径，其余角色读路径脱敏
_SENSITIVE_ROLES = ("platform_admin", "domain_admin", "compliance_officer")
_READ_DEPS = [Depends(require_roles(*_READ_ROLES)), Depends(guard_against_injection)]
# 写端点统一 RBAC + 注入守卫（对齐 semantic.py 的 _WRITE_DEPS 模式）
_WRITE_DEPS = [Depends(require_roles(*_WRITE_ROLES)), Depends(guard_against_injection)]


@router.post(
    "",
    response_model=ApiResponse[MetricResponse],
    status_code=201,
    summary="创建指标语义定义（FR-05）",
    dependencies=_WRITE_DEPS,
)
async def create_metric(
    request: MetricCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """创建指标语义定义（默认 DRAFT 状态，并生成版本 1 快照）。"""
    service = MetricService(db)
    metric = await service.create_metric(request, owner_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="CREATE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"domain": metric.domain, "type": metric.type, "pii_flag": metric.pii_flag},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    # L3 指标血缘：口径定义含 source_table/source_tables 时注册 metric↔table 边（同事务）
    await _register_metric_l3_lineage(db, metric)
    # PLAT-3: 业务写入 + 审计同事务原子提交（缺 commit 会导致事务随会话关闭被回滚）
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.get(
    "",
    response_model=ApiResponse[MetricListResponse],
    summary="查询指标语义定义列表（FR-06）",
    dependencies=_READ_DEPS,
)
async def list_metrics(
    params: Annotated[MetricListParams, Depends()],
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[MetricListResponse]:
    """支持域/状态/分级/关键词过滤与分页。"""
    service = MetricService(db)
    metrics, total = await service.list_metrics(params)
    # PII 读分级：非敏感角色对 PII 指标脱敏口径（保留键结构，值替换为 ***）
    sensitive = user.role in _SENSITIVE_ROLES
    items: list[MetricResponse] = []
    for m in metrics:
        item = MetricResponse.model_validate(m)
        if item.pii_flag and not sensitive:
            item = item.model_copy(
                update={"definition_json": redact_definition(item.definition_json)}
            )
        items.append(item)
    response = MetricListResponse(
        items=items,
        total=total,
        page=params.page,
        page_size=params.page_size,
    )
    # 批量 PII 访问审计（对齐 TD §15.4）：列表命中任何 PII 指标即记一条汇总审计，
    # 闭合「列表接口批量暴露 PII」的合规漏洞。
    pii_codes = [m.metric_code for m in metrics if m.pii_flag]
    if pii_codes:
        await write_audit(
            db,
            actor_id=user.id,
            action="LIST",
            entity_type="metric_definition",
            entity_id=f"pii_list:{len(pii_codes)}",
            detail={
                "data_classification": "PII",
                "count": len(pii_codes),
                "codes": pii_codes[:50],
            },
            ip=client_ip(request),
            trace_id=trace_id,
            pii_access=True,
        )
    # PLAT-3: PII 访问审计须提交持久化，否则随会话关闭被回滚（合规审计静默丢失）
    await db.commit()
    # 版本待确认标记：查询当前指标是否有 PENDING 状态确认记录
    metric_ids = [m.id for m in metrics]
    if metric_ids:
        from sqlalchemy import select

        from app.models.metric_version import PendingVersionConfirmation
        pending_rows = (
            (
                await db.execute(
                    select(PendingVersionConfirmation.metric_id).where(
                        PendingVersionConfirmation.metric_id.in_(metric_ids),
                        PendingVersionConfirmation.status == "PENDING",
                    )
                )
            )
            .scalars()
            .all()
        )
        pending_ids = set(pending_rows)
        for item in items:
            if item.id in pending_ids:
                item.pending_version = True
    # 健康度信号（目录页"健康"列）：批量查询 metric_health_score，无记录保持 None
    if metric_ids:
        from sqlalchemy import select

        from app.models.metric_health import MetricHealthScore
        health_rows = (
            await db.execute(
                select(
                    MetricHealthScore.metric_id,
                    MetricHealthScore.score,
                    MetricHealthScore.level,
                ).where(MetricHealthScore.metric_id.in_(metric_ids))
            )
        ).all()
        health_map = {r.metric_id: (r.score, r.level) for r in health_rows}
        for item in items:
            if item.id in health_map:
                item.health_score, item.health_level = health_map[item.id]
    return ok(data=response, trace_id=trace_id)


@router.get(
    "/{metric_code}",
    response_model=ApiResponse[MetricResponse],
    summary="获取指标语义定义详情（FR-06）",
    dependencies=_READ_DEPS,
)
async def get_metric(
    metric_code: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[MetricResponse]:
    service = MetricService(db)
    metric = await service.get_metric_public(metric_code)
    # PII 访问审计（对齐 TD §15.4 审计合规，data_classification=PII）
    if metric.pii_flag:
        await write_audit(
            db,
            actor_id=user.id,
            action="READ",
            entity_type="metric",
            entity_id=metric_code,
            detail={"data_classification": "PII", "metric_code": metric_code},
            ip=client_ip(request),
            trace_id=trace_id,
            pii_access=True,
        )
    # PLAT-3: PII 访问审计须提交持久化，否则随会话关闭被回滚（合规审计静默丢失）
    await db.commit()
    # PII 读分级：非敏感角色脱敏口径（保留键结构，值替换为 ***）
    data: MetricResponse = metric
    if metric.pii_flag and user.role not in _SENSITIVE_ROLES:
        data = metric.model_copy(
            update={"definition_json": redact_definition(metric.definition_json)}
        )
    return ok(data=data, trace_id=trace_id)


@router.get(
    "/{metric_code}/archived",
    response_model=ApiResponse[Any],
    summary="作废指标详情（含 successor 指针与历史口径，供作废引导页展示）",
    dependencies=_READ_DEPS,
)
async def get_archived_metric(
    metric_code: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """读取因口径仲裁作废指标的完整历史详情（口径定义/版本/裁决指针）。

    详情直访（GET /{code}）对作废指标返回 METRIC_ARCHIVED 错误码；本端点补充
    返回作废指标的**可读详情**（历史口径 + successor 指针 + 裁决标记），供前端
    作废引导页展示「指标详情 + 跳转权威指标」，而非仅一张错误卡片。
    """
    service = MetricService(db)
    data = await service.get_archived_metric_public(metric_code)
    metric = data["metric"]
    # PII 访问审计（对齐详情端点语义，标记 archived 来源）
    if metric.pii_flag:
        await write_audit(
            db,
            actor_id=user.id,
            action="READ",
            entity_type="metric",
            entity_id=metric_code,
            detail={"data_classification": "PII", "metric_code": metric_code, "archived": True},
            ip=client_ip(request),
            trace_id=trace_id,
            pii_access=True,
        )
    # PLAT-3: PII 访问审计须提交持久化
    await db.commit()
    # PII 读分级：非敏感角色脱敏口径（保留键结构，值替换为 ***）
    if metric.pii_flag and user.role not in _SENSITIVE_ROLES:
        data = {
            **data,
            "metric": metric.model_copy(
                update={"definition_json": redact_definition(metric.definition_json)}
            ),
        }
    return ok(data=data, trace_id=trace_id)


@router.post(
    "/{metric_code}/suggest-rename",
    response_model=ApiResponse[Any],
    summary="仲裁改名建议（LLM 生成区分性名称候选，FR-010）",
    dependencies=_READ_DEPS,
)
async def suggest_rename_metric(
    metric_code: str,
    request_body: dict[str, Any],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """为仲裁「保留差异+指定改名」生成 AI 建议名称候选（best-effort，LLM 不可用降级规则）。

    结合现有名称、对方指标名称、源表、度量列、所属域，LLM 生成最多 3 个
    「与对方明显区分」的中文业务名称候选；LLM 不可用/解析失败时降级为规则候选。
    仅作命名参考，不落库；Owner 在详情页改名弹窗中抉择或编辑后提交正式改名。
    """
    import json

    from app.services.semantic.service import MetricService

    service = MetricService(db)
    # 指标不存在/已作废时由 get_metric_public 抛标准异常（NOT_FOUND / METRIC_ARCHIVED）
    metric = await service.get_metric_public(metric_code)

    opposite_code = (request_body or {}).get("opposite_code") or None
    opposite_name: str | None = None
    if opposite_code:
        try:
            opp = await service.get_metric_public(opposite_code)
            opposite_name = opp.name
        except Exception:
            pass  # 对方指标不可读不影响建议（best-effort）

    defn = metric.definition_json or {}
    source_table = defn.get("source_table") if isinstance(defn, dict) else None
    measures = (defn or {}).get("measures") or (defn or {}).get("columns") or []
    measure: str | None = None
    if isinstance(measures, list) and measures:
        first = measures[0]
        if isinstance(first, dict):
            measure = first.get("name") or first.get("column")
        elif isinstance(first, str):
            measure = first

    cur_name = metric.name or metric.metric_code
    domain = metric.domain or ""
    suggestions: list[dict[str, Any]] = []

    # 1) LLM 生成（best-effort）：要求返回 JSON 数组，解析失败降级规则
    try:
        from app.services.llm.config_service import LlmConfigService

        llm_client = await LlmConfigService(db).build_client()
        if getattr(llm_client, "enabled", False):
            prompt = (
                "为一个需要与另一指标区分命名的指标生成 3 个中文业务名称候选。\n"
                f"现有名称={cur_name}；对方指标名称={opposite_name or '未知'}；\n"
                f"所属域={domain or '未知'}；源表={source_table or '未知'}；"
                f"度量列={measure or '未知'}。\n"
                "要求：语义准确、与对方名称明显区分、长度 4~12 字、适合作为指标展示名。\n"
                "严格只返回 JSON 数组，元素为 {\"name\": \"名称\", \"reason\": \"一句理由\"}，"
                "不要输出其他内容。"
            )
            resp = await llm_client.chat(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
            )
            raw = (resp.get("content") or "").strip().strip("`").strip()
            cleaned = raw
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip().strip("`").strip()
            try:
                parsed = json.loads(cleaned)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get("name"):
                        suggestions.append(
                            {
                                "name": str(item["name"]).strip(),
                                "reason": str(item.get("reason") or "").strip(),
                                "source": "llm",
                            }
                        )
    except Exception:
        pass  # LLM 故障/未配置：降级规则兜底

    # 2) 规则兜底：LLM 未产出有效候选时，基于上下文生成确定性候选
    if not suggestions:
        suffixes: list[str] = []
        if measure:
            suffixes.append(str(measure))
        if domain:
            suffixes.append(domain)
        if opposite_name:
            suffixes.append(opposite_name)
        for s in suffixes[:3]:
            suggestions.append(
                {
                    "name": f"{cur_name}（{s}）",
                    "reason": f"追加『{s}』以与对方区分同名不同义口径",
                    "source": "rule",
                }
            )
        if not suggestions:
            suggestions.append(
                {
                    "name": f"{cur_name}·新口径",
                    "reason": "规则兜底：追加『新口径』以区分同名指标",
                    "source": "rule",
                }
            )

    # 去重 + 截断为最多 3 个候选
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for cand in suggestions:
        n = cand["name"]
        if n in seen:
            continue
        seen.add(n)
        uniq.append(cand)
    return ok(data={"suggestions": uniq[:3], "current_name": cur_name}, trace_id=trace_id)


@router.put(
    "/{metric_code}",
    response_model=ApiResponse[MetricResponse],
    summary="更新指标语义定义（FR-05，带乐观锁与版本快照）",
    dependencies=_WRITE_DEPS,
)
async def update_metric(
    metric_code: str,
    request: MetricUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """变更口径时自动识别破坏性变更并递增版本号；乐观锁防止并发覆盖。"""
    service = MetricService(db)
    metric = await service.update_metric(
        metric_code, request, actor_id=user.id, role=user.role, user_domain=user.domain
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="UPDATE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"change_reason": request.change_reason, "pii_flag": metric.pii_flag},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    # L3 指标血缘：口径变更后幂等重注册 metric↔table 边（同事务）
    await _register_metric_l3_lineage(db, metric)
    # PLAT-3: 业务写入 + 审计同事务原子提交
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.put(
    "/{metric_code}/description",
    response_model=ApiResponse[MetricResponse],
    summary="更新指标业务描述（治理补充 TD §12.1，不触发版本/不参与口径变更）",
    dependencies=_WRITE_DEPS,
)
async def update_metric_description(
    metric_code: str,
    request: MetricDescriptionUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """资产地图/指标详情补充描述；空串清除；写审计与业务同事务提交。"""
    service = MetricService(db)
    metric = await service.update_metric_description(
        metric_code, request, actor_id=user.id, role=user.role, user_domain=user.domain
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="UPDATE",
        entity_type="metric_description",
        entity_id=metric.metric_code,
        detail={"cleared": not (request.description or "").strip()},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/infer-description",
    response_model=ApiResponse[MetricResponse],
    summary="LLM 推断指标业务描述（治理补充 TD §12.1，不触发版本/不参与口径变更）",
    dependencies=_WRITE_DEPS,
)
async def infer_metric_description(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
    force: bool = Query(False, description="强制重新推断；默认已存在 LLM 描述时短路返回"),
) -> ApiResponse[MetricResponse]:
    """资产地图/指标详情一键 LLM 推断描述并落库（source=llm）；写审计与业务同事务提交。

    ``force=false``（默认）时若指标已有 LLM 推断描述则短路返回，避免重复调用 LLM；
    ``force=true`` 忽略已有描述强制重新生成（前端"重新生成"确认后使用）。
    """
    service = MetricService(db)
    # FR-023: in-flight 去重——同一指标推断进行中时拒绝重复请求（409）
    async with _metric_infer_inflight(metric_code):
        metric = await service.infer_metric_description(
            metric_code,
            actor_id=user.id,
            role=user.role,
            user_domain=user.domain,
            force=force,
        )
    await write_audit(
        db,
        actor_id=user.id,
        action="UPDATE",
        entity_type="metric_description",
        entity_id=metric.metric_code,
        detail={"source": "llm"},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/publish",
    response_model=ApiResponse[MetricResponse],
    summary="发布指标（FR-07，路由到 approve_metric）",
    dependencies=_WRITE_DEPS,
)
async def publish_metric(
    metric_code: str,
    request: MetricPublishRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """发布指标（内部路由到 approve_metric，推荐直接使用 submit+approve）。"""
    service = MetricService(db)
    approve_req = MetricApproveRequest(
        mode="standard",
        target_version=request.version,
    )
    metric = await service.approve_metric(
        metric_code, approve_req, actor_id=user.id, role=user.role
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="PUBLISH",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"version": request.version, "pii_flag": metric.pii_flag},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/deprecate",
    response_model=ApiResponse[MetricResponse],
    summary="废弃指标（FR-07，successor_code 必填）",
    dependencies=_WRITE_DEPS,
)
async def deprecate_metric(
    metric_code: str,
    request: MetricDeprecateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """仅 PUBLISHED 状态可废弃，successor_code 必填且须为已发布指标。"""
    service = MetricService(db)
    metric = await service.deprecate_metric(
        metric_code,
        successor_code=request.successor_code,
        actor_id=user.id,
        role=user.role,
        user_domain=user.domain,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="DEPRECATE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"successor_code": request.successor_code},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/submit",
    response_model=ApiResponse[MetricResponse],
    summary="提交指标审核（FR-003，DRAFT → REVIEW）",
    dependencies=_WRITE_DEPS,
)
async def submit_metric(
    metric_code: str,
    request: MetricSubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """DRAFT → REVIEW，提交审核。状态机校验，非法跃迁返回 409。"""
    service = MetricService(db)
    metric = await service.submit_metric(
        metric_code, request, actor_id=user.id, role=user.role, user_domain=user.domain
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="SUBMIT",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"change_reason": request.change_reason},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/approve",
    response_model=ApiResponse[MetricResponse],
    summary="审核通过指标（FR-004，REVIEW → PUBLISHED/EXPERIMENTAL）",
    dependencies=[Depends(require_roles(*_REVIEW_ROLES)), Depends(guard_against_injection)],
)
async def approve_metric(
    metric_code: str,
    request: MetricApproveRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """REVIEW → PUBLISHED(standard) / EXPERIMENTAL(experimental)。含 PII 门禁 + 依赖校验。"""
    service = MetricService(db)
    metric = await service.approve_metric(
        metric_code, request, actor_id=user.id, role=user.role, user_domain=user.domain
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="APPROVE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"mode": request.mode, "target_version": request.target_version},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/reject",
    response_model=ApiResponse[MetricResponse],
    summary="审核驳回指标（FR-005，REVIEW → DRAFT）",
    dependencies=[Depends(require_roles(*_REVIEW_ROLES)), Depends(guard_against_injection)],
)
async def reject_metric(
    metric_code: str,
    request: MetricRejectRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """REVIEW → DRAFT，驳回审核。须填驳回原因，通知 Owner。"""
    service = MetricService(db)
    metric = await service.reject_metric(
        metric_code, request, actor_id=user.id, role=user.role, user_domain=user.domain
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="REJECT",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"reason": request.reason},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/confirm-version",
    response_model=ApiResponse[MetricResponse],
    summary="消费方确认版本（FR-007）",
    dependencies=_WRITE_DEPS,
)
async def confirm_version(
    metric_code: str,
    request: VersionConfirmRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """消费方确认 PENDING_VERSION：全部确认后新版本升 CURRENT。"""
    service = MetricService(db)
    metric = await service.confirm_version(metric_code, request.version, consumer_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="CONFIRM_VERSION",
        entity_type="metric_definition",
        entity_id=metric_code,
        detail={"version": request.version},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/reject-version",
    response_model=ApiResponse[MetricResponse],
    summary="消费方拒绝版本（FR-007）",
    dependencies=_WRITE_DEPS,
)
async def reject_version(
    metric_code: str,
    request: VersionRejectRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """消费方拒绝 PENDING_VERSION：任一拒绝则版本取消，旧版本保持 CURRENT。"""
    service = MetricService(db)
    metric = await service.reject_version(
        metric_code, request.version, reason=request.reason, consumer_id=user.id
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="REJECT_VERSION",
        entity_type="metric_definition",
        entity_id=metric_code,
        detail={"version": request.version, "reason": request.reason},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/extend-version",
    response_model=ApiResponse[MetricResponse],
    summary="版本确认延期（FR-008，+7 天，最多延期 1 次）",
    dependencies=_WRITE_DEPS,
)
async def extend_version(
    metric_code: str,
    request: VersionExtendRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """Owner 请求延期确认：+7 天，最多延期 1 次。"""
    service = MetricService(db)
    metric = await service.extend_version(metric_code, request.version)
    await write_audit(
        db,
        actor_id=user.id,
        action="EXTEND_VERSION",
        entity_type="metric_definition",
        entity_id=metric_code,
        detail={"version": request.version},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.delete(
    "/{metric_code}",
    response_model=ApiResponse[None],
    summary="删除指标（FR-07，软删除，仅 DRAFT 状态）",
    dependencies=[Depends(require_roles("platform_admin")), Depends(guard_against_injection)],
)
async def delete_metric(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[None]:
    """仅 platform_admin 可软删除 DRAFT 状态指标（非 DRAFT 拒绝）。"""
    service = MetricService(db)
    metric = await service.delete_metric(metric_code, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="DELETE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"status": metric.status},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    # PLAT-3: 业务写入 + 审计同事务原子提交
    await db.commit()
    return ok(data=None, trace_id=trace_id)


@router.post(
    "/{metric_code}/restore",
    response_model=ApiResponse[MetricResponse],
    summary="恢复已软删指标（回收站恢复，仅 DRAFT 且已删状态）",
    dependencies=_WRITE_DEPS,
)
async def restore_metric(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """恢复软删草稿指标；仅平台管理员或指标原 Owner 可恢复（service 层校验）。"""
    service = MetricService(db)
    metric = await service.restore_metric(metric_code, actor_id=user.id, role=user.role)
    await write_audit(
        db,
        actor_id=user.id,
        action="RESTORE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"status": metric.status, "actor_role": user.role},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    # PLAT-3: 业务写入 + 审计同事务原子提交
    await db.commit()
    return ok(data=MetricResponse.model_validate(metric), trace_id=trace_id)


@router.post(
    "/{metric_code}/promote",
    response_model=ApiResponse[MetricResponse],
    summary="灰度全量发布（FR-020，EXPERIMENTAL → PUBLISHED）",
    dependencies=_WRITE_DEPS,
)
async def promote_metric(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """灰度指标全量发布：清除灰度白名单，状态升为 PUBLISHED。"""
    service = MetricService(db)
    metric = await service.promote_metric(metric_code, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="PROMOTE",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"from_status": "EXPERIMENTAL", "to_status": "PUBLISHED"},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.post(
    "/{metric_code}/rollback",
    response_model=ApiResponse[MetricResponse],
    summary="灰度回滚（FR-020，EXPERIMENTAL → 回退上一 PUBLISHED 版本）",
    dependencies=_WRITE_DEPS,
)
async def rollback_metric(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """灰度指标回滚：EXPERIMENTAL 版本标记 ARCHIVED，回退到上一 PUBLISHED 版本。"""
    service = MetricService(db)
    metric = await service.rollback_metric(metric_code, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="ROLLBACK",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"from_status": "EXPERIMENTAL", "action": "rollback_to_previous_published"},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


@router.get(
    "/{metric_code}/versions",
    response_model=ApiResponse[list[MetricVersionResponse]],
    summary="查看指标版本历史（FR-05）",
    dependencies=_READ_DEPS,
)
async def get_metric_versions(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[list[MetricVersionResponse]]:
    service = MetricService(db)
    versions = await service.get_versions(metric_code)
    response = [MetricVersionResponse.model_validate(v) for v in versions]
    return ok(data=response, trace_id=trace_id)


@router.post(
    "/{metric_code}/pii-review",
    response_model=ApiResponse[MetricResponse],
    summary="PII 合规复核（打通 PII 指标发布闸门）",
    dependencies=[Depends(require_roles(*_PII_REVIEW_ROLES)), Depends(guard_against_injection)],
)
async def review_metric_compliance(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """PII 指标合规复核：置 compliance_reviewed=True，解除发布闸门（禁 Owner 自审）。"""
    service = MetricService(db)
    metric = await service.review_compliance(metric_code, actor_id=user.id, role=user.role)
    await write_audit(
        db,
        actor_id=user.id,
        action="PII_REVIEW",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"compliance_reviewed": metric.compliance_reviewed},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    # PLAT-3: 业务写入 + 审计同事务原子提交
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


# ----------------------------------------------------------------
# 紧急发布
# ----------------------------------------------------------------


@router.post(
    "/{metric_code}/emergency-publish",
    response_model=ApiResponse[MetricResponse],
    summary="紧急发布指标（跳过REVIEW，须填紧急原因，PII门禁不可跳）",
    dependencies=[
        Depends(require_roles("platform_admin", "domain_admin")),
        Depends(guard_against_injection),
    ],
)
async def emergency_publish_metric(
    metric_code: str,
    request: MetricEmergencyPublishRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """domain_admin 紧急发布：跳过 REVIEW 但不跳 PII 门禁。"""
    # OPS-09 特性开关：紧急发布能力可被平台管理员灰度关闭（默认开启，非破坏）
    from app.core.exceptions import AuthError
    from app.core.feature_flags import is_feature_enabled_or_default

    if not is_feature_enabled_or_default("emergency_publish"):
        raise AuthError(
            "紧急发布能力已被平台管理员关闭，请走常规评审发布流程",
            error_code="FORBIDDEN",
            ctx={"feature_flag": "emergency_publish"},
        )
    service = MetricService(db)
    metric = await service.emergency_publish_metric(
        metric_code,
        request,
        actor_id=user.id,
        role=user.role,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="EMERGENCY_PUBLISH",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={
            "reason": request.reason,
            "emergency_publish": True,
            "pii_flag": metric.pii_flag,
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data=MetricResponse.model_validate(metric),
        trace_id=trace_id,
    )


# ----------------------------------------------------------------
# 健康度评分
# ----------------------------------------------------------------


@router.get(
    "/{metric_code}/health",
    response_model=ApiResponse[MetricHealthResponse],
    summary="获取指标健康度评分（五维加权）",
    dependencies=_READ_DEPS,
)
async def get_metric_health(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[MetricHealthResponse]:
    """五维加权健康度评分：口径完整度/活跃度/质量/Owner响应/血缘覆盖。"""
    service = MetricService(db)
    health = await service.get_metric_health(metric_code)
    await db.commit()
    return ok(data=MetricHealthResponse.model_validate(health), trace_id=trace_id)


# ----------------------------------------------------------------
# 指标对比
# ----------------------------------------------------------------


@router.post(
    "/compare",
    response_model=ApiResponse,
    summary="两指标关键字段并排对比",
    dependencies=_READ_DEPS,
)
async def compare_metrics(
    request: MetricCompareRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """两指标关键字段并排 diff + 差异标记。"""
    service = MetricService(db)
    # T049: PII 指标对比需合规角色权限，非合规角色对 PII 指标返回脱敏口径
    result = await service.compare_metrics(
        request.metric_codes[0],
        request.metric_codes[1],
    )
    # PII 脱敏：非合规角色对比 PII 指标时，口径定义脱敏
    if user.role not in _SENSITIVE_ROLES:
        for key in ("fields",):
            field_data = result.get(key, {})
            if "definition" in field_data:
                for side in ("a", "b"):
                    defn = field_data["definition"].get(side)
                    if isinstance(defn, dict) and defn.get("pii"):
                        field_data["definition"][side] = redact_definition(defn)
    return ok(data=result, trace_id=trace_id)


# ----------------------------------------------------------------
# 批量注册
# ----------------------------------------------------------------


@router.post(
    "/batch-register",
    response_model=ApiResponse,
    summary="批量注册指标（从宽表度量列批量创建 DRAFT）",
    dependencies=_WRITE_DEPS,
)
async def batch_register_metrics(
    request: MetricBatchRegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """批量注册：LLM 预填 + 逐条校验 + 共享 batch_id。"""
    service = MetricService(db)
    result = await service.batch_register_metrics(request, actor_id=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="BATCH_REGISTER",
        entity_type="metric_definition",
        entity_id=f"batch:{result['batch_id']}",
        detail={"count": len(result["candidates"]), "domain": request.domain},
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result, trace_id=trace_id)


@router.post(
    "/auto-suggest",
    response_model=ApiResponse[Any],
    summary="指标注册自动推断（FR-010/FR-011）",
    dependencies=_READ_DEPS,
)
async def auto_suggest_metric(
    request_body: dict[str, Any],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    trace_id: Annotated[str, Depends(get_trace_id)],
) -> ApiResponse[Any]:
    """输入域 +（SQL 或 源表+度量列+周期）→ 返回 13 字段推断 + 口径定义/模式。

    推断优先级：域默认值 > SQL 解析 > 列元数据 > 规则 > AI/兜底。
    枚举字段全部确定性规则产出（合法字典 code）；仅名称可选 LLM（不可用自动降级）。
    对齐 spec FR-010/FR-011, plan.md auto-suggest API。
    """
    from app.models.data_source import DBCatalog
    from app.services.semantic.auto_fill import auto_fill
    from app.services.semantic.sql_infer import parse_sql_profile

    domain_code = request_body.get("domain_code", "")
    source_table = request_body.get("source_table")
    measure_column = request_body.get("measure_column")
    period = request_body.get("period")
    sql = request_body.get("sql")

    # 获取域默认值预设
    domain_defaults: dict[str, Any] = {}
    if domain_code:
        try:
            domain_service = SubjectDomainService(db)
            domain_defaults = await domain_service.get_defaults(domain_code)
        except Exception:
            pass  # 域不存在时默认值为空

    # SQL 解析（best-effort；失败不影响后续规则推断）
    parsed = parse_sql_profile(sql) if sql else None
    effective_table = source_table
    if (not effective_table) and parsed and parsed.source_tables:
        effective_table = parsed.source_tables[0]
    effective_measure = measure_column
    if (not effective_measure) and parsed and parsed.measures:
        effective_measure = parsed.measures[0]["column"]

    # 列元数据富集（best-effort）：从采集目录取列类型/注释/表刷新频率
    measure_meta: dict[str, Any] = {}
    table_meta: dict[str, Any] = {}
    if effective_table and effective_measure:
        try:
            norm_table = effective_table.split(".")[-1]
            # 通配符转义（对齐 FR-035）：表名用户可控，含 %/_ 时防模糊放大
            esc_table = norm_table.replace("/", "//").replace("%", "/%").replace("_", "/_")
            stmt = (
                select(DBCatalog)
                .where(DBCatalog.entity_name.like(f"%{esc_table}", escape="/"))
                .where(DBCatalog.deleted_at.is_(None))
                .limit(5)
            )
            rows = (await db.execute(stmt)).scalars().all()
            for row in rows:
                schema = row.schema_json or {}
                columns = schema.get("columns") if isinstance(schema, dict) else schema
                if isinstance(columns, list):
                    for col in columns:
                        if isinstance(col, dict) and col.get("name") == effective_measure:
                            measure_meta = {
                                "type": col.get("type", ""),
                                "comment": col.get("comment", ""),
                                "name": effective_measure,
                            }
                            break
                # 表级元数据：库名/注释推断刷新频率
                if row.schema_json and isinstance(row.schema_json, dict):
                    table_meta = {
                        "freshness": row.schema_json.get("freshness"),
                        "comment": row.schema_json.get("comment", ""),
                    }
                if measure_meta:
                    break
        except Exception:
            pass  # 富集失败不阻断推断

    # 可选 LLM 命名（best-effort，不可用降级到规则）
    llm_name: str | None = None
    try:
        from app.services.llm.config_service import LlmConfigService

        llm_client = await LlmConfigService(db).build_client()
        if getattr(llm_client, "enabled", False) and effective_table:
            period_cn = {
                "day": "日", "week": "周", "month": "月",
                "quarter": "季", "year": "年", "hour": "小时",
            }.get(
                (period or "day").lower(), "日"
            )
            prompt = (
                f"为指标生成中文业务名称。源表={effective_table}，度量列={effective_measure}，"
                f"统计周期={period_cn}，聚合={measure_meta.get('comment', '') or '见 SQL'}。"
                f"只返回名称本身（如：日订单金额），不要解释、不要引号、不要 JSON。"
            )
            resp = await llm_client.chat(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=32,
            )
            raw = (resp.get("content") or "").strip().strip("\"'").strip("`").strip()
            if raw:
                llm_name = raw
    except Exception:
        pass  # LLM 不可用 → 规则兜底

    result = auto_fill(
        domain_code=domain_code,
        source_table=effective_table,
        measure_column=effective_measure,
        period=period,
        domain_defaults=domain_defaults,
        sql=sql,
        measure_meta=measure_meta or None,
        table_meta=table_meta or None,
    )
    # 注入 LLM 名称（走 name 字段的 AI 来源）
    if llm_name and result["fields"].get("name", {}).get("source") != "column_meta":
        result["fields"]["name"] = {
            "value": llm_name,
            "source": "llm",
            "confidence": 0.7,
            "reason": "AI 依据表结构/SQL 生成的业务命名",
        }

    # 依赖表推断：从血缘图中提取源表的上游/下游关联表，供「口径定义 → 关联数据表」自动填充
    related_tables: list[str] = []
    if effective_table:
        try:
            from app.services.lineage.parser import node_table
            from app.services.lineage.repository import LineageRepository

            edges = await LineageRepository(db).edges_for_node(
                node_table(effective_table), direction="both"
            )
            self_node = node_table(effective_table)
            seen: set[str] = set()
            for edge in edges:
                for node in (edge.source_node, edge.target_node):
                    if node.startswith("table:") and node != self_node:
                        name = node[len("table:"):]
                        if name not in seen:
                            seen.add(name)
                            related_tables.append(name)
        except Exception:
            pass  # 血缘不可用/无关联边 → 不阻断推断

    result["related_tables"] = related_tables
    return ok(data=result, trace_id=trace_id)


# ---- 批量治理端点（TD §13：提交/通过/打回/下线，逐条收集结果不整体失败）----


def _batch_response(results: list[MetricBatchItemResult]) -> MetricBatchResponse:
    """组装批量响应（统计成功/失败数）。"""
    return MetricBatchResponse(
        results=results,
        ok_count=sum(1 for r in results if r.ok),
        fail_count=sum(1 for r in results if not r.ok),
    )


def _batch_failed_codes(results: list[MetricBatchItemResult]) -> list[str]:
    """批量操作的失败明细（编码+原因），供审计逐条追溯；截断 20 条防审计膨胀。"""
    return [f"{r.metric_code}: {r.message}" for r in results if not r.ok][:20]


@router.post(
    "/batch-submit",
    response_model=ApiResponse[MetricBatchResponse],
    summary="批量提交指标审核（可带评审指派，TD §13）",
    dependencies=_WRITE_DEPS,
)
async def batch_submit_metrics(
    request: MetricBatchSubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricBatchResponse]:
    """逐条 DRAFT→REVIEW；单条失败不阻断其余（返回逐条结果）。"""
    service = MetricService(db)
    results: list[MetricBatchItemResult] = []
    for item in request.items:
        try:
            await service.submit_metric(
                item.metric_code,
                MetricSubmitRequest(
                    change_reason=item.change_reason,
                    reviewer_id=item.reviewer_id,
                    reviewer_type=item.reviewer_type,
                    reviewer_domain=item.reviewer_domain,
                ),
                actor_id=user.id,
                role=user.role,
                user_domain=user.domain,
            )
            results.append(MetricBatchItemResult(metric_code=item.metric_code, ok=True))
        except Exception as exc:  # noqa: BLE001 - 批量逐条容错，单条失败不整体回滚
            results.append(
                MetricBatchItemResult(metric_code=item.metric_code, ok=False, message=str(exc))
            )
    await write_audit(
        db,
        actor_id=user.id,
        action="BATCH_SUBMIT",
        entity_type="metric_definition",
        entity_id=f"batch:{len(request.items)}",
        detail={
            "failed_codes": _batch_failed_codes(results),
            "ok": sum(1 for r in results if r.ok),
            "fail": sum(1 for r in results if not r.ok),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=_batch_response(results), trace_id=trace_id)


@router.post(
    "/batch-approve",
    response_model=ApiResponse[MetricBatchResponse],
    summary="批量审核通过（REVIEW → PUBLISHED/EXPERIMENTAL，即批量发布）",
    dependencies=[Depends(require_roles(*_REVIEW_ROLES)), Depends(guard_against_injection)],
)
async def batch_approve_metrics(
    request: MetricBatchApproveRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricBatchResponse]:
    """逐条 REVIEW→PUBLISHED/EXPERIMENTAL；评审人指派校验由 service 层逐条执行。"""
    service = MetricService(db)
    results: list[MetricBatchItemResult] = []
    for code in request.metric_codes:
        try:
            await service.approve_metric(
                code,
                MetricApproveRequest(
                    mode=request.mode, gray_tenant_ids=request.gray_tenant_ids
                ),
                actor_id=user.id,
                role=user.role,
                user_domain=user.domain,
            )
            results.append(MetricBatchItemResult(metric_code=code, ok=True))
        except Exception as exc:  # noqa: BLE001 - 批量逐条容错
            results.append(MetricBatchItemResult(metric_code=code, ok=False, message=str(exc)))
    await write_audit(
        db,
        actor_id=user.id,
        action="BATCH_APPROVE",
        entity_type="metric_definition",
        entity_id=f"batch:{len(request.metric_codes)}",
        detail={
            "mode": request.mode,
            "failed_codes": _batch_failed_codes(results),
            "ok": sum(1 for r in results if r.ok),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=_batch_response(results), trace_id=trace_id)


@router.post(
    "/batch-reject",
    response_model=ApiResponse[MetricBatchResponse],
    summary="批量审核驳回（REVIEW → DRAFT）",
    dependencies=[Depends(require_roles(*_REVIEW_ROLES)), Depends(guard_against_injection)],
)
async def batch_reject_metrics(
    request: MetricBatchRejectRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricBatchResponse]:
    """逐条 REVIEW→DRAFT；评审人指派校验由 service 层逐条执行。"""
    service = MetricService(db)
    results: list[MetricBatchItemResult] = []
    for code in request.metric_codes:
        try:
            await service.reject_metric(
                code,
                MetricRejectRequest(reason=request.reason),
                actor_id=user.id,
                role=user.role,
                user_domain=user.domain,
            )
            results.append(MetricBatchItemResult(metric_code=code, ok=True))
        except Exception as exc:  # noqa: BLE001 - 批量逐条容错
            results.append(MetricBatchItemResult(metric_code=code, ok=False, message=str(exc)))
    await write_audit(
        db,
        actor_id=user.id,
        action="BATCH_REJECT",
        entity_type="metric_definition",
        entity_id=f"batch:{len(request.metric_codes)}",
        detail={
            "failed_codes": _batch_failed_codes(results),
            "ok": sum(1 for r in results if r.ok),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=_batch_response(results), trace_id=trace_id)


@router.post(
    "/batch-deprecate",
    response_model=ApiResponse[MetricBatchResponse],
    summary="批量下线（废弃）指标（PUBLISHED → DEPRECATED）",
    dependencies=_WRITE_DEPS,
)
async def batch_deprecate_metrics(
    request: MetricBatchDeprecateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricBatchResponse]:
    """逐条 PUBLISHED→DEPRECATED（每项须带替代指标）；单条失败不阻断其余。"""
    service = MetricService(db)
    results: list[MetricBatchItemResult] = []
    for item in request.items:
        try:
            await service.deprecate_metric(
                item.metric_code,
                item.successor_code,
                actor_id=user.id,
                role=user.role,
                user_domain=user.domain,
            )
            results.append(MetricBatchItemResult(metric_code=item.metric_code, ok=True))
        except Exception as exc:  # noqa: BLE001 - 批量逐条容错
            results.append(
                MetricBatchItemResult(metric_code=item.metric_code, ok=False, message=str(exc))
            )
    await write_audit(
        db,
        actor_id=user.id,
        action="BATCH_DEPRECATE",
        entity_type="metric_definition",
        entity_id=f"batch:{len(request.items)}",
        detail={
            "failed_codes": _batch_failed_codes(results),
            "ok": sum(1 for r in results if r.ok),
        },
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=_batch_response(results), trace_id=trace_id)


# ------------------------------------------------------------------
# DATA_SOURCE_DROPPED 状态闭环（TD §12.3 / PRD R5-01）
#   recover-source-dropped : DSD → PUBLISHED（源恢复/误报）
#   confirm-deprecate-dropped : DSD → DEPRECATED（确认退役）
#   mark-source-dropped    : 数据源 DROP → 下游指标置 DSD（采集侧批量）
# ------------------------------------------------------------------


@router.post(
    "/{metric_code}/recover-source-dropped",
    response_model=ApiResponse[MetricResponse],
    summary="恢复数据源下线指标（DSD → PUBLISHED，源恢复/确认误报）",
    dependencies=_WRITE_DEPS,
)
async def recover_source_dropped(
    metric_code: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """数据源恢复/确认误报后，取消 DATA_SOURCE_DROPPED 回到 PUBLISHED。"""
    service = MetricService(db)
    metric = await service.recover_source_dropped(
        metric_code, actor_id=user.id, role=user.role
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="RECOVER_SOURCE_DROPPED",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"from": "DATA_SOURCE_DROPPED", "to": "PUBLISHED"},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MetricResponse.model_validate(metric), trace_id=trace_id)


@router.post(
    "/{metric_code}/confirm-deprecate-dropped",
    response_model=ApiResponse[MetricResponse],
    summary="确认数据源下线指标退役（DSD → DEPRECATED）",
    dependencies=_WRITE_DEPS,
)
async def confirm_deprecate_dropped(
    metric_code: str,
    request: MetricDeprecateRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[MetricResponse]:
    """源无法恢复，确认退役（DSD → DEPRECATED），可填替代指标。"""
    service = MetricService(db)
    metric = await service.confirm_deprecate_dropped(
        metric_code,
        successor_code=request.successor_code,
        actor_id=user.id,
        role=user.role,
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="CONFIRM_DEPRECATE_DROPPED",
        entity_type="metric_definition",
        entity_id=metric.metric_code,
        detail={"successor_code": request.successor_code},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=MetricResponse.model_validate(metric), trace_id=trace_id)


@router.post(
    "/mark-source-dropped",
    response_model=ApiResponse[dict[str, int]],
    summary="数据源 DROP → 血缘下游指标批量置 DATA_SOURCE_DROPPED（采集侧触发）",
    dependencies=_WRITE_DEPS,
)
async def mark_source_dropped(
    request: MetricSourceDroppedRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    user: CurrentUser,
    trace_id: Annotated[str, Depends(get_trace_id)],
    http_req: Request,
) -> ApiResponse[dict[str, int]]:
    """采集检测到源表 DROP 后批量标记下游指标（owner 生成 7 天待办）。"""
    service = MetricService(db)
    count = await service.mark_source_dropped(
        source_ids=request.source_ids, actor_id=user.id
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="MARK_SOURCE_DROPPED",
        entity_type="metric_definition",
        entity_id=f"source:{len(request.source_ids)}",
        detail={"source_ids": request.source_ids, "metrics_marked": count},
        ip=client_ip(http_req),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"marked": count}, trace_id=trace_id)
