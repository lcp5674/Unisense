"""冲突检测超时升级定时任务（TECH-08: T051）。

Arq 定时任务：扫描 conflict 表中 status=OPEN 且 created_at 超过阈值
（默认 48h）的记录，自动升级为 ESCALATED 状态并通知域管理员裁决。

对齐 TD §12.3 冲突检测 / DEV_GUIDE §14 事件驱动。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

logger = logging.getLogger(__name__)

# 冲突超时阈值：48 小时未裁决自动升级
_CONFLICT_ESCALATION_HOURS = 48


async def conflict_escalation_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """Arq 定时任务：冲突超时自动升级。

    步骤：
    1. 查询 conflict 表中 status=OPEN 且 created_at < 48h 前的记录
    2. 批量更新 status 为 ESCALATED
    3. 发布冲突升级事件（通知域管理员）

    任务自建 DB 会话（对齐 quality/semantic tasks 模式），不依赖 ctx 注入 db。
    """
    from app.db.mysql import async_session_factory

    async with async_session_factory() as db:
        cutoff = datetime.now(UTC) - timedelta(hours=_CONFLICT_ESCALATION_HOURS)

        # 1. 查询超时未裁决的冲突
        from app.models.conflict import Conflict

        stmt = (
            select(Conflict)
            .where(
                Conflict.status == "OPEN",
                Conflict.created_at < cutoff,
            )
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()

        if not rows:
            return {"status": "SUCCESS", "escalated": 0}

        # 2. 批量更新状态为 ESCALATED
        row_ids = [row.id for row in rows]
        await db.execute(
            update(Conflict)
            .where(Conflict.id.in_(row_ids))
            .values(status="ESCALATED")
        )
        await db.commit()

        # 3. 发布冲突升级事件（best-effort）
        try:
            from app.core.eventbus import get_eventbus

            bus = get_eventbus()
            for row in rows:
                await bus.publish(
                    "conflict.escalated",
                    {
                        "conflict_id": row.id,
                        "conflict_type": getattr(row, "conflict_type", "unknown"),
                        "metric_code": getattr(row, "metric_code", ""),
                        "hours_open": _CONFLICT_ESCALATION_HOURS,
                    },
                )
        except Exception:
            logger.warning("conflict_escalation_event_publish_failed", exc_info=True)

        logger.info(
            "conflict_escalation_task: escalated %d conflicts",
            len(rows),
        )
        return {"status": "SUCCESS", "escalated": len(rows)}
