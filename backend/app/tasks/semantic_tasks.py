"""语义模块 Arq 定时任务。

对齐 TD §12.3：PENDING_VERSION 超时检查、健康度每日刷新、
紧急发布补审提醒、灰度超期提醒。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

logger = structlog.get_logger("unisense.semantic.tasks")


async def check_pending_version_timeouts(ctx: dict[str, Any]) -> list[int]:
    """检查 PENDING_VERSION 超时（每分钟 cron）。

    查找 deadline 已过的 PendingVersionConfirmation 记录，
    超时未确认 → 默认接受 + 切换 CURRENT（应用新口径到主表并转正版本）。

    Returns:
        超时接受并完成转正的 metric_id 列表。
    """
    from sqlalchemy import select

    from app.db.mysql import async_session_factory
    from app.models.metric_version import PendingVersionConfirmation
    from app.services.semantic.service import MetricService

    promoted: list[int] = []
    now = datetime.now(UTC)

    async with async_session_factory() as db:
        # 查找超时的 PENDING 确认
        stmt = select(PendingVersionConfirmation).where(
            PendingVersionConfirmation.status == "PENDING",
            PendingVersionConfirmation.deadline < now,
        )
        result = await db.execute(stmt)
        expired = result.scalars().all()

        if not expired:
            return []

        # 按(metric_id, version)分组
        groups: dict[tuple[int, int], list[PendingVersionConfirmation]] = {}
        for conf in expired:
            key = (conf.metric_id, conf.version)
            groups.setdefault(key, []).append(conf)

        svc = MetricService(db)
        for (metric_id, version), _confirmations in groups.items():
            # 切换 CURRENT：标记超时接受 + 全部就绪后应用新口径并转正
            try:
                updated = await svc.auto_accept_timeout(metric_id, version)
                if updated is not None:
                    logger.info(
                        "pending_version_timeout_accepted",
                        metric_id=metric_id,
                        version=version,
                        metric_code=updated.metric_code,
                    )
                    promoted.append(metric_id)
            except Exception:
                logger.warning(
                    "pending_version_timeout_accept_failed",
                    metric_id=metric_id,
                    version=version,
                    exc_info=True,
                )

        await db.commit()

    return promoted


async def refresh_health_scores(ctx: dict[str, Any]) -> int:
    """每日凌晨批量重算健康度评分。

    Returns:
        刷新的指标数量。
    """
    from sqlalchemy import select

    from app.db.mysql import async_session_factory
    from app.models.metric import Metric
    from app.services.semantic.health_scorer import HealthScorer
    from app.services.semantic.repository import MetricRepository

    count = 0
    async with async_session_factory() as db:
        # 查询全部活跃指标
        stmt = select(Metric).where(Metric.deleted_at.is_(None))
        result = await db.execute(stmt)
        metrics = result.scalars().all()

        scorer = HealthScorer(db)
        repo = MetricRepository(db)

        for metric in metrics:
            try:
                health = await scorer.calculate(metric.id)
                await repo.save_health_score(health)
                count += 1

                # CRITICAL/WARNING 进整改待办
                if health.level in ("WARNING", "CRITICAL"):
                    logger.info(
                        "health_critical_detected",
                        metric_code=metric.metric_code,
                        score=health.score,
                        level=health.level,
                    )
            except Exception:
                logger.warning(
                    "health_refresh_failed",
                    metric_id=metric.id,
                )

        await db.commit()

    logger.info("health_scores_refreshed", count=count)
    return count


async def check_emergency_review_overdue(ctx: dict[str, Any]) -> list[int]:
    """检查紧急发布 24h 补审（每小时 cron）。

    Returns:
        超时未补审的 metric_id 列表。
    """
    from sqlalchemy import select

    from app.db.mysql import async_session_factory
    from app.models.metric import Metric

    overdue: list[int] = []
    now = datetime.now(UTC)
    deadline = now - timedelta(hours=24)

    async with async_session_factory() as db:
        stmt = select(Metric).where(
            Metric.emergency_publish.is_(True),
            Metric.emergency_reviewed_at.is_(None),
            Metric.created_at < deadline,
            Metric.deleted_at.is_(None),
        )
        result = await db.execute(stmt)
        metrics = result.scalars().all()

        for metric in metrics:
            logger.warning(
                "emergency_publish_review_overdue",
                metric_code=metric.metric_code,
                metric_id=metric.id,
            )
            overdue.append(metric.id)

    return overdue


async def check_experimental_expiry(ctx: dict[str, Any]) -> list[int]:
    """检查灰度指标超 30 天未决策（每日 cron）。

    Returns:
        超期灰度指标的 metric_id 列表。
    """
    from sqlalchemy import select

    from app.db.mysql import async_session_factory
    from app.models.metric import Metric

    expired: list[int] = []
    now = datetime.now(UTC)
    deadline = now - timedelta(days=30)

    async with async_session_factory() as db:
        stmt = select(Metric).where(
            Metric.status == "EXPERIMENTAL",
            Metric.updated_at < deadline,
            Metric.deleted_at.is_(None),
        )
        result = await db.execute(stmt)
        metrics = result.scalars().all()

        for metric in metrics:
            logger.warning(
                "experimental_metric_expired",
                metric_code=metric.metric_code,
                metric_id=metric.id,
            )
            expired.append(metric.id)

    return expired


# Arq Worker 注册
functions = [
    check_pending_version_timeouts,
    refresh_health_scores,
    check_emergency_review_overdue,
    check_experimental_expiry,
]

# Cron 调度配置（供 arq worker 使用）
cron_jobs = [
    {"func": check_pending_version_timeouts, "cron": "*/1 * * * *"},  # 每分钟
    {"func": refresh_health_scores, "cron": "0 3 * * *"},  # 每日凌晨3点
    {"func": check_emergency_review_overdue, "cron": "0 * * * *"},  # 每小时
    {"func": check_experimental_expiry, "cron": "0 4 * * *"},  # 每日凌晨4点
]
