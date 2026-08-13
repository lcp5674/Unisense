"""数据质量领域模型（TD §12.8 / FR-10）。

质量规则配置（随指标 PUBLISHED 注册，按 tier/dw_layer 差异化）+ 质量异常事件
（分级 P0/P1/P2，状态机 OPEN→ACK→RESOLVED→CLOSED，告警经 notify best-effort 降级）。

对齐 TD §4.1 quality_rule / quality_event 表。
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, Enum, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class QualityRuleType(enum.StrEnum):
    """质量规则类型（对齐 TD §4.1 quality_rule.rule_type）。"""

    COMPLETENESS = "COMPLETENESS"
    ACCURACY = "ACCURACY"
    TIMELINESS = "TIMELINESS"
    CONSISTENCY = "CONSISTENCY"
    UNIQUENESS = "UNIQUENESS"
    VALIDITY = "VALIDITY"
    WAVE_DIFF = "WAVE_DIFF"
    CROSS_SOURCE = "CROSS_SOURCE"


class QualityRuleMode(enum.StrEnum):
    """规则求值模式（对齐 TD §4.1 quality_rule.rule_mode）。"""

    STATIC = "static"
    DYNAMIC_BASELINE = "dynamic_baseline"
    YOY_WOY = "yoy_woy"
    CROSS_SOURCE = "cross_source"


class QualitySeverity(enum.StrEnum):
    """异常严重级（对齐 TD §4.1 level/severity）。"""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class QualityEventStatus(enum.StrEnum):
    """质量异常事件状态机（对齐 TD §4.1 quality_event.status）。"""

    OPEN = "OPEN"
    ACK = "ACK"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class QualityRule(Base, BaseModel):
    """质量规则配置（TD §4.1 quality_rule）。

    随指标 PUBLISHED 注册，按 tier/dw_layer 差异化；可针对指标/表/字段级。
    """

    __tablename__ = "quality_rule"

    metric_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="关联指标 ID"
    )
    rule_type: Mapped[QualityRuleType] = mapped_column(
        Enum(QualityRuleType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
        comment="规则类型",
    )
    threshold: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="阈值参数（静态阈值/dynamic baseline σ/同环比 等）"
    )
    rule_mode: Mapped[QualityRuleMode] = mapped_column(
        Enum(QualityRuleMode, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=QualityRuleMode.STATIC,
        comment="求值模式",
    )
    severity: Mapped[QualitySeverity] = mapped_column(
        Enum(QualitySeverity, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=QualitySeverity.P2,
        comment="严重级",
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    notify_targets: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="告警通知目标（Owner/关注者/domain_admin）"
    )
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="创建人 ID")


class QualityEvent(Base, BaseModel):
    """质量异常事件（TD §4.1 quality_event）。

    质量引擎检测到越界/波动异常后落库，分级并触发告警；支持 ack/resolve/close 闭环。
    """

    __tablename__ = "quality_event"

    metric_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="关联指标 ID"
    )
    level: Mapped[QualitySeverity] = mapped_column(
        Enum(QualitySeverity, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        comment="异常分级",
    )
    rule_type: Mapped[QualityRuleType] = mapped_column(
        Enum(QualityRuleType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
        comment="触发规则类型",
    )
    obs_value: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True, comment="观测值"
    )
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True, comment="阈值")
    status: Mapped[QualityEventStatus] = mapped_column(
        Enum(QualityEventStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=QualityEventStatus.OPEN,
        index=True,
        comment="事件状态",
    )
    # 操作人留痕：状态转移的责任人与时间，用于治理闭环审计回溯
    ack_note: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="ACK 备注（运营处理说明）"
    )
    ack_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="ACK 操作人 ID")
    ack_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="ACK 时间")
    resolved_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="RESOLVE 操作人 ID"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="RESOLVE 时间"
    )
    closed_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="CLOSE 操作人 ID"
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="CLOSE 时间"
    )
    # 修复建议（TD §4.8.5 / PRD 4.8.5）：异常触发时生成，Owner 线下修复闭环用
    repair_suggestion: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="修复建议（责任方/上游任务/建议SQL/确认留痕）"
    )


class ReconciliationStatus(enum.StrEnum):
    """外部基准对账记录状态（TD §4.15.7 reconciliation_record.status）。

    OK/WARN/ALERT 由差异率自动判定；CONFIRMED 由指标 Owner 确认闭环。
    """

    OK = "OK"
    WARN = "WARN"
    ALERT = "ALERT"
    CONFIRMED = "CONFIRMED"


class ExternalBenchmark(Base, BaseModel):
    """外部基准值（TD §4.15.7 external_benchmark）。

    导入权威值（如银行对账单 / 审计数），用于与平台指标值自动比对。
    幂等键为 (source_id, metric_code, bench_date, dims)：同一来源同一指标同一日
    期的基准重复导入视为更新（覆写 bench_value / provider / 口径），杜绝重复堆积。
    """

    __tablename__ = "external_benchmark"

    source_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="外部基准来源批次 ID"
    )
    metric_code: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="目标指标编码"
    )
    bench_date: Mapped[date] = mapped_column(Date, nullable=False, comment="基准归属日期")
    dims: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="维度上下文（比对口径/维度/币种声明）"
    )
    bench_value: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, comment="权威基准值"
    )
    provider: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="基准提供方（如 审计机构/银行）"
    )
    tolerance_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4), nullable=True, comment="可接受差异率(%)，为空时默认 1.00"
    )
    imported_by: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="导入人 ID")


class QualityObservation(Base, BaseModel):
    """质量观测时序样本（TD §4.8.3 / Epic 6：动态基线 / 同环比 / 跨源检测的数据底座）。

    采集 / 产出分区就绪时写入一次观测值，供动态基线（历史窗口中位数 + σ）、
    同环比（对照期观测）、跨源检测（同指标多 source 最新值）复用。仅落地聚合后的
    观测值，不持有源数据明细。
    """

    __tablename__ = "quality_observation"

    metric_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="关联指标 ID"
    )
    metric_code: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="指标编码（跨源分组键）"
    )
    source_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="观测来源（跨源分组；空表示平台聚合值）",
    )
    obs_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True, comment="观测时间（分区就绪时刻）"
    )
    value: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, comment="观测聚合值")
    dims: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="维度上下文（如 region/channel）"
    )


class ReconciliationRecord(Base, BaseModel):
    """外部基准对账记录（TD §4.15.7 reconciliation_record）。

    一次对账 = 基准值 vs 平台观测值，记录差异率与状态；Owner 确认后闭环。
    """

    __tablename__ = "reconciliation_record"

    benchmark_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="关联 external_benchmark.id"
    )
    metric_code: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="指标编码"
    )
    metric_value: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, comment="平台观测值"
    )
    bench_value: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, comment="基准值")
    diff_pct: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, comment="差异率(%) = (观测-基准)/基准*100"
    )
    window: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="比对窗口（如 2024-01 / D-1）"
    )
    status: Mapped[ReconciliationStatus] = mapped_column(
        Enum(ReconciliationStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ReconciliationStatus.OK,
        index=True,
        comment="对账状态",
    )
    owner_note: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="Owner 确认备注"
    )
    decision: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="CONFIRMED 时决策：reasonable(差异合理)/caliber_error(口径有误→走变更)",
    )
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="确认人 ID")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="确认时间")
