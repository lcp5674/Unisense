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
    # P11 C-2：乐观锁（编辑回传当前 row_version，他人已改则 409 防静默覆盖）
    row_version: int | None = Field(
        None, ge=1, description="乐观锁版本（编辑回传当前 row_version）"
    )


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
    # ---- 审核流字段（对齐主数据审核 ReviewFieldsMixin，供前端评审权判断/驳回展示）----
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
    # P11 C-2：乐观锁版本（前端编辑回传）
    row_version: int = 1

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
            row_version=(getattr(m, "row_version", None) or 1),
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
    status: str = "DRAFT"  # 默认草稿：新成员先进入未发布态（对齐维度主体/指标/术语
    # 的状态机起点），显式发布后才被下游消费

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
    #: 维度当前状态（DRAFT/PUBLISHED/DEPRECATED）——指标详情「关联维度」展示用；
    #: 来自 repository list_metric_dimensions 的 join Dimension，可能为空（旧调用契约）。
    dim_status: str | None = None

    @classmethod
    def from_model(cls, m: Any) -> MetricDimensionResponse:
        # 兼容两种入参：绑定对象 或 (binding, dimension) 元组（join 后带维度状态）
        if isinstance(m, tuple):
            binding, dimension = m
            return cls(
                id=binding.id,
                metric_id=binding.metric_id,
                dim_code=binding.dim_code,
                role=binding.role,
                default_member=getattr(binding, "default_member", None),
                dim_status=getattr(dimension, "status", None),
            )
        return cls(
            id=m.id,
            metric_id=m.metric_id,
            dim_code=m.dim_code,
            role=m.role,
            default_member=getattr(m, "default_member", None),
            dim_status=None,
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


# ---------------------------------------------------------------- 引用型维度


class DimensionReferenceBind(BaseModel):
    """绑定维度值来源（引用型：值集合来自维度表列快照）。

    绑定后 ``sync_mode`` 置 snapshot，值集合由 ``refresh_dimension_snapshot``
    从源表列 ``SELECT DISTINCT`` 拉取并版本化落快照表；成员表不再维护该维度值。
    """

    source_id: str = Field(..., max_length=128, description="数据源 ID（须已注册）")
    table: str = Field(..., max_length=256, description="维度值来源表（可带库前缀）")
    column: str = Field(..., max_length=256, description="维度值来源列")
    refresh_interval_hours: int = Field(
        default=24, ge=1, le=24 * 90, description="快照刷新间隔（小时，默认 24）"
    )


class SnapshotValueResponse(BaseModel):
    """引用型维度快照值（单行）。"""

    id: int
    dim_code: str
    value: str
    snapshot_at: datetime
    status: str


class SnapshotRunResponse(BaseModel):
    """引用型维度快照刷新记录（单次运行统计）。"""

    id: int
    dim_code: str
    snapshot_at: datetime
    status: str
    total_count: int = 0
    added_count: int = 0
    removed_count: int = 0
    null_count: int = 0
    null_rate: float | None = None
    added_sample: list[str] | None = None
    removed_sample: list[str] | None = None
    error_msg: str | None = None
    duration_ms: int | None = None
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, m: Any) -> SnapshotRunResponse:
        return cls(
            id=m.id,
            dim_code=m.dim_code,
            snapshot_at=m.snapshot_at,
            status=m.status,
            total_count=m.total_count,
            added_count=m.added_count,
            removed_count=m.removed_count,
            null_count=m.null_count,
            null_rate=float(m.null_rate) if m.null_rate is not None else None,
            added_sample=m.added_sample,
            removed_sample=m.removed_sample,
            error_msg=getattr(m, "error_msg", None),
            duration_ms=getattr(m, "duration_ms", None),
            created_at=getattr(m, "created_at", None),
        )


class MemberBatchRequest(BaseModel):
    """维度成员批量操作（发布/废弃/删除）请求体。"""

    member_codes: list[str] = Field(
        ..., min_length=1, max_length=500, description="成员编码列表"
    )


# ---------------------------------------------------------------- 值级映射


class MappingValueCreate(BaseModel):
    """值级维度映射（source_value → target_value 逐值对应）。"""

    source_value: str = Field(..., max_length=512, description="源值")
    target_value: str = Field(..., max_length=512, description="目标值")


class MappingValueResponse(BaseModel):
    """值级映射记录（单行）。"""

    id: int
    mapping_id: int
    source_value: str
    target_value: str
    created_by: int
    created_at: datetime | None = None


class MappingCoverageResponse(BaseModel):
    """值级映射覆盖率（源维度当前值集合中已配置/未配置逐值映射的统计）。"""

    mapping_id: int
    total: int = Field(default=0, description="源值集合总数")
    covered: int = Field(default=0, description="已配置逐值映射数")
    uncovered: list[str] = Field(default_factory=list, description="未映射源值样本")


class TranslateRequest(BaseModel):
    """批量值翻译请求（source 维度值 → target 维度值）。"""

    source_dim_code: str = Field(..., max_length=64, description="源维度编码")
    target_dim_code: str = Field(..., max_length=64, description="目标维度编码")
    values: list[str] = Field(..., min_length=1, max_length=500, description="待翻译的源值列表")


class TranslateResult(BaseModel):
    """单值翻译结果。"""

    source_value: str
    target_value: str | None = Field(default=None, description="翻译结果（未命中/无映射为 None）")
    covered: bool = Field(default=False, description="是否命中值级映射")
    source_dim_code: str
    target_dim_code: str


class TranslateResponse(BaseModel):
    """批量值翻译结果。"""

    results: list[TranslateResult] = Field(default_factory=list)
