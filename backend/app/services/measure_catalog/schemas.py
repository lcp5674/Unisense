"""逻辑度量目录 Schemas（OneData 原子层，TD §4.2 / FR-02-08）。

字段长度对齐模型列（measure_code=64 / name=128 / domain=64 / default_unit=32），
超长提交 422 而非 MySQL 500。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.measure_catalog import MeasureCategory, MeasureFormat

_VALID_FORMATS = {e.value for e in MeasureFormat}

#: 度量格式 → 默认单位/默认小数位（PRD FR-02-08 单位与默认值联动）
_FORMAT_DEFAULTS: dict[str, tuple[str, int | None]] = {
    "AMOUNT": ("元", 2),
    "RATIO": ("小数", 4),
    "NUMERIC": ("", None),
}


class MeasureCreate(BaseModel):
    measure_code: str | None = Field(
        default=None,
        max_length=64,
        description="逻辑度量编码（可选，缺省自动生成，格式小写字母开头）",
    )
    name: str = Field(..., max_length=128, description="度量中文名（如 支付金额）")
    description: str | None = None
    measure_format: str = Field(
        default=MeasureFormat.AMOUNT.value, description="度量格式（AMOUNT/RATIO/NUMERIC）"
    )
    # 缺省时由服务端按度量格式联动默认（金额:元/2 位，比率:小数/4 位，数值:自定义）
    default_unit: str | None = Field(default=None, max_length=32, description="默认单位")
    default_decimal_places: int | None = Field(
        default=None, ge=0, le=10, description="默认小数位数"
    )
    source_system: list[str] | None = None
    synonyms: list[str] | None = None
    category: str = Field(
        default=MeasureCategory.OTHER.value, description="度量分类（FLOW/FEE/DRUG/...）"
    )
    stat_caliber: str | None = Field(default=None, max_length=1000, description="统计口径")
    domain: str = Field(..., max_length=64)
    # PLAT-2: owner_id 允许客户端省略，服务端以认证身份覆盖（防越权指定责任人）。
    owner_id: int | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("度量名称不能为空")
        return v

    @field_validator("category")
    @classmethod
    def _category_valid(cls, v: str) -> str:
        # 字典化后分类值由 service 层校验（dict_type=measure_category），
        # 此处仅保证非空 + 长度（对齐模型列 String(32)），不再硬编码枚举集合。
        if not v.strip():
            raise ValueError("度量分类不能为空")
        if len(v) > 32:
            raise ValueError("度量分类长度不能超过 32")
        return v

    @field_validator("measure_format")
    @classmethod
    def _format_valid(cls, v: str) -> str:
        if v not in _VALID_FORMATS:
            raise ValueError(f"未知度量格式: {v}（须为 {sorted(_VALID_FORMATS)}）")
        return v

    @model_validator(mode="after")
    def _fill_format_defaults(self) -> MeasureCreate:
        """单位/小数位缺省时按度量格式联动默认（PRD FR-02-08）。"""
        fmt = self.measure_format
        if fmt not in _VALID_FORMATS:
            return self
        default_unit, default_decimal = _FORMAT_DEFAULTS[fmt]
        if self.default_unit is None:
            self.default_unit = default_unit
        if self.default_decimal_places is None:
            self.default_decimal_places = default_decimal
        return self


class MeasureUpdate(BaseModel):
    # 编辑可改编码（仅 DRAFT 状态允许，PUBLISHED/DEPRECATED 由 service 层拦截）
    measure_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=64,
        description="逻辑度量编码（可选，DRAFT 可改，已发布/已废弃禁止）",
    )
    name: str | None = Field(None, max_length=128)
    description: str | None = None
    measure_format: str | None = None
    default_unit: str | None = Field(None, max_length=32)
    default_decimal_places: int | None = Field(None, ge=0, le=10)
    source_system: list[str] | None = None
    synonyms: list[str] | None = None
    category: str | None = None
    stat_caliber: str | None = Field(None, max_length=1000)
    domain: str | None = Field(None, max_length=64)

    @field_validator("measure_format")
    @classmethod
    def _format_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_FORMATS:
            raise ValueError(f"未知度量格式: {v}（须为 {sorted(_VALID_FORMATS)}）")
        return v

    @field_validator("category")
    @classmethod
    def _category_valid(cls, v: str | None) -> str | None:
        # 字典化后分类值由 service 层校验（dict_type=measure_category），此处仅限长。
        if v is not None and len(v) > 32:
            raise ValueError("度量分类长度不能超过 32")
        return v


# ---- 审核请求：复用「主数据审核」共享 Schemas（统一模式，避免三套重复定义）----
from app.services.master_data_review.schemas import (  # noqa: E402
    ReviewApproveRequest,
    ReviewRejectRequest,
    ReviewSubmitRequest,
)

#: 度量审核请求 = 主数据审核共享请求（对齐指标审核流 TD §13 / FR-005）
MeasureSubmitRequest = ReviewSubmitRequest
MeasureApproveRequest = ReviewApproveRequest
MeasureRejectRequest = ReviewRejectRequest


class MeasureResponse(BaseModel):
    id: int
    measure_code: str
    name: str
    description: str | None = None
    measure_format: str
    default_unit: str
    default_decimal_places: int | None = None
    source_system: list[str] | None = None
    synonyms: list[str] | None = None
    category: str
    stat_caliber: str | None = None
    domain: str
    owner_id: int
    status: str
    # ---- 审核流字段（对齐指标审核流 TD §13）----
    submitted_by: int | None = None
    approver_id: int | None = None
    reviewer_id: int | None = None
    reviewer_type: str | None = None
    reviewer_domain: str | None = None
    reject_reason: str | None = None
    reject_reviewer_id: int | None = None
    rejected_at: datetime | None = None
    reviewed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> MeasureResponse:
        return cls(
            id=m.id,
            measure_code=m.measure_code,
            name=m.name,
            description=getattr(m, "description", None),
            measure_format=m.measure_format,
            default_unit=m.default_unit,
            default_decimal_places=getattr(m, "default_decimal_places", None),
            source_system=getattr(m, "source_system", None),
            synonyms=getattr(m, "synonyms", None),
            category=getattr(m, "category", "OTHER"),
            stat_caliber=getattr(m, "stat_caliber", None),
            domain=m.domain,
            owner_id=m.owner_id,
            status=m.status,
            submitted_by=getattr(m, "submitted_by", None),
            approver_id=getattr(m, "approver_id", None),
            reviewer_id=getattr(m, "reviewer_id", None),
            reviewer_type=getattr(m, "reviewer_type", None),
            reviewer_domain=getattr(m, "reviewer_domain", None),
            reject_reason=getattr(m, "reject_reason", None),
            reject_reviewer_id=getattr(m, "reject_reviewer_id", None),
            rejected_at=getattr(m, "rejected_at", None),
            reviewed_at=getattr(m, "reviewed_at", None),
            created_at=getattr(m, "created_at", None),
            updated_at=getattr(m, "updated_at", None),
        )


class MeasureAutoSuggestRequest(BaseModel):
    """度量目录 AI 推断请求：名称必填，其余可选作为推断上下文。"""

    name: str = Field(..., max_length=128, description="度量中文名（如 门诊收费金额）")
    description: str | None = Field(None, max_length=1000, description="度量描述（业务说明）")
    domain: str | None = Field(None, max_length=64, description="业务域（可选，辅助推断）")
    source_table: str | None = Field(None, max_length=256, description="参考源表（辅助推断）")
    measure_column: str | None = Field(None, max_length=128, description="参考度量列（辅助推断）")


class SuggestField(BaseModel):
    """推断字段：值 + 来源（llm/rule）+ 置信度 + 理由。"""

    value: Any = None
    source: str = "rule"  # llm / rule
    confidence: float = 0.0  # 0-1
    reason: str = ""


class MeasureSuggestResponse(BaseModel):
    """推断结果：逐字段带来源与置信度，用户可改后再提交。"""

    fields: dict[str, SuggestField]
    # 字段键：measure_code / name / description / measure_format / default_unit /
    #        default_decimal_places / source_system / synonyms / category / stat_caliber / domain
