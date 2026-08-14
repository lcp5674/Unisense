"""可观测性 Repository（TD §12.10 / FR-16）。

聚合查询覆盖质量事件、审计日志、通知、血缘等既有表，便于运营大盘。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.conflict import Conflict, ConflictStatus
from app.models.consume import ApiClient, ApiClientStatus
from app.models.data_source import DataSource
from app.models.dimension import Dimension
from app.models.escalation import EscalationRecord, EscalationStatus
from app.models.feedback import Feedback
from app.models.lineage import LineageEdge
from app.models.metric import Metric
from app.models.notify import EventLog, Notification
from app.models.quality import QualityEvent, QualityEventStatus
from app.models.subject_domain import SubjectDomain
from app.models.term import Term


class ObservabilityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_feedback(self, obj: Feedback) -> Feedback:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def get_feedback(self, feedback_id: int) -> Feedback | None:
        """获取单条反馈。"""
        stmt = select(Feedback).where(Feedback.id == feedback_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

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

    async def overview_stats(self) -> dict[str, Any]:
        """平台运营总览聚合（生产视角：健康/积压/资产/消费一次拉齐）。"""
        # 1. 数据源健康分布
        src_rows = (
            await self._session.execute(
                select(DataSource.health_status, func.count()).group_by(
                    DataSource.health_status
                )
            )
        ).all()
        sources_by_health = dict(cast("Sequence[tuple[Any, Any]]", src_rows))
        # 2. 治理积压：待处理冲突 / 未关闭质量事件 / 待审核指标 / 未闭环升级
        open_conflicts = (
            await self._session.execute(
                select(func.count())
                .select_from(Conflict)
                .where(
                    Conflict.status.in_(
                        [ConflictStatus.OPEN, ConflictStatus.NEGOTIATING]
                    )
                )
            )
        ).scalar() or 0
        pending_quality = (
            await self._session.execute(
                select(func.count())
                .select_from(QualityEvent)
                .where(
                    QualityEvent.status.in_(
                        [QualityEventStatus.OPEN, QualityEventStatus.ACK]
                    )
                )
            )
        ).scalar() or 0
        review_metrics = (
            await self._session.execute(
                select(func.count())
                .select_from(Metric)
                .where(Metric.status == "REVIEW", Metric.deleted_at.is_(None))
            )
        ).scalar() or 0
        open_escalations = (
            await self._session.execute(
                select(func.count())
                .select_from(EscalationRecord)
                .where(
                    EscalationRecord.status.in_(
                        [EscalationStatus.ESCALATED, EscalationStatus.ACKNOWLEDGED]
                    )
                )
            )
        ).scalar() or 0
        # 3. 资产规模（软删除过滤，反映真实存量）
        metrics_by_status_rows = (
            await self._session.execute(
                select(Metric.status, func.count())
                .where(Metric.deleted_at.is_(None))
                .group_by(Metric.status)
            )
        ).all()
        metrics_by_status = dict(
            cast("Sequence[tuple[Any, Any]]", metrics_by_status_rows)
        )
        term_count = (
            await self._session.execute(
                select(func.count()).select_from(Term).where(Term.deleted_at.is_(None))
            )
        ).scalar() or 0
        dimension_count = (
            await self._session.execute(
                select(func.count())
                .select_from(Dimension)
                .where(Dimension.deleted_at.is_(None))
            )
        ).scalar() or 0
        domain_count = (
            await self._session.execute(
                select(func.count())
                .select_from(SubjectDomain)
                .where(SubjectDomain.deleted_at.is_(None))
            )
        ).scalar() or 0
        # 4. 消费接入：接入方总数 / 活跃数
        clients_total = (
            await self._session.execute(
                select(func.count()).select_from(ApiClient).where(
                    ApiClient.deleted_at.is_(None)
                )
            )
        ).scalar() or 0
        clients_active = (
            await self._session.execute(
                select(func.count())
                .select_from(ApiClient)
                .where(
                    ApiClient.deleted_at.is_(None),
                    ApiClient.status == ApiClientStatus.ACTIVE,
                )
            )
        ).scalar() or 0
        return {
            "sources": {
                "by_health": sources_by_health,
                "total": sum(sources_by_health.values()),
            },
            "backlog": {
                "open_conflicts": open_conflicts,
                "pending_quality_events": pending_quality,
                "review_metrics": review_metrics,
                "open_escalations": open_escalations,
            },
            "assets": {
                "metrics_by_status": metrics_by_status,
                "terms": term_count,
                "dimensions": dimension_count,
                "domains": domain_count,
                "sources": sum(sources_by_health.values()),
            },
            "clients": {"total": clients_total, "active": clients_active},
        }

    async def commit(self) -> None:
        await self._session.commit()
