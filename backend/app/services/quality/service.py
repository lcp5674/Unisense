"""数据质量服务（TD §12.8 / FR-10）。

责任：
- 质量规则 CRUD（随指标 PUBLISHED 注册，按 tier/dw_layer 差异化）
- 异常事件闭环（OPEN→ACK→RESOLVED→CLOSED）
- 检测引擎一期：静态阈值评估（obs vs threshold），命中则落 QualityEvent + 触发告警（best-effort）
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.quality import (
    ExternalBenchmark,
    QualityEvent,
    QualityEventStatus,
    QualityObservation,
    QualityRule,
    QualityRuleMode,
    QualityRuleType,
    QualitySeverity,
    ReconciliationRecord,
    ReconciliationStatus,
)
from app.services.quality.events import QualityEventPublisher
from app.services.quality.repository import QualityRepository
from app.services.quality.schemas import (
    BenchmarkBind,
    BenchmarkImport,
    BenchmarkResponse,
    QualityEventResponse,
    QualityObservationRequest,
    QualityObservationResponse,
    QualityRuleCreate,
    QualityRuleResponse,
    QualityRuleUpdate,
    ReconciliationConfirm,
    ReconciliationRecordResponse,
    ReconciliationRun,
)

# ALERT 默认容忍差异率为 1.00%，WARN 阈值为容忍率的两倍
_DEFAULT_TOLERANCE = Decimal("1.00")

logger = structlog.get_logger()

_OPS: dict[str, Any] = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

# 严重级排序（P0 最优先），用于检测命中时取最高严重级
_SEV_RANK: dict[QualitySeverity, int] = {
    QualitySeverity.P0: 0,
    QualitySeverity.P1: 1,
    QualitySeverity.P2: 2,
}


def _median(values: list[Decimal]) -> Decimal:
    """中位数（动态基线中心趋势，对离群点鲁棒）。"""
    if not values:
        return Decimal(0)
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _population_std(values: list[Decimal]) -> Decimal:
    """总体标准差 σ（动态基线离散度）。"""
    if len(values) < 2:
        return Decimal(0)
    mean = sum(values) / Decimal(len(values))
    sq_sum = Decimal(0)
    for v in values:
        sq_sum += (v - mean) ** 2
    variance = sq_sum / Decimal(len(values))
    return variance.sqrt()


# 修复建议模板（TD §4.8.5 / PRD 4.8.5）：按规则类型给出处置动作与建议 SQL 骨架。
# 上游 collector_job / ETL 责任人建议沿血缘反查定位（lineage 集成属后续增强）。
_REPAIR_ACTION_TEMPLATES: dict[str, tuple[str, str]] = {
    "COMPLETENESS": (
        "核查上游采集/ETL 是否成功产出，补跑缺失分区并校验行数",
        "SELECT COUNT(*) AS cnt FROM {src} WHERE dt = :dt;",
    ),
    "ACCURACY": (
        "核对源表与目标口径一致性，复核转换/四舍五入/单位换算逻辑",
        "SELECT a.*, b.* FROM {src} a JOIN fct_source b ON a.dt = b.dt;",
    ),
    "TIMELINESS": (
        "检查 ETL 调度延迟与上游产出分区就绪时间，确认 SLA 是否突破",
        "SELECT MAX(partition_ready_at) FROM collector_job WHERE metric_id = :mid;",
    ),
    "CONSISTENCY": (
        "比对关联表一致性，定位写入乱序/重复/覆盖",
        "SELECT COUNT(*) FROM (SELECT 1 FROM {src} GROUP BY key HAVING COUNT(*) > 1) t;",
    ),
    "UNIQUENESS": (
        "排查重复写入（幂等键/去重逻辑失效）",
        "SELECT key, COUNT(*) c FROM {src} GROUP BY key HAVING c > 1;",
    ),
    "VALIDITY": (
        "校验字段格式与取值范围约束（枚举/非空/正则）",
        "SELECT * FROM {src} WHERE col IS NULL OR col NOT REGEXP '^[[:alnum:]]+$';",
    ),
    "WAVE_DIFF": (
        "对比历史基线，确认是业务真实波动还是采集异常",
        "SELECT metric_id, obs_time, value FROM quality_observation "
        "WHERE metric_id = :mid ORDER BY obs_time DESC LIMIT 30;",
    ),
    "CROSS_SOURCE": (
        "比对多来源最新值，定位异常来源采集任务",
        "SELECT source_id, value FROM quality_observation "
        "WHERE metric_id = :mid ORDER BY obs_time DESC;",
    ),
}

_PATTERN_BY_MODE: dict[str, str] = {
    "static": "static_threshold_breach",
    "dynamic_baseline": "dynamic_baseline_deviation",
    "yoy_woy": "period_over_period_delta",
    "cross_source": "cross_source_spread",
}


def _build_repair_suggestion(
    rule: QualityRule, metric_id: int, obs: Decimal, bound: Decimal | None
) -> dict[str, Any]:
    """构造质量异常修复建议（TD §4.8.5）。

    含规则类型/严重级/异常模式/责任方提示/上游任务引用/处置动作/建议 SQL 骨架/观测与基线，
    供 Owner 线下修复闭环；确认动作记录在 confirmed_by/confirmed_at。
    """
    rt = (
        rule.rule_type.value
        if isinstance(rule.rule_type, QualityRuleType)
        else str(rule.rule_type)
    )
    mode = (
        rule.rule_mode.value
        if isinstance(rule.rule_mode, QualityRuleMode)
        else str(rule.rule_mode)
    )
    severity = (
        rule.severity.value
        if isinstance(rule.severity, QualitySeverity)
        else str(rule.severity)
    )
    action, sql = _REPAIR_ACTION_TEMPLATES.get(
        rt, ("排查指标数据质量异常根因", "SELECT * FROM quality_event WHERE metric_id = :mid;")
    )
    return {
        "rule_type": rt,
        "severity": severity,
        "pattern": _PATTERN_BY_MODE.get(mode, "threshold_breach"),
        "owner_hint": "指标 Owner 或上游采集任务责任人（建议沿血缘反查定位 ETL 责任人）",
        "upstream_task": f"collector_job:metric:{metric_id}",
        "suggested_action": action,
        "suggested_sql": sql,
        "obs_value": str(obs),
        "baseline": str(bound) if bound is not None else None,
        "generated_at": datetime.utcnow().isoformat(),
        "confirmed_by": None,
        "confirmed_at": None,
    }


class QualityService:
    def __init__(self, db: AsyncSession, publisher: QualityEventPublisher | None = None) -> None:
        self._repo = QualityRepository(db)
        self._publisher = publisher or QualityEventPublisher()

    # ---- 规则 CRUD ----
    async def create_rule(self, payload: QualityRuleCreate, user_id: int) -> QualityRuleResponse:
        if payload.threshold is None or not isinstance(payload.threshold, dict):
            raise ValidationError(
                "threshold 必须为非空字典（静态阈值 / 动态基线 / 同环比 / 跨源参数）",
                error_code="QUALITY_THRESHOLD_INVALID",
            )
        rule = QualityRule(
            metric_id=payload.metric_id,
            rule_type=payload.rule_type,
            threshold=payload.threshold,
            rule_mode=payload.rule_mode,
            severity=payload.severity,
            enabled=payload.enabled,
            notify_targets=payload.notify_targets,
            created_by=user_id,
        )
        rule = await self._repo.create_rule(rule)
        return QualityRuleResponse.from_model(rule)

    async def get_rule(self, rule_id: int) -> QualityRuleResponse:
        rule = await self._repo.get_rule(rule_id)
        if rule is None:
            raise NotFoundError(f"quality rule not found: {rule_id}")
        return QualityRuleResponse.from_model(rule)

    async def list_rules(
        self,
        metric_id: int | None,
        rule_type: QualityRuleType | None,
        severity: QualitySeverity | None,
        enabled: bool | None,
        page: int,
        page_size: int,
    ) -> tuple[list[QualityRuleResponse], int]:
        rows, total = await self._repo.list_rules(
            metric_id, rule_type, severity, enabled, page, page_size
        )
        return [QualityRuleResponse.from_model(r) for r in rows], total

    async def update_rule(self, rule_id: int, payload: QualityRuleUpdate) -> QualityRuleResponse:
        if payload.threshold is not None and (
            not isinstance(payload.threshold, dict) or len(payload.threshold) == 0
        ):
            raise ValidationError(
                "threshold 必须为非空字典", error_code="QUALITY_THRESHOLD_INVALID"
            )
        rule = await self._repo.get_rule(rule_id)
        if rule is None:
            raise NotFoundError(f"quality rule not found: {rule_id}")
        rule = await self._repo.update_rule(
            rule,
            threshold=payload.threshold,
            rule_mode=payload.rule_mode,
            severity=payload.severity,
            enabled=payload.enabled,
            notify_targets=payload.notify_targets,
        )
        return QualityRuleResponse.from_model(rule)

    async def delete_rule(self, rule_id: int) -> None:
        rule = await self._repo.get_rule(rule_id)
        if rule is None:
            raise NotFoundError(f"quality rule not found: {rule_id}")
        await self._repo.delete_rule(rule)

    # ---- 检测引擎（静态阈值 + 动态基线 + 同环比 + 跨源）----
    async def detect(
        self,
        metric_id: int,
        rule_type: QualityRuleType,
        obs_value: Decimal,
        rule_mode: str | None = None,
    ) -> QualityEventResponse | None:
        rules = await self._repo.list_enabled_rules_for(metric_id, rule_type)
        # 按严重级降序，确保命中时取最高严重级（避免 P0 被随机丢弃）
        rules = sorted(rules, key=lambda r: _SEV_RANK.get(r.severity, 9))
        triggered: QualityEvent | None = None
        for rule in rules:
            # 按 rule_mode 分派到对应检测器；未知模式跳过并告警，杜绝静默失效
            mode = rule.rule_mode
            try:
                if mode == QualityRuleMode.STATIC:
                    abnormal, bound = self._evaluate(rule.threshold, obs_value)
                elif mode == QualityRuleMode.DYNAMIC_BASELINE:
                    abnormal, bound = await self._detect_dynamic_baseline(
                        rule, metric_id, obs_value
                    )
                elif mode == QualityRuleMode.YOY_WOY:
                    abnormal, bound = await self._detect_yoy_woy(rule, metric_id, obs_value)
                elif mode == QualityRuleMode.CROSS_SOURCE:
                    abnormal, bound = await self._detect_cross_source(rule, metric_id, obs_value)
                else:
                    logger.warning(
                        "quality.detect skip unknown mode",
                        rule_id=getattr(rule, "id", None),
                        rule_mode=mode.value if mode is not None else None,
                    )
                    continue
            except Exception as exc:  # noqa: BLE001 - 单条规则评估失败不应阻断其他规则
                logger.warning(
                    "quality.detect rule eval failed",
                    rule_id=getattr(rule, "id", None),
                    rule_mode=mode.value if mode is not None else None,
                    error=str(exc),
                )
                continue
            if not abnormal:
                continue
            event = QualityEvent(
                metric_id=metric_id,
                level=rule.severity,
                rule_type=rule.rule_type,
                obs_value=obs_value,
                # 记录实际越界的边界值（方向正确），便于运营回溯异常方向
                threshold=bound,
                status=QualityEventStatus.OPEN,
            )
            # FR-10 修复建议生成（TD §4.8.5）：异常触发时即生成，供 Owner 线下修复闭环
            event.repair_suggestion = _build_repair_suggestion(rule, metric_id, obs_value, bound)
            event = await self._repo.create_event(event)
            await self._publisher.publish(
                {
                    "event_type": "quality.anomaly",
                    "metric_id": metric_id,
                    "level": rule.severity.value,
                    "rule_type": rule.rule_type.value,
                    "obs_value": str(obs_value),
                    "notify_targets": rule.notify_targets,
                    "rule_mode": rule_mode or rule.rule_mode.value,
                }
            )
            triggered = event
            break  # 首条（最高严重级）命中即落一条事件（避免重复刷屏）
        if triggered is None:
            return None
        return QualityEventResponse.from_model(triggered)

    # ---- 观测样本写入（Epic 6）----
    async def record_observation(
        self, payload: QualityObservationRequest
    ) -> QualityObservationResponse:
        """写入一次质量观测样本，供动态基线 / 同环比 / 跨源检测复用。"""
        obs = QualityObservation(
            metric_id=payload.metric_id,
            metric_code=payload.metric_code,
            source_id=payload.source_id,
            obs_time=payload.obs_time,
            value=payload.value,
            dims=payload.dims,
        )
        obs = await self._repo.record_observation(obs)
        return QualityObservationResponse.from_model(obs)

    # ---- 高级模式检测器（Epic 6）----
    async def _detect_dynamic_baseline(
        self, rule: QualityRule, metric_id: int, obs: Decimal
    ) -> tuple[bool, Decimal | None]:
        """动态基线：历史窗口中位数 ± σ 越界判定（TD §4.8.3）。

        冷启动（样本不足 min_samples）时退化为规则内 static_fallback（若有），
        否则不触发（避免无历史即误报）。season_factor 默认 1.0（大促/节假日衰减为后续增强）。
        """
        thr = rule.threshold or {}
        window_days = int(thr.get("window_days", 28))
        sigma = Decimal(str(thr.get("sigma", 3)))
        min_samples = int(thr.get("min_samples", 3))
        now = datetime.utcnow()
        since = now - timedelta(days=window_days)
        history = await self._repo.list_recent_observations(metric_id, since=since)
        values = [Decimal(str(o.value)) for o in history]
        if len(values) < min_samples:
            fallback = thr.get("static_fallback")
            if isinstance(fallback, dict):
                return self._evaluate(fallback, obs)
            return False, None
        baseline = _median(values)
        std = _population_std(values)
        if std == 0:
            abnormal = abs(obs - baseline) > 0
        else:
            exceeding = abs(obs - baseline)
            abnormal = exceeding > sigma * std
        return abnormal, baseline

    async def _detect_yoy_woy(
        self, rule: QualityRule, metric_id: int, obs: Decimal
    ) -> tuple[bool, Decimal | None]:
        """同环比：当前观测 vs 对照期（上年同月 / 上周同小时）观测，超差异率触发。"""
        thr = rule.threshold or {}
        period = str(thr.get("period", "yoy")).lower()
        tolerance = Decimal(str(thr.get("tolerance_pct", 20)))
        window_hours = int(thr.get("window_hours", 12))
        offset = timedelta(weeks=52) if period == "yoy" else timedelta(weeks=1)
        target_time = datetime.utcnow() - offset
        base_obs = await self._repo.get_same_period_observation(
            metric_id, target_time, window_hours=window_hours
        )
        if base_obs is None:
            return False, None  # 冷启动：无对照期样本
        base = Decimal(str(base_obs.value))
        if base == 0:
            return False, None
        diff_pct = (obs - base) / base * 100
        abnormal = abs(diff_pct) > tolerance
        return abnormal, base

    async def _detect_cross_source(
        self, rule: QualityRule, metric_id: int, obs: Decimal
    ) -> tuple[bool, Decimal | None]:
        """跨源检测：当前观测 vs 同指标各 source 最新值，spread 超容忍率触发。"""
        thr = rule.threshold or {}
        tolerance = Decimal(str(thr.get("tolerance_pct", 15)))
        records = await self._repo.list_latest_per_source(metric_id)
        values = [Decimal(str(o.value)) for o in records] + [obs]
        values = [v for v in values if v is not None]
        if len(values) < 2:
            return False, None  # 冷启动：不足两个来源
        ref = min(values)
        if ref == 0:
            return False, None
        spread_pct = (max(values) - ref) / ref * 100
        abnormal = spread_pct > tolerance
        return abnormal, ref

    def _evaluate(self, threshold: dict[str, Any], obs: Decimal) -> tuple[bool, Decimal | None]:
        """评估是否异常，并返回实际越界的边界值（用于事件阈值回溯）。"""
        op = threshold.get("op")
        if op in _OPS and "value" in threshold:
            try:
                # op 描述「正常值应满足的条件」，越界即异常
                triggered = not _OPS[op](obs, Decimal(str(threshold["value"])))
                return triggered, (Decimal(str(threshold["value"])) if triggered else None)
            except Exception:  # noqa: BLE001 - 阈值格式异常按未命中处理
                return False, None
        lower = Decimal(str(threshold["min"])) if "min" in threshold else None
        upper = Decimal(str(threshold["max"])) if "max" in threshold else None
        if lower is not None and obs < lower:
            return True, lower
        if upper is not None and obs > upper:
            return True, upper
        return False, None

    def _eval(self, threshold: dict[str, Any], obs: Decimal) -> bool:
        return self._evaluate(threshold, obs)[0]

    # ---- 事件闭环 ----
    async def list_events(
        self,
        metric_id: int | None,
        status: QualityEventStatus | None,
        level: QualitySeverity | None,
        page: int,
        page_size: int,
    ) -> tuple[list[QualityEventResponse], int]:
        rows, total = await self._repo.list_events(metric_id, status, level, page, page_size)
        return [QualityEventResponse.from_model(r) for r in rows], total

    async def ack_event(self, event_id: int, note: str, user_id: int) -> QualityEventResponse:
        event = await self._repo.get_event(event_id)
        if event is None:
            raise NotFoundError(f"quality event not found: {event_id}")
        if event.status != QualityEventStatus.OPEN:
            raise ValidationError(
                f"event {event_id} status={event.status.value}, only OPEN can ACK",
                error_code="QUALITY_EVENT_STATE_INVALID",
            )
        event = await self._repo.transition_event(
            event, QualityEventStatus.ACK, user_id, ack_note=note
        )
        return QualityEventResponse.from_model(event)

    async def resolve_event(self, event_id: int, user_id: int) -> QualityEventResponse:
        event = await self._repo.get_event(event_id)
        if event is None:
            raise NotFoundError(f"quality event not found: {event_id}")
        if event.status != QualityEventStatus.ACK:
            raise ValidationError(
                f"event {event_id} status={event.status.value}, only ACK can RESOLVE",
                error_code="QUALITY_EVENT_STATE_INVALID",
            )
        event = await self._repo.transition_event(event, QualityEventStatus.RESOLVED, user_id)
        return QualityEventResponse.from_model(event)

    async def close_event(self, event_id: int, user_id: int) -> QualityEventResponse:
        event = await self._repo.get_event(event_id)
        if event is None:
            raise NotFoundError(f"quality event not found: {event_id}")
        if event.status != QualityEventStatus.RESOLVED:
            raise ValidationError(
                f"event {event_id} status={event.status.value}, only RESOLVED can CLOSE",
                error_code="QUALITY_EVENT_STATE_INVALID",
            )
        event = await self._repo.transition_event(event, QualityEventStatus.CLOSED, user_id)
        return QualityEventResponse.from_model(event)

    async def confirm_repair(self, event_id: int, user_id: int) -> QualityEventResponse:
        """Owner 确认已线下修复（TD §4.8.5 闭环）：在修复建议中记录确认留痕。

        修复后复检由下一次观测触发 detect 自然闭环（不在此自动改数）。
        """
        event = await self._repo.get_event(event_id)
        if event is None:
            raise NotFoundError(f"quality event not found: {event_id}")
        if event.status != QualityEventStatus.OPEN:
            raise ValidationError(
                f"event {event_id} status={event.status.value}, only OPEN can confirm repair",
                error_code="QUALITY_EVENT_STATE_INVALID",
            )
        suggestion = event.repair_suggestion
        if not isinstance(suggestion, dict):
            suggestion = {}
        suggestion = dict(suggestion)
        suggestion["confirmed_by"] = user_id
        suggestion["confirmed_at"] = datetime.utcnow().isoformat()
        event.repair_suggestion = suggestion
        event = await self._repo.save_event(event)
        return QualityEventResponse.from_model(event)

    # ---- 外部基准对账（TD §4.15.7）----
    async def import_benchmark(
        self, payload: BenchmarkImport, user_id: int
    ) -> BenchmarkResponse:
        """导入外部权威基准值，幂等（同 key 重复导入视为更新）。"""
        existing = await self._repo.find_benchmark(
            payload.source_id, payload.metric_code, payload.bench_date, payload.dims
        )
        if existing is not None:
            existing.bench_value = payload.bench_value
            existing.provider = payload.provider
            if payload.tolerance_pct is not None:
                existing.tolerance_pct = payload.tolerance_pct
            if payload.dims is not None:
                existing.dims = payload.dims
            existing.imported_by = user_id
            bench = await self._repo.save_benchmark(existing)
        else:
            bench = ExternalBenchmark(
                source_id=payload.source_id,
                metric_code=payload.metric_code,
                bench_date=payload.bench_date,
                dims=payload.dims,
                bench_value=payload.bench_value,
                provider=payload.provider,
                tolerance_pct=payload.tolerance_pct,
                imported_by=user_id,
            )
            bench = await self._repo.save_benchmark(bench)
        await self._publisher.publish(
            {
                "event_type": "benchmark.imported",
                "benchmark_id": bench.id,
                "metric_code": bench.metric_code,
                "provider": bench.provider,
            }
        )
        return BenchmarkResponse.from_model(bench)

    async def list_benchmarks(
        self,
        metric_code: str | None,
        source_id: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[BenchmarkResponse], int]:
        rows, total = await self._repo.list_benchmarks(metric_code, source_id, page, page_size)
        return [BenchmarkResponse.from_model(r) for r in rows], total

    async def bind_benchmark(
        self, benchmark_id: int, payload: BenchmarkBind, user_id: int
    ) -> BenchmarkResponse:
        """绑定基准到目标指标，声明比对口径 / 容忍率。"""
        bench = await self._repo.get_benchmark(benchmark_id)
        if bench is None:
            raise NotFoundError(f"benchmark not found: {benchmark_id}")
        if payload.metric_code is not None:
            bench.metric_code = payload.metric_code
        if payload.tolerance_pct is not None:
            bench.tolerance_pct = payload.tolerance_pct
        if payload.dims is not None:
            bench.dims = payload.dims
        bench = await self._repo.save_benchmark(bench)
        return BenchmarkResponse.from_model(bench)

    async def run_reconciliation(
        self, payload: ReconciliationRun, user_id: int
    ) -> ReconciliationRecordResponse:
        """执行一次对账：基准值 vs 平台观测值，自动判定差异状态。"""
        bench = await self._repo.get_benchmark(payload.benchmark_id)
        if bench is None:
            raise NotFoundError(f"benchmark not found: {payload.benchmark_id}")
        tolerance = bench.tolerance_pct if bench.tolerance_pct is not None else _DEFAULT_TOLERANCE
        if bench.bench_value == 0:
            raise ValidationError(
                "bench_value 为 0，无法计算差异率", error_code="BENCH_VALUE_ZERO"
            )
        diff_pct = (payload.metric_value - bench.bench_value) / bench.bench_value * 100
        abs_diff = abs(diff_pct)
        if abs_diff <= tolerance:
            status = ReconciliationStatus.OK
        elif abs_diff <= tolerance * 2:
            status = ReconciliationStatus.WARN
        else:
            status = ReconciliationStatus.ALERT
        rec = ReconciliationRecord(
            benchmark_id=bench.id,
            metric_code=bench.metric_code,
            metric_value=payload.metric_value,
            bench_value=bench.bench_value,
            diff_pct=diff_pct,
            window=payload.window,
            status=status,
        )
        rec = await self._repo.save_reconciliation(rec)
        if status == ReconciliationStatus.ALERT:
            await self._publisher.publish(
                {
                    "event_type": "reconciliation.alert",
                    "benchmark_id": bench.id,
                    "metric_code": bench.metric_code,
                    "diff_pct": str(diff_pct),
                }
            )
        return ReconciliationRecordResponse.from_model(rec)

    async def list_reconciliations(
        self,
        status: str | None,
        metric_code: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[ReconciliationRecordResponse], int]:
        rows, total = await self._repo.list_reconciliations(status, metric_code, page, page_size)
        return [ReconciliationRecordResponse.from_model(r) for r in rows], total

    async def confirm_reconciliation(
        self, record_id: int, payload: ReconciliationConfirm, user_id: int
    ) -> ReconciliationRecordResponse:
        """Owner 确认差异（reasonable 合理 / caliber_error 口径有误→走变更）。"""
        rec = await self._repo.get_reconciliation(record_id)
        if rec is None:
            raise NotFoundError(f"reconciliation record not found: {record_id}")
        if rec.status == ReconciliationStatus.CONFIRMED:
            raise ValidationError(
                f"record {record_id} already CONFIRMED", error_code="RECON_ALREADY_CONFIRMED"
            )
        rec.status = ReconciliationStatus.CONFIRMED
        rec.owner_note = payload.owner_note
        rec.decision = payload.decision
        rec.confirmed_by = user_id
        rec.checked_at = datetime.utcnow()
        rec = await self._repo.save_reconciliation(rec)
        return ReconciliationRecordResponse.from_model(rec)
