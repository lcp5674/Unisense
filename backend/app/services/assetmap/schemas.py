"""资产地图请求 Schema（FR-18 资产工作台写能力 + PII 合规增强）。

认领/转让归属、敏感级重分类、批量操作、表级复核、脱敏策略、字段误报标注、
保留期设置、行业分级模板。全部写操作仅限治理角色（API 层 RBAC），此处负责输入校验。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.enums import SensitivityLevelEnum

#: 单次批量操作实体数量上限（防超大请求拖垮 DB 事务）。
_BATCH_LIMIT = 200

#: 合法脱敏策略（与 governance/policy.py masking_for 取值对齐）。
MASKING_POLICIES = ("none", "mask", "hash", "deny")


class AssignOwnerRequest(BaseModel):
    """认领/转让归属。

    ``owner_id`` 为空表示解除归属（回到孤儿池）；非空须为存在的有效用户。
    """

    owner_id: int | None = Field(default=None, description="目标 Owner 用户 ID；None=解除归属")


class ReclassifySensitivityRequest(BaseModel):
    """重分类敏感级（仅允许枚举值，防止脏数据写入）。"""

    sensitivity_level: SensitivityLevelEnum = Field(..., description="目标敏感级别")

    @field_validator("sensitivity_level")
    @classmethod
    def _normalize(cls, v: object) -> object:
        """枚举成员统一为字符串值（真实 MySQL 加载枚举后成员为 enum 对象）。"""
        return v.value if isinstance(v, SensitivityLevelEnum) else v


class BatchOwnerRequest(BaseModel):
    """批量认领/转让归属。"""

    entity_ids: list[int] = Field(..., min_length=1, max_length=_BATCH_LIMIT)
    owner_id: int | None = Field(default=None, description="None=批量解除归属")


class BatchSensitivityRequest(BaseModel):
    """批量重分类敏感级。"""

    entity_ids: list[int] = Field(..., min_length=1, max_length=_BATCH_LIMIT)
    sensitivity_level: SensitivityLevelEnum = Field(..., description="目标敏感级别")

    @field_validator("sensitivity_level")
    @classmethod
    def _normalize(cls, v: object) -> object:
        return v.value if isinstance(v, SensitivityLevelEnum) else v


# ---------------------------------------------------------------- PII 合规增强


class CatalogReviewRequest(BaseModel):
    """目录资产（表/视图）表级 PII 合规复核。

    decision=APPROVE 置已复核；REJECT 保持未复核并记录理由。
    禁自审：资产责任人不得复核本人资产（service 层校验）。
    """

    decision: str = Field(..., description="APPROVE / REJECT")
    comment: str | None = Field(default=None, max_length=256, description="复核意见")

    @field_validator("decision")
    @classmethod
    def _validate_decision(cls, v: str) -> str:
        up = v.strip().upper()
        if up not in ("APPROVE", "REJECT"):
            raise ValueError("decision 仅支持 APPROVE / REJECT")
        return up


class SetMaskingPolicyRequest(BaseModel):
    """设置资产脱敏策略（none/mask/hash/deny）。"""

    policy: str = Field(..., description="脱敏策略")

    @field_validator("policy")
    @classmethod
    def _validate_policy(cls, v: str) -> str:
        p = v.strip().lower()
        if p not in MASKING_POLICIES:
            raise ValueError(f"非法脱敏策略: {v}（可选 {', '.join(MASKING_POLICIES)}）")
        return p


class PiiFieldOverrideRequest(BaseModel):
    """字段级人工标注（误报反馈/人工确认）。

    suppressed=True 表示「该列不是 PII」（误报，检测/展示忽略）；
    suppressed=False 表示「人工确认是 PII」（即使规则未命中也保留）。
    """

    column: str = Field(..., min_length=1, max_length=128, description="字段名")
    suppressed: bool = Field(default=True, description="True=标注非 PII；False=确认是 PII")
    reason: str | None = Field(default=None, max_length=256, description="标注理由")


class SetRetentionRequest(BaseModel):
    """设置资产保留期与合法性基础（合规留存期限）。

    ``retention_days=None`` 表示清除保留期（不设期限）。
    """

    retention_days: int | None = Field(default=None, ge=1, le=36500, description="保留期（天）")
    legal_basis: str | None = Field(default=None, max_length=64, description="合法性基础")


class ApplyPiiTemplateRequest(BaseModel):
    """应用行业分级模板（按字段类别升级敏感级）。

    三选一作用域：``catalog_ids``（指定资产）/ ``source_id``（整个数据源）/
    ``all_pii``（全部 PII 资产）。
    """

    template_id: str = Field(..., description="模板 ID（pii_templates 返回）")
    catalog_ids: list[int] | None = Field(
        default=None, max_length=_BATCH_LIMIT, description="指定资产 ID 列表"
    )
    source_id: str | None = Field(default=None, description="按数据源作用")
    all_pii: bool = Field(default=False, description="作用于全部 PII 资产")
