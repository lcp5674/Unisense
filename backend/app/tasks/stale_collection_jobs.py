"""worker 崩溃滞留任务清扫（H1：生产就绪审计）。

Arq 定时任务（每 15 分钟）：扫描 RedisJobStore 中 RUNNING/QUEUED 且超时
未更新的任务——worker 被 kill -9/OOM/滚动发布杀掉时，arq ``max_tries=1`` 不
重试、JobStore 非终态无 TTL，任务中心出现永久「采集中」幽灵任务、状态统计
失真、Redis 持续增长。本任务将其补写 FAILED 并同步收尾 collection_run。

与 ``collection_run_purge_task`` 分工：本任务只处理**滞留超时的非终态**任务，
不碰正常终态与保留期内的历史。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: 判定滞留的超时阈值（秒）：显著大于 worker job_timeout（1800s），
#: 避免误杀仍在正常推进的长任务（正常任务 progress 高频刷新 updated_at）。
_STALE_TIMEOUT_SECONDS = 3600
_STALE_ERROR = "worker 进程崩溃或长时间无进度，任务滞留超时，已自动标记失败"


async def stale_collection_jobs_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """Arq 定时任务：清扫 worker 崩溃遗留的 RUNNING/QUEUED 任务。

    自建 DB 会话（对齐 purge/audit_archive 模式）。返回
    {status, scanned, stale, cleaned} 供 worker 日志观测。
    """
    from app.db.mysql import async_session_factory
    from app.services.collector.queue import RedisJobStore
    from app.services.collector.repository import CollectorRepository

    redis = ctx.get("redis")
    if redis is None:
        return {"status": "SKIP", "reason": "no redis"}
    store = RedisJobStore(redis)
    now = datetime.now(UTC)
    stale = await store.stale_jobs(now, timeout_seconds=_STALE_TIMEOUT_SECONDS)

    cleaned = 0
    async with async_session_factory() as db:
        repo = CollectorRepository(db)
        for job_id, _status in stale:
            try:
                await store.set(
                    job_id,
                    "FAILED",
                    {"source_id": None, "error": _STALE_ERROR},
                )
            except Exception:  # noqa: BLE001 - 单任务失败不阻断其余
                logger.warning("stale_job_mark_failed_failed: job=%s", job_id)
                continue
            run = await repo.find_collection_run_by_job_id(job_id)
            if run is not None:
                await repo.fail_collection_run(run.id, _STALE_ERROR)
                cleaned += 1
        await db.commit()
    if stale:
        logger.warning(
            "stale_collection_jobs_cleaned: stale=%d run_cleaned=%d",
            len(stale),
            cleaned,
        )
    return {"status": "SUCCESS", "scanned": -1, "stale": len(stale), "cleaned": cleaned}
