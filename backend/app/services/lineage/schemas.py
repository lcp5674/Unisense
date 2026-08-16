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
    target_table: str | None = Field(
        default=None,
        max_length=512,
        description=(
            "可选落点表（方案 A+B）：纯 SELECT 无写入目标时，指定该值即把查询读取的"
            "源表/投影列指向该表，生成正式血缘并写入图谱"
        ),
    )


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


class TableLineageItem(BaseModel):
    """表级血缘边明细（本次解析结果，用于前端当页展示）。"""

    source: str = Field(description="上游表（catalog.db.table 规范化形式）")
    target: str = Field(description="下游表（写入目标）")


class FieldLineageItem(BaseModel):
    """字段级血缘边明细（本次解析结果，用于前端当页展示）。"""

    source_table: str = Field(description="上游表")
    source_column: str | None = Field(default=None, description="上游列（SELECT * 降级时为 None）")
    target_table: str = Field(description="下游表")
    target_column: str = Field(description="下游列")
    expression: str | None = Field(default=None, description="派生表达式原文（裸列引用为 None）")


class UpstreamDeps(BaseModel):
    """只读查询（纯 SELECT 无落点）读取的上游依赖清单（方案 B）。"""

    tables: list[str] = Field(default_factory=list, description="读取的源表（去重排序）")
    fields: list[str] = Field(
        default_factory=list,
        description="读取的源字段（真实表名.列名，未限定列保留裸列名）",
    )


class LineageParseResponse(BaseModel):
    """血缘解析结果。"""

    table_edges: int
    field_edges: int
    graph_written: bool
    table_lineage: list[TableLineageItem] = Field(
        default_factory=list, description="本次解析的表级边明细"
    )
    field_lineage: list[FieldLineageItem] = Field(
        default_factory=list, description="本次解析的字段级边明细"
    )
    upstream_deps: UpstreamDeps | None = Field(
        default=None,
        description="纯 SELECT 无落点时的上游依赖清单（只读展示，不写图谱）",
    )


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


class LineageNodeInfo(BaseModel):
    """血缘图中单个节点的基础信息（影响分析/边列表响应的 ``nodes`` 字段）。

    与 ``/lineage/graph`` 节点结构对齐（id/type/label/entity_id/pii/domain/owner），
    供前端血缘查询/影响分析图谱点击节点时在侧边栏展示具体信息（指标详情 / 表详情），
    并使图节点具备域/PII 属性（按业务域着色、PII 红色描边，与血缘图谱一致）。
    """

    id: str
    type: str
    label: str
    entity_id: int | None = Field(default=None, description="db_catalog 主键（仅表/视图节点有值）")
    pii: bool = Field(default=False, description="是否含 PII")
    domain: str | None = Field(default=None, description="业务域（表从数据源继承）")
    owner: str | None = Field(default=None, description="Owner ID（字符串）")


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
    detail: dict[str, Any] | None = Field(
        default=None,
        description="本次运行详情快照（SQL 解析：SQL 原文/方言/落点/边明细；批量采集：变更边明细）",
    )


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


class LineageNodeResponse(BaseModel):
    """血缘候选节点（影响分析/血缘查询选项框预加载与搜索）。"""

    id: str = Field(description="节点 id，如 table:db.orders / metric:gmv_total")
    label: str = Field(description="展示名（去类型前缀）")
    type: str = Field(description="节点类型：table/metric/field/external/other")
    count: int = Field(default=0, description="该节点参与的血缘边数（预加载排序用）")


# ---- 覆盖率治理（Task B）----


class CoverageOrphanItem(BaseModel):
    """无任何血缘边的指标（预案治理对象）。"""

    metric_code: str = Field(description="指标编码")
    domain: str | None = Field(default=None, description="指标所属业务域")


class LineageCoverageResponse(BaseModel):
    """血缘覆盖率统计（Task B 治理看板）。

    用于衡量「指标级血缘图谱」的血缘完整度：指标/表有多少接了血缘、多少孤立、
    有多少断链边（source 节点对应的目录/指标实体已不存在）。
    """

    metric_total: int = Field(description="指标总数（soft 删除过滤）")
    metric_with_lineage: int = Field(description="有血缘边的指标数")
    metric_orphan: int = Field(description="无血缘边的孤立指标数")
    table_total: int = Field(description="表总数（采集目录 TABLE/VIEW，soft 删除过滤）")
    table_no_downstream: int = Field(description="无下游血缘的表数（仅作为边目标、从未作为边源）")
    edge_total: int = Field(description="血缘边总数（soft 删除过滤）")
    broken_edges: int = Field(description="断链边数（source 节点对应实体已不存在）")


