"""指标 Pydantic Schema 定义。

对齐 TD §3 API 接口规范和 DEV_GUIDE §8a.1（Schema 命名 PascalCase + 后缀）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---- 请求 Schema ----


class MetricCreateRequest(BaseModel):
    """创建指标请求。

    对齐 TD §3 POST /api/v1/metric-definitions。
    """

    metric_code: str = Field(..., max_length=64, description="指标编码")
    name: str = Field(..., max_length=128, description="指标名称")
    domain: str = Field(..., max_length=64, description="所属域")
    type: Literal["atomic", "derived", "composite"] = Field(
        ..., description="指标类型: atomic/derived/composite"
    )
    granularity: str = Field(..., max_length=64, description="粒度")
    unit: str = Field(..., max_length=32, description="单位")
    currency: str | None = Field(None, max_length=16, description="币种")
    aggregation: Literal["SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE"] = Field(
        ..., description="聚合方式: SUM/AVG/COUNT/COUNT_DISTINCT/LAST_VALUE"
    )
    time_semantics: Literal["PERIOD", "YTD", "TTM", "AVG"] = Field(
        ..., description="时间语义: PERIOD/YTD/TTM/AVG"
    )
    freshness: Literal["REALTIME", "T1", "HOURLY"] = Field(
        ..., description="新鲜度: REALTIME/T1/HOURLY"
    )
    dw_layer: Literal["ODS", "DWD", "DWS", "ADS", "DM"] = Field(
        ..., description="数仓分层: ODS/DWD/DWS/ADS/DM"
    )
    metric_tier: Literal["T1", "T2", "T3"] = Field("T3", description="指标分级: T1/T2/T3")
    serving_mode: Literal["BATCH_ONLY", "REALTIME_ONLY", "BATCH_REALTIME_DUAL"] = Field(
        "BATCH_ONLY", description="服务模式"
    )
    additivity: Literal["ADDITIVE", "SEMI_ADDITIVE", "NON_ADDITIVE"] = Field(
        "ADDITIVE", description="可加性: ADDITIVE/SEMI_ADDITIVE/NON_ADDITIVE"
    )
    non_additive_dimensions: list[str] | None = Field(None, description="不可加维度列表")
    definition_json: dict[str, Any] = Field(..., description="口径定义")
    pii_flag: bool = Field(False, description="是否含 PII")
    sla: str | None = Field(None, max_length=128, description="SLA 契约")

    @field_validator("metric_code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        """校验指标编码格式: 域_业务对象_度量_统计周期。"""
        if not v or "_" not in v:
            raise ValueError("metric_code 须符合格式: 域_业务对象_度量_统计周期")
        return v


class MetricUpdateRequest(BaseModel):
    """更新指标请求。"""

    name: str | None = Field(None, max_length=128)
    granularity: str | None = Field(None, max_length=64)
    unit: str | None = Field(None, max_length=32)
    definition_json: dict[str, Any] | None = Field(None, description="口径定义")
    sla: str | None = Field(None, max_length=128)
    consumption_guide: dict[str, Any] | None = Field(None, description="消费指南")
    backup_owner_id: int | None = Field(None, description="副 Owner ID")
    change_reason: str = Field(..., min_length=4, description="变更原因")


class MetricPublishRequest(BaseModel):
    """发布指标请求（DRAFT → PUBLISHED）。"""

    version: int | None = Field(None, description="待发布版本号（缺省为当前版本）")
    change_reason: str = Field(..., min_length=4, description="发布说明")


class MetricListParams(BaseModel):
    """指标列表查询参数。"""

    domain: str | None = None
    status: str | None = None
    metric_tier: str | None = None
    keyword: str | None = None
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)


# ---- 响应 Schema ----


class MetricResponse(BaseModel):
    """指标详情响应。"""

    id: int
    metric_code: str
    name: str
    domain: str
    type: str
    granularity: str
    unit: str
    currency: str | None
    aggregation: str
    time_semantics: str
    freshness: str
    sla: str | None
    dw_layer: str
    metric_tier: str
    serving_mode: str
    additivity: str
    non_additive_dimensions: list[str] | None
    definition_json: dict[str, Any]
    version: int
    row_version: int
    status: str
    owner_id: int
    backup_owner_id: int | None
    pii_flag: bool
    compliance_reviewed: bool
    effective_version: int | None
    consumption_guide: dict[str, Any] | None
    successor_code: str | None
    deprecated_at: datetime | None
    sunset_until: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MetricListResponse(BaseModel):
    """指标列表响应。"""

    total: int
    page: int
    page_size: int
    items: list[MetricResponse]


class MetricVersionResponse(BaseModel):
    """指标版本响应。"""

    id: int
    metric_id: int
    version: int
    change_type: str
    definition_json: dict[str, Any]
    diff_json: dict[str, Any] | None
    status: str
    change_reason: str
    created_by: int
    published_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ErrorResponse(BaseModel):
    """统一错误响应。"""

    code: str
    message: str
    trace_id: str
    detail: dict[str, Any] | None = None
