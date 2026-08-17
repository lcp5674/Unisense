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

                # CRITICAL/WARNING → 定向告警指标 Owner/备份 Owner（P1-5：
                # 此前仅 log 不通知——每日刷新是发现健康恶化的主路径，恰恰不通知。
                # notify_user 为定向投递，不依赖订阅偏好，保证 Owner 必达。）
                if health.level in ("WARNING", "CRITICAL"):
                    logger.info(
                        "health_critical_detected",
                        metric_code=metric.metric_code,
                        score=health.score,
                        level=health.level,
                    )
                    await _notify_health_degraded(db, metric, health)
            except Exception:
                logger.warning(
                    "health_refresh_failed",
                    metric_id=metric.id,
                )

        await db.commit()

    logger.info("health_scores_refreshed", count=count)
    return count


async def _notify_health_degraded(db: Any, metric: Any, health: Any) -> None:
    """健康恶化（WARNING/CRITICAL）→ 定向通知指标 Owner + 备份 Owner。

    复用已注册的 ``metric.health_critical`` 模板（标题映射见 notify/service.py），
    与读端点事件一致；best-effort，通知失败仅记日志不阻断每日刷新。
    """
    from app.services.notify.service import NotifyService

    level_cn = {"WARNING": "预警", "CRITICAL": "严重"}.get(health.level, health.level)
    targets = [metric.owner_id]
    if getattr(metric, "backup_owner_id", None) and metric.backup_owner_id != metric.owner_id:
        targets.append(metric.backup_owner_id)
    missing = getattr(health, "missing_dimensions", None) or []
    for uid in targets:
        try:
            await NotifyService(db).notify_user(
                user_id=uid,
                event_type="metric.health_critical",
                title=f"指标 {metric.metric_code} 健康度{level_cn}",
                body=(
                    f"{metric.metric_code} 健康评分 {health.score:.0f} 分（{level_cn}），"
                    f"缺失维度 {len(missing)} 项，请及时关注修复。"
                ),
                payload={
                    "metric_code": metric.metric_code,
                    "score": health.score,
                    "level": health.level,
                    "missing_dimensions": missing,
                },
            )
        except Exception as exc:  # noqa: BLE001 - best-effort 不阻断刷新
            logger.warning(
                "health_notify_failed metric=%s user=%s err=%s",
                metric.metric_code,
                uid,
                exc,
            )


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
    """灰度超期强制回收（每日 cron，P1-7）。

    查找超 30 天未决策的 EXPERIMENTAL 指标：定向通知 Owner+备份 Owner 后，
    强制回收到 DRAFT（``metric.gray_recycled`` 事件 + 审计）。
    回收避免灰度无限滞留——Owner 可重新提交评审继续推进。

    Returns:
        被回收的 EXPERIMENTAL 指标 metric_id 列表。
    """
    from sqlalchemy import select

    from app.db.mysql import async_session_factory
    from app.models.metric import Metric
    from app.services.semantic.service import MetricService

    recycled: list[int] = []
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

        svc = MetricService(db)
        for metric in metrics:
            try:
                # 1) 定向告警 Owner/备份 Owner：灰度超期将被回收
                await _notify_gray_recycled(db, metric)
                # 2) 强制回收 EXPERIMENTAL → DRAFT（系统触发）
                await svc.recycle_expired_gray(metric.metric_code, actor_id=0)
                recycled.append(metric.id)
                logger.warning(
                    "experimental_metric_recycled",
                    metric_code=metric.metric_code,
                    metric_id=metric.id,
                )
            except Exception:
                logger.warning(
                    "experimental_recycle_failed",
                    metric_id=metric.id,
                    exc_info=True,
                )

        await db.commit()

    return recycled


async def _notify_gray_recycled(db: Any, metric: Any) -> None:
    """灰度超期回收 → 定向通知指标 Owner + 备份 Owner（IN_APP，不依赖订阅偏好）。

    复用已注册的 ``metric.gray_recycled`` 模板；best-effort，通知失败仅记日志。
    """
    from app.services.notify.service import NotifyService

    targets = [metric.owner_id]
    if getattr(metric, "backup_owner_id", None) and metric.backup_owner_id != metric.owner_id:
        targets.append(metric.backup_owner_id)
    for uid in targets:
        try:
            await NotifyService(db).notify_user(
                user_id=uid,
                event_type="metric.gray_recycled",
                title=f"指标 {metric.metric_code} 灰度超期已回收",
                body=(
                    f"{metric.metric_code} 灰度发布超过 30 天未决策，已强制回收至草稿。"
                    "如需继续发布，请重新提交评审。"
                ),
                payload={
                    "metric_code": metric.metric_code,
                    "reason": "gray_expiry",
                    "domain": metric.domain,
                },
            )
        except Exception as exc:  # noqa: BLE001 - best-effort 不阻断回收
            logger.warning(
                "gray_recycle_notify_failed metric=%s user=%s err=%s",
                metric.metric_code,
                uid,
                exc,
            )


