"""跨表批量 LLM 推断后台任务（后端任务化 + 任务内并发）。

描述缺失治理「批量推断所选表」提交 ``batch_llm_infer_task`` 记录 + arq 任务执行：
worker 按任务 ``concurrency``（默认 3，上限 10）**有界并发**处理多张表，每张表在
**独立 session** 中调用 ``CollectorService`` 的编排方法（``infer_catalog_columns`` /
``infer_catalog_table_description``，与同步端点同一实现来源）；每表完成经任务级
``asyncio.Lock`` 串行化把进度写回任务行并 commit——任意页面/刷新后经任务查询端点
可见实时进度，解决「切页后看不到批量进度/结果」，同时让前端并发选择真实生效。

协作取消：API 取消端点置 ``cancel_requested``；每个 worker 在表执行前（置 running）
与完成后（写回结果）各探测一次，检测到取消即停止派发剩余 pending 表，主流程把
剩余 pending 标为 cancelled 并结束。

部署：注册进 ``app.services.collector.worker.WorkerSettings.functions``。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.orm.attributes import flag_modified

from app.db.mysql import async_session_factory
from app.models.collector_models import BatchInferHistory, BatchLlmInferTask
from app.services.collector.service import BatchTaskCancelledError, CollectorService

logger = structlog.get_logger("unisense.collector.batch_infer")

#: 单表字段推断批块大小（与同步端点一致）。
_BATCH_CHUNK = 40


async def _load_task(db: Any, task_id: int) -> BatchLlmInferTask | None:
    """按主键读取任务（含软删过滤）。"""
    from sqlalchemy import select

    return (
        await db.execute(
            select(BatchLlmInferTask).where(
                BatchLlmInferTask.id == task_id,
                BatchLlmInferTask.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


def _new_progress(tasks_json: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """由任务清单初始化逐表进度（保持前端勾选顺序，保留执行动作标记）。"""
    return [
        {
            "catalog_id": t.get("catalog_id"),
            "entity_name": t.get("entity_name") or "",
            "missing_fields": int(t.get("missing_fields") or 0),
            "needs_table_desc": bool(t.get("needs_table_desc", False)),
            "status": "pending",
            "summary": "",
            "detail": "",
            "error_category": None,
            "added": 0,
            "skipped": 0,
            "inferred": [],
        }
        for t in tasks_json
    ]


async def _run_single_table(task_id: int, item: dict[str, Any]) -> dict[str, Any]:
    """执行一张表的字段/表描述推断，返回该表更新后的进度项。

    方案 A（任务内并发）：每张表在**独立 session**（``async_session_factory`` 自建）
    中执行——规避「单个 AsyncSession 被多个并发协程共享」的 SQLAlchemy 2.0 限制
    （一个 session 同一时刻只能承载一个进行中的 DB 操作），使任务内 N 张表可安全
    并发；推断写库后在本函数内显式 commit（同步端点/串行版由调用方 commit）。

    语义对齐描述缺失治理单表动作（inferOneTable）：
    - missing_fields>0 → 字段批量推断（infer_catalog_columns）
    - needs_table_desc → 表描述推断（infer_catalog_table_description，幂等短路）
    两动作相互独立：字段失败不阻断表描述，反之亦然。
    """
    async with async_session_factory() as db:
        from sqlalchemy import select

        from app.models.data_source import DBCatalog

        svc = CollectorService(db)
        catalog_id = int(item["catalog_id"])
        parts: list[str] = []
        errs: list[str] = []
        added = 0
        skipped = 0
        error_category: str | None = None
        inferred_names: list[str] = []

        # 目录可能被软删/物理删除 → 该表按失败计（error_category=not_found）
        cat = (
            await db.execute(
                select(DBCatalog).where(
                    DBCatalog.id == catalog_id, DBCatalog.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if cat is None:
            return {
                **item,
                "status": "error",
                "summary": "目录实体不存在",
                "detail": f"目录实体不存在: {catalog_id}",
                "error_category": "not_found",
            }

        needs_fields = bool(item.get("missing_fields", 0) > 0)
        needs_table = bool(item.get("needs_table_desc", False))

        # chunk 级协作取消探测：每块 LLM 调用前重读任务行，检测到 cancel_requested
        # 即返回 False → infer_catalog_columns 抛 BatchTaskCancelledError 中断剩余块。
        async def _is_cancelled() -> bool:
            async with async_session_factory() as _probe_db:
                _fresh = await _load_task(_probe_db, task_id)
                return _fresh is not None and not (
                    _fresh.cancel_requested or _fresh.status == "cancelled"
                )

        if needs_fields:
            try:
                res = await svc.infer_catalog_columns(
                    catalog_id,
                    batch_chunk=_BATCH_CHUNK,
                    cancel_checker=_is_cancelled,
                )
                if res.get("error"):
                    raise RuntimeError(str(res["error"]))
                added = len(res["inferred"])
                skipped = len(res["skipped"])
                inferred_names.extend(i["column_name"] for i in res["inferred"])
                parts.append(f"字段 +{added}（跳过 {skipped}）")
                if res["failed"]:
                    errs.append(
                        f"字段失败 {len(res['failed'])} 个："
                        + "、".join(res["failed"][:3])
                    )
                    error_category = error_category or "llm_failed"
            except BatchTaskCancelledError:
                # 任务已被请求取消：不标 error、不吞异常，交由外层把本表标 cancelled。
                raise
            except Exception as exc:  # noqa: BLE001 - 单动作失败不阻断另一动作/后续表
                logger.warning(
                    "batch_infer_table_fields_failed",
                    task_id=task_id,
                    catalog_id=catalog_id,
                    error=str(exc)[:300],
                )
                error_category = error_category or "llm_error"
                parts.append(f"字段推断失败：{str(exc)[:200]}")
                errs.append(f"字段推断失败：{str(exc)[:200]}")

        if needs_table:
            # 表描述为单次 LLM 调用（无法块内中断）：字段动作完成后/执行前
            # 各探测一次，避免「字段跑完期间被取消」仍发起表描述。
            if not await _is_cancelled():
                raise BatchTaskCancelledError("任务已请求取消，表描述执行前中止")
            try:
                tres = await svc.infer_catalog_table_description(catalog_id)
                if tres is None:
                    raise RuntimeError("LLM 推断暂时不可用（表描述）")
                parts.append("表描述已生成")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "batch_infer_table_desc_failed",
                    task_id=task_id,
                    catalog_id=catalog_id,
                    error=str(exc)[:300],
                )
                error_category = error_category or "llm_error"
                parts.append(f"表描述推断失败：{str(exc)[:200]}")
                errs.append(f"表描述推断失败：{str(exc)[:200]}")

        ok = not errs
        await db.commit()
        return {
            **item,
            "status": "done" if ok else "error",
            "summary": parts and "；".join(parts) or "无缺失描述",
            "detail": errs and "；".join(errs) or "",
            "error_category": error_category,
            "added": added,
            "skipped": skipped,
            "inferred": inferred_names,
        }


async def _set_progress_running(
    lock: asyncio.Lock, task_id: int, idx: int
) -> dict[str, Any] | None:
    """锁内读任务行并把 ``progress[idx]`` 置 running，返回该项快照。

    返回 None 表示任务已被取消/删除——调用方应停止派发后续表。
    进度行并发更新用任务级 ``asyncio.Lock`` 串行化（本进程内），防多表 worker
    同时读改写同一 ``progress_json`` 造成 lost update。
    """
    async with lock, async_session_factory() as db:
        fresh = await _load_task(db, task_id)
        if fresh is None or fresh.cancel_requested or fresh.status == "cancelled":
            return None
        item = dict(fresh.progress_json[idx])
        item["status"] = "running"
        fresh.progress_json[idx] = item
        flag_modified(fresh, "progress_json")
        await db.commit()
        return item


async def _apply_progress(
    lock: asyncio.Lock,
    task_id: int,
    idx: int,
    updated: dict[str, Any],
) -> bool:
    """锁内写回单表结果并重算 done/failed/added_total。

    Returns:
        True=任务仍可继续派发；False=任务已取消/删除（应停止）。
    """
    async with lock, async_session_factory() as db:
        fresh = await _load_task(db, task_id)
        if fresh is None:
            return False
        fresh.progress_json[idx] = updated
        flag_modified(fresh, "progress_json")
        fresh.done = sum(
            1 for p in fresh.progress_json if p.get("status") == "done"
        )
        fresh.failed = sum(
            1 for p in fresh.progress_json if p.get("status") == "error"
        )
        fresh.added_total = sum(
            int(p.get("added") or 0) for p in fresh.progress_json
        )
        await db.commit()
        return not (fresh.cancel_requested or fresh.status == "cancelled")


async def run_batch_llm_infer_task(ctx: dict[str, Any], task_id: int) -> None:
    """执行跨表批量 LLM 推断任务（任务内并发 + 逐表进度实时落库 + 协作取消）。

    方案 A（任务内并发）：按 ``task.concurrency``（默认 3，上限 10）起有界 worker
    池并发处理多张表——每张表在**独立 session** 中执行推断（``_run_single_table``），
    规避 AsyncSession 共享限制；任务行进度回写经任务级 ``asyncio.Lock`` 串行化。
    跨任务并发由 arq ``max_jobs`` 提供；若需跨进程（多副本）取消一致，须由 API 侧
    置 ``cancel_requested`` 后本任务在每表回写处探测（与串行版语义一致）。
    """
    logger.info("batch_infer_task_start", task_id=task_id)
    async with async_session_factory() as db:
        task = await _load_task(db, task_id)
        if task is None:
            logger.warning("batch_infer_task_not_found", task_id=task_id)
            return
        if task.status != "pending":
            logger.info(
                "batch_infer_task_skip_non_pending",
                task_id=task_id,
                status=task.status,
            )
            return

        task.status = "running"
        task.started_at = datetime.now(UTC)
        if not task.progress_json:
            task.progress_json = _new_progress(task.tasks_json or [])
        total = len(task.progress_json)
        task.total = total
        await db.commit()

        concurrency = max(1, min(int(task.concurrency or 3) or 3, 10))
        pending_idx = [
            i for i, p in enumerate(task.progress_json) if p.get("status") == "pending"
        ]

    if total == 0:
        async with async_session_factory() as db:
            task = await _load_task(db, task_id)
            if task is None:
                return
            task.status = "completed"
            task.finished_at = datetime.now(UTC)
            await db.commit()
            await _write_infer_history(db, task)
        return

    # 有界 worker 池：queue 分发 pending 表，任务级锁串行化任务行进度回写。
    lock = asyncio.Lock()
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in pending_idx:
        queue.put_nowait(i)
    stop = {"aborted": False}
    global_error: str | None = None

    async def worker() -> None:
        nonlocal global_error
        while not stop["aborted"]:
            try:
                idx = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            # 锁内置 running（同时探测取消）
            item = await _set_progress_running(lock, task_id, idx)
            if item is None:
                stop["aborted"] = True
                return
            # 锁外独立 session 执行表推断（慢 LLM 调用不占锁）
            try:
                updated = await _run_single_table(task_id, item)
            except BatchTaskCancelledError as exc:
                # chunk 级探测到取消：本表标 cancelled（非 error），停止派发剩余表。
                logger.warning(
                    "batch_infer_table_cancelled",
                    task_id=task_id,
                    idx=idx,
                    reason=str(exc)[:200],
                )
                updated = {
                    **item,
                    "status": "cancelled",
                    "summary": "任务已取消",
                    "detail": str(exc)[:200],
                    "error_category": None,
                }
            except Exception as exc:  # noqa: BLE001 - 表级兜底，不拖垮整批
                logger.exception(
                    "batch_infer_table_unexpected",
                    task_id=task_id,
                    idx=idx,
                    error=str(exc)[:300],
                )
                updated = {
                    **item,
                    "status": "error",
                    "summary": "任务内异常",
                    "detail": str(exc)[:200],
                    "error_category": "llm_error",
                }
            # 锁内写回结果；探测取消则停止派发剩余表
            if not await _apply_progress(lock, task_id, idx, updated):
                stop["aborted"] = True
                return
            logger.info(
                "batch_infer_table_done",
                task_id=task_id,
                catalog_id=updated.get("catalog_id"),
                status=updated.get("status"),
            )

    try:
        workers = [worker() for _ in range(min(concurrency, len(pending_idx) or 1))]
        await asyncio.gather(*workers)
    except Exception as exc:  # noqa: BLE001 - 任务级异常落 failed 终态
        logger.exception("batch_infer_task_error", task_id=task_id, error=str(exc)[:300])
        global_error = str(exc)[:500]

    # 终态收敛（重读最新状态：取消可能已由 API 置位）
    async with async_session_factory() as db:
        task = await _load_task(db, task_id)
        if task is None:
            return
        if task.cancel_requested or task.status == "cancelled" or stop["aborted"]:
            for p in task.progress_json:
                if p.get("status") == "pending":
                    p["status"] = "cancelled"
                    p["summary"] = p.get("summary") or "未执行"
            flag_modified(task, "progress_json")
            task.status = "cancelled"
        elif global_error:
            task.error = global_error
            task.status = "failed"
        else:
            task.status = "completed"
        task.done = sum(1 for p in task.progress_json if p.get("status") == "done")
        task.failed = sum(1 for p in task.progress_json if p.get("status") == "error")
        task.cancelled = sum(
            1 for p in task.progress_json if p.get("status") == "cancelled"
        )
        task.added_total = sum(int(p.get("added") or 0) for p in task.progress_json)
        task.finished_at = datetime.now(UTC)
        await db.commit()

        # 终态回写 batch_infer_history（兼容既有批量历史视图/跨会话入口）
        if task.status in ("completed", "cancelled", "failed"):
            await _write_infer_history(db, task)
        logger.info(
            "batch_infer_task_finish",
            task_id=task_id,
            status=task.status,
            done=task.done,
            failed=task.failed,
            cancelled=task.cancelled,
        )


async def _write_infer_history(db: Any, task: BatchLlmInferTask) -> None:
    """任务终态写入 batch_infer_history（等价原前端 persistServerHistory）。"""
    failed_tables = [
        {"catalog_id": p.get("catalog_id"), "entity_name": p.get("entity_name")}
        for p in task.progress_json
        if p.get("status") == "error"
    ]
    tables = [
        {"catalog_id": p.get("catalog_id"), "entity_name": p.get("entity_name")}
        for p in task.progress_json
    ]
    started = task.started_at or task.created_at
    finished = task.finished_at or datetime.now(UTC)
    row = BatchInferHistory(
        actor_id=task.actor_id,
        actor_name=task.actor_name,
        tables_json=tables,
        done=task.done,
        failed=task.failed,
        cancelled=task.cancelled,
        added=task.added_total,
        elapsed=max(0, int((finished - started).total_seconds())),
        failed_tables_json=failed_tables,
    )
    db.add(row)
    await db.commit()
