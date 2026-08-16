"""指标模型（语义层核心，状态机）。

对齐 TD §4.1 metric / metric_version 表。
这是整个系统的核心数据模型。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.mysql import Base
from app.models.base import BaseModel


class Metric(Base, BaseModel):
    """指标实体（语义层核心，状态机）。

    对齐 TD §4.1 metric 表，包含完整的治理一等字段结构化字段。

    Attributes:
        metric_code: 指标编码（唯一），格式: 域_业务对象_度量_统计周期。
        name: 指标名称。
        domain: 所属域。
        type: 指标类型（atomic/derived/composite）。
        granularity: 粒度（一行代表什么）。
        unit: 单位。
        currency: 币种（可空）。
        aggregation: 聚合方式。
        time_semantics: 时间语义。
        freshness: 数据新鲜度。
        sla: 产出 SLA 契约。
        dw_layer: 数仓分层。
        metric_tier: 指标分级（T1/T2/T3）。
        serving_mode: 服务模式（批/流/双路）。
        additivity: 可加性。
        non_additive_dimensions: 不可加维度列表（JSON）。
        definition_json: 口径定义（表达式/依赖/来源字段/分区键）。
        version: 当前版本号。
        row_version: 乐观锁行版本。
        term_id: 关联术语 ID（可空）。
        status: 指标状态机。
        owner_id: 主 Owner ID。
        backup_owner_id: 副 Owner ID（可空）。
        approver_id: 审批人 ID（可空）。
        pii_flag: 是否含 PII。
        compliance_reviewed: 是否已合规审核。
        effective_version: 当前生效版本（可空）。
        consumption_guide: 消费指南（JSON，可空）。
        batch_id: 批量注册批次 ID（可空）。
        successor_code: 替代指标码（DEPRECATED 时必填，可空）。
        deprecated_at: 废弃时间（可空）。
        sunset_until: Sunset 截止日期（可空）。
    """

    __tablename__ = "metric"

    # ---- 基本信息字段 ----
    metric_code: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="指标编码（唯一）"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="指标名称")
    domain: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属域")
    type: Mapped[str] = mapped_column(
        Enum("atomic", "derived", "composite", name="metric_type"),
        nullable=False,
        comment="指标类型",
    )

    # ---- 治理一等字段 ----
    granularity: Mapped[str] = mapped_column(String(64), nullable=False, comment="粒度")
    unit: Mapped[str] = mapped_column(String(32), nullable=False, comment="单位")
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="币种")
    # 与字典种子（aggregation 9 值）对齐：补充 MAX/MIN/MEDIAN/PERCENTILE
    aggregation: Mapped[str] = mapped_column(
        Enum(
            "SUM",
            "AVG",
            "COUNT",
            "COUNT_DISTINCT",
            "LAST_VALUE",
            "MAX",
            "MIN",
            "MEDIAN",
            "PERCENTILE",
            name="agg_type",
        ),
        nullable=False,
        comment="聚合方式",
    )
    # 与字典种子（time_semantics 6 值）对齐：补充 MOM/YOY
    time_semantics: Mapped[str] = mapped_column(
        Enum("PERIOD", "YTD", "TTM", "AVG", "MOM", "YOY", name="time_sem"),
        nullable=False,
        comment="时间语义",
    )
    # 与字典种子（freshness 4 值）对齐：补充 T0（实时/流）
    freshness: Mapped[str] = mapped_column(
        Enum("REALTIME", "T0", "T1", "HOURLY", name="freshness_type"),
        nullable=False,
        comment="数据新鲜度",
    )
    sla: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="SLA 契约")
    dw_layer: Mapped[str] = mapped_column(
        Enum("ODS", "DWD", "DWS", "ADS", "DM", name="dw_layer_type"),
        nullable=False,
        comment="数仓分层",
    )
    metric_tier: Mapped[str] = mapped_column(
        Enum("T1", "T2", "T3", name="metric_tier_type"),
        nullable=False,
        default="T3",
        comment="指标分级",
    )
    serving_mode: Mapped[str] = mapped_column(
        Enum("BATCH_ONLY", "REALTIME_ONLY", "BATCH_REALTIME_DUAL", name="serving_mode_type"),
        nullable=False,
        comment="服务模式",
    )
    additivity: Mapped[str] = mapped_column(
        Enum("ADDITIVE", "SEMI_ADDITIVE", "NON_ADDITIVE", name="additivity_type"),
        nullable=False,
        comment="可加性",
    )
    non_additive_dimensions: Mapped[list[Any] | None] = mapped_column(
        JSON, nullable=True, comment="不可加维度列表"
    )

    # ---- 口径与版本 ----
    definition_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="口径定义（表达式/依赖/来源字段/分区键）"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="当前版本号")
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="乐观锁行版本"
    )
    term_id: Mapped[int | None] = mapped_column(
        ForeignKey("term.id", name="fk_metric_term"),
        nullable=True,
        comment="关联术语 ID",
    )

    # ---- 状态机 ----
    status: Mapped[str] = mapped_column(
        Enum(
            "DRAFT",
            "REVIEW",
            "PUBLISHED",
            "EXPERIMENTAL",
            "DEPRECATED",
            "DATA_SOURCE_DROPPED",
            name="metric_status",
        ),
        nullable=False,
        default="DRAFT",
        comment="指标状态",
    )

    # ---- 治理字段 ----
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", name="fk_metric_owner"), nullable=False, comment="主 Owner ID"
    )
    backup_owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", name="fk_metric_backup_owner"),
        nullable=True,
        comment="副 Owner ID",
    )
    approver_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", name="fk_metric_approver"),
        nullable=True,
        comment="审批人 ID",
    )
    submitted_by: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="提交评审人 ID（approve/reject 时禁止自审）",
    )
    # 驳回可追溯（FR-005 闭环）：reject 时落库驳回原因/审核人/时间，
    # DRAFT 详情页展示"上次驳回原因"引导提交人修改后重提（历史原因不丢失）。
    reject_reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="最近一次审核驳回原因（REVIEW→DRAFT 时写入，用于详情页引导修改）",
    )
    reject_reviewer_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="驳回审核人 ID（reject 时写入）",
    )
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="驳回时间（REVIEW→DRAFT 时写入）",
    )
    # 评审指派（TD §13 治理闭环）：提交评审时可指定评审用户或域评审组；
    # approve/reject 仅被指派评审人（或 platform_admin 兜底）可操作。
    reviewer_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="指定评审用户 ID（reviewer_type=user 时生效）",
    )
    reviewer_type: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="评审指派类型: user(指定用户)/domain(域评审组)",
    )
    reviewer_domain: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="评审团队所在域（reviewer_type=domain 时生效）",
    )
    pii_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否含 PII"
    )
    compliance_reviewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="是否已合规审核"
    )
    effective_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="当前生效版本"
    )
    consumption_guide: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="消费指南"
    )
    # 治理补充：指标业务描述（对齐 DBCatalog 表级描述模式 TD §12.1，独立于口径/版本）
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="指标业务描述"
    )
    description_source: Mapped[str | None] = mapped_column(
        Enum("manual", "llm", "schema", name="description_source_enum"),
        nullable=True,
        comment="描述来源（manual/llm/schema）",
    )
    description_updated_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="描述更新人 ID"
    )
    description_updated_at: Mapped[datetime | None] = mapped_column(
        nullable=True, comment="描述更新时间"
    )
    batch_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="批量注册批次 ID"
    )
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("metric_template.id", name="fk_metric_template"),
        nullable=True,
        comment="关联模板 ID（从模板创建时记录）",
    )

    # ---- 废弃与替代 ----
    successor_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="替代指标码（DEPRECATED 时必填）"
    )
    deprecated_at: Mapped[datetime | None] = mapped_column(nullable=True, comment="废弃时间")
    sunset_until: Mapped[date | None] = mapped_column(nullable=True, comment="Sunset 截止日期")

    # ---- 紧急发布 + 灰度 + 冲突预检（对齐 TD §12.3 / FR-022/FR-019/FR-012）----
    emergency_publish: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="紧急发布标记"
    )
    emergency_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="紧急发布原因"
    )
    emergency_reviewed_at: Mapped[datetime | None] = mapped_column(
        nullable=True, comment="紧急发布补审时间"
    )
    gray_tenant_ids: Mapped[list[Any] | None] = mapped_column(
        JSON, nullable=True, comment="灰度白名单租户 ID 列表"
    )
    pending_conflict: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="冲突预检标记"
    )
    pending_conflict_detail: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="冲突详情"
    )
    # 仲裁裁决标记（TD §12.4）：canonical（胜方）/ coexist（保留差异共存）。
    # 详情页据此展示「权威口径」/「已裁定共存」；落败方另走废弃/作废。
    arbitration_mark: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="仲裁裁决标记"
    )

    # ---- 关系 ----
    versions: Mapped[list[MetricVersion]] = relationship(
        "MetricVersion", back_populates="metric", lazy="selectin"
    )

    __table_args__ = (
        Index("idx_metric_status", "status"),
        Index("idx_metric_domain", "domain"),
        Index("idx_metric_tier", "metric_tier"),
        Index("idx_metric_batch", "batch_id"),
    )


# MetricVersion 已拆至 app.models.metric_version，此处保留 re-export 兼容
from app.models.metric_version import MetricVersion  # noqa: E402