class CoverageBrokenEdgeItem(BaseModel):
    """断链边明细（source 节点对应的目录/指标实体已不存在）。"""

    id: int
    source_node: str = Field(description="上游节点（已不存在的实体）")
    target_node: str = Field(description="下游节点")
    edge_type: str
    granularity: str
    confidence: float
    provenance: str


# ---- PII 影响面分析（Task C）----


class PiiImpactItem(BaseModel):
    """受 PII 影响的下游节点（合规审计用）。"""

    node: str = Field(description="受影响下游节点 id，如 metric:m1 / consumer:c1")
    edge_type: str = Field(description="到达该节点的边类型（DERIVED_FROM/CONSUMED_BY/...）")
    path: list[str] = Field(description="路径（起点 → ... → 该节点）")
    hops: int = Field(description="跳数")


# ---- 血缘边详情（Task D）----


class LineageEdgeHistoryResponse(BaseModel):
    """血缘边变更历史快照响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_node: str
    target_node: str
    edge_type: str
    granularity: str
    confidence: float
    provenance: str
    pii_inherited: bool = Field(default=False, description="PII 是否沿血缘继承")
    change_reason: str = Field(description="变更原因：schema_drift/reparse/manual/rename")
    created_at: datetime = Field(description="历史快照创建时间（UTC）")


class LineageEdgeDetailResponse(BaseModel):
    """血缘边详情：单条边 + 其 LineageEdgeHistory 变更历史。"""

    edge: LineageEdgeResponse = Field(description="血缘边当前值")
    history: list[LineageEdgeHistoryResponse] = Field(
        default_factory=list, description="该边的变更历史（按时间倒序）"
    )


# ---- 手动登记血缘边（人工治理，TD §12.2）----


#: 支持人工登记的节点前缀（上游/下游均须为以下类型之一）。
#: 每类节点对应一种命名约定（前缀:值）与语义：
#: - ``metric:{code}``        指标编码（口径定义登记）
#: - ``table:{db}.{tbl}``     数据表（含 schema 前缀，如 table:wedw_ods.xxx）
#: - ``column:{db}.{tbl}.{col}`` 表字段（列级血缘）
#: - ``dimension:{code}``     维度编码（指标↔维度绑定）
#: - ``consumer:{client_id}`` 数据消费方（报表/接口/接入方）
#: - ``external:{name}``      外部依赖（文档/系统边界，仅登记不作处理）
MANUAL_NODE_PREFIXES = frozenset(
    {"metric", "table", "column", "dimension", "consumer", "external"}
)

#: 手动登记允许的边类型（对齐 lineage_edge_type 枚举，排除内部流转方向标记）。
MANUAL_EDGE_TYPES = frozenset(
    {"DERIVED_FROM", "CONSUMED_BY", "USES_DIMENSION", "READS_COLUMN", "EXTERNAL_BREAK"}
)


class ManualEdgeCreateRequest(BaseModel):
    """手动登记一条血缘边（人工治理）。

    上游/下游节点须带前缀（``metric:``/``table:``/``column:``/``dimension:``/
    ``consumer:``/``external:``）；方向语义：source_node 为上游（被依赖方），
    target_node 为下游（消费/加工方）。
    """

    source_node: str = Field(..., min_length=3, max_length=512, description="上游节点（带前缀）")
    target_node: str = Field(..., min_length=3, max_length=512, description="下游节点（带前缀）")
    edge_type: str = Field(
        default="DERIVED_FROM",
        description=(
            "边类型：DERIVED_FROM / CONSUMED_BY / USES_DIMENSION / "
            "READS_COLUMN / EXTERNAL_BREAK"
        ),
    )
    note: str | None = Field(
        default=None, max_length=500, description="登记说明（写入变更历史 change_reason）"
    )


class ManualEdgeCreateResponse(BaseModel):
    """手动登记结果。"""

    edge: LineageEdgeResponse = Field(description="登记后的血缘边")
    created: bool = Field(description="是否新建（False=更新既有边）")


class EdgeDeleteResult(BaseModel):
    """单条血缘边软删结果。"""

    edge_id: int = Field(description="被删除的边 ID")
    source_node: str = Field(description="上游节点")
    target_node: str = Field(description="下游节点")
