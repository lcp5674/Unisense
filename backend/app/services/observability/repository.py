"""可观测性 Repository（TD §12.10 / FR-16）。

聚合查询覆盖质量事件、审计日志、通知、血缘等既有表，便于运营大盘。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.feedback import Feedback
from app.models.lineage import LineageEdge
from app.models.notify import EventLog, Notification
from app.models.quality import QualityEvent


class ObservabilityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_feedback(self, obj: Feedback) -> Feedback:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def list_feedback(self, target_type: str | None, limit: int) -> list[Feedback]:
        stmt = select(Feedback)
        if target_type:
            stmt = stmt.where(Feedback.target_type == target_type)
        rows = (
            (await self._session.execute(stmt.order_by(Feedback.id.desc()).limit(limit)))
            .scalars()
            .all()
        )
        return list(rows)

    async def quality_stats(self) -> dict[str, Any]:
        by_level = (
            await self._session.execute(
                select(QualityEvent.level, func.count()).group_by(QualityEvent.level)
            )
        ).all()
        by_status = (
            await self._session.execute(
                select(QualityEvent.status, func.count()).group_by(QualityEvent.status)
            )
        ).all()
        return {
            "by_level": dict(cast("Sequence[tuple[Any, Any]]", by_level)),
            "by_status": dict(cast("Sequence[tuple[Any, Any]]", by_status)),
            "total": sum(cnt for _, cnt in by_status),
        }

    async def api_stats(self) -> dict[str, int]:
        rows = (
            await self._session.execute(
                select(AuditLog.action, func.count()).group_by(AuditLog.action)
            )
        ).all()
        return dict(cast("Sequence[tuple[Any, Any]]", rows))

    async def notification_stats(self) -> dict[str, Any]:
        by_status = (
            await self._session.execute(
                select(Notification.status, func.count()).group_by(Notification.status)
            )
        ).all()
        total_events = (
            await self._session.execute(select(func.count()).select_from(EventLog))
        ).scalar() or 0
        notified_events = (
            await self._session.execute(
                select(func.count()).select_from(EventLog).where(EventLog.notified.is_(True))
            )
        ).scalar() or 0
        return {
            "by_status": dict(cast("Sequence[tuple[Any, Any]]", by_status)),
            "event_total": total_events,
            "event_notified": notified_events,
        }

    async def lineage_stats(self) -> dict[str, int]:
        edges = (
            await self._session.execute(select(func.count()).select_from(LineageEdge))
        ).scalar() or 0
        return {"edges": edges}

    async def commit(self) -> None:
        await self._session.commit()
