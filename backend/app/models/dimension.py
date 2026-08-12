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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class DimensionType(enum.StrEnum):
    SCD1 = "SCD1"
    SCD2 = "SCD2"


class DimensionStatus(enum.StrEnum):
    DRAFT = "DRAFT"
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


class Dimension(Base, BaseModel):
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
