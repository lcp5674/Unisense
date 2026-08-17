"""敏感规则配置台数据模型（方案 A：可视化配置规则引擎）。

覆盖「敏感规则」产品级配置台的全部交互负载：
- 规则列表（内置 + 自定义合并，标注来源与启用状态）
- 结构化创建 / 更新 / 启停 / 删除
- 正则合法性校验（保存前即时反馈）
- 规则测试台（输入列名 + 样本值 + 注释 → 命中类别/规则/置信度/最终敏感级）
- 类别目录（PII 12 类 + 机密 3 类）
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SensitiveRuleItem(BaseModel):
    """规则列表项（内置与 DB 覆盖合并后的一行）。"""

    rule_id: str
    label: str
    category: str
    category_label: str
    name_re: str
    sample_re: str | None
    confidence: float
    pii: bool
    source: Literal["builtin", "custom"]  # 是否被自定义覆盖/新增
    status: Literal["active", "inactive"]
    updated_at: datetime | None = None


class SensitiveRuleUpsert(BaseModel):
    """创建 / 更新规则的公共字段（写 system_dict ``pii_rule``）。"""

    label: str = Field(..., min_length=1, max_length=128, description="规则显示名")
    category: str = Field(..., description="类别编码（PII_CATEGORY / CONFIDENTIAL_CATEGORY）")
    name_re: str = Field(..., min_length=1, description="字段名/注释关键字正则")
    sample_re: str | None = Field(None, description="取值样本正则（可选）")
    confidence: float = Field(0.7, ge=0.0, le=1.0, description="基础置信度")
    pii: bool = Field(True, description="True=计入 PII；False=机密规则")


class SensitiveRuleCreate(SensitiveRuleUpsert):
    """创建规则；rule_id 缺省由 label 自动生成英文编码。"""

    rule_id: str | None = Field(None, max_length=64, description="规则标识（缺省自动生成）")


class RegexCheckRequest(BaseModel):
    pattern: str = Field(..., description="待校验的正则表达式")


class RegexCheckResponse(BaseModel):
    valid: bool
    error: str | None = None


class RuleTestRequest(BaseModel):
    """测试台入参：模拟一条待识别字段。"""

    entity_name: str = Field("", max_length=128, description="表/视图名（可选）")
    column_name: str = Field(..., min_length=1, max_length=128, description="字段名")
    sample_value: str | None = Field(None, max_length=256, description="取值样本（可选）")
    comment: str | None = Field(None, max_length=256, description="字段注释（可选）")


class RuleTestHit(BaseModel):
    column: str
    category: str
    category_label: str
    rule: str
    confidence: float
    matched_by: str
    pii: bool


class RuleTestResponse(BaseModel):
    """测试台结果：最终敏感级 + 命中明细（与真实采集一致）。"""

    sensitivity_level: str
    hits: list[RuleTestHit]


class CategoryItem(BaseModel):
    """类别目录项（配置台类别下拉用）。"""

    category: str
    label: str
    pii: bool
