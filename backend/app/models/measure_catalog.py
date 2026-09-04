"""逻辑度量目录领域模型（OneData 原子层，TD §4.2 metric 基础信息）。

对齐 `docs/指标设计以及界限说明.md` §2.1 原子指标三层配置：
- 基础信息（9 项）：英文名称/数据域/中文名称/度量格式/度量单位/小数位数/源头系统/同义词/指标描述
- 原子指标 = 逻辑度量（本实体）+ 基础统计粒度（日）变体标签，**不绑定物理表/粒度**
- 粒度/周期属挂载实体（metric_mount）与派生层（原子 + 业务限定 + 时间周期），
  不进原子定义（界限文档 §2.3 第 3 条 / DEV_GUIDE §7a 共识）

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
from app.models.review_fields import ReviewFieldsMixin


class MeasureFormat(enum.StrEnum):
    """度量格式（PRD FR-02-08）：决定默认单位与小数位。"""

    AMOUNT = "AMOUNT"  # 金额（默认单位 元、小数 2 位）
    RATIO = "RATIO"  # 比率（默认单位 小数、小数 4 位）
    NUMERIC = "NUMERIC"  # 数值（自定义单位、小数按需）


class MeasureCategory(enum.StrEnum):
    """度量分类：按业务视角组织度量目录，跨域通用（医疗/电商等均可挂）。"""

    FLOW = "FLOW"  # 流量类（人次/单量：门诊人次、订单量）
    FEE = "FEE"  # 费用类（金额：门诊费用、GMV）
    DRUG = "DRUG"  # 药品类（处方、药品用量/费用）
    MEDICAL_INSURANCE = "MEDICAL_INSURANCE"  # 医保类（结算金额、报销比例）
    EFFICIENCY = "EFFICIENCY"  # 效率类（次均/人效、单价）
    QUALITY = "QUALITY"  # 质量类（率/占比、质控指标）
    OTHER = "OTHER"  # 其他/未分类


class MeasureStatus(enum.StrEnum):
    """逻辑度量状态机（对齐指标审核流：DRAFT → REVIEW → PUBLISHED → DEPRECATED）。

    度量是原子指标的权威继承源（单位/格式/小数位/口径直接传播到下游指标），
    故发布须先提交审核（DRAFT → REVIEW），审核通过才 PUBLISHED（对齐指标治理闭环）。
    """

    DRAFT = "DRAFT"
    REVIEW = "REVIEW"  # 待审核（已提交审核，审核通过才发布）
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"


class MeasureCatalog(Base, BaseModel, ReviewFieldsMixin):
    __tablename__ = "measure_catalog"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # T5（审查修复）：乐观锁行版本——并发编辑 last-write-wins 会使
    # 破坏性字段判定（格式/单位/小数位联动）失真，对齐 metric/dimension 标准。
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="乐观锁行版本"
    )
    measure_code: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="逻辑度量编码（英文，如 pay_amt）"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="度量中文名（支付金额）")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="度量描述")
    # 字典驱动治理改造（0143）：measure_format 列放开为 VARCHAR(32)，值域以 system_dict
    # measure_format 为单一事实源（extra 带默认单位/小数位联动，保存侧 validate_dict_value
    # 校验）——DB 建表即 varchar，仅 model 残留 Enum 声明，此处对齐真实列类型。
    # MeasureFormat python 枚举仍保留：default 常量引用 + 格式→默认单位/小数位联动回退。
    measure_format: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MeasureFormat.AMOUNT.value,
        comment="度量格式（AMOUNT/RATIO/NUMERIC）",
    )
    default_unit: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="元",
        comment="默认单位（金额:元/比率:小数/数值:自定义）",
    )
    default_decimal_places: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="默认小数位数（金额2/比率4/数值按需，NULL=未定）"
    )
    #: 源头系统（PRD FR-04-03：业务系统术语多值，存术语名称列表）
    source_system: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True, comment="源头系统")
    #: 同义词（统一查询/查重匹配，指标基础信息第 8 项）
    synonyms: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True, comment="同义词")
    #: 度量分类（FLOW/FEE/DRUG/MEDICAL_INSURANCE/EFFICIENCY/QUALITY/OTHER）
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default=MeasureCategory.OTHER.value, comment="度量分类"
    )
    #: 统计口径（业务侧如何计算该度量，如"收费明细按结算日期去重后求和"）
    stat_caliber: Mapped[str | None] = mapped_column(Text, nullable=True, comment="统计口径")
    domain: Mapped[str] = mapped_column(String(64), nullable=False, comment="业务域")
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="负责人 ID")
    status: Mapped[str] = mapped_column(
        Enum(MeasureStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=MeasureStatus.DRAFT.value,
        comment="状态（DRAFT/REVIEW/PUBLISHED/DEPRECATED）",
    )