async def _notify_dsd_overdue(db: Any, metric: Any) -> None:
    """DSD 处理超期 → 定向升级提醒指标 Owner + 备份 Owner（IN_APP，不依赖订阅偏好）。

    best-effort，通知失败仅记日志；复用 ``metric.source_dropped`` 模板，
    正文标注「已超 7 天处理期」，与初次 DSD 通知区分开。
    """
    from app.services.notify.service import NotifyService

    targets = [metric.owner_id]
    if getattr(metric, "backup_owner_id", None) and metric.backup_owner_id != metric.owner_id:
        targets.append(metric.backup_owner_id)
    for uid in targets:
        try:
            await NotifyService(db).notify_user(
                user_id=uid,
                event_type="metric.source_dropped",
                title=f"指标 {metric.metric_code} 数据源下线超期待处理",
                body=(
                    f"{metric.metric_code} 数据源下线已超过 7 天处理期仍未处理，"
                    "请尽快恢复发布或确认退役，避免消费方持续受影响。"
                ),
                payload={
                    "metric_code": metric.metric_code,
                    "domain": metric.domain,
                    "reason": "dsd_overdue",
                },
            )
        except Exception as exc:  # noqa: BLE001 - best-effort 不阻断巡检
            logger.warning(
                "dsd_overdue_notify_failed metric=%s user=%s err=%s",
                metric.metric_code,
                uid,
                exc,
            )


async def check_dsd_overdue(ctx: dict[str, Any]) -> list[int]:
    """DSD 处理超期升级提醒（每日 cron，P1-4 闭环）。

    数据源下线后 Owner 有 7 天处理期（恢复发布 / 确认退役）。超过 7 天仍未
    处理的 DATA_SOURCE_DROPPED 指标，定向升级提醒 Owner+备份 Owner（复用
    ``metric.source_dropped`` 模板），驱动闭环完成；处理（恢复/退役）后
    状态离开 DSD，巡检自然不再命中。

    Returns:
        已发送超期提醒的 DSD 指标 metric_id 列表。
    """
    from sqlalchemy import select

    from app.db.mysql import async_session_factory
    from app.models.metric import Metric

    reminded: list[int] = []
    now = datetime.now(UTC)
    deadline = now - timedelta(days=7)

    async with async_session_factory() as db:
        stmt = select(Metric).where(
            Metric.status == "DATA_SOURCE_DROPPED",
            Metric.updated_at < deadline,
            Metric.deleted_at.is_(None),
        )
        result = await db.execute(stmt)
        metrics = result.scalars().all()

        for metric in metrics:
            try:
                await _notify_dsd_overdue(db, metric)
                reminded.append(metric.id)
                logger.warning(
                    "dsd_overdue_reminded",
                    metric_code=metric.metric_code,
                    metric_id=metric.id,
                )
            except Exception:
                logger.warning(
                    "dsd_overdue_check_failed",
                    metric_id=metric.id,
                    exc_info=True,
                )

        await db.commit()

    return reminded


# Arq Worker 注册
functions = [
    check_pending_version_timeouts,
    refresh_health_scores,
    check_emergency_review_overdue,
    check_experimental_expiry,
    check_dsd_overdue,
]

# Cron 调度配置（供 arq worker 使用）
cron_jobs = [
    {"func": check_pending_version_timeouts, "cron": "*/1 * * * *"},  # 每分钟
    {"func": refresh_health_scores, "cron": "0 3 * * *"},  # 每日凌晨3点
    {"func": check_emergency_review_overdue, "cron": "0 * * * *"},  # 每小时
    {"func": check_experimental_expiry, "cron": "0 4 * * *"},  # 每日凌晨4点
    {"func": check_dsd_overdue, "cron": "30 3 * * *"},  # 每日凌晨3点30分（健康刷新后）
]
