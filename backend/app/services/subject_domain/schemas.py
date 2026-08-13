"""主题域 Pydantic Schema 定义。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# 域编码格式：小写字母开头，后跟小写字母数字下划线
_DOMAIN_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class SubjectDomainCreate(BaseModel):
    """创建主题域请求。

    ``code`` 可选：不传时由服务端按显示名自动生成（子域带父域前缀、冲突自增后缀）。
    """

    code: str | None = Field(None, max_length=64, description="域编码（可选，缺省自动生成）")
    name: str = Field(..., max_length=128, description="域显示名")
    parent_id: int | None = Field(None, description="父域ID（根域为null）")
    sort_order: int = Field(0, description="同级排序")
    description: str | None = Field(None, description="描述")
    # PLAT-2: owner_id 允许客户端省略，服务端以认证身份覆盖（防越权指定他人为域管理员）。
    owner_id: int | None = Field(None, description="域管理员ID（可选，缺省以创建人身份）")
    defaults_json: dict[str, Any] = Field(default_factory=dict, description="域级默认值预设")

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _DOMAIN_CODE_PATTERN.match(v):
            raise ValueError("域编码须以小写字母开头，仅含小写字母、数字和下划线")
        return v


class SubjectDomainUpdate(BaseModel):
    """更新主题域请求。"""

    name: str | None = Field(None, max_length=128)
    sort_order: int | None = None
    description: str | None = None
    owner_id: int | None = None
    defaults_json: dict[str, Any] | None = None


class SubjectDomainDefaultsUpdate(BaseModel):
    """更新域默认值预设请求。"""

    defaults_json: dict[str, Any] = Field(..., description="域级默认值预设")


class SubjectDomainResponse(BaseModel):
    """主题域详情响应。"""

    id: int
    code: str
    name: str
    parent_id: int | None
    level: int
    path: str | None
    sort_order: int
    status: str
    defaults_json: dict[str, Any]
    description: str | None
    owner_id: int
    metric_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SubjectDomainTreeNode(BaseModel):
    """主题域树节点。"""

    id: int
    code: str
    name: str
    parent_id: int | None
    level: int
    sort_order: int
    status: str
    metric_count: int = 0
    children: list[SubjectDomainTreeNode] = []

    model_config = {"from_attributes": True}
