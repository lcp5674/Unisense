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

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from arq import ArqRedis, cron
from arq.connections import RedisSettings
from croniter import croniter

from app.core.config import settings
from app.core.eventbus import init_eventbus
from app.core.logging import configure_logging
from app.services.collector.batch_infer_tasks import run_batch_llm_infer_task
from app.services.collector.queue import RedisJobStore
from app.services.collector.repository import CollectorRepository
from app.services.collector.tasks import run_collection_task
from app.services.conflict.sla_tasks import auto_escalate_overdue, remind_stale_escalated
from app.services.lineage.dp_sync_tasks import dp_lineage_poll_task
from app.services.lineage.neo4j_sync import sync_neo4j_assets_task
from app.services.lineage.scan_tasks import lineage_scan_task
from app.services.notify.consumers import register_notify_event_consumers
from app.services.notify.escalation_tasks import check_escalation_retries
from app.services.quality.tasks import run_quality_checks
from app.tasks.audit_archive import audit_archive_task
from app.tasks.collection_run_purge import purge_collection_runs_task
from app.tasks.data_retention import check_table_growth, purge_retained_records
from app.tasks.dimension_snapshot_tasks import refresh_dimension_snapshots_task
from app.tasks.notify_purge import notify_purge_task
from app.tasks.search_tasks import sync_es_indexes_task
from app.tasks.semantic_tasks import (
    check_dsd_overdue,
    check_emergency_review_overdue,
    check_experimental_expiry,
    check_pending_version_timeouts,
    check_sunset_expiry,
    refresh_health_scores,
)
from app.tasks.stale_collection_jobs import stale_collection_jobs_task

# P2-2（第八轮）：worker 用 structlog（与 backend 统一 JSON 格式 + 脱敏 processor），
# 任务执行时可通过 structlog.contextvars.bind_contextvars(job_id=...) 绑定任务级 trace_id，
# 替代此前标准 logging 无 trace_id、格式与 backend 割裂的问题。
logger = structlog.get_logger("unisense.collector.worker")


async def _on_job_start(ctx: dict[str, Any]) -> None:
    """arq 任务启动钩子（P11 C-6）：绑定任务级 trace_id，串起单次任务执行日志。"""
    from structlog.contextvars import bind_contextvars

    bind_contextvars(
        job_id=ctx.get("job_id", ""),
        job_name=ctx.get("job_name", ""),
        trace_id=ctx.get("job_id", ""),
    )


async def _on_job_end(ctx: dict[str, Any]) -> None:
    """arq 任务结束钩子（P11 C-6）：清理任务级 contextvars，避免串扰后续任务。

    T8（审查修复）：任务失败（max_tries=1 无重试）时发站内告警通知——
    质量巡检/健康度/冲突升级等定时任务连续失败若无告警，会静默空转无人知晓。
    """
    from structlog.contextvars import unbind_contextvars

    unbind_contextvars("job_id", "job_name", "trace_id")

    result = ctx.get("result")
    if isinstance(result, BaseException):
        job_name = ctx.get("job_name", ctx.get("function", "unknown"))
        job_id = ctx.get("job_id", "")
        try:
            from app.core.eventbus import get_eventbus

            await get_eventbus().publish(
                "system.task_failed",
                {
                    "job_name": job_name,
                    "job_id": job_id,
                    "error": str(result)[:500],
                },
                actor_id="",
            )
            logger.warning(
                "task_failed_alerted",
                job_name=job_name,
                job_id=job_id,
                error=str(result)[:500],
            )
        except Exception as exc:  # noqa: BLE001 - 告警本身失败不阻断 worker
            logger.warning("task_failed_alert_publish_failed", error=str(exc))


#: P1-6 错过调度补偿：每个源的上次触发水位 key 前缀 / 补偿上限 / 补偿窗口。
_SCHED_WATERMARK_PREFIX = "collect:sched_watermark:"
#: 单次扫描最多补偿的错失触发次数（防停机很久导致积压风暴）。
_SCHED_CATCHUP_MAX = 5
#: 首次（无水位）或停机恢复时的补偿窗口：只补偿最近 24h 内的错失触发。
_SCHED_CATCHUP_WINDOW_HOURS = 24


