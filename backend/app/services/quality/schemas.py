"""数据质量服务 Schemas（TD §12.8 / FR-10）。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.models.quality import (
    ExternalBenchmark,
    QualityEventStatus,
    QualityObservation,
    QualityRuleMode,
    QualityRuleType,
    QualitySeverity,
    ReconciliationRecord,
    ReconciliationStatus,
)


class QualityRuleCreate(BaseModel):
    metric_id: int
    rule_type: QualityRuleType
    threshold: dict[str, Any]
    rule_mode: QualityRuleMode = QualityRuleMode.STATIC
    severity: QualitySeverity = QualitySeverity.P2
    enabled: bool = True
    notify_targets: dict[str, Any] | None = None


class QualityRuleUpdate(BaseModel):
    threshold: dict[str, Any] | None = None
    rule_mode: QualityRuleMode | None = None
    severity: QualitySeverity | None = None
    enabled: bool | None = None
    notify_targets: dict[str, Any] | None = None


class QualityRuleResponse(BaseModel):
    id: int
    metric_id: int
    rule_type: QualityRuleType
    threshold: dict[str, Any]
    rule_mode: QualityRuleMode
    severity: QualitySeverity
    enabled: bool
    notify_targets: dict[str, Any] | None = None
    created_by: int
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> QualityRuleResponse:
        return cls(
            id=m.id,
            metric_id=m.metric_id,
            rule_type=m.rule_type,
            threshold=m.threshold,
            rule_mode=m.rule_mode,
            severity=m.severity,
            enabled=m.enabled,
            notify_targets=m.notify_targets,
            created_by=m.created_by,
            created_at=m.created_at,
        )


class QualityEventResponse(BaseModel):
    id: int
    metric_id: int
    level: QualitySeverity
    rule_type: QualityRuleType
    obs_value: Decimal | None = None
    threshold: Decimal | None = None
    status: QualityEventStatus
    created_at: datetime | None = None
    ack_note: str | None = None
    ack_by: int | None = None
    ack_at: datetime | None = None
    resolved_by: int | None = None
    resolved_at: datetime | None = None
    closed_by: int | None = None
    closed_at: datetime | None = None
    repair_suggestion: dict[str, Any] | None = None

    @classmethod
    def from_model(cls, m: Any) -> QualityEventResponse:
        return cls(
            id=m.id,
            metric_id=m.metric_id,
            level=m.level,
            rule_type=m.rule_type,
            obs_value=m.obs_value,
            threshold=m.threshold,
            status=m.status,
            created_at=m.created_at,
            ack_note=getattr(m, "ack_note", None),
            ack_by=getattr(m, "ack_by", None),
            ack_at=getattr(m, "ack_at", None),
            resolved_by=getattr(m, "resolved_by", None),
            resolved_at=getattr(m, "resolved_at", None),
            closed_by=getattr(m, "closed_by", None),
            closed_at=getattr(m, "closed_at", None),
            repair_suggestion=getattr(m, "repair_suggestion", None),
        )


class QualityEventAck(BaseModel):
    note: str = ""


class QualityDetectRequest(BaseModel):
    metric_id: int
    rule_type: QualityRuleType
    obs_value: Decimal
    rule_mode: QualityRuleMode | None = None


# --------------------------------------------------- 外部基准对账（TD §4.15.7）

class BenchmarkImport(BaseModel):
    """外部权威基准值导入（幂等键：source_id + metric_code + bench_date + dims）。"""

    source_id: str = Field(min_length=1, max_length=64)
    metric_code: str = Field(min_length=1, max_length=64)
    bench_date: date
    dims: dict[str, Any] | None = None
    bench_value: Decimal = Field(gt=0)  # 基准值须为正，0 无意义
    provider: str = Field(min_length=1, max_length=128)
    tolerance_pct: Decimal | None = Field(default=None, ge=0, le=100)


class BenchmarkBind(BaseModel):
    """绑定基准到目标指标，声明比对口径 / 容忍率。"""

    metric_code: str | None = Field(default=None, min_length=1, max_length=64)
    tolerance_pct: Decimal | None = Field(default=None, ge=0, le=100)
    dims: dict[str, Any] | None = None


class BenchmarkResponse(BaseModel):
    id: int
    source_id: str
    metric_code: str
    bench_date: date
    dims: dict[str, Any] | None = None
    bench_value: Decimal
    provider: str
    tolerance_pct: Decimal | None = None
    imported_by: int
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: ExternalBenchmark) -> BenchmarkResponse:
        return cls(
            id=m.id,
            source_id=m.source_id,
            metric_code=m.metric_code,
            bench_date=m.bench_date,
            dims=m.dims,
            bench_value=m.bench_value,
            provider=m.provider,
            tolerance_pct=m.tolerance_pct,
            imported_by=m.imported_by,
            created_at=m.created_at,
        )


class ReconciliationRun(BaseModel):
    """执行一次对账：提供平台观测值进行比对。"""

    benchmark_id: int = Field(gt=0)
    metric_value: Decimal
    window: str | None = Field(default=None, max_length=64)


class ReconciliationRecordResponse(BaseModel):
    id: int
    benchmark_id: int
    metric_code: str
    metric_value: Decimal
    bench_value: Decimal
    diff_pct: Decimal
    window: str | None = None
    status: ReconciliationStatus
    owner_note: str | None = None
    decision: str | None = None
    confirmed_by: int | None = None
    checked_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: ReconciliationRecord) -> ReconciliationRecordResponse:
        return cls(
            id=m.id,
            benchmark_id=m.benchmark_id,
            metric_code=m.metric_code,
            metric_value=m.metric_value,
            bench_value=m.bench_value,
            diff_pct=m.diff_pct,
            window=m.window,
            status=m.status,
            owner_note=m.owner_note,
            decision=m.decision,
            confirmed_by=m.confirmed_by,
            checked_at=m.checked_at,
            created_at=m.created_at,
        )


class ReconciliationConfirm(BaseModel):
    """Owner 确认差异结论。"""

    decision: str = Field(pattern="^(reasonable|caliber_error)$")
    owner_note: str | None = Field(default=None, max_length=512)


# --------------------------------------------------- 质量观测样本（Epic 6）

class QualityObservationRequest(BaseModel):
    """写入一次质量观测样本（采集 / 产出分区就绪时调用）。

    供动态基线（历史窗口中位数 + σ）、同环比（对照期）、跨源检测（多 source 最新值）复用。
    """

    metric_id: int = Field(gt=0)
    metric_code: str = Field(min_length=1, max_length=64)
    value: Decimal
    obs_time: datetime
    source_id: str | None = Field(default=None, max_length=64)
    dims: dict[str, Any] | None = None


class QualityObservationResponse(BaseModel):
    id: int
    metric_id: int
    metric_code: str
    source_id: str | None = None
    obs_time: datetime
    value: Decimal
    dims: dict[str, Any] | None = None

    @classmethod
    def from_model(cls, m: QualityObservation) -> QualityObservationResponse:
        return cls(
            id=m.id,
            metric_id=m.metric_id,
            metric_code=m.metric_code,
            source_id=m.source_id,
            obs_time=m.obs_time,
            value=m.value,
            dims=m.dims,
        )
