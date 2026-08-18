"""逻辑度量目录领域模型（OneData 原子层，TD §4.2 metric 基础信息）。

对齐 `docs/指标设计以及界限说明.md` §2.1 原子指标三层配置：
- 基础信息（9 项）：英文名称/数据域/中文名称/度量格式/度量单位/小数位数/源头系统/同义词/指标描述
- 原子指标 = 逻辑度量（本实体）+ 聚合方式（metric 侧），**不绑定物理表/粒度**
- 粒度/周期属挂载实体（metric_mount），不进指标定义（界限文档 §2.3 第 3 条）

本实体承载"度量格式/默认单位/默认小数位/源头系统/同义词"，供原子指标继承（One Metric
一处定义多处复用）。状态机 DRAFT → PUBLISHED → DEPRECATED（照 dimension 发布式主数据）。
"""

from __future__ import annotations

import enum
from typing import Any

from sqlalchemy import BigInteger, Enum, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


class MeasureFormat(enum.StrEnum):
    """度量格式（PRD FR-02-08）：决定默认单位与小数位。"""

    AMOUNT = "AMOUNT"  # 金额（默认单位 元、小数 2 位）
    RATIO = "RATIO"  # 比率（默认单位 小数、小数 4 位）
    NUMERIC = "NUMERIC"  # 数值（自定义单位、小数按需）


class MeasureStatus(enum.StrEnum):
    """逻辑度量状态机（照 dimension 发布式主数据）。"""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"


class MeasureCatalog(Base, BaseModel):
    __tablename__ = "measure_catalog"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    measure_code: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="逻辑度量编码（英文，如 pay_amt）"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="度量中文名（支付金额）")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="度量描述")
    measure_format: Mapped[str] = mapped_column(
        Enum(MeasureFormat, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=MeasureFormat.AMOUNT.value,
        comment="度量格式（AMOUNT/RATIO/NUMERIC）",
    )
    default_unit: Mapped[str] = mapped_column(
        String(32), nullable=False, default="元", comment="默认单位（金额:元/比率:小数/数值:自定义）"
    )
    default_decimal_places: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="默认小数位数（金额2/比率4/数值按需，NULL=未定）"
    )
    #: 源头系统（PRD FR-04-03：业务系统术语多值，存术语名称列表）
    source_system: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True, comment="源头系统")
    #: 同义词（统一查询/查重匹配，指标基础信息第 8 项）
    synonyms: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True, comment="同义词")
    domain: Mapped[str] = mapped_column(String(64), nullable=False, comment="业务域")
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="负责人 ID")
    status: Mapped[str] = mapped_column(
        Enum(MeasureStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=MeasureStatus.DRAFT.value,
        comment="状态（DRAFT/PUBLISHED/DEPRECATED）",
    )
