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
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.mysql import Base
from app.models.base import BaseModel


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

    # ---- 审核流字段（对齐指标审核流 TD §13：提交/指派/通过/驳回可追溯）----
    #: 提交评审人 ID（approve/reject 时禁止自审，管理员豁免）
    submitted_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="提交评审人 ID（approve/reject 时禁止自审）"
    )
    #: 审核通过人 ID（REVIEW→PUBLISHED 时写入）
    approver_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="审核通过人 ID"
    )
    #: 评审指派（TD §13）：可指定评审用户或域评审组；未指派由域管理员兜底评审
    reviewer_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="指定评审用户 ID（reviewer_type=user 时生效）"
    )
    reviewer_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="评审指派类型: user(指定用户)/domain(域评审组)"
    )
    reviewer_domain: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="评审团队所在域（reviewer_type=domain 时生效）"
    )
    #: 驳回可追溯（对齐指标 FR-005）：reject 时落库驳回原因/审核人/时间，DRAFT 详情页展示引导修改
    reject_reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="最近一次审核驳回原因（REVIEW→DRAFT 时写入，用于引导修改后重提）",
    )
    reject_reviewer_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="驳回审核人 ID（reject 时写入）"
    )
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="驳回时间（REVIEW→DRAFT 时写入）"
    )
    #: 最近审核时间（approve/reject 时写入，审计可追溯）
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最近审核时间（approve/reject 时写入）"
    )
