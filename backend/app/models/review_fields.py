"""主数据审核字段共享 Mixin（统一「主数据审核」复用模式）。

背景：逻辑度量/维度/术语三类主数据此前 DRAFT → PUBLISHED 直接发布、无审核，
形成"基础层比上层更松"的治理倒挂（基础定义静默传播到下游指标）。三类主数据
统一对齐指标审核流（TD §13）——DRAFT → REVIEW → PUBLISHED → DEPRECATED。

本 Mixin 集中 9 个审核字段（提交/指派/通过/驳回可追溯），供三个模型复用，
避免三套重复列定义。配套服务层共享实现见
``app.services.master_data_review.service.MasterDataReviewMixin``。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column


class ReviewFieldsMixin:
    """审核字段 Mixin：挂载到任意主数据模型即获得完整审核留痕列。

    状态机本身仍由各模型自己的 status 枚举承载（须含 REVIEW），
    本 Mixin 仅提供审核留痕字段，保证三模型字段名一致、服务层可统一读写。
    """

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
