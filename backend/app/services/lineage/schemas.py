"""血缘领域 Schemas（Pydantic v2）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LineageParseRequest(BaseModel):
    """血缘解析请求。"""

    sql: str = Field(..., min_length=1, max_length=200_000, description="待解析 SQL")
    dialect: str | None = Field(default=None, description="sqlglot dialect，如 hive/mysql")
    source_node: str | None = Field(default=None, max_length=512, description="可选上游资产节点")
    provenance: str = Field(default="sqlglot", max_length=32, description="来源通道")


class LineageEdgeResponse(BaseModel):
    """血缘边响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_node: str
    target_node: str
    edge_type: str
    granularity: str
    confidence: float
    provenance: str


class LineageParseResponse(BaseModel):
    """血缘解析结果。"""

    table_edges: int
    field_edges: int
    graph_written: bool


class LineageImpactParams(BaseModel):
    """影响分析查询参数（query）。"""

    node: str = Field(..., min_length=1, max_length=512)
    direction: Literal["upstream", "downstream", "both"] = "downstream"
    max_hops: int = Field(default=5, ge=1, le=10)


class LineageEdgeListParams(BaseModel):
    """血缘边列表查询参数（query）。"""

    node: str = Field(..., min_length=1, max_length=512)
    direction: Literal["upstream", "downstream", "both"] = "both"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


def impact_to_dict(edges: list[Any]) -> list[dict[str, Any]]:
    """将血缘边 ORM 列表序列化为字典。"""
    return [LineageEdgeResponse.model_validate(e).model_dump() for e in edges]
