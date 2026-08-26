"""SQL 智能推断评测样本管理 Schemas（自定义样本 CRUD + 即时解析预览）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExpectedMeasureIn(BaseModel):
    """期望度量输入：列名 + 聚合方式（枚举，空为派生）。"""

    column: str = Field(..., max_length=256, description="度量列名")
    agg: str | None = Field(None, max_length=32, description="聚合方式（空=派生）")
    alias: str | None = Field(None, max_length=256, description="别名（同列多语义消歧）")
    table: str | None = Field(None, max_length=256, description="源表（可选）")


class EvalSampleIn(BaseModel):
    """创建/更新自定义评测样本。"""

    case_id: str = Field(..., min_length=1, max_length=128, description="样本编码（唯一）")
    dialect: str = Field("hive", max_length=32, description="方言/场景标注")
    sql: str = Field(..., min_length=1, max_length=65536, description="待解析 SQL 脚本")
    expected_period: str = Field(
        "day", max_length=16, description="期望统计周期（day/week/month/quarter/year/hour）"
    )
    expected_measures: list[ExpectedMeasureIn] | None = Field(
        None, description="期望度量 [{column, agg, alias?, table?}]"
    )
    expected_tables: list[str] | None = Field(
        None, description="期望源表集合"
    )
    note: str = Field("", max_length=512, description="样本说明")


class EvalSampleUpdate(BaseModel):
    """更新自定义评测样本（仅提交的字段变更；内置样本拒绝）。"""

    case_id: str | None = Field(None, min_length=1, max_length=128)
    dialect: str | None = Field(None, max_length=32)
    sql: str | None = Field(None, min_length=1, max_length=65536)
    expected_period: str | None = Field(None, max_length=16)
    expected_measures: list[ExpectedMeasureIn] | None = None
    expected_tables: list[str] | None = None
    note: str | None = Field(None, max_length=512)
    enabled: bool | None = None


class EvalSamplePreviewIn(BaseModel):
    """即时解析预览输入（不落库）：规则解析该 SQL 的实际画像。"""

    sql: str = Field(..., min_length=1, max_length=65536, description="待解析 SQL 脚本")


def _measures_to_dicts(
    measures: list[ExpectedMeasureIn] | None,
) -> list[dict[str, Any]]:
    """ExpectedMeasureIn 列表 → dict 列表（服务层落库）。"""
    if not measures:
        return []
    return [
        {
            "column": m.column,
            "agg": m.agg,
            "alias": m.alias,
            "table": m.table,
        }
        for m in measures
    ]
