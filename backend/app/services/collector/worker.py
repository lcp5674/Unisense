"""采集 arq worker 入口（P0-7：定时调度真实实现）。

此前 ``schedule_cron`` 只落库、无任何调度器读取，设置 cron 永不触发。
本模块提供：
- ``WorkerSettings``：arq worker 配置类（采集任务 + 每分钟调度扫描 + 启动钩子）。
- ``collect_scheduler``：每分钟扫描一次配置了 ``schedule_cron`` 的数据源，
  用 croniter 解析 cron 表达式判断是否到点，到点则投递 ``run_collection_task``。
- ``startup`` / ``shutdown``：worker 生命周期内创建/关闭 ArqRedis 与 JobStore。

部署：compose 新增 ``worker`` 服务，命令 ``arq app.services.collector.worker.WorkerSettings``。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from arq import ArqRedis, cron
from arq.connections import RedisSettings
from croniter import croniter

from app.core.config import settings
from app.services.collector.queue import RedisJobStore
from app.services.collector.repository import CollectorRepository
from app.services.collector.tasks import run_collection_task
from app.services.quality.tasks import run_quality_checks
from app.tasks.semantic_tasks import (
    check_emergency_review_overdue,
    check_experimental_expiry,
    check_pending_version_timeouts,
    refresh_health_scores,
)

logger = logging.getLogger("unisense.collector.worker")


async def startup(ctx: dict[str, Any]) -> None:
    """worker 启动：创建 ArqRedis（可 enqueue_job）与 JobStore 注入上下文。"""
    redis = ArqRedis.from_url(settings.redis_url)
    ctx["redis"] = redis
    ctx["job_store"] = RedisJobStore(redis)


async def shutdown(ctx: dict[str, Any]) -> None:
    """worker 关闭：释放 ArqRedis 连接池。"""
    redis = ctx.get("redis")
    if redis is not None:
        try:
            await redis.aclose()
        except Exception as exc:  # noqa: BLE001 - 关闭失败仅记录
            logger.warning("worker redis 关闭失败: %s", exc)


async def collect_scheduler(ctx: dict[str, Any], *args: Any) -> None:
    """每分钟扫描 schedule_cron 配置的数据源，按 cron 表达式触发采集（P0-7）。

    触发窗口：cron 表达式的下一次执行时间在当前时刻后 1 分钟内即触发。
    幂等：job_id 含目标执行时间戳（``collect:sched:{source_id}:{ts}``），
    同一分钟内的重复扫描不会重复入队（arq 按 job_id 去重）。
    """
    from app.db.mysql import async_session_factory

    now = datetime.now(UTC)
    redis = ctx.get("redis")
    if redis is None:
        logger.error("调度器 redis 不可用，跳过本轮扫描")
        return

    async with async_session_factory() as db:
        repo = CollectorRepository(db)
        try:
            sources = await repo.list_scheduled_sources()
        except Exception as exc:
            logger.warning("调度器扫描数据源失败: %s", exc)
            return

        triggered = 0
        for src in sources:
            cron_expr = src.schedule_cron
            if not cron_expr:
                continue
            try:
                itr = croniter(cron_expr, now)
                next_run = itr.get_next(datetime)
                if (next_run - now) <= timedelta(minutes=1):
                    job_id = f"collect:sched:{src.source_id}:{int(next_run.timestamp())}"
                    await redis.enqueue_job(
                        "run_collection_task",
                        src.source_id,
                        src.created_by,
                        _max_tries=3,
                        _timeout=600,
                        _job_id=job_id,
                    )
                    triggered += 1
                    logger.info(
                        "scheduler_trigger: source=%s next_run=%s",
                        src.source_id,
                        next_run.isoformat(),
                    )
            except Exception as exc:
                logger.warning(
                    "scheduler_parse_failed: source=%s cron=%r err=%s",
                    src.source_id,
                    cron_expr,
                    exc,
                )
        if triggered:
            logger.info("scheduler_scan_done: scanned=%d triggered=%d", len(sources), triggered)


class WorkerSettings:
    """统一 arq worker 配置：采集 + 语义定时 + 质量自动检测。

    承载全部后台定时任务，避免为每个模块单独起 worker：
    - 采集：run_collection_task（入队任务）+ collect_scheduler（每分钟扫 cron）
    - 语义：PENDING 超时（每分钟）/ 健康度（每日 3 点）/ 紧急补审（每小时）/ 灰度超期（每日 4 点）
    - 质量：run_quality_checks（每 5 分钟用最近观测自动评估启用规则）
    """

    functions = [
        run_collection_task,
        collect_scheduler,
        check_pending_version_timeouts,
        refresh_health_scores,
        check_emergency_review_overdue,
        check_experimental_expiry,
        run_quality_checks,
    ]
    cron_jobs = [
        cron(
            collect_scheduler,
            name="collect-scheduler",
            second=0,
            run_at_startup=False,
        ),
        cron(
            check_pending_version_timeouts,
            name="pending-version-timeouts",
            minute=None,
            second=0,
            run_at_startup=True,
        ),
        cron(
            refresh_health_scores,
            name="health-scores",
            hour=3,
            minute=0,
            run_at_startup=False,
        ),
        cron(
            check_emergency_review_overdue,
            name="emergency-review",
            hour=None,
            minute=0,
            run_at_startup=True,
        ),
        cron(
            check_experimental_expiry,
            name="experimental-expiry",
            hour=4,
            minute=0,
            run_at_startup=False,
        ),
        cron(
            run_quality_checks,
            name="quality-checks",
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            second=0,
            run_at_startup=True,
        ),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
