"""跨表批量 LLM 推断后台任务（方案 B：后端任务化）。

描述缺失治理「批量推断所选表」由前端有界并发改为提交 ``batch_llm_infer_task``
记录 + arq 任务执行：worker 逐表调用 ``CollectorService`` 的编排方法
（``infer_catalog_columns`` / ``infer_catalog_table_description``，与同步端点
同一实现来源），每表完成后把进度写回任务行并 commit——任意页面/刷新后经
任务查询端点可见实时进度，解决「切页后看不到批量进度/结果」。

协作取消：API 取消端点置 ``cancel_requested``；本任务每完成一张表后重新读取
任务行，检测到取消请求即把剩余 pending 表标为 cancelled 并结束。

部署：注册进 ``app.services.collector.worker.WorkerSettings.functions``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.db.mysql import async_session_factory
from app.models.collector_models import BatchInferHistory, BatchLlmInferTask
from app.services.collector.service import CollectorService

logger = structlog.get_logger("unisense.collector.batch_infer")

#: 单表字段推断批块大小（与同步端点一致）。
_BATCH_CHUNK = 60


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
    """由任务清单初始化逐表进度（保持前端勾选顺序）。"""
    return [
        {
            "catalog_id": t.get("catalog_id"),
            "entity_name": t.get("entity_name") or "",
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


async def _run_single_table(
    db: Any,
    task_id: int,
    item: dict[str, Any],
    idx: int,
    task: BatchLlmInferTask,
) -> dict[str, Any]:
    """执行一张表的字段/表描述推断，返回该表更新后的进度项。

    语义对齐描述缺失治理单表动作（inferOneTable）：
    - missing_fields>0 → 字段批量推断（infer_catalog_columns）
    - needs_table_desc → 表描述推断（infer_catalog_table_description，幂等短路）
    两动作相互独立：字段失败不阻断表描述，反之亦然。
    """
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

    if needs_fields:
        try:
            res = await svc.infer_catalog_columns(catalog_id, batch_chunk=_BATCH_CHUNK)
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


async def run_batch_llm_infer_task(ctx: dict[str, Any], task_id: int) -> None:
    """执行跨表批量 LLM 推断任务（逐表进度实时落库，支持协作取消）。

    Args:
        task_id: ``batch_llm_infer_task`` 主键。
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

        concurrency = max(1, min(task.concurrency or 3, 8))
        pending_idx = [
            i for i, p in enumerate(task.progress_json) if p.get("status") == "pending"
        ]
        next_pos = 0
        cancelled = False
        global_error: str | None = None

        async def worker() -> None:
            nonlocal next_pos, cancelled, global_error
            while not cancelled:
                if next_pos >= len(pending_idx):
                    break
                pos = next_pos
                next_pos += 1
                i = pending_idx[pos]
                # 每表前重读任务行：API 可能已置 cancel_requested（跨进程/跨会话）
                fresh = await _load_task(db, task_id)
                if fresh is None or fresh.cancel_requested or fresh.status == "cancelled":
                    cancelled = True
                    break
                item = dict(task.progress_json[i])
                item["status"] = "running"
                task.progress_json[i] = item
                await db.commit()

                updated = await _run_single_table(db, task_id, item, i, fresh)
                task.progress_json[i] = updated
                task.done = sum(
                    1 for p in task.progress_json if p.get("status") == "done"
                )
                task.failed = sum(
                    1 for p in task.progress_json if p.get("status") == "error"
                )
                task.added_total = sum(int(p.get("added") or 0) for p in task.progress_json)
                await db.commit()
                logger.info(
                    "batch_infer_table_done",
                    task_id=task_id,
                    catalog_id=updated.get("catalog_id"),
                    status=updated.get("status"),
                )

        try:
            await asyncio_gather_concurrent(concurrency, worker, pending_idx)
        except Exception as exc:  # noqa: BLE001 - 任务级异常落 failed 终态
            logger.exception("batch_infer_task_error", task_id=task_id, error=str(exc)[:300])
            global_error = str(exc)[:500]

        # 重读最新状态（取消可能已由 API 置位）
        task = await _load_task(db, task_id)
        if task is None:
            return
        if task.cancel_requested or cancelled:
            for p in task.progress_json:
                if p.get("status") == "pending":
                    p["status"] = "cancelled"
                    p["summary"] = p.get("summary") or "未执行"
            task.cancelled = sum(
                1 for p in task.progress_json if p.get("status") == "cancelled"
            )
            task.done = sum(1 for p in task.progress_json if p.get("status") == "done")
            task.failed = sum(1 for p in task.progress_json if p.get("status") == "error")
            task.added_total = sum(int(p.get("added") or 0) for p in task.progress_json)
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


async def asyncio_gather_concurrent(
    concurrency: int, worker: Any, pending_idx: list[int]
) -> None:
    """有界并发执行 worker（pending 为空时直接返回）。"""
    if not pending_idx:
        return
    import asyncio

    await asyncio.gather(
        *[worker() for _ in range(max(1, min(concurrency, len(pending_idx))))]
    )
