"""dp 调度血缘同步 API（配置 / 待抉择 / 运维）。

对齐 `spec/dp-lineage-ingest/plan.md` §8（前端三 Tab 的后端）：
- 同步配置读写（开关/轮询间隔/过滤/LLM 规则/回填策略）
- 待抉择单列表/详情/裁决四操作（采纳 sqlglot / 采纳 LLM / 手动修正 / 忽略）
- 运维：水位查看/重置（触发全量）、运行记录分页、手动立即扫描一轮

权限：写/运维/抉择为治理动作（platform_admin / domain_admin）；读同管理角色
（dp 同步是数据写入 + 解析裁决，不向普通血缘查看者开放）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy import select

from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, get_trace_id, ok
from app.core.audit import client_ip, write_audit
from app.db.mysql import get_db_session
from app.models.data_source import DataSource
from app.services.collector.spi import build_collector
from app.services.lineage import dp_sync_manual
from app.services.lineage.dp_sync_meta import (
    DP_STEP_TYPES,
    DP_TASK_TYPES,
    catalog_with_counts,
    count_regex_matches,
)
from app.services.lineage.dp_sync_parser import DEFAULT_EXCLUDE_TABLE_PATTERNS
from app.services.lineage.dp_sync_repo import DpLineageRepository
from app.services.lineage.dp_sync_service import (
    DpSyncService,
    _in_clause,
    _safe_table_name,
    _type_filters,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lineage/dp-sync", tags=["lineage-dp-sync"])

#: dp 血缘同步为治理能力：仅平台/域管理员可配置、运维与抉择。
_ADMIN_ROLES = ("platform_admin", "domain_admin")
_ADMIN_DEPS = [Depends(require_roles(*_ADMIN_ROLES))]


def _ident_field_error(payload: dict[str, Any]) -> str | None:
    """校验配置中的 schema/表名标识符（防 SQL 拼接注入）；非法返回错误信息。

    仅校验 payload 中显式出现的字段；缺省值由 service 层兜底。
    """
    for field, default in (
        ("schema_name", "dp_stable"),
        ("task_table", "dispatch_task"),
        ("step_table", "dispatch_task_step"),
    ):
        if field in payload and payload.get(field) is not None:
            try:
                _safe_table_name(payload.get(field), field, default)
            except ValueError as exc:
                return str(exc)
    return None


#: 类型过滤字段（JSON 数组列）——校验必须为整数数组（显式空 = 全部）。
_TYPE_FILTER_FIELDS = ("task_type_filter", "step_type_filter")
#: 排除正则字段（JSON 数组列）——必须为字符串数组。
_EXCLUDE_PATTERN_FIELDS = ("exclude_task_patterns", "exclude_table_patterns")
#: 布尔开关字段。
_BOOL_FIELDS = ("enabled", "llm_enabled", "resolve_memory_enabled")
#: owner_backfill SQLEnum 合法取值（DB 枚举非法值 commit 会抛 DataError → 裸 500）。
_OWNER_BACKFILL_ALLOWED = ("orphan_only", "never")


def _config_value_error(payload: dict[str, Any]) -> str | None:
    """配置值域/类型校验（T8）——非法枚举/类型提前返回错误。

    此前仅校验 poll_interval 与标识符：``owner_backfill`` 非法枚举（如 "always"）
    被 repo.update_config 白名单放行后 commit 抛 MySQL 枚举 DataError → 裸 500；
    ``task_type_filter``/``step_type_filter`` 可传任意类型使 service ``_type_filters``
    迭代错误、扫描行为静默出错。返回错误信息或 None。
    """
    if (
        "owner_backfill" in payload
        and payload.get("owner_backfill") not in _OWNER_BACKFILL_ALLOWED
    ):
        return "owner_backfill 取值仅 orphan_only / never"
    for field in _TYPE_FILTER_FIELDS:
        if field not in payload or payload.get(field) is None:
            continue
        val = payload[field]
        if not isinstance(val, list):
            return f"{field} 必须为整数数组（空数组=全部）"
        nums: list[int] = []
        for item in val:
            # 兼容数字与数字字符串（前端可能传字符串），统一归一为 int
            if isinstance(item, bool) or not isinstance(item, (int, str)):
                return f"{field} 必须为整数数组（空数组=全部）"
            try:
                nums.append(int(item))
            except (TypeError, ValueError):
                return f"{field} 必须为整数数组（空数组=全部）"
        payload[field] = nums
    for field in _EXCLUDE_PATTERN_FIELDS:
        if field in payload and payload.get(field) is not None:
            val = payload[field]
            if not isinstance(val, list) or not all(
                isinstance(p, str) for p in val
            ):
                return f"{field} 必须为字符串数组"
    for field in _BOOL_FIELDS:
        if field in payload and payload.get(field) is not None:
            if not isinstance(payload[field], bool):
                return f"{field} 必须为布尔值"
    return None


async def _collector_factory(db):
    """构建 dp 数据源只读采集器（供排除规则预览）。"""

    async def fetch(source_id: str):
        src = (
            await db.execute(
                select(DataSource).where(DataSource.source_id == source_id)
            )
        ).scalar_one_or_none()
        if src is None:
            raise LookupError(f"dp 数据源不存在: {source_id}")
        return build_collector(src.source_type, src.connection_config)

    return fetch


@router.get("/config", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def get_dp_sync_config(
    db=Depends(get_db_session),
):
    """读取同步配置（未初始化则返回 None，由保存时创建）。"""
    repo = DpLineageRepository(db)
    cfg = await repo.get_config()
    if cfg is None:
        return ok(data=None)
    return ok(data=cfg.to_dict())


@router.put("/config", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def update_dp_sync_config(
    user: CurrentUser,
    payload: dict[str, Any] = Body(...),
    db=Depends(get_db_session),
    request: Request = None,
    trace_id: str = Depends(get_trace_id),
):
    """创建或更新同步配置（首次保存创建单行；更新只覆盖白名单字段）。"""
    repo = DpLineageRepository(db)
    ident_err = _ident_field_error(payload)
    if ident_err:
        return ok(code="VALIDATION_ERROR", message=ident_err, data=None)
    value_err = _config_value_error(payload)
    if value_err:
        return ok(code="VALIDATION_ERROR", message=value_err, data=None)
    if "poll_interval_minutes" in payload:
        try:
            interval = int(payload["poll_interval_minutes"])
        except (TypeError, ValueError):
            interval = 0
        if not 1 <= interval <= 1440:
            return ok(
                code="VALIDATION_ERROR",
                message="poll_interval_minutes 取值范围 1~1440（分钟，最长 24 小时）",
                data=None,
            )
    cfg = await repo.get_config()
    if cfg is None:
        source_id = str(payload.get("source_id") or "").strip()
        if not source_id:
            return ok(
                code="VALIDATION_ERROR",
                message="首次保存必须提供 source_id（dp 数据源标识）",
                data=None,
            )
        cfg = await repo.create_default_config(source_id)
        await db.commit()
    await repo.update_config(cfg.id, **payload, updated_by=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="dp_sync.config_update",
        entity_type="dp_sync_config",
        entity_id=str(cfg.id),
        detail={k: v for k, v in payload.items() if k != "api_key"},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    fresh = await repo.get_config()
    return ok(data=fresh.to_dict())


@router.post("/exclude-preview", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def preview_exclude_rules(
    payload: dict[str, Any] = Body(...),
    db=Depends(get_db_session),
):
    """排除表名正则「规则校验 + 命中量预览」。

    body: ``{source_id?, schema_name?, patterns: [str]}``——patterns 每行为一条
    正则；source_id/schema_name 缺省回退当前配置。连 dp 源读任务产出表
    （out_table 去重全集）统计命中表数与样例；正则非法返回逐条错误。
    """
    patterns = payload.get("patterns")
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        return ok(
            code="VALIDATION_ERROR", message="patterns 必须为字符串数组", data=None
        )
    patterns = [s.strip() for s in patterns if s and s.strip()]
    repo = DpLineageRepository(db)
    cfg = await repo.get_config()
    source_id = str(
        payload.get("source_id") or (cfg.source_id if cfg is not None else "") or ""
    ).strip()
    if not source_id:
        return ok(
            code="VALIDATION_ERROR",
            message="请先配置 dp 数据源 source_id 再预览（或传入 source_id）",
            data=None,
        )
    try:
        schema = _safe_table_name(
            payload.get("schema_name")
            or (cfg.schema_name if cfg is not None else None),
            "schema_name",
            "dp_stable",
        )
        task_table = _safe_table_name(
            payload.get("task_table") or (cfg.task_table if cfg is not None else None),
            "task_table",
            "dispatch_task",
        )
    except ValueError as exc:
        return ok(code="VALIDATION_ERROR", message=str(exc), data=None)
    fetch = await _collector_factory(db)
    try:
        collector = await fetch(source_id)
        try:
            rows = await collector.query(
                f"SELECT DISTINCT out_table AS t FROM {schema}.{task_table} "
                "WHERE is_deleted=0 AND out_table IS NOT NULL AND out_table <> ''"
            )
        finally:
            await collector.dispose()
    except Exception as exc:  # noqa: BLE001 —— 数据源不可达/查询失败给明确信号
        return ok(
            code="SOURCE_UNREACHABLE",
            message=f"dp 数据源不可达或查询失败：{exc}",
            data={"reachable": False, "error": str(exc)},
        )
    tables = [str(r["t"]) for r in rows if r.get("t")]
    result = count_regex_matches(tables, patterns)
    result["reachable"] = True
    result["note"] = (
        "预览范围为 dp 任务产出表（out_table 去重全集）；脚本内源表命中在真实解析时逐 SQL 排除。"
    )
    return ok(data=result)


@router.get("/stats", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def get_dp_sync_stats(
    db=Depends(get_db_session),
):
    """dp 血缘同步统计概览（运维页顶部卡片）。

    - ``task_total`` / ``step_total``：dp 数据源活跃任务/节点总量（按配置类型
      过滤，实时 COUNT；源不可达为 null 并给 reason）
    - ``last_full_scan``：最近一次成功全量轮的解析成果（成功 = parsed_ok +
      llm_confirmed，失败 = diverged + llm_fallback + unparseable + errors，
      附成功率 parse_rate）——增量轮只扫变更任务，不作概览口径
    - ``cumulative``：历史成功轮累计
    - ``pending_tickets``：待抉择存量（未裁决单按状态计数）
    - ``lineage``：dp 通道血缘沉淀——活跃表级边 / 涉及 distinct 表节点 /
      字段映射条数
    """
    repo = DpLineageRepository(db)
    cfg = await repo.get_config()
    stats = await repo.sync_stats()
    task_total: int | None = None
    step_total: int | None = None
    reachable = False
    reason = "not_configured" if cfg is None else None
    if cfg is not None:
        fetch = await _collector_factory(db)
        collector = None
        try:
            collector = await fetch(cfg.source_id)
            schema = _safe_table_name(cfg.schema_name, "schema_name", "dp_stable")
            task_table = _safe_table_name(
                cfg.task_table, "task_table", "dispatch_task"
            )
            step_table = _safe_table_name(
                cfg.step_table, "step_table", "dispatch_task_step"
            )
            task_types, step_types = _type_filters(cfg)
            tparams: dict[str, Any] = {}
            tclause, tparams = _in_clause("type", task_types, "t", tparams)
            task_rows = await collector.query(
                f"SELECT COUNT(*) AS c FROM {schema}.{task_table} "
                f"WHERE is_deleted=0{tclause}",
                tparams,
            )
            sparams: dict[str, Any] = {}
            tj_clause, sparams = _in_clause("t.type", task_types, "t", sparams)
            ss_clause, sparams = _in_clause(
                "st.task_step_type", step_types, "s", sparams
            )
            step_rows = await collector.query(
                f"SELECT COUNT(*) AS c FROM {schema}.{step_table} st "
                f"JOIN {schema}.{task_table} t ON st.task_id=t.id "
                f"WHERE st.is_deleted=0 AND t.is_deleted=0{tj_clause}{ss_clause}",
                sparams,
            )
            task_total = int(task_rows[0]["c"]) if task_rows else 0
            step_total = int(step_rows[0]["c"]) if step_rows else 0
            reachable = True
        except Exception as exc:  # noqa: BLE001 —— 源不可达不阻断统计，给原因
            reason = f"dp 数据源不可达或查询失败：{exc}"
        finally:
            if collector is not None:
                await collector.dispose()

    def _derived(bucket: dict[str, Any] | None) -> None:
        if not bucket:
            return
        ok = int(bucket.get("parsed_ok") or 0) + int(bucket.get("llm_confirmed") or 0)
        bad = (
            int(bucket.get("diverged") or 0)
            + int(bucket.get("llm_fallback") or 0)
            + int(bucket.get("unparseable") or 0)
            + int(bucket.get("errors") or 0)
        )
        denom = ok + bad
        bucket["parse_success_total"] = ok
        bucket["parse_fail_total"] = bad
        bucket["parse_rate"] = round(ok * 100.0 / denom, 1) if denom else None

    _derived(stats.get("last_full_scan"))
    _derived(stats.get("cumulative"))
    return ok(
        data={
            "task_total": task_total,
            "step_total": step_total,
            "dp_reachable": reachable,
            "dp_unreachable_reason": reason,
            **stats,
        }
    )


@router.get("/meta", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def get_dp_sync_meta(
    db=Depends(get_db_session),
):
    """dp 类型枚举目录 + 内置排除默认规则。

    内置已知映射（有实据）；配置的数据源可达时以 DISTINCT+COUNT 探测合并
    真实类型（未内置值标注「未识别」），保证选项框覆盖全部枚举而不编造。
    """
    repo = DpLineageRepository(db)
    cfg = await repo.get_config()
    meta: dict[str, Any] = {
        "task_types": catalog_with_counts(DP_TASK_TYPES, {}),
        "step_types": catalog_with_counts(DP_STEP_TYPES, {}),
        "exclude_defaults": list(DEFAULT_EXCLUDE_TABLE_PATTERNS),
        "reachable": False,
        "reason": "not_configured" if cfg is None else None,
    }
    if cfg is None:
        return ok(data=meta)
    fetch = await _collector_factory(db)
    collector = None
    try:
        collector = await fetch(cfg.source_id)
        schema = _safe_table_name(cfg.schema_name, "schema_name", "dp_stable")
        task_table = _safe_table_name(cfg.task_table, "task_table", "dispatch_task")
        step_table = _safe_table_name(cfg.step_table, "step_table", "dispatch_task_step")
        task_rows = await collector.query(
            f"SELECT type AS v, COUNT(*) AS c FROM {schema}.{task_table} "
            "WHERE is_deleted=0 GROUP BY type ORDER BY type"
        )
        step_rows = await collector.query(
            f"SELECT task_step_type AS v, COUNT(*) AS c FROM {schema}.{step_table} "
            "WHERE is_deleted=0 GROUP BY task_step_type ORDER BY task_step_type"
        )
        tcounts = {int(r["v"]): int(r["c"]) for r in task_rows}
        scounts = {int(r["v"]): int(r["c"]) for r in step_rows}
        meta.update(
            {
                "task_types": catalog_with_counts(DP_TASK_TYPES, tcounts),
                "step_types": catalog_with_counts(DP_STEP_TYPES, scounts),
                "reachable": True,
                "reason": None,
            }
        )
    except Exception as exc:  # noqa: BLE001 —— 探测失败不阻断：返回内置 + 原因
        meta["reachable"] = False
        meta["reason"] = f"dp 数据源不可达或探测失败：{exc}"
    finally:
        if collector is not None:
            await collector.dispose()
    return ok(data=meta)


@router.get("/tickets", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def list_tickets(
    status: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db=Depends(get_db_session),
):
    """待抉择单分页列表（状态筛选 + 关键字）。"""
    repo = DpLineageRepository(db)
    rows, total = await repo.list_tickets(
        status=status, keyword=keyword, page=page, page_size=page_size
    )
    return ok(
        data={
            "items": [_ticket_dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )

@router.get(
    "/tickets/retry-tasks", response_model=ApiResponse, dependencies=_ADMIN_DEPS
)
async def list_dp_retry_tasks(
    user: CurrentUser,
    db=Depends(get_db_session),
    trace_id: str = Depends(get_trace_id),
    limit: int = Query(30, ge=1, le=100, description="返回条数"),
):
    """dp 重试任务列表（含进行中与最近历史，按创建倒序）。

    可见性：platform_admin 全部；其余仅本人发起任务（防跨用户窥探）。
    """
    from app.models.dp_sync import DpTicketRetryTask

    stmt = (
        select(DpTicketRetryTask)
        .where(DpTicketRetryTask.deleted_at.is_(None))
        .order_by(DpTicketRetryTask.created_at.desc())
        .limit(limit)
    )
    if "platform_admin" not in user.roles_all():
        stmt = stmt.where(DpTicketRetryTask.actor_id == user.id)
    rows = (await db.execute(stmt)).scalars().all()
    return ok(data=[_retry_task_to_dict(r) for r in rows], trace_id=trace_id)


@router.get(
    "/tickets/retry-tasks/{task_id}",
    response_model=ApiResponse,
    dependencies=_ADMIN_DEPS,
)
async def get_dp_retry_task(
    task_id: int,
    user: CurrentUser,
    db=Depends(get_db_session),
    trace_id: str = Depends(get_trace_id),
):
    """单任务进度（前端任务中心轮询用）。"""
    from app.models.dp_sync import DpTicketRetryTask

    row = (
        await db.execute(
            select(DpTicketRetryTask).where(
                DpTicketRetryTask.id == task_id,
                DpTicketRetryTask.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"dp 重试任务不存在: {task_id}")
    _assert_retry_task_owner(row, user)
    return ok(data=_retry_task_to_dict(row), trace_id=trace_id)


@router.post(
    "/tickets/retry-tasks/{task_id}/cancel",
    response_model=ApiResponse,
    dependencies=_ADMIN_DEPS,
)
async def cancel_dp_retry_task(
    task_id: int,
    user: CurrentUser,
    db=Depends(get_db_session),
    request: Request = None,
    trace_id: str = Depends(get_trace_id),
):
    """请求取消 dp 重试任务（置 cancel_requested；worker 每张完成后检查并收尾）。

    T5：任务不在 running（pending 未启动 / 已终态）时无 worker 会来收敛——此处
    立即把 pending 收敛为 cancelled（不留僵尸），终态任务幂等 no-op；仅 running
    任务置 cancel_requested 交由 worker 协作取消（worker 侧 finally 兜底）。
    """
    from app.models.dp_sync import DpTicketRetryTask

    row = (
        await db.execute(
            select(DpTicketRetryTask).where(
                DpTicketRetryTask.id == task_id,
                DpTicketRetryTask.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"dp 重试任务不存在: {task_id}")
    _assert_retry_task_owner(row, user)
    if row.status == "running":
        row.cancel_requested = True
    elif row.status == "pending":
        # 从未被 worker 拾起：直接收敛终态，避免任务中心永久 pending
        row.status = "cancelled"
        row.finished_at = datetime.now(UTC)
    # completed / cancelled / failed → 幂等 no-op（保留已收敛终态）
    await write_audit(
        db,
        actor_id=user.id,
        action="dp_sync.retry_task_cancel",
        entity_type="dp_ticket_retry_task",
        entity_id=str(row.id),
        detail={"status": row.status},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=_retry_task_to_dict(row), trace_id=trace_id)

@router.get("/tickets/{ticket_id}", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def get_ticket(
    ticket_id: int,
    db=Depends(get_db_session),
):
    """待抉择单详情（SQL 原文 + sqlglot 结果 + LLM 意见三栏）。"""
    repo = DpLineageRepository(db)
    ticket = await repo.get_ticket(ticket_id)
    if ticket is None:
        return ok(data=None)
    return ok(data=_ticket_dict(ticket, full=True))


@router.post("/tickets/{ticket_id}/resolve", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def resolve_ticket(
    user: CurrentUser,
    ticket_id: int,
    payload: dict[str, Any] = Body(...),
    db=Depends(get_db_session),
    request: Request = None,
    trace_id: str = Depends(get_trace_id),
):
    """裁决待抉择单：resolution ∈ accept_sqlglot / accept_llm / manual / ignore。"""
    resolution = str(payload.get("resolution") or "")
    manual_edges = payload.get("manual_edges")
    svc = DpSyncService(db)
    try:
        result = await svc.resolve_ticket(
            ticket_id,
            resolution=resolution,
            resolved_by=user.id,
            manual_edges=manual_edges if isinstance(manual_edges, dict) else None,
        )
    except LookupError as exc:
        return ok(code="NOT_FOUND", message=str(exc), data=None)
    except ValueError as exc:
        return ok(code="VALIDATION_ERROR", message=str(exc), data=None)
    await write_audit(
        db,
        actor_id=user.id,
        action="dp_sync.resolve",
        entity_type="dp_ticket",
        entity_id=str(ticket_id),
        detail={"resolution": resolution, "ticket_id": ticket_id},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=result)


@router.get("/runs", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def list_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db=Depends(get_db_session),
):
    """运行记录分页（每轮扫描结果，detail_json 可下钻）。"""
    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    from app.models.dp_sync import DpSyncRunLog

    base = sa_select(DpSyncRunLog).where(DpSyncRunLog.deleted_at.is_(None))
    total = (
        await db.execute(
            sa_select(func.count()).select_from(base.subquery())
        )
    ).scalar() or 0
    rows = list(
        (
            await db.execute(
                base.order_by(DpSyncRunLog.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
    )
    return ok(
        data={
            "items": [
                {
                    "id": r.id,
                    "run_at": r.run_at,
                    "status": r.status,
                    "scan_mode": r.scan_mode,
                    "scanned_tasks": r.scanned_tasks,
                    "scanned_steps": r.scanned_steps,
                    "parsed_ok": r.parsed_ok,
                    "llm_confirmed": r.llm_confirmed,
                    "diverged": r.diverged,
                    "llm_fallback": r.llm_fallback,
                    "unparseable": r.unparseable,
                    "tickets_created": r.tickets_created,
                    "tickets_resolved": r.tickets_resolved,
                    "errors": r.errors,
                    "llm_calls": r.llm_calls,
                    "field_mappings_written": r.field_mappings_written,
                    "field_edges_degraded": r.field_edges_degraded,
                    "duration_ms": r.duration_ms,
                    "error": r.error,
                    "detail": _safe_json(r.detail_json),
                }
                for r in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.get("/watermark", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def get_watermark(
    db=Depends(get_db_session),
):
    """增量水位查看（task/step 的上次扫描时间与水位值）。"""
    repo = DpLineageRepository(db)
    result = {}
    for name in ("task", "step"):
        wm = await repo.get_watermark(name)
        if wm is None:
            result[name] = None
        else:
            result[name] = {
                "last_max_update": wm.last_max_update,
                "last_scan_at": wm.last_scan_at,
                "last_full_scan_at": wm.last_full_scan_at,
            }
    return ok(data=result)


@router.post("/reset", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def reset_watermark(
    user: CurrentUser,
    db=Depends(get_db_session),
    request: Request = None,
    trace_id: str = Depends(get_trace_id),
):
    """重置水位（下轮扫描自动全量；幂等安全）。"""
    repo = DpLineageRepository(db)
    for name in ("task", "step"):
        wm = await repo.get_watermark(name)
        if wm is not None:
            wm.last_max_update = None
    await write_audit(
        db,
        actor_id=user.id,
        action="dp_sync.reset",
        entity_type="dp_sync_watermark",
        entity_id="task,step",
        detail={"reset": True},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data={"reset": True})


@router.post("/scan-now", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def scan_now(
    user: CurrentUser,
    db=Depends(get_db_session),
    request: Request = None,
    trace_id: str = Depends(get_trace_id),
):
    """提交一轮手动立即扫描（后台异步执行，立即返回 task_id）。

    进度/结果/取消走下方 ``scan/status/{task_id}`` 与 ``scan/{task_id}/cancel``：
    提交不阻塞请求；异常以状态接口的 error 呈现，不再被包成「成功」。
    """
    task_id, already_running = await dp_sync_manual.submit_scan(force=True)
    if task_id == 0:
        return ok(
            code="SCAN_THROTTLED",
            message="触发过于频繁，请稍候再试（全量扫描为重操作）",
            data={"task_id": None, "status": "throttled"},
        )
    await write_audit(
        db,
        actor_id=user.id,
        action="dp_sync.scan_now",
        entity_type="dp_sync_scan",
        entity_id=str(task_id),
        detail={"already_running": already_running},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(
        data={
            "task_id": task_id,
            "status": "running",
            "already_running": already_running,
        }
    )


@router.get(
    "/scan/current", response_model=ApiResponse, dependencies=_ADMIN_DEPS
)
async def get_current_scan():
    """查询当前运行中的手动扫描（OpsTab 挂载时自动恢复进度跟踪用）。

    有运行中任务 → ``{running: true, task_id, ...state}``；无 → ``{running: false}``。
    任务 registry 在 backend 进程内，页面切走不中断；前端据此在回来时接上轮询。
    """
    state = dp_sync_manual.current_running_status()
    if state is None:
        return ok(data={"running": False})
    return ok(data={"running": True, **state})


@router.get(
    "/scan/status/{task_id}", response_model=ApiResponse, dependencies=_ADMIN_DEPS
)
async def get_scan_status(task_id: int):
    """读取手动扫描任务状态（OpsTab 轮询实时进度/结束态/异常）。"""
    state = dp_sync_manual.scan_status(task_id)
    if state is None:
        return ok(
            code="SCAN_NOT_FOUND",
            message="扫描任务不存在或已随进程结束",
            data=None,
        )
    return ok(data=state)


@router.post(
    "/reprocess-unparseable", response_model=ApiResponse, dependencies=_ADMIN_DEPS
)
async def reprocess_unparseable(
    user: CurrentUser,
    db=Depends(get_db_session),
    request: Request = None,
    trace_id: str = Depends(get_trace_id),
    limit: int = Query(200, ge=1, le=2000),
):
    """调度宏展开上线后对存量「无法解析」单自动重判并尽量消解。

    可解析（宏展开后）→ 自动入库并置「采纳 sqlglot（系统）」；无数据流 →
    忽略；仍失败（UDF 声明/方言等）→ 保留待人工。返回 parsed/no_flow/kept。
    """
    svc = DpSyncService(db)
    counters = await svc.reprocess_unparseable_tickets(limit=limit)
    await write_audit(
        db,
        actor_id=user.id,
        action="dp_sync.reprocess_unparseable",
        entity_type="dp_sync_ticket",
        entity_id="batch",
        detail=counters,
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=counters)


@router.post(
    "/resolve-llm-disabled", response_model=ApiResponse, dependencies=_ADMIN_DEPS
)
async def resolve_llm_disabled(
    user: CurrentUser,
    db=Depends(get_db_session),
    request: Request = None,
    trace_id: str = Depends(get_trace_id),
):
    """一键处置「LLM 关闭期」待抉择单（diverged 且原因含 LLM 已关闭）。

    这些单是 llm_enabled=false 时复杂节点按 plan §3.1 降级建单的产物——无真实
    语义分歧（无 LLM 对比），sqlglot 结果完整，批量 ``accept_sqlglot`` 入库即可
    清空工作台，只保留真分歧/兜底/无法解析。返回 resolved/failed/skipped。
    """
    svc = DpSyncService(db)
    counters = await svc.resolve_llm_disabled_tickets(resolved_by=user.id)
    await write_audit(
        db,
        actor_id=user.id,
        action="dp_sync.resolve_llm_disabled",
        entity_type="dp_sync_ticket",
        entity_id="batch",
        detail=counters,
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=counters)


@router.post(
    "/tickets/retry-llm", response_model=ApiResponse, dependencies=_ADMIN_DEPS
)
async def retry_llm_tickets(
    user: CurrentUser,
    db=Depends(get_db_session),
    request: Request = None,
    trace_id: str = Depends(get_trace_id),
    payload: dict = Body(default={}),
):
    """LLM 恢复/修复后重试「LLM 类型错误」待抉择单（单条或批量）。

    body: ``{"ticket_ids": [1,2] | null}``——传 id 列表则仅重试指定单；
    不传/空则批量重试全部未裁决且 LLM 失败/兜底低置信的单。
    处置：diverged 单重跑 LLM confirm（agree → 自动采纳 sqlglot 消解；
    disagree → 刷新意见保留）；llm_fallback/unparseable 单重跑 LLM 兜底
    （可提炼 → 刷新为 llm_fallback 参考；仍失败 → 保留 unparseable）。
    返回 ``auto_resolved/refreshed/kept/failed`` 计数。
    """
    ticket_ids = payload.get("ticket_ids") or None
    svc = DpSyncService(db)
    counters = await svc.retry_llm_tickets(
        ticket_ids=ticket_ids, resolved_by=user.id
    )
    await write_audit(
        db,
        actor_id=user.id,
        action="dp_sync.retry_llm",
        entity_type="dp_sync_ticket",
        entity_id="batch" if not ticket_ids else ",".join(str(i) for i in ticket_ids),
        detail={"ticket_ids": ticket_ids, **counters},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    return ok(data=counters)


# ---- dp 待抉择单 LLM 重试后台任务（异步：任务中心跨页可见/可取消） ----


def _retry_task_to_dict(row: Any) -> dict[str, Any]:
    """dp 重试任务行 → API dict（含逐张进度与终态语义计数）。"""
    return {
        "id": row.id,
        "actor_id": row.actor_id,
        "actor_name": row.actor_name,
        "status": row.status,
        "total": row.total,
        "done": row.done,
        "failed": row.failed,
        "cancelled": row.cancelled,
        "counts": dict(row.counts_json or {}),
        "progress": list(row.progress_json or []),
        "cancel_requested": bool(row.cancel_requested),
        "error": row.error,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


def _assert_retry_task_owner(row: Any, user: CurrentUser) -> None:
    """非平台管理员仅可查看/取消本人发起的重试任务（防跨用户窥探）。"""
    if "platform_admin" not in user.roles_all() and row.actor_id != user.id:
        raise PermissionError("无权操作他人的 dp 重试任务")


@router.post(
    "/tickets/retry-llm/async", response_model=ApiResponse, dependencies=_ADMIN_DEPS
)
async def create_dp_retry_task(
    user: CurrentUser,
    db=Depends(get_db_session),
    request: Request = None,
    trace_id: str = Depends(get_trace_id),
    payload: dict = Body(default={}),
):
    """创建 dp 待抉择单 LLM 重试后台任务（替代同步 retry-llm：切页可见进度/结果）。

    body: ``{"ticket_ids": [1,2] | null}``——传 id 列表则仅重试指定单；
    不传/空则候选全部未裁决且 LLM 失败/兜底低置信的单。候选在创建时快照
    落 ``tickets_json``，worker 逐张执行并实时写回 progress；经右下角任务
    中心跨页面轮询/取消。无候选时返回 ``{"task": null}``（不落任务行）。
    """
    from app.core.config import settings
    from app.models.dp_sync import DpTicketRetryTask
    from app.services.collector.queue import _get_shared_arq_redis

    ticket_ids = payload.get("ticket_ids") or None
    svc = DpSyncService(db)
    candidates = await svc.collect_retry_candidates(ticket_ids=ticket_ids)
    if not candidates:
        return ok(data={"task": None}, trace_id=trace_id)

    row = DpTicketRetryTask(
        actor_id=user.id,
        actor_name=getattr(user, "username", None),
        org_id=getattr(user, "org_id", None),
        tickets_json=candidates,
        progress_json=[],
        status="pending",
        total=len(candidates),
        counts_json={"auto_resolved": 0, "refreshed": 0, "kept": 0, "failed": 0},
    )
    db.add(row)
    await db.flush()
    await write_audit(
        db,
        actor_id=user.id,
        action="dp_sync.retry_llm_async",
        entity_type="dp_ticket_retry_task",
        entity_id=str(row.id),
        detail={"ticket_ids": ticket_ids, "candidates": len(candidates)},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    await db.refresh(row)

    try:
        redis = _get_shared_arq_redis(settings.redis_url)
        await redis.enqueue_job(
            "run_dp_ticket_retry_task",
            row.id,
            _job_id=f"dp-retry:{row.id}",
        )
    except Exception as exc:  # noqa: BLE001
        # T5：入队失败（Redis 不可达等）收敛终态 failed——此前任务行留 pending、
        # 无 worker 会来拾起、也无重投路径，任务中心永久 pending 僵尸。
        logger.warning(
            "dp_retry_enqueue_failed task_id=%s error=%s", row.id, str(exc)[:200]
        )
        row.status = "failed"
        row.error = f"入队失败：{str(exc)[:300]}"
        row.finished_at = datetime.now(UTC)
        await db.commit()
    return ok(data={"task": _retry_task_to_dict(row)}, trace_id=trace_id)


@router.post(
    "/scan/{task_id}/cancel", response_model=ApiResponse, dependencies=_ADMIN_DEPS
)
async def cancel_scan(
    task_id: int,
    user: CurrentUser,
    db=Depends(get_db_session),
    request: Request = None,
    trace_id: str = Depends(get_trace_id),
):
    """请求取消运行中的手动扫描（协作式：当前步骤完成后停止，水位不推进）。

    与 ``force-cancel`` 的区别：cancel 等待当前写库/LLM 子步骤自然完成（保证
    事务原子）；若当前步骤卡在慢 IO 长时间未停，可调用 ``force-cancel``。
    """
    accepted = await dp_sync_manual.cancel_scan(task_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="dp_sync.scan_cancel",
        entity_type="dp_sync_scan",
        entity_id=str(task_id),
        detail={"cancelled": accepted},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    if not accepted:
        return ok(
            code="SCAN_NOT_RUNNING",
            message="扫描任务不存在或已不在运行",
            data={"cancelled": False},
        )
    return ok(data={"cancelled": True})


@router.post(
    "/scan/{task_id}/force-cancel", response_model=ApiResponse, dependencies=_ADMIN_DEPS
)
async def force_cancel_scan(
    task_id: int,
    user: CurrentUser,
    db=Depends(get_db_session),
    request: Request = None,
    trace_id: str = Depends(get_trace_id),
):
    """强制终止运行中的手动扫描（子步骤检查点立即中断，事务回滚不落半成品）。

    仅作最后手段（如当前步骤卡在慢 IO）；与协作 cancel 不同，可能在子步骤
    中间中断——未完成部分不落库，已提交部分保留，水位不推进。
    """
    accepted = await dp_sync_manual.force_cancel_scan(task_id)
    await write_audit(
        db,
        actor_id=user.id,
        action="dp_sync.scan_force_cancel",
        entity_type="dp_sync_scan",
        entity_id=str(task_id),
        detail={"force_cancelled": accepted},
        ip=client_ip(request),
        trace_id=trace_id,
    )
    await db.commit()
    if not accepted:
        return ok(
            code="SCAN_NOT_RUNNING",
            message="扫描任务不存在或已不在运行",
            data={"cancelled": False},
        )
    return ok(data={"cancelled": True})


def _ticket_dict(ticket, full: bool = False) -> dict[str, Any]:
    data = {
        "id": ticket.id,
        "task_id": ticket.task_id,
        "step_id": ticket.step_id,
        "task_name": ticket.task_name,
        "out_table": ticket.out_table,
        "sql_hash": ticket.sql_hash,
        "status": ticket.status,
        "resolution": ticket.resolution,
        "divergence_reason": ticket.divergence_reason,
        "resolved_by": ticket.resolved_by,
        "resolved_at": ticket.resolved_at,
        "created_at": ticket.created_at,
    }
    if full:
        data["sql_text"] = ticket.sql_text
        data["sqlglot_result"] = _safe_json(
            json.dumps(ticket.sqlglot_result, ensure_ascii=False)
            if ticket.sqlglot_result is not None
            else None
        )
        data["llm_opinion"] = _safe_json(
            json.dumps(ticket.llm_opinion, ensure_ascii=False)
            if ticket.llm_opinion is not None
            else None
        )
        data["manual_edges_json"] = ticket.manual_edges_json
    return data


def _safe_json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
