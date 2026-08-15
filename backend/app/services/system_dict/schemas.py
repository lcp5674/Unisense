"""系统字典 Pydantic Schema 定义。"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

_DICT_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


class DictItemCreate(BaseModel):
    """创建字典项请求。

    ``code`` 可选：未传（或空串）时由后端按显示名自动生成英文编码
    （术语字典翻译 + 拼音兜底 + 冲突自增后缀），见
    ``SystemDictService._generate_unique_code``。
    """

    code: str | None = Field(None, max_length=64, description="字典项编码（缺省自动生成）")
    label: str = Field(..., max_length=128, description="显示名")
    sort_order: int = Field(0, description="排序序号")
    description: str | None = Field(None, max_length=256, description="描述")

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _DICT_CODE_PATTERN.match(v):
            raise ValueError("字典项编码仅含字母、数字和下划线")
        return v


class DictItemUpdate(BaseModel):
    """更新字典项请求。"""

    label: str | None = Field(None, max_length=128)
    sort_order: int | None = None
    description: str | None = Field(None, max_length=256)


class DictItemResponse(BaseModel):
    """字典项响应。"""

    id: int
    dict_type: str
    code: str
    label: str
    sort_order: int
    status: str
    description: str | None
    ref_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DictTypeListResponse(BaseModel):
    """字典类型列表响应。"""

    dict_type: str
    items: list[DictItemResponse]
