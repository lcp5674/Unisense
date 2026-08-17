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
from app.models.user import User


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

    async def list_feedback(
        self,
        target_type: str | None,
        status: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[Feedback], int]:
        """反馈列表（分页 + 状态过滤 + 软删过滤），返回 (items, total)。"""
        # 与其它模块一致的软删语义：deleted_at IS NULL 的记录才展示
        stmt = select(Feedback).where(Feedback.deleted_at.is_(None))
        count_stmt = (
            select(func.count()).select_from(Feedback).where(Feedback.deleted_at.is_(None))
        )
        if target_type:
            stmt = stmt.where(Feedback.target_type == target_type)
            count_stmt = count_stmt.where(Feedback.target_type == target_type)
        if status:
            stmt = stmt.where(Feedback.status == status)
            count_stmt = count_stmt.where(Feedback.status == status)
        total = (await self._session.execute(count_stmt)).scalar() or 0
        rows = (
            (
                await self._session.execute(
                    stmt.order_by(Feedback.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), total

    async def resolve_target_names(self, items: list[Feedback]) -> dict[int, str | None]:
        """批量解析反馈对象名称（当前支持 metric 类），供前端直显。

        返回 ``{feedback_id: 对象名称}``；对象不存在/已软删/非 metric 类时该 id 为
        None（前端据此标记「已失效」）。一次批量查询，避免前端逐条探测详情接口
        产生 N+1 请求与 404 噪音。
        """
        if not items:
            return {}
        metric_codes = {
            f.target_id for f in items if f.target_type == "metric" and f.target_id
        }
        names: dict[str, str] = {}
        if metric_codes:
            rows = (
                await self._session.execute(
                    select(Metric.metric_code, Metric.name).where(
                        Metric.metric_code.in_(metric_codes),
                        Metric.deleted_at.is_(None),
                    )
                )
            ).all()
            names = dict(rows)
        return {
            f.id: (names.get(f.target_id) if f.target_type == "metric" and f.target_id else None)
            for f in items
        }

    async def nps_stats(self) -> dict[str, Any]:
        """NPS 分布统计：promoter≥9 / passive 7-8 / detractor≤6，过滤 nps_score 为空。"""
        total = (
            await self._session.execute(
                select(func.count())
                .select_from(Feedback)
                .where(Feedback.nps_score.is_not(None))
            )
        ).scalar() or 0
        promoters = (
            await self._session.execute(
                select(func.count())
                .select_from(Feedback)
                .where(Feedback.nps_score.is_not(None), Feedback.nps_score >= 9)
            )
        ).scalar() or 0
        passives = (
            await self._session.execute(
                select(func.count())
                .select_from(Feedback)
                .where(
                    Feedback.nps_score.is_not(None),
                    Feedback.nps_score.between(7, 8),
                )
            )
        ).scalar() or 0
        detractors = (
            await self._session.execute(
                select(func.count())
                .select_from(Feedback)
                .where(Feedback.nps_score.is_not(None), Feedback.nps_score <= 6)
            )
        ).scalar() or 0
        score = round((promoters - detractors) / total * 100, 2) if total else 0.0
        return {
            "total": total,
            "promoters": promoters,
            "passives": passives,
            "detractors": detractors,
            "score": score,
        }

    async def quality_events(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近质量事件明细（供运营中心明细面板）。

        字段对齐 QualityEvent 模型：补全观测值/阈值/规则类型/操作留痕/修复建议，
        并批量 JOIN Metric 取得指标名（避免 N+1），让运营一眼看清"什么指标、
        因为什么规则、观测值多少/阈值多少、当前谁在处理"。
        """
        events = list(
            (
                await self._session.execute(
                    select(QualityEvent)
                    .order_by(QualityEvent.created_at.desc(), QualityEvent.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        # 批量取关联指标名（一次 IN 查询，避免逐条查）
        metric_ids = {e.metric_id for e in events if e.metric_id}
        metric_names: dict[int, str] = {}
        metric_codes: dict[int, str] = {}
        metric_domains: dict[int, str] = {}
        if metric_ids:
            metric_rows = (
                await self._session.execute(
                    select(Metric.id, Metric.name, Metric.metric_code, Metric.domain).where(
                        Metric.id.in_(metric_ids), Metric.deleted_at.is_(None)
                    )
                )
            ).all()
            for mid, mname, mcode, mdomain in metric_rows:
                metric_names[mid] = mname
                metric_codes[mid] = mcode
                metric_domains[mid] = mdomain
        # 批量解析处理人用户名（ACK/RESOLVE/CLOSE 留痕的负责人，数字 ID → 可读用户名）
        user_ids = {
            uid
            for e in events
            for uid in (e.ack_by, e.resolved_by, e.closed_by)
            if uid
        }
        user_names: dict[int, str] = {}
        if user_ids:
            user_rows = (
                await self._session.execute(
                    select(User.id, User.display_name, User.username).where(User.id.in_(user_ids))
                )
            ).all()
            user_names = {
                uid: (display_name if display_name else username)
                for uid, display_name, username in user_rows
            }
        return [
            {
                "id": e.id,
                "level": e.level.value,
                "status": e.status.value,
                "rule_type": e.rule_type.value,
                "obs_value": float(e.obs_value) if e.obs_value is not None else None,
                "threshold": float(e.threshold) if e.threshold is not None else None,
                "metric_id": e.metric_id,
                "metric_name": metric_names.get(e.metric_id),
                "metric_code": metric_codes.get(e.metric_id),
                "metric_domain": metric_domains.get(e.metric_id),
                "ack_note": e.ack_note,
                "ack_by": e.ack_by,
                "ack_by_name": user_names.get(e.ack_by) if e.ack_by else None,
                "ack_at": e.ack_at,
                "resolved_by": e.resolved_by,
                "resolved_by_name": user_names.get(e.resolved_by) if e.resolved_by else None,
                "resolved_at": e.resolved_at,
                "closed_by": e.closed_by,
                "closed_by_name": user_names.get(e.closed_by) if e.closed_by else None,
                "closed_at": e.closed_at,
                "repair_suggestion": e.repair_suggestion,
                "created_at": e.created_at,
            }
            for e in events
        ]

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
        """平台运营总览聚合（生产视角：健康/积压/资产/消费一次拉齐）。

        数据口径统一对齐各模块自身语义 + 软删过滤（deleted_at IS NULL）：
        - 数据源健康 / 资产规模：仅统计存活（未软删）数据源——此前漏过滤会
          把已软删数据源计入，导致平台概览与数据源管理页数字不一致；
        - 治理积压的"待处理冲突"：对齐冲突模块的未决口径
          （OPEN/NEGOTIATING/ESCALATED），且仅统计未软删冲突；
        - 质量事件 / 升级：同样过滤软删，与 health_scorer / 升级模块口径一致。
        """
        # 1. 数据源健康分布（仅未软删）
        src_rows = (
            await self._session.execute(
                select(DataSource.health_status, func.count())
                .where(DataSource.deleted_at.is_(None))
                .group_by(DataSource.health_status)
            )
        ).all()
        sources_by_health = dict(cast("Sequence[tuple[Any, Any]]", src_rows))
        # 2. 治理积压：待处理冲突 / 未关闭质量事件 / 待审核指标 / 未闭环升级
        #    冲突未决口径对齐冲突模块（count_open_for_metric）：OPEN/NEGOTIATING/ESCALATED
        open_conflicts = (
            await self._session.execute(
                select(func.count())
                .select_from(Conflict)
                .where(
                    Conflict.deleted_at.is_(None),
                    Conflict.status.in_(
                        [
                            ConflictStatus.OPEN,
                            ConflictStatus.NEGOTIATING,
                            ConflictStatus.ESCALATED,
                        ]
                    ),
                )
            )
        ).scalar() or 0
        pending_quality = (
            await self._session.execute(
                select(func.count())
                .select_from(QualityEvent)
                .where(
                    QualityEvent.deleted_at.is_(None),
                    QualityEvent.status.in_(
                        [QualityEventStatus.OPEN, QualityEventStatus.ACK]
                    ),
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
                    EscalationRecord.deleted_at.is_(None),
                    EscalationRecord.status.in_(
                        [EscalationStatus.ESCALATED, EscalationStatus.ACKNOWLEDGED]
                    ),
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
