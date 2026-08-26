"""数据生命周期治理定时任务（第九轮 L-3/L-4）。

L-3 ``purge_retained_records``：软删记录超期物理清理——
- conflict / ruling_record / escalation_record 的软删行（deleted_at 非空）超
  ``SOFT_DELETE_RETENTION_DAYS``（180 天）物理删除（先删子表 ruling_record，
  避免 conflict_id 字符串引用成孤儿）；
- sql_infer_eval_run 保留近 ``EVAL_RUN_RETENTION_DAYS``（365 天），超期物理删除
  （该表只增不减、无软删，频率低但无界）。

L-4 ``check_table_growth``：通用表大小/行数巡检——经 information_schema 聚合各核心表
行数与数据大小，超阈值发布 ``storage.table_oversized`` 告警事件（best-effort），
解决"全库仅 audit 行数一项阈值"的监控盲区。

任务自建 DB 会话（对齐 semantic_tasks / audit_archive 模式），不依赖 ctx 注入 db。
每表单次最多物理删除 ``_MAX_DELETE_PER_TABLE`` 行（防大表一次删爆 undo/锁）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, select, text

from app.tasks.lock import task_locked

logger = structlog.get_logger("unisense.data_retention")

# L-3：软删行保留期（天）——超期物理删除
SOFT_DELETE_RETENTION_DAYS = 180
# L-3：评测运行记录保留期（天）——只保留近 N 天（无软删，按 ran_at）
EVAL_RUN_RETENTION_DAYS = 365
# L-3：行为日志类保留期（天）——query_log（提数查询日志）/ tracking_event（埋点）
# 只增不减无软删，超期物理删（对齐 notify 90 天清理的日志治理语义，日志类非 WORM）
LOG_RETENTION_DAYS = 180
# L-3：单次任务每表最多物理删除的行数（防大表一次删爆 undo/锁）
_MAX_DELETE_PER_TABLE = 5000
# L-4：行数/数据大小（MB）告警阈值
_TABLE_ROW_WARN_THRESHOLD = 1_000_000
_TABLE_SIZE_MB_WARN_THRESHOLD = 2048  # 2GB
# L-4：巡检的核心表清单（信息架构全量里只关注这些）
_CORE_TABLES = (
    "audit_log",
    "metric",
    "metric_version",
    "pending_version_confirmation",
    "conflict",
    "ruling_record",
    "escalation_record",
    "notification",
    "event_log",
    "metric_value_snapshot",
    "query_log",
    "collection_run",
    "db_catalog",
    "sql_infer_eval_run",
)


@task_locked("retention-purge")
async def purge_retained_records(ctx: dict[str, Any]) -> dict[str, Any]:
    """物理清理超期软删记录与超期评测运行（L-3）。"""
    from app.db.mysql import async_session_factory
    from app.models.conflict import Conflict, RulingRecord
    from app.models.consume import QueryLog
    from app.models.escalation import EscalationRecord
    from app.models.sql_infer_eval import SqlInferEvalRun
    from app.models.tracking import TrackingEvent

    cutoff = datetime.now(UTC) - timedelta(days=SOFT_DELETE_RETENTION_DAYS)
    eval_cutoff = datetime.now(UTC) - timedelta(days=EVAL_RUN_RETENTION_DAYS)
    log_cutoff = datetime.now(UTC) - timedelta(days=LOG_RETENTION_DAYS)
    stats: dict[str, int] = {
        "ruling_record": 0,
        "conflict": 0,
        "escalation_record": 0,
        "sql_infer_eval_run": 0,
        "query_log": 0,
        "tracking_event": 0,
    }

    async with async_session_factory() as db:
        # 先删子表 ruling_record（引用 conflict.conflict_id 字符串，无 FK，须先清避免孤儿）
        ruled = (
            await db.execute(
                select(RulingRecord.id)
                .where(
                    RulingRecord.deleted_at.is_not(None),
                    RulingRecord.deleted_at < cutoff,
                )
                .limit(_MAX_DELETE_PER_TABLE)
            )
        ).scalars().all()
        if ruled:
            await db.execute(delete(RulingRecord).where(RulingRecord.id.in_(ruled)))
            stats["ruling_record"] = len(ruled)

        # conflict 软删超期物理删
        conflicts = (
            await db.execute(
                select(Conflict.id)
                .where(
                    Conflict.deleted_at.is_not(None),
                    Conflict.deleted_at < cutoff,
                )
                .limit(_MAX_DELETE_PER_TABLE)
            )
        ).scalars().all()
        if conflicts:
            await db.execute(delete(Conflict).where(Conflict.id.in_(conflicts)))
            stats["conflict"] = len(conflicts)

        # escalation_record 软删超期
        escalations = (
            await db.execute(
                select(EscalationRecord.id)
                .where(
                    EscalationRecord.deleted_at.is_not(None),
                    EscalationRecord.deleted_at < cutoff,
                )
                .limit(_MAX_DELETE_PER_TABLE)
            )
        ).scalars().all()
        if escalations:
            await db.execute(delete(EscalationRecord).where(EscalationRecord.id.in_(escalations)))
            stats["escalation_record"] = len(escalations)

        # sql_infer_eval_run 保留近 N 天（无软删，按 ran_at）
        evals = (
            await db.execute(
                select(SqlInferEvalRun.id)
                .where(SqlInferEvalRun.ran_at < eval_cutoff)
                .limit(_MAX_DELETE_PER_TABLE)
            )
        ).scalars().all()
        if evals:
            await db.execute(delete(SqlInferEvalRun).where(SqlInferEvalRun.id.in_(evals)))
            stats["sql_infer_eval_run"] = len(evals)

        # 行为日志类保留期（只增不减无软删，超期物理删）：
        # query_log（提数查询日志）/ tracking_event（埋点）
        logs = (
            await db.execute(
                select(QueryLog.id)
                .where(QueryLog.created_at < log_cutoff)
                .limit(_MAX_DELETE_PER_TABLE)
            )
        ).scalars().all()
        if logs:
            await db.execute(delete(QueryLog).where(QueryLog.id.in_(logs)))
            stats["query_log"] = len(logs)

        tracking = (
            await db.execute(
                select(TrackingEvent.id)
                .where(TrackingEvent.created_at < log_cutoff)
                .limit(_MAX_DELETE_PER_TABLE)
            )
        ).scalars().all()
        if tracking:
            await db.execute(delete(TrackingEvent).where(TrackingEvent.id.in_(tracking)))
            stats["tracking_event"] = len(tracking)

        await db.commit()

    total = sum(stats.values())
    logger.info("purge_retained_records", total=total, stats=stats)
    return {"status": "SUCCESS", **stats}


@task_locked("table-growth-check")
async def check_table_growth(ctx: dict[str, Any]) -> dict[str, Any]:
    """通用表大小/行数巡检（L-4）：超阈值发布告警事件。"""
    from app.core.eventbus import get_eventbus
    from app.db.mysql import async_session_factory

    oversized: list[dict[str, Any]] = []
    async with async_session_factory() as db:
        result = await db.execute(
            text(
                "SELECT table_name, table_rows, data_length "
                "FROM information_schema.tables "
                "WHERE table_schema = DATABASE()"
            )
        )
        for row in result.mappings():
            table = row["table_name"]
            if table not in _CORE_TABLES:
                continue
            rows = row["table_rows"] or 0
            size_mb = (row["data_length"] or 0) / (1024 * 1024)
            if rows > _TABLE_ROW_WARN_THRESHOLD or size_mb > _TABLE_SIZE_MB_WARN_THRESHOLD:
                oversized.append(
                    {
                        "table": table,
                        "rows": int(rows),
                        "size_mb": round(size_mb, 1),
                        "row_threshold": _TABLE_ROW_WARN_THRESHOLD,
                        "size_mb_threshold": _TABLE_SIZE_MB_WARN_THRESHOLD,
                    }
                )

    if oversized:
        logger.warning("table_growth_oversized", tables=oversized)
        try:
            await get_eventbus().publish(
                "storage.table_oversized",
                {"tables": oversized, "checked_at": datetime.now(UTC).isoformat()},
            )
        except Exception:  # noqa: BLE001 - 告警事件 best-effort，不阻断巡检
            logger.warning("table_growth_alert_publish_failed", exc_info=True)
    else:
        logger.info("check_table_growth", status="ok")

    return {"status": "SUCCESS", "oversized": oversized}
