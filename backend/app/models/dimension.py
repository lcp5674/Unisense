"""维度管理领域模型（TD §12.15 / FR-05 / FR-09）。

包含维度主表、维度成员（维值/层级）、维度映射、指标-维度关联、口径对账记录。
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel
from app.models.review_fields import ReviewFieldsMixin


class DimensionType(enum.StrEnum):
    """缓慢变化维类型全集（生产标准）。

    - SCD0: 原样保留（不跟踪变化）
    - SCD1: 覆盖旧值（仅保留当前值）
    - SCD2: 保留历史（按时间切片新增记录）
    - SCD3: 有限历史（同时保留当前值 + 原值列）
    - SCD4: 历史表（当前值与全量历史分离存放）
    - SCD6: 混合（SCD1 + SCD2 组合，历史 + 当前均可查）
    """

    SCD0 = "SCD0"
    SCD1 = "SCD1"
    SCD2 = "SCD2"
    SCD3 = "SCD3"
    SCD4 = "SCD4"
    SCD6 = "SCD6"


class SyncMode(enum.StrEnum):
    """维度值来源模式。

    - none: 纯枚举型（dimension_member 逐值维护，默认）
    - snapshot: 引用型（值集合来自维度表列的 SNAPSHOT 快照，独立于 member 表）
    """

    NONE = "none"
    SNAPSHOT = "snapshot"


class SnapshotStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


class SnapshotRunStatus(enum.StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class DimensionStatus(enum.StrEnum):
    """维度状态机（对齐指标审核流：DRAFT → REVIEW → PUBLISHED → DEPRECATED）。

    维度是下游指标绑定/消费校验的权威来源，发布须先提交审核（审核流复用
    ``app.services.master_data_review``，与逻辑度量/术语统一）。
    """

    DRAFT = "DRAFT"
    REVIEW = "REVIEW"  # 待审核（已提交审核，审核通过才发布）
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"


class MappingType(enum.StrEnum):
    EQUIVALENT = "EQUIVALENT"
    PARTIAL = "PARTIAL"


class MetricDimensionRole(enum.StrEnum):
    PARTITION = "PARTITION"
    SPLICE = "SPLICE"
    FILTER = "FILTER"


class ReconciliationStatus(enum.StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Dimension(Base, BaseModel, ReviewFieldsMixin):
    __tablename__ = "dimension"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dim_code: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="维度编码"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="维度名称")
    domain: Mapped[str] = mapped_column(String(64), nullable=False, comment="业务域")
    type: Mapped[str] = mapped_column(
        Enum(DimensionType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=DimensionType.SCD1.value,
        comment="缓慢变化维类型",
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="描述")
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="责任人 ID")
    status: Mapped[str] = mapped_column(
        Enum(DimensionStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=DimensionStatus.DRAFT.value,
        comment="状态",
    )
    # P11 C-2：乐观锁版本（编辑回传当前 row_version，不一致即 409 防并发静默覆盖）
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1", comment="乐观锁版本"
    )
    # 引用型（sync_mode=snapshot）：值集合来自维度表列快照，不写 member 表
    source_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="数据源 ID（引用型值来源）"
    )
    source_table: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="维度值来源表"
    )
    source_column: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="维度值来源列"
    )
    sync_mode: Mapped[str] = mapped_column(
        Enum(SyncMode, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SyncMode.NONE.value,
        comment="值来源模式（none 枚举型 / snapshot 引用型）",
    )
    refresh_interval_hours: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="快照刷新间隔（小时，默认 24）"
    )
    last_snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最近一次快照时间"
    )


class DimensionMember(Base, BaseModel):
    __tablename__ = "dimension_member"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dim_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="维度编码")
    member_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="成员编码")
    member_name: Mapped[str] = mapped_column(String(128), nullable=False, comment="成员名称")
    parent_code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="父成员编码")
    path: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="层级路径")
    attributes: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="扩展属性"
    )
    status: Mapped[str] = mapped_column(
        Enum(DimensionStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=DimensionStatus.PUBLISHED.value,
        comment="状态",
    )

    __table_args__ = (UniqueConstraint("dim_code", "member_code", name="uk_dim_member"),)


class DimensionMapping(Base, BaseModel):
    __tablename__ = "dimension_mapping"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_dim_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="源维度")
    target_dim_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="目标维度")
    mapping_type: Mapped[str] = mapped_column(
        Enum(MappingType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        comment="映射类型",
    )
    expression: Mapped[str | None] = mapped_column(Text, nullable=True, comment="映射表达式")
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="创建人 ID")

    __table_args__ = (
        UniqueConstraint("source_dim_code", "target_dim_code", "mapping_type", name="uk_dim_map"),
    )


class MetricDimension(Base, BaseModel):
    __tablename__ = "metric_dimension"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    metric_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="指标 ID", index=True
    )
    dim_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="维度编码")
    role: Mapped[str] = mapped_column(
        Enum(MetricDimensionRole, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        comment="关联角色",
    )
    default_member: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="默认成员"
    )

    __table_args__ = (UniqueConstraint("metric_id", "dim_code", name="uk_metric_dim"),)


class Reconciliation(Base, BaseModel):
    __tablename__ = "reconciliation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    metric_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="指标 ID", index=True
    )
    dim_code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="维度编码")
    expected_expr: Mapped[str] = mapped_column(Text, nullable=False, comment="期望口径")
    actual_expr: Mapped[str] = mapped_column(Text, nullable=False, comment="实际口径")
    status: Mapped[str] = mapped_column(
        Enum(ReconciliationStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ReconciliationStatus.PENDING.value,
        comment="对账状态",
        index=True,
    )
    diff_summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="差异摘要")
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="复核人 ID")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="复核时间"
    )


class DimensionValueSnapshot(Base, BaseModel):
    """引用型维度值快照（版本化：保留最近 2 批，diff 用两批集合差）。

    value 集合来自维度表列（SELECT DISTINCT col FROM tbl），snapshot_at 区分批次；
    REMOVED 标记上一批存在、本批消失的值（用于「消失值检测」）。
    """

    __tablename__ = "dimension_value_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dim_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="维度编码")
    source_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="数据源 ID")
    source_table: Mapped[str] = mapped_column(String(256), nullable=False, comment="来源表")
    source_column: Mapped[str] = mapped_column(String(256), nullable=False, comment="来源列")
    value: Mapped[str] = mapped_column(String(512), nullable=False, comment="维度值")
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="快照批次时间"
    )
    status: Mapped[str] = mapped_column(
        Enum(SnapshotStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SnapshotStatus.ACTIVE.value,
        comment="ACTIVE 当前批 / REMOVED 上批有本批消失",
    )

    __table_args__ = (
        UniqueConstraint("dim_code", "snapshot_at", "value", name="uk_dim_snapshot_value"),
    )


class DimensionSnapshotRun(Base, BaseModel):
    """引用型维度快照刷新记录（每次刷新的统计与差异样本）。"""

    __tablename__ = "dimension_snapshot_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dim_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="维度编码")
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="快照批次时间"
    )
    status: Mapped[str] = mapped_column(
        Enum(SnapshotRunStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SnapshotRunStatus.RUNNING.value,
        comment="运行状态",
    )
    total_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="值总数"
    )
    added_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="新增值数"
    )
    removed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="消失值数"
    )
    null_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="空值数"
    )
    null_rate: Mapped[float | None] = mapped_column(
        Numeric(5, 4), nullable=True, comment="空值率"
    )
    added_sample: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="新增值样本"
    )
    removed_sample: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="消失值样本"
    )
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="耗时（毫秒）")


class DimensionMappingValue(Base, BaseModel):
    """值级维度映射（source_value → target_value 逐值对应，供 translate_value 消费）。

    expression 是自由文本仅供人工参考；机器可消费的逐值对应由本表承载。
    """

    __tablename__ = "dimension_mapping_value"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mapping_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="映射 ID")
    source_value: Mapped[str] = mapped_column(String(512), nullable=False, comment="源值")
    target_value: Mapped[str] = mapped_column(String(512), nullable=False, comment="目标值")
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="创建人 ID")

    __table_args__ = (
        UniqueConstraint("mapping_id", "source_value", name="uk_dim_mapping_value"),
    )
