"""数据质量仓储（TD §12.8 / FR-10）。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

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
        count_stmt = select(func.count()).select_from(QualityRule).where(*conditions)
        total = int((await self._db.execute(count_stmt)).scalar() or 0)
        stmt = (
            select(QualityRule)
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

    async def list_events(
        self,
        metric_id: int | None,
        status: QualityEventStatus | None,
        level: QualitySeverity | None,
        page: int,
        page_size: int,
    ) -> tuple[list[QualityEvent], int]:
        conditions: list[Any] = [QualityEvent.deleted_at.is_(None)]
        if metric_id is not None:
            conditions.append(QualityEvent.metric_id == metric_id)
        if status is not None:
            conditions.append(QualityEvent.status == status)
        if level is not None:
            conditions.append(QualityEvent.level == level)
        count_stmt = select(func.count()).select_from(QualityEvent).where(*conditions)
        total = int((await self._db.execute(count_stmt)).scalar() or 0)
        stmt = (
            select(QualityEvent)
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
    ) -> tuple[list[ExternalBenchmark], int]:
        conditions: list[Any] = []
        if metric_code is not None:
            conditions.append(ExternalBenchmark.metric_code == metric_code)
        if source_id is not None:
            conditions.append(ExternalBenchmark.source_id == source_id)
        count_stmt = select(func.count()).select_from(ExternalBenchmark).where(*conditions)
        total = int((await self._db.execute(count_stmt)).scalar() or 0)
        stmt = (
            select(ExternalBenchmark)
            .where(*conditions)
            .order_by(ExternalBenchmark.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return list(rows), total

    # ---- ReconciliationRecord（外部基准对账记录，TD §4.15.7）----
    async def save_reconciliation(
        self, rec: ReconciliationRecord
    ) -> ReconciliationRecord:
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
    ) -> tuple[list[ReconciliationRecord], int]:
        conditions: list[Any] = []
        if status is not None:
            conditions.append(ReconciliationRecord.status == status)
        if metric_code is not None:
            conditions.append(ReconciliationRecord.metric_code == metric_code)
        count_stmt = select(func.count()).select_from(ReconciliationRecord).where(*conditions)
        total = int((await self._db.execute(count_stmt)).scalar() or 0)
        stmt = (
            select(ReconciliationRecord)
            .where(*conditions)
            .order_by(ReconciliationRecord.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self._db.execute(stmt)).scalars().all()
        return list(rows), total