async def startup(ctx: dict[str, Any]) -> None:
    """worker 启动：创建 ArqRedis（可 enqueue_job）与 JobStore 注入上下文。

    C1：注入 Redis 版 EventBus + 注册通知消费者，使后台任务（定时采集/
    质量巡检/审计归档/冲突 SLA 升级）触发的事件进入通知闭环——此前 worker
    进程从不 ``init_eventbus`` 且不注册订阅者，worker 侧事件双链路全丢
    （本地订阅为空 + Redis 未注入），导致「手动触发有通知、定时触发无通知」。
    """
    # P2-2：worker 日志与 backend 统一（structlog JSON + 脱敏 processor）
    configure_logging()

    redis = ArqRedis.from_url(settings.redis_url)
    ctx["redis"] = redis
    ctx["job_store"] = RedisJobStore(redis)

    # C1: EventBus 注入 Redis（ArqRedis 兼容 publish/psubscribe）+ notify 消费者注册
    try:
        init_eventbus(redis)
        register_notify_event_consumers()
        logger.info("worker_eventbus_initialized")
    except Exception:  # noqa: BLE001 - 事件总线初始化失败不应阻断 worker 主流程
        logger.warning("worker_eventbus_init_failed", exc_info=True)


async def shutdown(ctx: dict[str, Any]) -> None:
    """worker 关闭：释放 ArqRedis 连接池。"""
    redis = ctx.get("redis")
    if redis is not None:
        try:
            await redis.aclose()
        except Exception as exc:  # noqa: BLE001 - 关闭失败仅记录
            logger.warning("worker_redis_close_failed", error=str(exc))


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
        logger.error("scheduler_redis_unavailable")
        return

    async with async_session_factory() as db:
        repo = CollectorRepository(db)
        try:
            sources = await repo.list_scheduled_sources()
        except Exception as exc:
            logger.warning("scheduler_scan_failed", error=str(exc))
            return

        triggered = 0
        catchup_window = now - timedelta(hours=_SCHED_CATCHUP_WINDOW_HOURS)
        for src in sources:
            cron_expr = src.schedule_cron
            if not cron_expr:
                continue
            try:
                # P1-6: 错过调度补偿——记录每个源的上次触发水位，停机期间到点的
                # cron 在恢复后补触发（最多补最近 CATCHUP_MAX 次 / 24h 窗口），
                # 消除「worker 停机错过调度直接丢失」。
                watermark_key = f"{_SCHED_WATERMARK_PREFIX}{src.source_id}"
                raw = await redis.get(watermark_key)
                base = (
                    datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw)
                    if raw
                    else catchup_window
                )
                itr = croniter(cron_expr, base)
                # 收集 base 之后已到点（≤ now）的所有触发时刻，最多 CATCHUP_MAX 次
                missed: list[datetime] = []
                while len(missed) < _SCHED_CATCHUP_MAX:
                    candidate = itr.get_next(datetime)
                    if candidate > now:
                        break
                    missed.append(candidate)
                for ts in missed:
                    job_id = f"collect:sched:{src.source_id}:{int(ts.timestamp())}"
                    # run_collection_task 以 job_id 作第 4 位置参数（幂等键 + 状态回写）；
                    # mode 读取源配置的 collection_mode（/schedule 保存），定时链路同样
                    # 尊重 INCREMENTAL 而非静默全量（跨链路一致性，M4）。
                    # actor_id 传 None：collection_run 约定定时调度归因 NULL，不归因给源创建人。
                    await redis.enqueue_job(
                        "run_collection_task",
                        src.source_id,
                        None,
                        job_id,
                        mode=getattr(src, "collection_mode", None) or "FULL",
                        _job_id=job_id,
                    )
                    triggered += 1
                    logger.info(
                        "scheduler_trigger",
                        source=src.source_id,
                        run_at=ts.isoformat(),
                        catchup=ts < now,
                    )
                if missed:
                    # 水位推进到本次最后一个触发时刻（避免重复补偿同一时间点）
                    await redis.set(watermark_key, missed[-1].isoformat())
            except Exception as exc:
                logger.warning(
                    "scheduler_parse_failed",
                    source=src.source_id,
                    cron=cron_expr,
                    error=str(exc),
                )
        if triggered:
            logger.info("scheduler_scan_done", scanned=len(sources), triggered=triggered)


