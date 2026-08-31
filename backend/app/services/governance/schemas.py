"""governance 服务契约（TD §3.5 / §12.5，FR-11）。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.governance import GrantType, SensitivityLevel


class RoleCreate(BaseModel):
    """``POST /roles`` 请求体（内置角色名或自定义角色名）。"""

    name: str = Field(
        ...,
        min_length=2,
        max_length=32,
        pattern=r"^[a-z][a-z0-9_]{1,31}$",
        description="角色名（内置七角色或自定义角色，小写字母/数字/下划线）",
    )
    description: str | None = Field(default=None, max_length=256, description="角色说明")
    is_custom: bool = Field(default=False, description="是否自定义角色（True 走自定义创建校验）")


class RoleResponse(BaseModel):
    """角色响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    is_custom: bool = False


class RolePermissionItem(BaseModel):
    """角色 × 权限点配置项（RBAC 可配置化）。

    Attributes:
        role: 角色名。
        default_actions: 默认基线资源动作（``policy.ROLE_ACTIONS``）。
        custom_actions: ``role_permission`` 表覆盖的资源动作；未覆盖为 None。
        effective_actions: 生效资源动作（覆盖优先，无覆盖取默认）。
        ui_default_actions: 默认基线 UI 权限点（``policy.ROLE_UI_ACTIONS``）。
        ui_custom_actions: 覆盖的 UI 权限点；未覆盖为 None。
        ui_effective_actions: 生效 UI 权限点。
        protected: 受保护角色（platform_admin），权限点不可配置。
        is_custom: 是否自定义角色。
    """

    role: str
    default_actions: list[str]
    custom_actions: list[str] | None = None
    effective_actions: list[str]
    ui_default_actions: list[str] = Field(default_factory=list)
    ui_custom_actions: list[str] | None = None
    ui_effective_actions: list[str] = Field(default_factory=list)
    protected: bool = False
    is_custom: bool = False


class ActionRegistryItem(BaseModel):
    """动作点注册表项（``GET /action-registry``，角色管理可视化配置数据源）。

    Attributes:
        action: 权限点键（``模块:功能``）。
        module: 所属模块（前端分组渲染）。
        label: 中文名。
        description: 配置悬停说明。
    """

    action: str
    module: str
    label: str
    description: str = ""


class UserPermissionResponse(BaseModel):
    """用户按钮权限点视图（角色继承 + 直挂并集，供「按用户授权」矩阵）。

    Attributes:
        user_id: 用户 ID。
        role: 用户当前角色。
        role_actions: 角色继承的 UI 权限点（默认基线 + 覆盖）。
        direct_actions: 用户直挂的 UI 权限点（``user_permission`` 表）。
        effective_actions: 并集（前端矩阵回显勾选）。
    """

    user_id: int
    role: str
    role_actions: list[str] = Field(default_factory=list)
    direct_actions: list[str] = Field(default_factory=list)
    effective_actions: list[str] = Field(default_factory=list)


class UserPermissionUpdateRequest(BaseModel):
    """用户直挂按钮权限点更新请求。

    Attributes:
        actions: 直挂的 UI 权限点集合（整表替换，空=清空直挂）。
        reason: 直挂授权事由（审计留痕）。
    """

    actions: list[str] = Field(default_factory=list)
    reason: str | None = Field(default=None, max_length=512)


class RolePermissionUpdate(BaseModel):
    """``PUT /roles/{role}/permissions`` 请求体：覆盖某角色的权限点集合。"""

    actions: list[str] = Field(
        min_length=0,
        max_length=256,
        description=(
            "权限点集合（空数组=全部回收；上限对齐 action-registry 规模+扩展空间，"
            "合法性由 service 按权威集合校验）"
        ),
    )


