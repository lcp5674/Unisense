"""手动「立即扫描」后台任务管理（进程内 registry：启动 / 状态 / 取消 / 进度）。

设计（对齐 `spec/dp-lineage-ingest/plan.md` §8 运维区 + 用户诉求）：
- backend 为单进程 uvicorn（Dockerfile 无 --workers），registry 用模块级 dict；
  扫描全程 async（collector.query / db 均 await），``asyncio.create_task`` 后台
  执行不阻塞请求事件循环。
- ``scan-now`` 提交后立即返回 ``task_id``；OpsTab 轮询 ``scan/status/{id}``
  实时展示进度（total/processed/current_task_id/stage）；``cancel`` 置位
  ``asyncio.Event``，``scan_once`` 在**当前 step 边界**停止（协作式，不打断正在
  写库/调 LLM 的子步骤，保证事务原子）；``force-cancel`` 置位 ``force_event``
  在子步骤检查点立即中断（仅作慢 IO 卡住时的最后手段）。
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

#: registry 保留的最大**终态**任务数（防只增不减无限增长；运行中任务不受限）。
_MAX_FINISHED_SCANS = 20

#: 手动「立即扫描」最小触发间隔（秒）——全量扫描是重操作（拉全量调度表 +
#: 逐节点解析 + 可能调 LLM），防误连点/脚本反复触发压垮 dp 源库（L1）。
_MANUAL_SCAN_MIN_INTERVAL = 30


def _prune_registry() -> None:
    """终态任务超量时清理最旧（保留运行中 + 最新 N 个终态）。

    手动扫描低频，保留最近 20 条终态供前端查看足够；进程重启 registry 本就
    清空，仅防单进程长期运行内无限累积（P2-8）。
    """
    finished = sorted(
        (s for s in _SCANS.values() if s["status"] != "running"),
        key=lambda s: s["finished_at"] or datetime.min.replace(tzinfo=UTC),
    )
    overflow = len(finished) - _MAX_FINISHED_SCANS
    if overflow > 0:
        for st in finished[:overflow]:
            _SCANS.pop(st["task_id"], None)


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
            "current_step_type": None,
            "current_step_label": None,
        },
        "cancel_event": asyncio.Event(),
        "force_event": asyncio.Event(),
        "cancel_requested_at": None,
        "force_stop": False,
    }


async def submit_scan(*, force: bool = True) -> tuple[int, bool]:
    """提交一轮手动扫描。返回 (task_id, already_running)。

    already_running=True 时返回现有运行中任务 id（不重复启动）。
    距上次触发 < ``_MANUAL_SCAN_MIN_INTERVAL`` 时返回 (None, False)（节流，
    L1——全量扫描重操作，防误连点反复压 dp 源库）。
    """
    async with _GUARD:
        now = datetime.now(UTC)
        for st in _SCANS.values():
            if st["status"] == "running":
                return st["task_id"], True
        # 节流：查最近一次触发（含运行中之外的最早完成时间不适用——以最近触发为准）
        recent = sorted(
            (s.get("started_at") for s in _SCANS.values() if s.get("started_at")),
            reverse=True,
        )
        if recent:
            last = recent[0]
            if (now - last).total_seconds() < _MANUAL_SCAN_MIN_INTERVAL:
                return 0, False  # 0 = 被节流拒绝（无任务 id）
        task_id = next(_IDS)
        _SCANS[task_id] = _new_state(task_id)
    asyncio.create_task(_run_scan_job(task_id, force=force))
    return task_id, False


def current_running_status() -> dict[str, Any] | None:
    """返回当前运行中的手动扫描状态（无则 None）。

    供前端「切走页面回来自动恢复进度跟踪」：OpsTab 挂载时查询一次，有运行中
    任务则接上轮询，无需重新点「立即扫描」（任务跑在 backend 进程内，不因
    页面切换而中断——只有 backend 进程重启才会丢失）。
    """
    for st in _SCANS.values():
        if st["status"] == "running":
            return scan_status(st["task_id"])
    return None


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
        "cancel_requested": st["cancel_requested_at"] is not None,
        "cancel_requested_at": (
            st["cancel_requested_at"].isoformat()
            if st["cancel_requested_at"]
            else None
        ),
        "force_stop": st["force_stop"],
    }


async def cancel_scan(task_id: int) -> bool:
    """请求取消运行中的扫描（协作式：当前步骤完成后停止）。

    仅置位 ``cancel_event``——由 ``scan_once``/``_process_task`` 在 step 边界消费，
    不强制打断正在写库/调 LLM 的子步骤（保证事务原子性）。返回是否受理。
    """
    st = _SCANS.get(task_id)
    if st is None or st["status"] != "running":
        return False
    st["cancel_event"].set()
    st["cancel_requested_at"] = datetime.now(UTC)
    st["message"] = "正在停止扫描：等待当前步骤完成后停止（若长时间未停可强制终止）"
    return True


async def force_cancel_scan(task_id: int) -> bool:
    """强制终止运行中的扫描（更强信号：子步骤检查点立即中断）。

    同时置位 ``force_event`` 与 ``cancel_event``；``scan_once`` 在下一个检查点
    抛出 ``_ScanCancelled`` 中断本轮，事务由 scan_once 回滚兜底（不落半成品）。
    仅作最后手段（如当前步骤卡在慢 IO 时用户可强制终止）。返回是否受理。
    """
    st = _SCANS.get(task_id)
    if st is None or st["status"] != "running":
        return False
    st["force_stop"] = True
    st["force_event"].set()
    st["cancel_event"].set()
    st["cancel_requested_at"] = datetime.now(UTC)
    st["message"] = "强制终止中：将在当前步骤处理点立即停止（未完成部分不落库）"
    return True


async def _run_scan_job(task_id: int, *, force: bool) -> None:
    """后台执行一轮手动扫描并收尾状态（成功/失败/取消区分）。

    分布式锁（H3 补充）：手动扫描跑在 backend 进程，与 worker 的周期 cron
    并发会双跑同一批任务（InnoDB 1205 lock wait 实证）——用与 cron 同一把
    ``dp_lineage_poll`` 锁跨进程互斥；拿不到锁标记 skipped（提示稍后再试）。
    """
    st = _SCANS.get(task_id)
    if st is None:
        return
    from app.core.eventbus import get_eventbus
    from app.services.collector.distributed_lock import CollectionLock

    lock = CollectionLock(getattr(get_eventbus(), "_redis_pool", None))
    lock_key = "dp_lineage_poll"
    owner = f"manual-{task_id}"
    acquired = await lock.acquire(lock_key, owner, ttl=3600)
    if not acquired:
        st["status"] = "failed"
        st["error"] = "已有周期/手动扫描在运行，请稍后再试（跨进程互斥保护）"
        return
    try:
        await _run_scan_job_locked(task_id, st, force=force)
    finally:
        await lock.release(lock_key, owner)


async def _run_scan_job_locked(task_id: int, st: dict[str, Any], *, force: bool) -> None:
    """持锁执行扫描主体（成功/失败/取消收尾）。"""
    try:
        async with async_session_factory() as db:
            svc = DpSyncService(db, llm_chat=_make_llm_chat(db))
            # force_full=True：手动「立即扫描」的用户心智是「完整跑一遍看真实
            # 解析」，忽略水位全量重扫（幂等，边 upsert 不重复）；周期任务走
            # scan_once 默认增量 + 周期自动全量，不受本模块影响。
            result = await svc.scan_once(
                lambda sid: _fetch_collector(db, sid),
                progress=st["progress"],
                cancel_event=st["cancel_event"],
                force_event=st["force_event"],
                force=force,
                force_full=True,
            )
        st["result"] = result
        skipped = result.get("skipped")
        if skipped == "cancelled":
            st["status"] = "cancelled"
            if st["force_stop"]:
                st["message"] = (
                    "扫描已强制终止：未完成部分不落库，水位未推进，下轮从原水位重扫"
                )
            else:
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
            # 手动扫描恒全量（force_full）——成功 message 明确「全量」而非
            # 含糊的「本轮」，避免与周期增量空扫 0 任务混淆（用户心智：点了
            # 立即扫描就要看到完整解析结果）。
            st["message"] = (
                f"全量扫描完成：任务 {result.get('scanned_tasks', 0)} / "
                f"节点 {result.get('scanned_steps', 0)}，直入 "
                f"{result.get('parsed_ok', 0)}，分歧 {result.get('diverged', 0)}，"
                f"无法解析 {result.get('unparseable', 0)}"
            )
    except Exception as exc:  # noqa: BLE001 —— 兜底失败态，不静默
        logger.exception("dp_manual_scan_job_failed task_id=%s", task_id)
        st["status"] = "failed"
        st["error"] = str(exc)
    finally:
        st["finished_at"] = datetime.now(UTC)
        _prune_registry()
