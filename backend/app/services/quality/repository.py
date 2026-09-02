"""数据质量仓储（TD §12.8 / FR-10）。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric import Metric
from app.models.quality import (
    ExternalBenchmark,
    QualityEvent,
    QualityEventStatus,
    QualityObservation,
    QualityRule,
    QualityRuleType,
    QualitySeverity,
    ReconciliationRecord,
)


class QualityRepository:
    """质量规则与异常事件的持久化访问。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ---- QualityRule ----
    @staticmethod
    def _domain_metric_condition(domain: str | None, is_platform_admin: bool) -> list[Any]:
        """域作用域条件（非管理角色按指标域隔离跨域质量数据）。

        返回 ``Metric.domain == domain`` 的 join 条件列表；平台管理员全量。

        用户级隔离（方案 A）：非管理角色域为空（未指派域 = 不限域）时不过滤——
        数据范围不限制（与前端「不限域」展示一致）。
        """
        if is_platform_admin:
            return []
        if not domain:
            return []
        return [Metric.domain == domain]

    async def create_rule(self, rule: QualityRule) -> QualityRule:
        self._db.add(rule)
        await self._db.flush()
        await self._db.refresh(rule)
        return rule

    async def get_rule(self, rule_id: int) -> QualityRule | None:
        stmt = select(QualityRule).where(
            QualityRule.id == rule_id, QualityRule.deleted_at.is_(None)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def list_all_enabled_rules(self) -> list[QualityRule]:
        """列出全部启用规则（供自动调度扫描，不做分页——规则数量级小）。

        Returns:
            全部 enabled=True 且未删除的质量规则。
        """
        stmt = select(QualityRule).where(
            QualityRule.enabled.is_(True),
            QualityRule.deleted_at.is_(None),
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def list_rules(
        self,
        metric_id: int | None,
        rule_type: QualityRuleType | None,
        severity: QualitySeverity | None,
        enabled: bool | None,
        page: int,
        page_size: int,
        domain: str | None = None,
        is_platform_admin: bool = False,
    ) -> tuple[list[QualityRule], int]:
        conditions: list[Any] = [QualityRule.deleted_at.is_(None)]
        if metric_id is not None:
            conditions.append(QualityRule.metric_id == metric_id)
        if rule_type is not None:
            conditions.append(QualityRule.rule_type == rule_type)
        if severity is not None:
            conditions.append(QualityRule.severity == severity)
        if enabled is not None:
            conditions.append(QualityRule.enabled == enabled)
        conditions += self._domain_metric_condition(domain, is_platform_admin)
        count_stmt = (
            select(func.count())
            .select_from(QualityRule)
            .join(Metric, QualityRule.metric_id == Metric.id)
            .where(*conditions)
        )
        total = int((await self._db.execute(count_stmt)).scalar() or 0)
        stmt = (
            select(QualityRule)
            .join(Metric, QualityRule.metric_id == Metric.id)
            .where(*conditions)
            .order_by(QualityRule.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return list(rows), total

    async def update_rule(self, rule: QualityRule, **fields: Any) -> QualityRule:
        for key, value in fields.items():
            if value is not None:
                setattr(rule, key, value)
        await self._db.flush()
        return rule

    async def delete_rule(self, rule: QualityRule) -> None:
        rule.deleted_at = datetime.now(UTC)
        await self._db.flush()

    async def list_enabled_rules_for(
        self, metric_id: int, rule_type: QualityRuleType
    ) -> list[QualityRule]:
        stmt = select(QualityRule).where(
            QualityRule.metric_id == metric_id,
            QualityRule.rule_type == rule_type,
            QualityRule.enabled.is_(True),
            QualityRule.deleted_at.is_(None),
        )
        return list((await self._db.execute(stmt)).scalars().all())

    # ---- QualityEvent ----
    async def create_event(self, event: QualityEvent) -> QualityEvent:
        self._db.add(event)
        await self._db.flush()
        await self._db.refresh(event)
        return event

    async def get_event(self, event_id: int) -> QualityEvent | None:
        stmt = select(QualityEvent).where(
            QualityEvent.id == event_id, QualityEvent.deleted_at.is_(None)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def find_open_event(self, metric_id: int, rule_type: Any) -> QualityEvent | None:
        """查找该指标+规则类型下仍为 OPEN 的质量事件（幂等去重用）。

        观测持续异常且无新观测时，若既有 OPEN 事件未关闭，自动检测不应重复
        落新事件+告警刷屏（审查发现 quality 自动任务非幂等）。
        """
        stmt = (
            select(QualityEvent)
            .where(
                QualityEvent.metric_id == metric_id,
                QualityEvent.rule_type == rule_type,
                QualityEvent.status == QualityEventStatus.OPEN,
                QualityEvent.deleted_at.is_(None),
            )
            .order_by(QualityEvent.id.desc())
            .limit(1)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def list_events(
        self,
        metric_id: int | None,
        status: QualityEventStatus | None,
        level: QualitySeverity | None,
        page: int,
        page_size: int,
        domain: str | None = None,
        is_platform_admin: bool = False,
        actor_id: int | None = None,
    ) -> tuple[list[QualityEvent], int]:
        conditions: list[Any] = [QualityEvent.deleted_at.is_(None)]
        if metric_id is not None:
            conditions.append(QualityEvent.metric_id == metric_id)
        if status is not None:
            conditions.append(QualityEvent.status == status)
        if level is not None:
            conditions.append(QualityEvent.level == level)
        conditions += self._domain_metric_condition(domain, is_platform_admin)
        # 个人工作台（待办中心）收敛：仅本人名下（Owner/副 Owner）指标的质量事件，
        # 避免把本域他人指标告警混入个人待办。
        if actor_id is not None:
            conditions.append(
                or_(Metric.owner_id == actor_id, Metric.backup_owner_id == actor_id)
            )
        count_stmt = (
            select(func.count())
            .select_from(QualityEvent)
            .join(Metric, QualityEvent.metric_id == Metric.id)
            .where(*conditions)
        )
        total = int((await self._db.execute(count_stmt)).scalar() or 0)
        stmt = (
            select(QualityEvent)
            .join(Metric, QualityEvent.metric_id == Metric.id)
            .where(*conditions)
            .order_by(QualityEvent.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return list(rows), total

    # ---- QualityObservation（Epic 6：动态基线 / 同环比 / 跨源时序底座）----
    async def record_observation(self, obs: QualityObservation) -> QualityObservation:
        self._db.add(obs)
        await self._db.flush()
        await self._db.refresh(obs)
        return obs

    async def list_recent_observations(
        self, metric_id: int, since: datetime | None = None, limit: int = 2000
    ) -> list[QualityObservation]:
        """取某指标在时间窗口内的历史观测（按时间升序，供基线计算）。"""
        conditions = [
            QualityObservation.metric_id == metric_id,
            QualityObservation.deleted_at.is_(None),
        ]
        if since is not None:
            conditions.append(QualityObservation.obs_time >= since)
        stmt = (
            select(QualityObservation)
            .where(*conditions)
            .order_by(QualityObservation.obs_time.asc())
            .limit(limit)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def latest_observations_for_metrics(
        self, metric_ids: list[int]
    ) -> dict[int, QualityObservation]:
        """批量取多个指标的最新观测（P6：一次 IN 查询替代 N 次单查）。"""
        if not metric_ids:
            return {}
        stmt = (
            select(QualityObservation)
            .where(
                QualityObservation.metric_id.in_(metric_ids),
                QualityObservation.deleted_at.is_(None),
            )
            .order_by(QualityObservation.metric_id, QualityObservation.obs_time.desc())
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        result: dict[int, QualityObservation] = {}
        for r in rows:
            result.setdefault(r.metric_id, r)
        return result

    async def latest_observation(self, metric_id: int) -> QualityObservation | None:
        """取某指标最近一次观测（按时间倒序取 1 条）。

        供自动检测使用：与 ``list_recent_observations``（升序 + limit）区分，
        后者取前 N 条最旧观测，取末条并非最新（观测超过 limit 时取到的是陈旧值）。
        """
        stmt = (
            select(QualityObservation)
            .where(
                QualityObservation.metric_id == metric_id,
                QualityObservation.deleted_at.is_(None),
            )
            .order_by(QualityObservation.obs_time.desc())
            .limit(1)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def get_same_period_observation(
        self, metric_id: int, target_time: datetime, window_hours: int = 12
    ) -> QualityObservation | None:
        """取对照期（如上年同月 / 上周同小时）最接近 target_time 的观测，用于同环比。

        在 target_time ± window_hours 内按时间距离升序取第一条；无覆盖则返回 None（冷启动）。
        """
        from datetime import timedelta

        low = target_time - timedelta(hours=window_hours)
        high = target_time + timedelta(hours=window_hours)
        stmt = (
            select(QualityObservation)
            .where(
                QualityObservation.metric_id == metric_id,
                QualityObservation.deleted_at.is_(None),
                QualityObservation.obs_time >= low,
                QualityObservation.obs_time <= high,
            )
            .order_by(
                func.abs(
                    func.timestampdiff(text("SECOND"), QualityObservation.obs_time, target_time)
                ).asc()
            )
            .limit(1)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def list_latest_per_source(self, metric_id: int) -> list[QualityObservation]:
        """取某指标各 source 的最新一次观测（供跨源 spread 检测）。"""
        latest = (
            select(
                QualityObservation.source_id,
                func.max(QualityObservation.obs_time).label("max_time"),
            )
            .where(
                QualityObservation.metric_id == metric_id,
                QualityObservation.deleted_at.is_(None),
            )
            .group_by(QualityObservation.source_id)
        ).subquery()
        stmt = (
            select(QualityObservation)
            .join(
                latest,
                (QualityObservation.source_id == latest.c.source_id)
                & (QualityObservation.obs_time == latest.c.max_time),
            )
            .order_by(QualityObservation.source_id.asc())
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def transition_event(
        self,
        event: QualityEvent,
        status: QualityEventStatus,
        operator_id: int,
        ack_note: str | None = None,
    ) -> QualityEvent:
        """推进事件状态机，并落操作人留痕（责任人与时间）。

        ACK 时附 ack_note（运营处理说明）。operator_id 必填，消除 user_id 死参数。
        """
        event.status = status
        now = datetime.now(UTC)
        if status == QualityEventStatus.ACK:
            event.ack_by = operator_id
            event.ack_at = now
            if ack_note is not None:
                event.ack_note = ack_note
        elif status == QualityEventStatus.RESOLVED:
            event.resolved_by = operator_id
            event.resolved_at = now
        elif status == QualityEventStatus.CLOSED:
            event.closed_by = operator_id
            event.closed_at = now
        await self._db.flush()
        return event

    async def save_event(self, event: QualityEvent) -> QualityEvent:
        """持久化事件字段变更（如 repair_suggestion 确认留痕）。"""
        self._db.add(event)
        await self._db.flush()
        await self._db.refresh(event)
        return event

    # ---- ExternalBenchmark（外部基准值，TD §4.15.7）----
    async def find_benchmark(
        self,
        source_id: str,
        metric_code: str,
        bench_date: date,
        dims: dict[str, Any] | None,
    ) -> ExternalBenchmark | None:
        """按幂等键 (source_id, metric_code, bench_date, dims) 查找既有基准。"""
        stmt = select(ExternalBenchmark).where(
            ExternalBenchmark.source_id == source_id,
            ExternalBenchmark.metric_code == metric_code,
            ExternalBenchmark.bench_date == bench_date,
        )
        rows = list((await self._db.execute(stmt)).scalars().all())
        if dims is None:
            return next((r for r in rows if r.dims is None), None)
        dims_json = json.dumps(dims, sort_keys=True, ensure_ascii=False)
        for r in rows:
            if r.dims is None:
                continue
            if json.dumps(r.dims, sort_keys=True, ensure_ascii=False) == dims_json:
                return r
        return None

    async def save_benchmark(self, bench: ExternalBenchmark) -> ExternalBenchmark:
        self._db.add(bench)
        await self._db.flush()
        await self._db.refresh(bench)
        return bench

    async def get_benchmark(self, benchmark_id: int) -> ExternalBenchmark | None:
        stmt = select(ExternalBenchmark).where(ExternalBenchmark.id == benchmark_id)
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def list_benchmarks(
        self,
        metric_code: str | None,
        source_id: str | None,
        page: int,
        page_size: int,
        domain: str | None = None,
        is_platform_admin: bool = False,
    ) -> tuple[list[ExternalBenchmark], int]:
        conditions: list[Any] = []
        if metric_code is not None:
            conditions.append(ExternalBenchmark.metric_code == metric_code)
        if source_id is not None:
            conditions.append(ExternalBenchmark.source_id == source_id)
        conditions += self._domain_metric_condition(domain, is_platform_admin)
        count_stmt = (
            select(func.count())
            .select_from(ExternalBenchmark)
            .join(Metric, ExternalBenchmark.metric_code == Metric.metric_code)
            .where(*conditions)
        )
        total = int((await self._db.execute(count_stmt)).scalar() or 0)
        stmt = (
            select(ExternalBenchmark)
            .join(Metric, ExternalBenchmark.metric_code == Metric.metric_code)
            .where(*conditions)
            .order_by(ExternalBenchmark.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return list(rows), total

    async def list_due_benchmark_ids(self, cutoff: datetime) -> list[int]:
        """返回超过对账周期的 benchmark id 清单（P1 对账触发）。

        到期定义：该 benchmark 在 ``cutoff`` 之后没有任何对账记录（含从未对账）。
        """
        recent_exists = (
            select(func.count())
            .select_from(ReconciliationRecord)
            .where(
                ReconciliationRecord.benchmark_id == ExternalBenchmark.id,
                ReconciliationRecord.deleted_at.is_(None),
                ReconciliationRecord.created_at >= cutoff,
            )
            .exists()
        )
        stmt = (
            select(ExternalBenchmark.id)
            .where(ExternalBenchmark.deleted_at.is_(None), ~recent_exists)
        )
        rows = (await self._db.execute(stmt)).all()
        return [r[0] for r in rows]

    # ---- ReconciliationRecord（外部基准对账记录，TD §4.15.7）----
    async def save_reconciliation(self, rec: ReconciliationRecord) -> ReconciliationRecord:
        self._db.add(rec)
        await self._db.flush()
        await self._db.refresh(rec)
        return rec

    async def get_reconciliation(self, record_id: int) -> ReconciliationRecord | None:
        stmt = select(ReconciliationRecord).where(ReconciliationRecord.id == record_id)
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def list_reconciliations(
        self,
        status: str | None,
        metric_code: str | None,
        page: int,
        page_size: int,
        domain: str | None = None,
        is_platform_admin: bool = False,
    ) -> tuple[list[ReconciliationRecord], int]:
        conditions: list[Any] = []
        if status is not None:
            conditions.append(ReconciliationRecord.status == status)
        if metric_code is not None:
            conditions.append(ReconciliationRecord.metric_code == metric_code)
        conditions += self._domain_metric_condition(domain, is_platform_admin)
        count_stmt = (
            select(func.count())
            .select_from(ReconciliationRecord)
            .join(Metric, ReconciliationRecord.metric_code == Metric.metric_code)
            .where(*conditions)
        )
        total = int((await self._db.execute(count_stmt)).scalar() or 0)
        stmt = (
            select(ReconciliationRecord)
            .join(Metric, ReconciliationRecord.metric_code == Metric.metric_code)
            .where(*conditions)
            .order_by(ReconciliationRecord.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return list(rows), total