class GrantCreate(BaseModel):
    """``POST /grants`` 请求体（对齐 TD §3.5 Schema 示例）。"""

    user_id: int = Field(gt=0, description="被授权用户 ID")
    role_id: int | None = Field(default=None, gt=0, description="关联角色 ID")
    domain: str | None = Field(default=None, max_length=64, description="授权主题域")
    metric_whitelist: list[str] | None = Field(default=None, description="指标白名单")
    grant_type: GrantType = Field(default=GrantType.READ, description="授权类型")
    row_level: bool = Field(default=False, description="行级权限开关")
    expires_at: datetime | None = Field(default=None, description="临时授权到期时间（UTC）")
    reason: str | None = Field(default=None, max_length=512, description="授权事由")

    @field_validator("metric_whitelist")
    @classmethod
    def _dedup_whitelist(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned = sorted({item.strip() for item in v if item and item.strip()})
        return cleaned or None

    @field_validator("domain")
    @classmethod
    def _strip_domain(cls, v: str | None) -> str | None:
        return v.strip() if v and v.strip() else None


class GrantResponse(BaseModel):
    """授权响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    role_id: int | None = None
    domain: str | None = None
    metric_whitelist: list[Any] | None = None
    grant_type: str
    status: str
    row_level: bool
    expires_at: datetime | None = None
    granted_by: int | None = None
    reason: str | None = None


class GrantBatchRequest(BaseModel):
    """``POST /grants/batch`` 请求体（R3-07：dry-run + 逐条审计 + 失败回滚）。"""

    operation: Literal["grant", "revoke"] = Field(default="grant", description="批量操作类型")
    items: Annotated[list[GrantCreate], Field(min_length=1, max_length=200)] = Field(
        description="批量条目（上限 200，防止一次性放权面过大）"
    )


class GrantBatchItemResult(BaseModel):
    """批量单条结果。"""

    user_id: int
    domain: str | None = None
    action: str
    ok: bool
    detail: str = ""


class GrantBatchResult(BaseModel):
    """批量操作/预览结果。"""

    dry_run: bool
    operation: str
    affected_users: int
    affected_metrics: int
    succeeded: int
    failed: int
    items: list[GrantBatchItemResult]


class GrantListParams(BaseModel):
    """``GET /grants`` 查询参数。"""

    user_id: int | None = Field(default=None, gt=0)
    domain: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=16)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)


class PiiReviewRequest(BaseModel):
    """``POST /pii/review`` 请求体（COMPL-1 合规官复核，留痕）。"""

    metric_code: str = Field(min_length=1, max_length=128, description="待复核指标编码")
    decision: Literal["APPROVE", "REJECT"] = Field(description="复核结论")
    sensitivity_level: SensitivityLevel = Field(
        default=SensitivityLevel.PII, description="复核后敏感级别"
    )
    pii_columns: list[str] | None = Field(default=None, description="确认的 PII 字段")
    masking_policy: Literal["none", "mask", "hash", "deny"] | None = Field(
        default=None, description="脱敏策略；缺省按敏感级推导"
    )
    comment: str = Field(min_length=1, max_length=512, description="复核意见（必填，留痕）")


class PiiReviewResult(BaseModel):
    """复核结果。"""

    metric_code: str
    decision: str
    compliance_reviewed: bool
    sensitivity_level: str
    masking_policy: str
    reviewer_id: int
    reviewed_at: datetime
    secondary_validation: PiiSecondaryValidationResult | None = None


class PiiSecondaryValidationResult(BaseModel):
    """PII 字段级脱敏二次校验结果（落库外 / 查询侧补强校验，依赖 governance）。

    在 DB 落库脱敏之外，再次核验：① 是否通过合规复核；② 字段级脱敏策略是否已
    生效；③ 口径定义中是否存在 PII 字段明文暴露。任一不通过则 ``passed=False``
    并列出 ``findings``。
    """

    metric_code: str
    passed: bool
    masking_policy: str
    checked_columns: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)


class ClassificationRescanRequest(BaseModel):
    """``POST /classification/rescan`` 请求体（COMPL-2 分级重扫）。"""

    source_id: str | None = Field(default=None, max_length=64, description="按数据源重扫")
    source_ids: list[str] | None = Field(
        default=None, max_length=10, description="按多个数据源重扫（前端多选）"
    )
    catalog_ids: list[int] | None = Field(default=None, description="按资产 ID 重扫")
    limit: int = Field(default=200, ge=1, le=1000, description="单次扫描上限")

    @field_validator("catalog_ids")
    @classmethod
    def _dedup_ids(cls, v: list[int] | None) -> list[int] | None:
        return sorted(set(v)) if v else None

    @field_validator("source_ids")
    @classmethod
    def _dedup_source_ids(cls, v: list[str] | None) -> list[str] | None:
        return sorted(set(v)) if v else None


class ClassificationItem(BaseModel):
    """单个资产的分级结果。"""

    catalog_id: int
    entity_name: str
    sensitivity_before: str
    sensitivity_after: str
    pii_columns: list[dict[str, Any]]
    degraded: bool = False


class ClassificationRescanResult(BaseModel):
    """重扫汇总。"""

    scanned: int
    changed: int
    pii_found: int
    degraded: int
    model_version: str
    items: list[ClassificationItem]


class PermissionSnapshot(BaseModel):
    """``GET /me/permissions`` 响应：当前用户权限快照。"""

    user_id: int
    role: str
    roles: list[str] = Field(
        default_factory=list,
        description="全部角色（主角色在前，含 user_role 扩展角色，方案 A 多角色）",
    )
    home_domain: str | None = None
    allowed_actions: list[str]
    ui_actions: list[str] = Field(
        default_factory=list,
        description="UI 权限点（模块:功能，前端 usePermission 消费；默认+覆盖合并）",
    )
    granted_domains: list[str]
    metric_whitelist: list[str]
    row_level_restricted: bool
    grants: list[GrantResponse]
    expiring_soon: list[GrantResponse]


class PermissionCheckRequest(BaseModel):
    """内部 PDP 校验入参（供 consume/semantic 调用）。"""

    user_id: int = Field(gt=0)
    action: Literal["read", "write", "approve", "export", "review"]
    domain: str | None = None
    metric_code: str | None = None


class PermissionCheckResult(BaseModel):
    """PDP 校验结果。"""

    allow: bool
    reason: str
    error_code: str = ""
    restricted: bool = False
    masking: str = "none"


class ErasureRequestCreate(BaseModel):
    """``POST /erasure`` 请求体（D9 被遗忘权执行，R7-09③）。"""

    subject_user_id: int = Field(gt=0, description="数据主体（被遗忘）用户 ID")
    reason: str | None = Field(default=None, max_length=512, description="执行事由")


class ErasureResult(BaseModel):
    """被遗忘权执行结果。"""

    subject_user_id: int
    status: str
    token_prefix: str = Field(description="脱敏令牌前缀（前 12 位，用于合规复核去标识化）")
    affected_rows: int
    requested_at: datetime


class ClassificationFalsePositiveRequest(BaseModel):
    """``POST /catalogs/classification/{id}/false-positive`` 请求体（误报反馈）。

    误报反馈闭环：把被误判 PII 的字段/前缀写入 ``pii_vocab`` 豁免词表
    （exempt_field 精确 / exempt_prefix 前缀），并触发该实体重算降级——
    治理者发现误判后一键豁免，无需改代码发版。
    """

    column: str = Field(min_length=1, max_length=128, description="被误判为 PII 的字段名")
    scope: Literal["field", "prefix"] = Field(
        default="field", description="豁免粒度：field=精确字段名；prefix=字段名前缀"
    )
    reason: str = Field(min_length=1, max_length=256, description="误报原因（留痕，必填）")


class ClassificationFalsePositiveResult(BaseModel):
    """误报反馈处理结果。"""

    catalog_id: int
    entity_name: str
    column: str
    scope: str
    exempted_as: str
    sensitivity_before: str
    sensitivity_after: str
    remaining_pii_columns: list[str]
