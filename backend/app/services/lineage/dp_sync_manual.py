"""手动「立即扫描」后台任务管理（进程内 registry：启动 / 状态 / 取消 / 进度）。

设计（对齐 `spec/dp-lineage-ingest/plan.md` §8 运维区 + 用户诉求）：
- backend 为单进程 uvicorn（Dockerfile 无 --workers），registry 用模块级 dict；
  扫描全程 async（collector.query / db 均 await），``asyncio.create_task`` 后台
  执行不阻塞请求事件循环。
- ``scan-now`` 提交后立即返回 ``task_id``；OpsTab 轮询 ``scan/status/{id}``
  实时展示进度（total/processed/current_task_id/stage）；``cancel`` 置位
  ``asyncio.Event``，``scan_once`` 在当前任务处理完后停止（进度见 service）。
- 进程重启 registry 丢失：状态查询对未知 id 返回 None（前端提示任务已随
  进程结束）；该轮 run_log 若残留 running，由下轮扫描/运维可见处置。

为什么不用 Redis/DB 做任务状态：手动扫描是 backend 进程内短任务（分钟级），
进程内 registry 已足够；arq 周期扫描由 worker 管理（不受本模块影响）。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from datetime import UTC, datetime
from typing import Any

from app.db.mysql import async_session_factory
from app.services.lineage.dp_sync_service import DpSyncService
from app.services.lineage.dp_sync_tasks import _fetch_collector, _make_llm_chat

logger = logging.getLogger(__name__)

#: 提交/查询互斥（registry 为单进程共享结构）。
_GUARD = asyncio.Lock()
#: task_id -> state dict（status/progress/result/cancel_event…）。
_SCANS: dict[int, dict[str, Any]] = {}
_IDS = itertools.count(1)


def _new_state(task_id: int) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "running",
        "started_at": datetime.now(UTC),
        "finished_at": None,
        "error": None,
        "message": None,
        "result": None,
        "progress": {
            "stage": "queued",
            "total": 0,
            "processed": 0,
            "current_task_id": None,
        },
        "cancel_event": asyncio.Event(),
    }


async def submit_scan(*, force: bool = True) -> tuple[int, bool]:
    """提交一轮手动扫描。返回 (task_id, already_running)。

    already_running=True 时返回现有运行中任务 id（不重复启动）。
    """
    async with _GUARD:
        for st in _SCANS.values():
            if st["status"] == "running":
                return st["task_id"], True
        task_id = next(_IDS)
        _SCANS[task_id] = _new_state(task_id)
    asyncio.create_task(_run_scan_job(task_id, force=force))
    return task_id, False


def scan_status(task_id: int) -> dict[str, Any] | None:
    """读取任务状态（供轮询）；未知/已随进程结束返回 None。"""
    st = _SCANS.get(task_id)
    if st is None:
        return None
    return {
        "task_id": st["task_id"],
        "status": st["status"],
        "started_at": st["started_at"].isoformat() if st["started_at"] else None,
        "finished_at": st["finished_at"].isoformat() if st["finished_at"] else None,
        "error": st["error"],
        "message": st["message"],
        "result": st["result"],
        "progress": dict(st["progress"]),
    }


async def cancel_scan(task_id: int) -> bool:
    """请求取消运行中的扫描。返回是否受理（任务不存在/已结束返回 False）。"""
    st = _SCANS.get(task_id)
    if st is None or st["status"] != "running":
        return False
    st["cancel_event"].set()
    st["message"] = "取消请求已发送，等待当前任务处理完成后停止"
    return True


async def _run_scan_job(task_id: int, *, force: bool) -> None:
    """后台执行一轮手动扫描并收尾状态（成功/失败/取消区分）。"""
    st = _SCANS.get(task_id)
    if st is None:
        return
    try:
        async with async_session_factory() as db:
            svc = DpSyncService(db, llm_chat=_make_llm_chat(db))
            result = await svc.scan_once(
                lambda sid: _fetch_collector(db, sid),
                progress=st["progress"],
                cancel_event=st["cancel_event"],
                force=force,
            )
        st["result"] = result
        skipped = result.get("skipped")
        if skipped == "cancelled":
            st["status"] = "cancelled"
            st["message"] = (
                "扫描已取消：已处理结果保留，水位未推进，下轮从原水位重扫"
            )
        elif skipped == "failed":
            st["status"] = "failed"
            st["error"] = result.get("error") or "扫描失败（详情见运行记录）"
        elif skipped:
            st["status"] = "success"
            st["message"] = f"本轮跳过：{skipped}"
        else:
            st["status"] = "success"
            st["message"] = "本轮扫描完成"
    except Exception as exc:  # noqa: BLE001 —— 兜底失败态，不静默
        logger.exception("dp_manual_scan_job_failed task_id=%s", task_id)
        st["status"] = "failed"
        st["error"] = str(exc)
    finally:
        st["finished_at"] = datetime.now(UTC)
