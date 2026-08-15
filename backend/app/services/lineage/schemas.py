"""血缘领域 Schemas（Pydantic v2）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LineageParseRequest(BaseModel):
    """血缘解析请求。"""

    sql: str = Field(..., min_length=1, max_length=200_000, description="待解析 SQL")
    dialect: str | None = Field(
        default=None,
        description="sqlglot dialect，如 mysql/hive/doris/clickhouse（对齐数据源类型）",
    )
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
    pii_inherited: bool = Field(default=False, description="PII 是否沿血缘继承")


class LineageParseResponse(BaseModel):
    """血缘解析结果。"""

    table_edges: int
    field_edges: int
    graph_written: bool


class ImpactedMetric(BaseModel):
    """受影响的指标条目。"""

    metric_code: str = Field(description="指标编码")
    change_type: str = Field(description="影响路径上的变更类型，如 UPDATED/DELETED")


class ImpactPreviewResponse(BaseModel):
    """变更影响预览（what-if）响应。"""

    model_config = ConfigDict(from_attributes=True)

    affected_metrics: list[ImpactedMetric] = Field(
        description="受影响的指标列表（含 metric_code 与影响类型）"
    )
    affected_tables: list[str] = Field(description="受影响的物理表列表（table: 前缀）")
    affected_consumers: list[str] = Field(description="消费方节点列表（CONSUMED_BY 边终点）")
    risk_level: str = Field(description="风险等级：critical/high/medium/low")


class LineageImpactParams(BaseModel):
    """影响分析查询参数（query）。"""

    node: str = Field(..., min_length=1, max_length=512)
    direction: Literal["upstream", "downstream", "both"] = "downstream"
    max_hops: int = Field(default=5, ge=1, le=10)
    page: int = Field(default=1, ge=1, description="分页页码（从 1 开始）")
    page_size: int = Field(default=50, ge=1, le=200, description="每页条数")


class LineageEdgeListParams(BaseModel):
    """血缘边列表查询参数（query）。"""

    node: str = Field(..., min_length=1, max_length=512)
    direction: Literal["upstream", "downstream", "both"] = "both"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class ImpactPreviewRequest(BaseModel):
    """变更影响预览（what-if）请求。"""

    metric_code: str = Field(..., min_length=1, max_length=512, description="拟变更的指标编码")
    change_type: str = Field(
        default="UPDATE",
        max_length=32,
        description="变更类型，如 UPDATE/BREAKING/DROP/ADD，用于风险分级",
    )


def impact_to_dict(edges: list[Any]) -> list[dict[str, Any]]:
    """将血缘边 ORM 列表序列化为字典。"""
    return [LineageEdgeResponse.model_validate(e).model_dump() for e in edges]


class LineageIngestRunResponse(BaseModel):
    """血缘采集通道运行记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    run_at: datetime
    status: str
    total_edges: int
    added_count: int
    updated_count: int
    missing_count: int
    stale_flagged_count: int
    restored_count: int
    error: str | None = None


class LineageChannelResponse(BaseModel):
    """血缘采集通道总览响应。"""

    source: str = Field(description="来源通道标识，如 dp_csv")
    edge_count: int = Field(description="该来源血缘边总数")
    node_count: int = Field(description="涉及节点数（源∪目标去重）")
    stale_count: int = Field(description="当前失效队列边数")
    last_run: LineageIngestRunResponse | None = Field(
        default=None, description="最近一次采集运行记录"
    )


class StaleEdgeResponse(BaseModel):
    """失效队列边响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_node: str
    target_node: str
    edge_type: str
    granularity: str
    confidence: float
    provenance: str
    missing_count: int = Field(description="连续未确认轮次")
    stale_since: datetime | None = Field(default=None, description="进入失效队列时间")


class LineageStaleParams(BaseModel):
    """失效队列查询参数（query）。"""

    source: str | None = Field(default=None, max_length=32, description="按来源通道过滤")
    limit: int = Field(default=200, ge=1, le=1000, description="返回条数上限")