class WorkerSettings:
    """统一 arq worker 配置：采集 + 语义定时 + 质量自动检测 + 血缘图对账。

    承载全部后台定时任务，避免为每个模块单独起 worker：
    - 采集：run_collection_task（入队任务）+ collect_scheduler（每分钟扫 cron）
    - 语义：PENDING 超时（每分钟）/ 健康度（每日 3 点）/ 紧急补审（每小时）/ 灰度超期（每日 4 点）
    - 质量：run_quality_checks（每 5 分钟用最近观测自动评估启用规则）
    - 血缘：sync_neo4j_assets_task（每日 2 点对账 MySQL 权威血缘 → Neo4j 图存储）
       + lineage_scan_task（每日 3:30 扫描 UNISENSE_LINEAGE_SCAN_DIR 目录写血缘）
    """

    functions = [
        run_collection_task,
        run_batch_llm_infer_task,
        collect_scheduler,
        check_pending_version_timeouts,
        refresh_health_scores,
        check_emergency_review_overdue,
        check_experimental_expiry,
        check_dsd_overdue,
        check_sunset_expiry,
        run_quality_checks,
        check_escalation_retries,
        audit_archive_task,
        notify_purge_task,
        purge_collection_runs_task,
        stale_collection_jobs_task,
        auto_escalate_overdue,
        remind_stale_escalated,
        sync_neo4j_assets_task,
        lineage_scan_task,
        dp_lineage_poll_task,
        purge_retained_records,
        check_table_growth,
        refresh_dimension_snapshots_task,
        sync_es_indexes_task,
    ]
    # 任务级超时（秒）：源库挂起/慢查询拖死 worker 的最终防线。
    # 单查询超时由连接器 query_timeout 兜底（60s），此处约束整个任务上限——
    # 避免采集/扫描任务无限期占用 worker 事件循环。
    job_timeout = 1800
    # P1-4: 全局并发采集任务上限（保护 worker 资源；同源串行由 CollectionLock 保证，
    # 跨副本并发由部署副本数约束）。
    max_jobs = 4
    # max_tries 保持默认（1）：幂等键在任务开头 SET NX 占位，失败重试会命中
    # 已占位而跳过，故不引入重试，避免与幂等语义冲突。
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
            check_dsd_overdue,
            name="dsd-overdue",
            hour=3,
            minute=30,
            run_at_startup=False,
        ),
        cron(
            check_sunset_expiry,
            name="sunset-expiry",
            hour=3,
            minute=45,
            run_at_startup=False,
        ),
        cron(
            run_quality_checks,
            name="quality-checks",
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            second=0,
            run_at_startup=True,
        ),
        cron(
            check_escalation_retries,
            name="escalation-retries",
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            second=0,
            run_at_startup=True,
        ),
        cron(
            sync_es_indexes_task,
            name="es-index-sync",
            hour=2,
            minute=30,
            run_at_startup=False,
        ),
        cron(
            audit_archive_task,
            name="audit-archive",
            hour=2,
            minute=0,
            run_at_startup=False,
        ),
        cron(
            notify_purge_task,
            name="notify-purge",
            hour=1,
            minute=30,
            run_at_startup=False,
        ),
        cron(
            auto_escalate_overdue,
            name="conflict-sla-escalation",
            hour=6,
            minute=0,
            run_at_startup=True,
        ),
        cron(
            remind_stale_escalated,
            name="conflict-escalated-reminder",
            hour=6,
            minute=5,
            run_at_startup=True,
        ),
        cron(
            sync_neo4j_assets_task,
            name="neo4j-assets-sync",
            hour=2,
            minute=30,
            run_at_startup=False,
        ),
        cron(
            lineage_scan_task,
            name="lineage-scan",
            hour=3,
            minute=30,
            run_at_startup=False,
        ),
        cron(
            dp_lineage_poll_task,
            name="dp-lineage-poll",
            second=0,
            run_at_startup=False,
        ),
        cron(
            purge_collection_runs_task,
            name="collection-run-purge",
            hour=3,
            minute=0,
            run_at_startup=False,
        ),
        cron(
            stale_collection_jobs_task,
            name="stale-collection-jobs",
            minute={0, 15, 30, 45},
            second=0,
            run_at_startup=True,
        ),
        cron(
            purge_retained_records,
            name="retention-purge",
            hour=3,
            minute=45,
            run_at_startup=False,
        ),
        cron(
            check_table_growth,
            name="table-growth-check",
            hour=5,
            minute=0,
            run_at_startup=False,
        ),
        cron(
            refresh_dimension_snapshots_task,
            name="dimension-snapshot-refresh",
            minute={0, 30},
            second=0,
            run_at_startup=True,
        ),
    ]
    on_startup = startup
    on_shutdown = shutdown
    # P11 C-6：任务执行期间绑定 job_id trace_id（此前 bind_contextvars 仅 HTTP 中间件有，
    # cron/采集任务日志无 trace_id，无法串起单次任务执行）。on_job_end 清理避免串扰。
    on_job_start = _on_job_start
    on_job_end = _on_job_end
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
