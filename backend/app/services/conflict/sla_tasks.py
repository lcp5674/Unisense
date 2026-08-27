"""冲突仲裁 SLA 自动升级任务（审查发现：仲裁无 SLA，冲突可永久滞留 OPEN）。

此前仲裁仅人工 escalate（API ``POST /conflicts/{id}/escalate``），无任何定时
扫描超时 OPEN 冲突自动升级。本任务周期扫描超过 SLA 时长仍未裁决的
OPEN/NEGOTIATING 冲突，自动置 ESCALATED 并发布事件——与人工 escalate 共用
同一状态机与事件通道（``ConflictService.escalate``）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.tasks.lock import task_locked

logger = structlog.get_logger("unisense.conflict.sla_tasks")

#: 冲突仲裁 SLA（天）：超过该时长仍未裁决的 OPEN/NEGOTIATING 冲突自动升级。
_CONFLICT_SLA_DAYS = 7
#: 升级后强提醒阈值（小时）：ESCALATED 超过该时长仍未处置，定向提醒治理管理员。
_ESCALATED_REMIND_HOURS = 48
#: 单轮最多升级条数（防御：极端积压时先处理最老的一批，下轮继续）。
_BATCH_LIMIT = 200


@task_locked("conflict-sla-escalation")
async def auto_escalate_overdue(ctx: dict[str, Any]) -> dict[str, int]:
    """扫描超过 SLA 的 OPEN/NEGOTIATING 冲突并自动升级（每日 cron）。

    Args:
        ctx: arq worker 上下文（本任务自建会话，仅用日志）。

    Returns:
        ``{scanned, escalated}`` 统计。
    """
    from sqlalchemy import select

    from app.db.mysql import async_session_factory
    from app.models.conflict import Conflict, ConflictStatus
    from app.services.conflict.schemas import EscalateRequest
    from app.services.conflict.service import ConflictService

    cutoff = datetime.now(UTC) - timedelta(days=_CONFLICT_SLA_DAYS)
    scanned = 0
    escalated = 0

    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(Conflict)
                    .where(
                        Conflict.deleted_at.is_(None),
                        Conflict.status.in_([ConflictStatus.OPEN, ConflictStatus.NEGOTIATING]),
                        Conflict.created_at < cutoff,
                    )
                    .order_by(Conflict.created_at.asc())
                    .limit(_BATCH_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        scanned = len(rows)

        svc = ConflictService(db)
        for conflict in rows:
            try:
                await svc.escalate(
                    conflict.conflict_id,
                    EscalateRequest(note="SLA 超时自动升级"),
                )
                escalated += 1
            except Exception as exc:  # noqa: BLE001 - 单条失败不阻断整批
                logger.warning(
                    "conflict_sla_escalate_failed",
                    conflict_id=conflict.conflict_id,
                    error=str(exc),
                )
        await db.commit()

    logger.info("conflict_sla_escalation_done", scanned=scanned, escalated=escalated)
    return {"scanned": scanned, "escalated": escalated}


@task_locked("conflict-escalated-reminder")
async def remind_stale_escalated(ctx: dict[str, Any]) -> dict[str, int]:
    """扫描升级后超时仍未处置的 ESCALATED 冲突，定向提醒治理管理员（每日 cron）。

    升级链路补全（审查发现）：人工/自动升级只发一次「已升级」通知，无后续兜底——
    ESCALATED 可能无限期滞留（无人负责、无超时提醒）。本任务对升级超过
    ``_ESCALATED_REMIND_HOURS`` 仍未仲裁/关闭的冲突，定向通知其域的 domain_admin
    与全部 platform_admin（notify_user 定向，不依赖订阅偏好），保证升级必有处置出口。

    Args:
        ctx: arq worker 上下文（本任务自建会话，仅用日志）。

    Returns:
        ``{scanned, notified}`` 统计。
    """
    from sqlalchemy import or_, select

    from app.db.mysql import async_session_factory
    from app.models.conflict import Conflict, ConflictStatus
    from app.models.user import User, UserRole
    from app.services.notify.service import NotifyService

    cutoff = datetime.now(UTC) - timedelta(hours=_ESCALATED_REMIND_HOURS)
    scanned = 0
    notified = 0

    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(Conflict)
                    .where(
                        Conflict.deleted_at.is_(None),
                        Conflict.status == ConflictStatus.ESCALATED,
                        Conflict.updated_at < cutoff,
                    )
                    .order_by(Conflict.updated_at.asc())
                    .limit(_BATCH_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        scanned = len(rows)
        if not rows:
            logger.info("conflict_escalated_reminder_done", scanned=0, notified=0)
            return {"scanned": 0, "notified": 0}

        # 治理管理员集合：冲突域 domain_admin + 全部 platform_admin（多角色 union）
        admin_subq = select(UserRole.user_id).where(UserRole.role == "platform_admin")
        admin_stmt = select(User.id).where(
            or_(User.role == "platform_admin", User.id.in_(admin_subq))
        )
        platform_admin_ids = set((await db.execute(admin_stmt)).scalars().all())

        notify = NotifyService(db)
        for conflict in rows:
            recipients = set(platform_admin_ids)
            if conflict.domain:
                dadmin_subq = select(UserRole.user_id).where(UserRole.role == "domain_admin")
                dadmin_stmt = select(User.id).where(
                    User.domain == conflict.domain,
                    or_(User.role == "domain_admin", User.id.in_(dadmin_subq)),
                )
                recipients.update((await db.execute(dadmin_stmt)).scalars().all())
            else:
                dadmin_subq = select(UserRole.user_id).where(UserRole.role == "domain_admin")
                dadmin_stmt = select(User.id).where(
                    or_(User.role == "domain_admin", User.id.in_(dadmin_subq))
                )
                recipients.update((await db.execute(dadmin_stmt)).scalars().all())
            for uid in recipients:
                try:
                    await notify.notify_user(
                        user_id=int(uid),
                        event_type="conflict_escalation_overdue",
                        title="口径冲突升级超时未处置",
                        body=(
                            f"冲突 {conflict.conflict_id} 已升级超过 {_ESCALATED_REMIND_HOURS} "
                            "小时仍未处置，请在冲突仲裁台处理（仲裁/关闭/强制关闭）。"
                        ),
                        payload={
                            "conflict_id": conflict.conflict_id,
                            "source": "conflict",
                        },
                        channel="IN_APP",
                    )
                    notified += 1
                except Exception as exc:  # noqa: BLE001 - 单条通知失败不阻断整批
                    logger.warning(
                        "conflict_escalated_remind_failed",
                        conflict_id=conflict.conflict_id,
                        user_id=uid,
                        error=str(exc),
                    )
        await db.commit()

    logger.info("conflict_escalated_reminder_done", scanned=scanned, notified=notified)
    return {"scanned": scanned, "notified": notified}
