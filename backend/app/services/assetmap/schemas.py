"""资产地图请求 Schema（FR-18 资产工作台写能力）。

认领/转让归属、敏感级重分类、批量操作。全部写操作仅限
platform_admin / domain_admin（API 层 RBAC），此处负责输入校验。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.enums import SensitivityLevelEnum

#: 单次批量操作实体数量上限（防超大请求拖垮 DB 事务）。
_BATCH_LIMIT = 200


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
