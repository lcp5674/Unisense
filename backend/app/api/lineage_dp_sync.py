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
from app.services.lineage.dp_sync_repo import DpLineageRepository
from app.services.lineage.dp_sync_service import DpSyncService

router = APIRouter(prefix="/lineage/dp-sync", tags=["lineage-dp-sync"])

#: dp 血缘同步为治理能力：仅平台/域管理员可配置、运维与抉择。
_ADMIN_ROLES = ("platform_admin", "domain_admin")
_ADMIN_DEPS = [Depends(require_roles(*_ADMIN_ROLES))]


def _service(db) -> DpSyncService:
    return DpSyncService(db)


async def _collector_factory(db):
    """构建 dp 数据源只读采集器（供手动扫描）。"""

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


async def _llm_chat(messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
    """手动扫描时的 LLM 调用（平台默认客户端，异常转空由协议层建单）。"""
    from app.services.llm.client import LlmClient

    try:
        return await LlmClient().chat(
            messages,
            temperature=0.0,
            max_tokens=int(kwargs.get("max_tokens") or 2000),
        )
    except Exception:  # noqa: BLE001 —— LLM 故障转空输出
        return {"content": ""}


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
async def scan_now(
    db=Depends(get_db_session),
):
    """手动立即扫描一轮（同步执行，返回本轮统计）。"""
    fetch = await _collector_factory(db)
    svc = DpSyncService(db, llm_chat=_llm_chat)
    result = await svc.scan_once(fetch)
    return ok(data=result)


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
