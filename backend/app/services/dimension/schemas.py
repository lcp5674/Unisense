"""维度管理 Schemas（TD §12.15 / FR-05 / FR-09）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DimensionCreate(BaseModel):
    # 长度对齐模型列（name=128 / domain=64 / dim_code=64），超长提交 422 而非 MySQL 500
    dim_code: str | None = Field(
        default=None,
        max_length=64,
        description="维度编码（可选，缺省自动生成，格式小写字母开头）",
    )
    name: str = Field(..., max_length=128)
    domain: str = Field(..., max_length=64)
    type: str = "SCD1"
    description: str | None = None
    # PLAT-2: owner_id 允许客户端省略，服务端以认证身份覆盖（防越权指定责任人）。
    owner_id: int | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("维度名称不能为空")
        return v


class DimensionUpdate(BaseModel):
    # 编辑可改编码（仅 DRAFT 状态允许，PUBLISHED/DEPRECATED 由 service 层拦截）；
    # 格式与创建时一致（小写字母/数字/下划线，且不以数字开头）。
    dim_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=64,
        description="维度编码（可选，DRAFT 可改，已发布/已废弃禁止）",
    )
    name: str | None = Field(None, max_length=128)
    domain: str | None = Field(None, max_length=64)
    type: str | None = None
    description: str | None = None


class DimensionResponse(BaseModel):
    id: int
    dim_code: str
    name: str
    domain: str
    type: str
    description: str | None = None
    owner_id: int
    status: str
    #: 绑定指标数（list_dimensions LEFT JOIN 聚合，from_model 默认 0）
    metric_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> DimensionResponse:
        return cls(
            id=m.id,
            dim_code=m.dim_code,
            name=m.name,
            domain=m.domain,
            type=m.type,
            description=getattr(m, "description", None),
            owner_id=m.owner_id,
            status=m.status,
            metric_count=getattr(m, "metric_count", 0),
            created_at=getattr(m, "created_at", None),
            updated_at=getattr(m, "updated_at", None),
        )


class DimensionMemberCreate(BaseModel):
    # 长度对齐模型列（member_code=64 / member_name=128 / parent_code=64 / path=512）
    dim_code: str = Field(..., max_length=64)
    member_code: str | None = Field(
        default=None,
        max_length=64,
        description="成员编码（可选，缺省自动生成，维度内唯一）",
    )
    member_name: str = Field(..., max_length=128)
    parent_code: str | None = Field(None, max_length=64)
    path: str | None = Field(None, max_length=512)  # 缺省由服务端按父级路径自动推测
    attributes: dict[str, Any] | None = None
    status: str = "PUBLISHED"

    @field_validator("member_name")
    @classmethod
    def _member_name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("成员名称不能为空")
        return v


class DimensionMemberUpdate(BaseModel):
    """维度成员编辑（member_code 为业务标识，不可变更；仅改名称/父级/属性/状态）。"""

    member_name: str | None = Field(None, max_length=128)
    parent_code: str | None = Field(None, max_length=64)  # 变更父级时服务端自动重算 path
    path: str | None = Field(None, max_length=512)
    attributes: dict[str, Any] | None = None
    status: str | None = None


class DimensionMemberResponse(BaseModel):
    id: int
    dim_code: str
    member_code: str
    member_name: str
    parent_code: str | None = None
    path: str | None = None
    attributes: dict[str, Any] | None = None
    status: str
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> DimensionMemberResponse:
        return cls(
            id=m.id,
            dim_code=m.dim_code,
            member_code=m.member_code,
            member_name=m.member_name,
            parent_code=getattr(m, "parent_code", None),
            path=getattr(m, "path", None),
            attributes=getattr(m, "attributes", None),
            status=m.status,
            created_at=getattr(m, "created_at", None),
        )


class DimensionMappingCreate(BaseModel):
    source_dim_code: str
    target_dim_code: str
    mapping_type: str
    expression: str | None = None
    # PLAT-2: created_by 允许客户端省略，服务端以认证身份覆盖。
    created_by: int | None = None


class DimensionMappingUpdate(BaseModel):
    """维度映射编辑（仅可改映射类型/表达式，源/目标维度不可变更）。"""

    mapping_type: str | None = None
    expression: str | None = None


class DimensionMappingResponse(BaseModel):
    id: int
    source_dim_code: str
    target_dim_code: str
    mapping_type: str
    expression: str | None = None
    created_by: int
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> DimensionMappingResponse:
        return cls(
            id=m.id,
            source_dim_code=m.source_dim_code,
            target_dim_code=m.target_dim_code,
            mapping_type=m.mapping_type,
            expression=getattr(m, "expression", None),
            created_by=m.created_by,
            created_at=getattr(m, "created_at", None),
        )


class MetricDimensionBind(BaseModel):
    metric_id: int
    dim_code: str
    role: str
    default_member: str | None = None


class MetricDimensionResponse(BaseModel):
    id: int
    metric_id: int
    dim_code: str
    role: str
    default_member: str | None = None

    @classmethod
    def from_model(cls, m: Any) -> MetricDimensionResponse:
        return cls(
            id=m.id,
            metric_id=m.metric_id,
            dim_code=m.dim_code,
            role=m.role,
            default_member=getattr(m, "default_member", None),
        )


class DimensionMetricBinding(BaseModel):
    """按维度查绑定指标：绑定关系 + 指标信息（治理追溯）。"""

    metric_id: int
    dim_code: str
    role: str
    default_member: str | None = None
    metric_code: str | None = None
    metric_name: str | None = None
    metric_status: str | None = None


class ReconciliationSubmit(BaseModel):
    metric_id: int
    dim_code: str | None = None
    expected_expr: str
    actual_expr: str
    diff_summary: str | None = None


class ReconciliationReview(BaseModel):
    decision: str  # APPROVED | REJECTED
    # PLAT-2: reviewer_id 允许客户端省略，服务端以认证身份覆盖（防越权以他人名义复核）。
    reviewer_id: int | None = None


class ReconciliationResponse(BaseModel):
    id: int
    metric_id: int
    dim_code: str | None = None
    expected_expr: str
    actual_expr: str
    status: str
    diff_summary: str | None = None
    reviewed_by: int | None = None
    created_at: datetime | None = None
    reviewed_at: datetime | None = None
    metric_code: str | None = None
    metric_name: str | None = None

    @classmethod
    def from_model(cls, m: Any) -> ReconciliationResponse:
        return cls(
            id=m.id,
            metric_id=m.metric_id,
            dim_code=getattr(m, "dim_code", None),
            expected_expr=m.expected_expr,
            actual_expr=m.actual_expr,
            status=m.status,
            diff_summary=getattr(m, "diff_summary", None),
            reviewed_by=getattr(m, "reviewed_by", None),
            created_at=getattr(m, "created_at", None),
            reviewed_at=getattr(m, "reviewed_at", None),
            metric_code=getattr(m, "metric_code", None),
            metric_name=getattr(m, "metric_name", None),
        )


class PreviewValuesRequest(BaseModel):
    """从数据源表列拉取去重枚举值（维度值自动获取）。"""

    source_id: str = Field(..., max_length=128, description="数据源 ID")
    table: str = Field(..., max_length=256, description="表名（可带库前缀，如 dwd.sales）")
    column: str = Field(..., max_length=256, description="列名")
    limit: int = Field(default=200, ge=1, le=1000, description="去重值上限")


class PreviewValuesResponse(BaseModel):
    """表列去重枚举值预览结果。"""

    values: list[str] = Field(default_factory=list, description="去重后的枚举值")
    total: int = Field(default=0, description="实际获取条数")
    truncated: bool = Field(default=False, description="是否因达到 limit 被截断（结果不完整）")
