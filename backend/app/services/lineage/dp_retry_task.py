"""dp 待抉择单 LLM 重试后台任务（方案 A：后端任务化）。

待抉择「LLM 重试」原为同步 HTTP 请求（``POST /tickets/retry-llm`` 在请求内
逐张串行调 LLM，实测可达 4 分钟/批）——前端阻塞等待期间切页即「看不到进度与
结果」。本任务把重试改为提交 ``dp_ticket_retry_task`` + arq 执行：

- 任务行经 ``DpSyncService.collect_retry_candidates`` 快照候选单（tickets_json），
  worker 逐张在**独立 session** 中执行——每张重读最新单状态（被他人裁决/删除
  则跳过），调 ``DpSyncService._retry_one_ticket``（与同步端点同一处置实现，
  含 agree 自动消解/刷新意见/保留/失败容错），逐张进度写回任务行。
- 任意页面/刷新后经任务查询端点可见实时进度；支持协作取消（每张开始前探测
  cancel_requested，剩余 pending 收敛为 cancelled）。

部署：注册进 ``app.services.collector.worker.WorkerSettings.functions``。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db.mysql import async_session_factory
from app.models.dp_sync import DpResolutionTicket, DpTicketRetryTask

logger = logging.getLogger(__name__)

#: 重试动作 → 任务进度行 status 映射（auto_resolved/refreshed/kept 均视为成功动作）。
_ACTION_TO_STATUS = {
    "auto_resolved": "done",
    "refreshed": "done",
    "kept": "done",
    "failed": "error",
}


async def _load_task(db: Any, task_id: int) -> DpTicketRetryTask | None:
    return (
        await db.execute(
            select(DpTicketRetryTask).where(
                DpTicketRetryTask.id == task_id,
                DpTicketRetryTask.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _new_progress(tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """由候选单快照初始化逐张进度（保持提交顺序）。"""
    return [
        {
            "ticket_id": int(t.get("ticket_id") or 0),
            "task_name": t.get("task_name") or "",
            "out_table": t.get("out_table") or "",
            "status": "pending",
            "action": None,
            "summary": "",
            "detail": "",
        }
        for t in tickets
    ]


async def _load_ticket(db: Any, ticket_id: int) -> DpResolutionTicket | None:
    return (
        await db.execute(
            select(DpResolutionTicket).where(
                DpResolutionTicket.id == ticket_id,
                DpResolutionTicket.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _ticket_is_cancellable(db: Any, task_id: int) -> bool:
    """返回 False 表示任务已被请求取消/删除——调用方应停止派发剩余单。"""
    fresh = await _load_task(db, task_id)
    return fresh is not None and not (
        fresh.cancel_requested or fresh.status == "cancelled"
    )


async def _run_single_ticket(
    task_id: int, item: dict[str, Any]
) -> dict[str, Any]:
    """对一张单执行 LLM 重试（独立 session），返回更新后的进度项。

    - 单不存在/已裁决/非 LLM 可重试（状态已被外部改变）→ 跳过（不计数失败）。
    - 其余 → 复用同步端点同一处置（``_retry_one_ticket``），写库后本函数
      commit（每张独立事务，单张失败不拖累任务行与后续单）。
    """
    from app.services.lineage.dp_sync_service import DpSyncService

    ticket_id = int(item["ticket_id"])
    async with async_session_factory() as db:
        # 任务级取消探测（每张开始前）：已取消则整批收敛由主流程处理。
        if not await _ticket_is_cancellable(db, task_id):
            return {**item, "status": "cancelled", "summary": "任务已取消"}
        tk = await _load_ticket(db, ticket_id)
        if tk is None:
            return {
                **item,
                "status": "done",
                "action": "skipped",
                "summary": "单不存在（可能已删除）",
            }
        if tk.resolution is not None or tk.status == "resolved":
            return {
                **item,
                "status": "done",
                "action": "skipped",
                "summary": "已裁决，跳过",
                "detail": f"resolution={tk.resolution}",
            }
        if tk.status not in ("diverged", "llm_fallback", "unparseable"):
            return {
                **item,
                "status": "done",
                "action": "skipped",
                "summary": f"状态 {tk.status} 非可重试，跳过",
            }

        svc = DpSyncService(db)
        action, detail, err = await svc._retry_one_ticket(
            tk, resolved_by=task_id
        )
        await db.commit()
        status = _ACTION_TO_STATUS.get(action, "error")
        return {
            **item,
            "status": status,
            "action": action,
            "summary": detail.get("reason") or detail.get("action") or action,
            "detail": (err or detail.get("reason"))[:400],
        }


async def _apply_progress(task_id: int, idx: int, updated: dict[str, Any]) -> bool:
    """独立 session 写回单张结果并重算计数。返回 False=任务已取消（停止派发）。"""
    async with async_session_factory() as db:
        task = await _load_task(db, task_id)
        if task is None:
            return False
        task.progress_json[idx] = updated
        flag_modified(task, "progress_json")
        task.done = sum(1 for p in task.progress_json if p.get("status") == "done")
        task.failed = sum(1 for p in task.progress_json if p.get("status") == "error")
        task.cancelled = sum(
            1 for p in task.progress_json if p.get("status") == "cancelled"
        )
        await db.commit()
        return not (task.cancel_requested or task.status == "cancelled")


async def run_dp_ticket_retry_task(ctx: dict[str, Any], task_id: int) -> None:
    """执行 dp 待抉择单 LLM 重试任务（逐张串行 + 进度实时落库 + 协作取消）。"""
    logger.info("dp_retry_task_start", task_id=task_id)
    async with async_session_factory() as db:
        task = await _load_task(db, task_id)
        if task is None:
            logger.warning("dp_retry_task_not_found", task_id=task_id)
            return
        if task.status != "pending":
            logger.info("dp_retry_task_skip_non_pending", task_id=task_id, status=task.status)
            return
        task.status = "running"
        task.started_at = datetime.now(UTC)
        if not task.progress_json:
            task.progress_json = await _new_progress(task.tickets_json or [])
        task.total = len(task.progress_json)
        task.counts_json = {
            "auto_resolved": 0,
            "refreshed": 0,
            "kept": 0,
            "failed": 0,
        }
        await db.commit()
        total = task.total

    if total == 0:
        async with async_session_factory() as db:
            task = await _load_task(db, task_id)
            if task is not None:
                task.status = "completed"
                task.finished_at = datetime.now(UTC)
                await db.commit()
        return

    stop = False
    for idx in range(total):
        async with async_session_factory() as db:
            if not await _ticket_is_cancellable(db, task_id):
                stop = True
                break
        async with async_session_factory() as db:
            task = await _load_task(db, task_id)
            if task is None:
                return
            item = dict(task.progress_json[idx])
        item["status"] = "running"
        await _apply_progress(task_id, idx, item)
        try:
            updated = await _run_single_ticket(task_id, item)
        except Exception as exc:  # noqa: BLE001 —— 任务级兜底，单张失败不拖垮
            logger.exception(
                "dp_retry_ticket_unexpected",
                task_id=task_id,
                idx=idx,
                error=str(exc)[:300],
            )
            updated = {
                **item,
                "status": "error",
                "action": "failed",
                "summary": "任务内异常",
                "detail": str(exc)[:200],
            }
        if not await _apply_progress(task_id, idx, updated):
            stop = True
            break
        logger.info(
            "dp_retry_ticket_done",
            task_id=task_id,
            ticket_id=updated.get("ticket_id"),
            status=updated.get("status"),
            action=updated.get("action"),
        )

    # 终态收敛（重读最新：取消可能已由 API 置位）
    async with async_session_factory() as db:
        task = await _load_task(db, task_id)
        if task is None:
            return
        if stop or task.cancel_requested or task.status == "cancelled":
            for p in task.progress_json:
                if p.get("status") == "pending":
                    p["status"] = "cancelled"
                    p["summary"] = p.get("summary") or "未执行"
            flag_modified(task, "progress_json")
            task.status = "cancelled"
        else:
            task.status = "completed"
        task.done = sum(1 for p in task.progress_json if p.get("status") == "done")
        task.failed = sum(1 for p in task.progress_json if p.get("status") == "error")
        task.cancelled = sum(
            1 for p in task.progress_json if p.get("status") == "cancelled"
        )
        counts: dict[str, int] = {"auto_resolved": 0, "refreshed": 0, "kept": 0, "failed": 0}
        for p in task.progress_json:
            action = p.get("action")
            if action in counts:
                counts[action] += 1
        task.counts_json = counts
        task.finished_at = datetime.now(UTC)
        await db.commit()
        logger.info(
            "dp_retry_task_finish",
            task_id=task_id,
            status=task.status,
            done=task.done,
            failed=task.failed,
            cancelled=task.cancelled,
            counts=counts,
        )
