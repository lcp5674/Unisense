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

logger = structlog.get_logger("unisense.conflict.sla_tasks")

#: 冲突仲裁 SLA（天）：超过该时长仍未裁决的 OPEN/NEGOTIATING 冲突自动升级。
_CONFLICT_SLA_DAYS = 7
#: 单轮最多升级条数（防御：极端积压时先处理最老的一批，下轮继续）。
_BATCH_LIMIT = 200


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
