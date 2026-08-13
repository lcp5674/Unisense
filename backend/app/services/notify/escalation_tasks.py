"""告警升级重试周期任务（B2：升级状态化重试/逐级升级的调度驱动）。

对 ``escalation_record`` 中到点未确认（``next_retry_at <= now``）的升级执行：
- 未达当前级别上限 → 重发 ``escalation.triggered``（attempts+1）；
- 达到上限 → 逐级升级（P2→P1→P0）并重置计数；
- 已是 P0 且到上限 → ``MAXED_OUT`` 停止。

自建会话执行（与 quality/tasks 一致），结束后统一 commit；
任何记录失败不影响整轮扫描（best-effort）。
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger("unisense.notify.escalation_tasks")


async def check_escalation_retries(ctx: dict[str, Any]) -> dict[str, int]:
    """扫描到点升级并驱动重试/升级。

    Args:
        ctx: arq worker 上下文（本任务自建会话）。

    Returns:
        ``{due, resent, escalated, maxed_out}`` 统计。
    """
    from app.db.mysql import async_session_factory
    from app.services.notify.escalation import EscalationService

    async with async_session_factory() as db:
        svc = EscalationService(session=db)
        stats = await svc.check_retries()
        await db.commit()

    if stats["due"]:
        logger.info(
            "escalation_retries_done",
            due=stats["due"],
            resent=stats["resent"],
            escalated=stats["escalated"],
            maxed_out=stats["maxed_out"],
        )
    return stats
