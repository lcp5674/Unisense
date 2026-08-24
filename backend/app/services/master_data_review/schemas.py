"""主数据审核共享请求 Schemas（统一「主数据审核」复用模式）。

逻辑度量/维度/术语三类主数据共用的审核请求结构——提交审核（评审指派）、
审核通过（意见）、审核驳回（原因）。各模块 API 直接引用，避免三套重复定义。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ReviewSubmitRequest(BaseModel):
    """提交审核请求（DRAFT → REVIEW，对齐指标审核流 TD §13）。

    评审指派：可指定评审用户（reviewer_type=user + reviewer_id）或域评审组
    （reviewer_type=domain + reviewer_domain，缺省用实体自身域）。
    均不传则未指派——由域管理员兜底评审。
    """

    change_reason: str = Field(
        ..., min_length=4, description="提交审核说明（为什么发布该主数据）"
    )
    reviewer_id: int | None = Field(
        None, description="指定评审用户 ID（reviewer_type=user 时必填）"
    )
    reviewer_type: Literal["user", "domain"] | None = Field(
        None, description="评审指派类型: user(指定用户)/domain(域评审组)"
    )
    reviewer_domain: str | None = Field(
        None,
        max_length=64,
        description="域评审组所在域（reviewer_type=domain 时生效，缺省用实体自身域）",
    )

    @field_validator("reviewer_id", "reviewer_domain", mode="after")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        """空字符串/0 归一为 None，前端未选择时传空串/0 不致校验失败。"""
        if v is None:
            return v
        if isinstance(v, str) and not v.strip():
            return None
        if isinstance(v, int) and v <= 0:
            return None
        return v


class ReviewApproveRequest(BaseModel):
    """审核通过请求（REVIEW → PUBLISHED，对齐指标审核流）。"""

    comment: str | None = Field(None, max_length=500, description="审核意见（可选）")


class ReviewRejectRequest(BaseModel):
    """审核驳回请求（REVIEW → DRAFT，对齐指标审核流 FR-005）。"""

    reason: str = Field(..., min_length=4, description="驳回原因（须明确，通知提交人引导修改）")
