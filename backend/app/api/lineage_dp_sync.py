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
from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy import select

from app.api.deps import CurrentUser, require_roles
from app.api.responses import ApiResponse, ok
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
from app.services.lineage.dp_sync_service import DpSyncService

router = APIRouter(prefix="/lineage/dp-sync", tags=["lineage-dp-sync"])

#: dp 血缘同步为治理能力：仅平台/域管理员可配置、运维与抉择。
_ADMIN_ROLES = ("platform_admin", "domain_admin")
_ADMIN_DEPS = [Depends(require_roles(*_ADMIN_ROLES))]


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
):
    """创建或更新同步配置（首次保存创建单行；更新只覆盖白名单字段）。"""
    repo = DpLineageRepository(db)
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
    schema = (
        str(
            payload.get("schema_name")
            or (cfg.schema_name if cfg is not None else "dp_stable")
        ).strip()
        or "dp_stable"
    )
    fetch = await _collector_factory(db)
    try:
        collector = await fetch(source_id)
        try:
            rows = await collector.query(
                f"SELECT DISTINCT out_table AS t FROM {schema}.dispatch_task "
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
        schema = cfg.schema_name or "dp_stable"
        task_rows = await collector.query(
            f"SELECT type AS v, COUNT(*) AS c FROM {schema}.dispatch_task "
            "WHERE is_deleted=0 GROUP BY type ORDER BY type"
        )
        step_rows = await collector.query(
            f"SELECT task_step_type AS v, COUNT(*) AS c FROM {schema}.dispatch_task_step "
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
    db=Depends(get_db_session),
):
    """重置水位（下轮扫描自动全量；幂等安全）。"""
    repo = DpLineageRepository(db)
    for name in ("task", "step"):
        wm = await repo.get_watermark(name)
        if wm is not None:
            wm.last_max_update = None
    await db.commit()
    return ok(data={"reset": True})


@router.post("/scan-now", response_model=ApiResponse, dependencies=_ADMIN_DEPS)
async def scan_now():
    """提交一轮手动立即扫描（后台异步执行，立即返回 task_id）。

    进度/结果/取消走下方 ``scan/status/{task_id}`` 与 ``scan/{task_id}/cancel``：
    提交不阻塞请求；异常以状态接口的 error 呈现，不再被包成「成功」。
    """
    task_id, already_running = await dp_sync_manual.submit_scan(force=True)
    return ok(
        data={
            "task_id": task_id,
            "status": "running",
            "already_running": already_running,
        }
    )


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
    "/scan/{task_id}/cancel", response_model=ApiResponse, dependencies=_ADMIN_DEPS
)
async def cancel_scan(task_id: int):
    """请求取消运行中的手动扫描（当前任务处理完停止，水位不推进）。"""
    accepted = await dp_sync_manual.cancel_scan(task_id)
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
