"""系统字典 Pydantic Schema 定义。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

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
    #: 扩展属性（JSON）：如度量格式的 {"unit": "元", "decimal": 2}，前端据此联动默认
    extra: dict[str, Any] | None = Field(
        None, description="扩展属性（JSON，如度量格式的默认单位/小数位）"
    )

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _DICT_CODE_PATTERN.match(v):
            raise ValueError("字典项编码仅含字母、数字和下划线")
        return v

    @field_validator("extra")
    @classmethod
    def validate_extra(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None and len(v) > 32:
            raise ValueError("扩展属性键值数量不能超过 32")
        return v


class DictItemUpdate(BaseModel):
    """更新字典项请求。

    ``code`` 可选：传入且与当前编码不同时执行**改码**——校验格式/唯一性后
    同步更新全部引用该编码的业务数据（指标/逻辑度量/挂载/模板/域默认值），
    见 ``SystemDictService.update_item``。未传或不变化时不触发改码。
    """

    code: str | None = Field(None, max_length=64, description="新编码（变更时同步全部引用）")
    label: str | None = Field(None, max_length=128)
    sort_order: int | None = None
    description: str | None = Field(None, max_length=256)
    extra: dict[str, Any] | None = None

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _DICT_CODE_PATTERN.match(v):
            raise ValueError("字典项编码仅含字母、数字和下划线")
        return v


class DictInferDescriptionRequest(BaseModel):
    """参照数据项描述 LLM 推断请求（供新增/编辑弹窗的「AI 生成描述」按钮调用）。

    LLM 只生成描述文本回填表单，不落库（落库仍走既有 create/update 流程）。
    注入扫描豁免 label/dict_type/dict_type_label——它们是合法业务输入，仅作 LLM
    prompt 上下文、不拼接进 DB 查询。
    """

    dict_type: str = Field(..., min_length=1, max_length=64, description="字典类型编码")
    label: str = Field(..., min_length=1, max_length=128, description="参照数据项显示名")
    dict_type_label: str | None = Field(
        None, max_length=64, description="字典类型中文名（供 LLM 参考上下文）"
    )


class DictItemResponse(BaseModel):
    """字典项响应。"""

    id: int
    dict_type: str
    code: str
    label: str
    sort_order: int
    status: str
    description: str | None
    extra: dict[str, Any] | None = None
    ref_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DictTypeListResponse(BaseModel):
    """字典类型列表响应。"""

    dict_type: str
    items: list[DictItemResponse]


class DictValueCheckItem(BaseModel):
    """字典值校验项（dict_type + value）。"""

    dict_type: str = Field(..., max_length=64, description="字典类型（如 currency/unit）")
    value: str = Field(..., max_length=64, description="待校验取值")


class DictValuesVerifyRequest(BaseModel):
    """批量校验字典值是否未收录请求。"""

    values: list[DictValueCheckItem] = Field(..., min_length=1, max_length=50)


class DictValuesVerifyResponse(BaseModel):
    """未收录字典值响应。"""

    unknown: list[DictValueCheckItem]


class DictUnknownNotifyRequest(BaseModel):
    """无收录权限用户保存未收录值时，通知管理员收录/打回请求。"""

    metric_code: str | None = Field(None, max_length=64, description="指标编码")
    values: list[DictValueCheckItem] = Field(..., min_length=1, max_length=50)
    note: str | None = Field(None, max_length=500, description="提交说明")


class DictUnknownRejectRequest(BaseModel):
    """管理员打回字典收录申请请求。"""

    notification_id: int = Field(..., gt=0, description="原待办通知 ID")
    reason: str | None = Field(None, max_length=500, description="打回原因")


class DictBatchCreateRequest(BaseModel):
    """批量新增字典项请求（同一 dict_type 下）。"""

    items: list[DictItemCreate] = Field(..., min_length=1, max_length=100, description="待新增项")


class DictBatchStatusRequest(BaseModel):
    """批量启用/停用字典项请求。"""

    codes: list[str] = Field(..., min_length=1, max_length=100, description="待操作编码列表")
    action: Literal["activate", "deactivate"] = Field(
        "activate", description="activate 启用 / deactivate 停用"
    )

    @field_validator("codes")
    @classmethod
    def validate_codes(cls, v: list[str]) -> list[str]:
        for code in v:
            if not _DICT_CODE_PATTERN.match(code):
                raise ValueError(f"字典项编码仅含字母、数字和下划线: {code}")
        return v


class DictBatchDeleteRequest(BaseModel):
    """批量删除字典项请求。"""

    codes: list[str] = Field(..., min_length=1, max_length=100, description="待删除编码列表")

    @field_validator("codes")
    @classmethod
    def validate_codes(cls, v: list[str]) -> list[str]:
        for code in v:
            if not _DICT_CODE_PATTERN.match(code):
                raise ValueError(f"字典项编码仅含字母、数字和下划线: {code}")
        return v


class DictBatchItem(BaseModel):
    """批量操作结果单项（207 语义，逐项标注成败原因）。"""

    code: str = Field(..., description="字典项编码（新增失败时可能为空串）")
    label: str | None = Field(None, description="显示名（新增失败/未找到时为 None）")
    ok: bool = Field(..., description="是否成功")
    error_code: str | None = Field(None, description="失败原因编码")
    message: str | None = Field(None, description="失败原因描述")


class DictBatchResult(BaseModel):
    """批量操作结果（succeeded + failed 分桶）。"""

    succeeded: list[DictBatchItem] = Field(default_factory=list)
    failed: list[DictBatchItem] = Field(default_factory=list)
