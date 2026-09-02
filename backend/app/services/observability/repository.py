"""可观测性 Repository（TD §12.10 / FR-16）。

聚合查询覆盖质量事件、审计日志、通知、血缘等既有表，便于运营大盘。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.collector_models import CollectionRun, CollectionWatermark, SchemaDriftLog
from app.models.conflict import Conflict, ConflictStatus
from app.models.consume import ApiClient, ApiClientStatus
from app.models.data_source import DataSource, DBCatalog
from app.models.dependency_health import DependencyHealth
from app.models.dimension import Dimension
from app.models.escalation import EscalationRecord, EscalationStatus
from app.models.feedback import Feedback
from app.models.governance import Grant, GrantStatus
from app.models.lineage import LineageEdge, LineageIngestRun
from app.models.metric import Metric
from app.models.metric_health import MetricHealthScore
from app.models.notify import EventLog, Notification
from app.models.quality import QualityEvent, QualityEventStatus
from app.models.subject_domain import SubjectDomain
from app.models.term import Term
from app.models.user import User
from app.services.semantic.visibility import metric_visibility_conditions


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
        org_id: int | None = None,
    ) -> tuple[list[Feedback], int]:
        """反馈列表（分页 + 状态过滤 + 软删过滤），返回 (items, total)。

        ``org_id`` 非 None 时按反馈人所属组织隔离（防跨组织反馈/处理意见泄露给
        任意 viewer，对齐 overview_stats 的 org 隔离语义）；平台管理员 None 全量。
        """
        # 与其它模块一致的软删语义：deleted_at IS NULL 的记录才展示
        stmt = select(Feedback).where(Feedback.deleted_at.is_(None))
        count_stmt = (
            select(func.count()).select_from(Feedback).where(Feedback.deleted_at.is_(None))
        )
        if org_id is not None:
            # Feedback 无 org_id 列，经反馈人 user_id → user.org_id 关联到组织
            org_user_ids = select(User.id).where(User.org_id == org_id)
            stmt = stmt.where(Feedback.user_id.in_(org_user_ids))
            count_stmt = count_stmt.where(Feedback.user_id.in_(org_user_ids))
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

    async def quality_events(
        self, limit: int = 20, domain: str | None = None
    ) -> list[dict[str, Any]]:
        """最近质量事件明细（供运营中心明细面板）。

        字段对齐 QualityEvent 模型：补全观测值/阈值/规则类型/操作留痕/修复建议，
        并批量 JOIN Metric 取得指标名（避免 N+1），让运营一眼看清"什么指标、
        因为什么规则、观测值多少/阈值多少、当前谁在处理"。

        domain: 第三轮审查——域管理员按本域收敛（join Metric 过滤），platform_admin
            传 None 全量，防 domain_admin 跨域读他域质量事件（指标名/修复建议）。
        """
        stmt = select(QualityEvent).order_by(
            QualityEvent.created_at.desc(), QualityEvent.id.desc()
        )
        if domain is not None:
            stmt = (
                stmt.join(Metric, Metric.id == QualityEvent.metric_id)
                .where(Metric.deleted_at.is_(None), Metric.domain == domain)
            )
        events = list((await self._session.execute(stmt.limit(limit))).scalars())
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

    async def quality_stats(self, domain: str | None = None) -> dict[str, Any]:
        """质量事件统计（level/status 分布），按域收敛（第三轮审查，对齐 events）。"""
        def _apply(stmt: Any) -> Any:
            if domain is not None:
                stmt = stmt.join(Metric, Metric.id == QualityEvent.metric_id).where(
                    Metric.deleted_at.is_(None), Metric.domain == domain
                )
            return stmt

        by_level = (
            await self._session.execute(
                _apply(select(QualityEvent.level, func.count())).group_by(QualityEvent.level)
            )
        ).all()
        by_status = (
            await self._session.execute(
                _apply(select(QualityEvent.status, func.count())).group_by(QualityEvent.status)
            )
        ).all()
        return {
            "by_level": dict(cast("Sequence[tuple[Any, Any]]", by_level)),
            "by_status": dict(cast("Sequence[tuple[Any, Any]]", by_status)),
            "total": sum(cnt for _, cnt in by_status),
        }

    async def metric_health_stats(
        self,
        actor_id: int | None = None,
        role: str | None = None,
        user_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """指标健康度摘要（总览仪表「指标可信度」卡片数据源，全员可读）。

        与 /overview 的 quality.metric_health 同源，但独立轻量端点：非管理角色按
        P0-3 可见性收敛（仅公开状态 + 本人私有），避免仪表盘经 /overview 拉取
        全局 OPS 遥测；管理角色（actor_id=None）全量。
        """
        visibility = metric_visibility_conditions(actor_id, role, user_domains)
        live_join = (
            MetricHealthScore.metric_id == Metric.id,
            MetricHealthScore.deleted_at.is_(None),
            Metric.deleted_at.is_(None),
            *visibility,
        )
        by_level_rows = (
            await self._session.execute(
                select(MetricHealthScore.level, func.count())
                .join(Metric, Metric.id == MetricHealthScore.metric_id)
                .where(*live_join)
                .group_by(MetricHealthScore.level)
            )
        ).all()
        by_level = {str(level): int(cnt) for level, cnt in by_level_rows}
        total_scored = sum(by_level.values())
        avg_score = (
            await self._session.execute(
                select(func.avg(MetricHealthScore.score))
                .join(Metric, Metric.id == MetricHealthScore.metric_id)
                .where(*live_join)
            )
        ).scalar() or 0
        risk_rows = (
            await self._session.execute(
                select(
                    MetricHealthScore.metric_id,
                    MetricHealthScore.score,
                    MetricHealthScore.level,
                    Metric.name,
                    Metric.metric_code,
                )
                .join(Metric, Metric.id == MetricHealthScore.metric_id)
                .where(*live_join, MetricHealthScore.level.in_(["WARNING", "CRITICAL"]))
                .order_by(MetricHealthScore.score.asc())
                .limit(5)
            )
        ).all()
        total_metrics = (
            await self._session.execute(
                select(func.count())
                .select_from(Metric)
                .where(Metric.deleted_at.is_(None), *visibility)
            )
        ).scalar() or 0
        return {
            "metric_health": {
                "by_level": by_level,
                "total_scored": total_scored,
                "avg_score": round(float(avg_score)) if total_scored else 0,
                "coverage_pct": (
                    round(total_scored / total_metrics * 100, 1) if total_metrics else 0.0
                ),
                "top_risk": [
                    {
                        "metric_id": mid,
                        "metric_name": mname,
                        "metric_code": mcode,
                        "score": score,
                        "level": str(level),
                    }
                    for mid, score, level, mname, mcode in risk_rows
                ],
            }
        }

    async def api_stats(self) -> dict[str, int]:
        # P1（性能审查）：action 无索引 + audit_log 为 WORM 只增不减表——无时间窗口
        # 的全表 GROUP BY 随数据增长线性恶化，且旧日志对「当前 API 画像」无意义。
        # 加近 30 天窗口（created_at 有索引），扫描范围收敛到近期写入。
        since = datetime.now(UTC) - timedelta(days=30)
        rows = (
            await self._session.execute(
                select(AuditLog.action, func.count())
                .where(AuditLog.created_at >= since)
                .group_by(AuditLog.action)
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

    async def overview_stats(self, org_id: int | None = None) -> dict[str, Any]:
        """平台运营总览聚合（企业级：系统健康 / 资产质量 / 风险雷达 / 近7天趋势 一次拉齐）。

        数据口径统一对齐各模块自身语义 + 软删过滤（deleted_at IS NULL）：
        - 资产存量快照：数据源健康 / 治理积压 / 资产规模 / 消费接入；
        - 系统健康：核心依赖实时态（dependency_health）+ 采集链路健康（collection_run/watermark）；
        - 资产质量：指标健康度分布（metric_health_score）+ 血缘健康（lineage_edge/ingest_run）；
        - 风险雷达：PII 待复核（按组织隔离）、授权即将到期、近 7 天 Schema 漂移；
        - 趋势：近 7 天指标新增 / 采集运行按天聚合。

        ``org_id`` 非 None 时 PII 待复核数经 ``db_catalog → data_source.org_id`` 按
        组织隔离（平台管理员 org_id=None 全量；其余角色仅见本组织，防 PII 合规计数
        跨组织泄露给任意 viewer）。

        P5（性能审查）：约 15-20 个独立聚合 COUNT 全表，每次请求重算无缓存——
        加 30s cache-aside（key 含 org_id 区分组织），写操作后可显式失效。
        """
        from app.core.agg_cache import agg_cached

        return await agg_cached(
            f"observability:overview:{org_id or 'all'}",
            lambda: self._overview_uncached(org_id),
        )

    async def _overview_uncached(self, org_id: int | None = None) -> dict[str, Any]:
        """overview_stats 的回源实现（缓存未命中时执行全量聚合）。"""
        assets = await self._overview_assets()
        system = await self._overview_system()
        quality = await self._overview_quality()
        risks = await self._overview_risks(org_id)
        trends = await self._overview_trends()
        # 指标健康度覆盖率：已评分指标 / 资产总指标（口径一致，软删过滤）
        total_metrics = sum(assets["assets"]["metrics_by_status"].values())
        scored = quality["metric_health"]["total_scored"]
        quality["metric_health"]["coverage_pct"] = (
            round(scored / total_metrics * 100, 1) if total_metrics else 0.0
        )
        return {**assets, "system": system, "quality": quality, "risks": risks, "trends": trends}

    async def _overview_assets(self) -> dict[str, Any]:
        """资产存量快照：数据源健康 / 治理积压 / 资产规模 / 消费接入（软删过滤）。"""
        src_rows = (
            await self._session.execute(
                select(DataSource.health_status, func.count())
                .where(DataSource.deleted_at.is_(None))
                .group_by(DataSource.health_status)
            )
        ).all()
        sources_by_health = dict(cast("Sequence[tuple[Any, Any]]", src_rows))
        # 冲突未决口径对齐冲突模块：OPEN/NEGOTIATING/ESCALATED
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

    async def _overview_system(self) -> dict[str, Any]:
        """系统健康：核心依赖实时态 + 采集链路健康（熔断/失败/新鲜度是运维第一信号）。"""
        dep_rows = (await self._session.execute(select(DependencyHealth))).scalars().all()
        by_status: dict[str, int] = {}
        circuit_open = 0
        items: list[dict[str, Any]] = []
        for d in dep_rows:
            by_status[d.status] = by_status.get(d.status, 0) + 1
            if d.circuit_state == "OPEN":
                circuit_open += 1
            items.append(
                {
                    "dependency_type": d.dependency_type,
                    "dependency_id": d.dependency_id,
                    "status": d.status,
                    "circuit_state": d.circuit_state,
                    "consecutive_failures": d.consecutive_failures,
                    "latency_p95_ms": d.latency_p95_ms,
                    "error_rate_pct": d.error_rate_pct,
                    "last_check_at": d.last_check_at,
                    # 探针扩展信息（enabled=false 表示未配置未启用，前端展示「未启用」）
                    "meta": d.meta,
                }
            )
        run_rows = (
            await self._session.execute(
                select(CollectionRun.status, func.count())
                .where(CollectionRun.deleted_at.is_(None))
                .group_by(CollectionRun.status)
            )
        ).all()
        by_run = dict(cast("Sequence[tuple[Any, Any]]", run_rows))
        running = by_run.get("RUNNING", 0)
        failed = by_run.get("FAILED", 0)
        completed = by_run.get("COMPLETED", 0)
        finished = completed + failed
        success_rate = round(completed / finished * 100, 1) if finished else 0.0
        latest_wm = (
            await self._session.execute(
                select(func.max(CollectionWatermark.last_collected_at)).where(
                    CollectionWatermark.deleted_at.is_(None)
                )
            )
        ).scalar()
        return {
            "dependencies": {
                "by_status": by_status,
                "circuit_open": circuit_open,
                "total": len(items),
                "items": items,
            },
            "collection": {
                "by_status": by_run,
                "total": running + failed + completed,
                "running": running,
                "failed": failed,
                "success_rate_pct": success_rate,
                "last_collected_at": latest_wm,
            },
        }

    async def _overview_quality(self) -> dict[str, Any]:
        """资产质量：指标健康度分布（EXCELLENT/GOOD/WARNING/CRITICAL）+ 血缘健康。

        P3（性能审查）：原实现把全量 MetricHealthScore JOIN Metric 拉到 Python
        内存算分布/平均分，risks 先全量收集再截断 top5——指标量大后每次 overview
        全量装载。改为 SQL 层 GROUP BY / AVG / ORDER+LIMIT 聚合，内存只承载
        聚合结果与 top5 明细。
        """
        from sqlalchemy import func, select

        # 仅统计健康评分对应指标仍存活（未软删）的记录——覆盖率与资产指标数同口径，
        # 避免历史评分记录（对应指标已删除）导致 coverage_pct > 100% 的数据失真。
        live_join = (
            MetricHealthScore.metric_id == Metric.id,
            MetricHealthScore.deleted_at.is_(None),
            Metric.deleted_at.is_(None),
        )
        # 1) 按等级分组计数（替代全量行内存遍历）
        by_level_rows = (
            await self._session.execute(
                select(MetricHealthScore.level, func.count())
                .join(Metric, Metric.id == MetricHealthScore.metric_id)
                .where(*live_join)
                .group_by(MetricHealthScore.level)
            )
        ).all()
        by_level = {str(level): int(cnt) for level, cnt in by_level_rows}
        total_scored = sum(by_level.values())
        # 2) 平均分（SQL 层 AVG）
        avg_score = (
            await self._session.execute(
                select(func.avg(MetricHealthScore.score))
                .join(Metric, Metric.id == MetricHealthScore.metric_id)
                .where(*live_join)
            )
        ).scalar() or 0
        # 3) 低健康 Top5 明细（WARNING/CRITICAL 按分升序取前 5，替代全量收集后截断）
        risk_rows = (
            await self._session.execute(
                select(
                    MetricHealthScore.metric_id,
                    MetricHealthScore.score,
                    MetricHealthScore.level,
                    MetricHealthScore.missing_dimensions,
                    Metric.name,
                    Metric.metric_code,
                )
                .join(Metric, Metric.id == MetricHealthScore.metric_id)
                .where(*live_join, MetricHealthScore.level.in_(["WARNING", "CRITICAL"]))
                .order_by(MetricHealthScore.score.asc())
                .limit(5)
            )
        ).all()
        risks = [
            {
                "metric_id": mid,
                # 指标名/编码随行返回，前端「低健康指标」直接展示业务名称而非裸 ID
                "metric_name": mname,
                "metric_code": mcode,
                "score": score,
                "level": str(level),
                "missing_dimensions": missing,
            }
            for mid, score, level, missing, mname, mcode in risk_rows
        ]
        edges = (
            await self._session.execute(
                select(func.count())
                .select_from(LineageEdge)
                .where(LineageEdge.deleted_at.is_(None))
            )
        ).scalar() or 0
        stale = (
            await self._session.execute(
                select(func.count())
                .select_from(LineageEdge)
                .where(
                    LineageEdge.deleted_at.is_(None),
                    LineageEdge.stale.is_(True),
                )
            )
        ).scalar() or 0
        ingest = (
            await self._session.execute(
                select(
                    func.count(),
                    func.max(LineageIngestRun.run_at),
                ).where(LineageIngestRun.status == "success")
            )
        ).one()
        return {
            "metric_health": {
                "by_level": by_level,
                "total_scored": total_scored,
                "avg_score": round(float(avg_score)) if total_scored else 0,
                "top_risk": risks,
            },
            "lineage": {
                "edges": edges,
                "stale": stale,
                "ingest_success": int(ingest[0] or 0),
                "last_ingest_at": ingest[1],
            },
        }

    async def _overview_risks(self, org_id: int | None = None) -> dict[str, Any]:
        """风险雷达：PII 待复核 / 授权即将到期 / 近 7 天 Schema 漂移。

        PII 待复核按 ``org_id`` 隔离（数据源资产维度，join data_source.org_id）；
        授权到期 / Schema 漂移为平台级治理项，保持全量。
        """
        pii_stmt = (
            select(func.count())
            .select_from(DBCatalog)
            .where(
                DBCatalog.deleted_at.is_(None),
                DBCatalog.sensitivity_level.in_(["PII", "CONFIDENTIAL"]),
                DBCatalog.compliance_reviewed.is_(False),
            )
        )
        if org_id is not None:
            pii_stmt = pii_stmt.join(
                DataSource, DataSource.source_id == DBCatalog.source_id
            ).where(DataSource.deleted_at.is_(None), DataSource.org_id == org_id)
        pii_pending = (await self._session.execute(pii_stmt)).scalar() or 0
        expiring_cutoff = datetime.now(UTC) + timedelta(days=7)
        expiring = (
            await self._session.execute(
                select(func.count())
                .select_from(Grant)
                .where(
                    Grant.deleted_at.is_(None),
                    Grant.status == GrantStatus.ACTIVE,
                    Grant.expires_at.is_not(None),
                    Grant.expires_at <= expiring_cutoff,
                )
            )
        ).scalar() or 0
        drift_since = datetime.now(UTC) - timedelta(days=7)
        drift = (
            await self._session.execute(
                select(func.count())
                .select_from(SchemaDriftLog)
                .where(
                    SchemaDriftLog.deleted_at.is_(None),
                    SchemaDriftLog.detected_at >= drift_since,
                )
            )
        ).scalar() or 0
        return {
            "pii_review_pending": pii_pending,
            "grants_expiring_soon": expiring,
            "schema_drift_7d": drift,
        }

    async def _overview_trends(self, days: int = 7) -> dict[str, Any]:
        """近 N 天趋势：指标新增 / 采集运行 按天聚合（缺失日期补 0）。"""
        since = datetime.now(UTC) - timedelta(days=days - 1)
        metric_rows = (
            await self._session.execute(
                select(func.date(Metric.created_at), func.count())
                .where(
                    Metric.deleted_at.is_(None),
                    Metric.created_at >= since,
                )
                .group_by(func.date(Metric.created_at))
            )
        ).all()
        collect_rows = (
            await self._session.execute(
                select(func.date(CollectionRun.started_at), func.count())
                .where(
                    CollectionRun.deleted_at.is_(None),
                    CollectionRun.started_at >= since,
                )
                .group_by(func.date(CollectionRun.started_at))
            )
        ).all()
        metric_map = dict(cast("Sequence[tuple[Any, Any]]", metric_rows))
        collect_map = dict(cast("Sequence[tuple[Any, Any]]", collect_rows))
        dates = [since.date() + timedelta(days=i) for i in range(days)]
        return {
            "days": days,
            "metrics_created": [
                {"date": str(d), "count": int(metric_map.get(d, 0))} for d in dates
            ],
            "collections": [
                {"date": str(d), "count": int(collect_map.get(d, 0))} for d in dates
            ],
        }

    async def commit(self) -> None:
        await self._session.commit()
