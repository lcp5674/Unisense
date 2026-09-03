"""血缘领域 Schemas（Pydantic v2）。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    dp_task_refs: list[dict[str, Any]] | None = Field(
        default=None,
        description="DP 调度来源引用（任务/负责人/产出，来自 dp_task_refs JSON；"
        "非 DP 通道为 None）",
    )

    @field_validator("dp_task_refs", mode="before")
    @classmethod
    def _parse_dp_task_refs(cls, v: Any) -> Any:
        """model 列是 JSON 字符串（Text），解析为 list；空/非法返回 None。"""
        if v is None or v == "":
            return None
        if isinstance(v, list):
            return v or None
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except (ValueError, TypeError):
                return None
            return parsed if isinstance(parsed, list) and parsed else None
        return None


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


class DDLEdgeItem(BaseModel):
    """DDL 血缘边明细（结构变更/依赖，区别于 DML 数据流转边）。"""

    ddl_type: str = Field(
        description="DDL 类型：create_like/create_as_copy/rename_table/rename_column/…"
    )
    source: str | None = Field(default=None, description="源表/旧表")
    target: str | None = Field(default=None, description="目标表/新表")
    table: str | None = Field(default=None, description="列变更所在表")
    source_column: str | None = Field(default=None, description="旧列名（rename_column）")
    target_column: str | None = Field(default=None, description="新列名（rename_column）")
    column: str | None = Field(default=None, description="受影响列（add/drop/alter column）")


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
    ddl_edges: list[DDLEdgeItem] = Field(
        default_factory=list, description="本次解析的 DDL 血缘边明细（结构变更/依赖）"
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
MANUAL_NODE_PREFIXES = frozenset({"metric", "table", "column", "dimension", "consumer", "external"})

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
            "边类型：DERIVED_FROM / CONSUMED_BY / USES_DIMENSION / READS_COLUMN / EXTERNAL_BREAK"
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


class BatchParseStatementResult(BaseModel):
    """批次解析单条语句的结果（含错误定位）。"""

    index: int = Field(description="语句序号（从 0 开始）")
    sql: str = Field(description="语句原文")
    table_edges: list[TableLineageItem] = Field(
        default_factory=list, description="该条产出的表级边明细"
    )
    field_edges: list[FieldLineageItem] = Field(
        default_factory=list, description="该条产出的字段级边明细"
    )
    error: str | None = Field(default=None, description="解析异常信息（单条失败不阻断批次）")


class LineageParseBatchRequest(BaseModel):
    """血缘批次解析请求（企业级批量导入）。

    ``statements``（多条独立 SQL）与 ``text``（多语句文本块，分号智能拆分）二选一，
    至少提供一个。整批共用同一 ``dialect`` 与可选 ``target_table`` 落点。
    """

    dialect: str | None = Field(
        default=None,
        description="sqlglot dialect（对齐数据源类型；None=自动猜测）",
    )
    statements: list[str] = Field(
        default_factory=list,
        description="多条 SQL（每条独立解析产血缘）",
    )
    text: str | None = Field(
        default=None,
        max_length=1_000_000,
        description="多语句文本块（按分号拆分，自动剥注释；与 statements 二选一）",
    )
    provenance: str = Field(default="sqlglot", max_length=32, description="来源通道")
    target_table: str | None = Field(
        default=None,
        max_length=512,
        description="可选整批共用落点（纯 SELECT 场景的方案 A+B 指定写入目标）",
    )
    source_node: str | None = Field(default=None, max_length=512, description="可选上游资产节点")

    @property
    def resolved_statements(self) -> list[str]:
        """解析出待处理语句列表（statements 优先，其次 text 按分号拆分）。"""
        if self.statements:
            return [s for s in self.statements if s and s.strip()]
        if self.text:
            from app.services.lineage.parser import _split_statements

            return _split_statements(self.text)
        return []


class LineageParseBatchResponse(BaseModel):
    """血缘批次解析结果（变更摘要 + 逐条明细）。"""

    total_statements: int = Field(description="待处理语句数（含失败/无血缘）")
    succeeded: int = Field(description="解析成功且产出至少一条边的语句数")
    failed: int = Field(description="解析失败（异常）的语句数")
    total_edges: int = Field(description="本次写入的边总数（表级+字段级）")
    added: int = Field(description="新增边数")
    updated: int = Field(description="更新边数")
    skipped: int = Field(default=0, description="因成环被跳过的边数")
    graph_written: bool = Field(description="图谱（Neo4j）是否写入成功")
    statements: list[BatchParseStatementResult] = Field(
        default_factory=list, description="逐条解析结果"
    )


# ---- 血缘平台健康度（P2：企业级治理综合评分）----


class HealthDimension(BaseModel):
    """健康度单一维度评分（0-100，值越高越健康）。

    每个维度独立可解释：coverage=覆盖完整度、broken=断链率、stale=失效率、
    freshness=采集新鲜度、reconciliation=图-库对账偏差。``detail`` 携带该维度
    的原始指标，供前端 tooltip 下钻。
    """

    score: float = Field(description="该维度得分 0-100")
    weight: float = Field(description="该维度在总分中的权重 0-1")
    detail: dict[str, Any] = Field(default_factory=dict, description="该维度原始指标明细")


class LineageHealthResponse(BaseModel):
    """血缘平台综合健康度（企业级治理看板核心指标）。

    五维加权总分 0-100：coverage 40% / broken 20% / stale 15% / freshness 15% /
    reconciliation 10%。图存储不可达时 reconciliation 维度为 ``None`` 且不参与
    总分（其余维度权重归一化）。``grade`` 按总分分档：excellent≥90 / good≥75 /
    fair≥60 / poor<60。
    """

    overall_score: float = Field(description="综合健康度总分 0-100")
    grade: str = Field(description="健康等级：excellent/good/fair/poor")
    dimensions: dict[str, HealthDimension] = Field(
        description="五维评分明细（coverage/broken/stale/freshness/reconciliation）"
    )
    edge_total: int = Field(description="血缘边总数")
    metric_total: int = Field(description="指标总数")
    table_total: int = Field(description="表总数")
    evaluated_at: str = Field(description="评估时间（ISO8601 UTC）")


# ---- 血缘路径查询（P3：A→B 链路 + 断链定位）----


class LineagePathEdge(BaseModel):
    """路径中的一条血缘边（有向：source 上游 → target 下游）。"""

    source: str = Field(description="上游节点 id")
    target: str = Field(description="下游节点 id")
    edge_type: str = Field(description="边类型（DERIVED_FROM/READS_COLUMN/...）")


class LineagePathItem(BaseModel):
    """A→B 的一条完整血缘链路。"""

    nodes: list[str] = Field(description="路径节点序列（source → ... → target）")
    edges: list[LineagePathEdge] = Field(description="路径边序列（与 nodes 对应）")
    hops: int = Field(description="跳数（边数）")


class LineagePathResponse(BaseModel):
    """A→B 路径查询结果（Neo4j 优先、MySQL 兜底）。"""

    source: str = Field(description="起点节点 id")
    target: str = Field(description="终点节点 id")
    has_path: bool = Field(description="是否存在至少一条可达路径")
    path_count: int = Field(description="路径条数（可能因 limit 截断）")
    shortest_hops: int | None = Field(default=None, description="最短路径跳数；不可达为 None")
    paths: list[LineagePathItem] = Field(default_factory=list, description="路径列表")
    truncated: bool = Field(default=False, description="是否因 limit 截断（实际路径多于返回）")


class LineageTerminalItem(BaseModel):
    """下游终止节点（断链定位：从起点沿下游可达、但无继续下游的节点）。

    终止节点分两类：合理边界（如 ADS 结果表）与断链嫌疑（对应实体已不存在
    但仍被边引用）。``entity_exists`` 仅对 metric:/table: 节点判定权威库存在性，
    其余类型中性为 True。
    """

    node: str = Field(description="终止节点 id")
    path: list[str] = Field(description="从起点到该节点的最短路径节点序列")
    hops: int = Field(description="到达该节点的跳数")
    node_type: str = Field(description="节点类型：table/metric/field/external/other")
    entity_exists: bool = Field(description="对应实体在权威库中是否存在（断链嫌疑标记）")


class LineageTerminalsResponse(BaseModel):
    """从指定节点下游展开的终止节点清单（断链定位）。"""

    node: str = Field(description="起点节点 id")
    terminal_count: int = Field(description="终止节点数（可能因 limit 截断）")
    terminals: list[LineageTerminalItem] = Field(default_factory=list, description="终止节点列表")
    truncated: bool = Field(default=False, description="是否因 limit 截断")


# ---- 标准血缘导出（P4：OpenLineage / JSON 开放 API）----


LineageExportFormat = Literal["openlineage", "json"]


class LineageExportParams(BaseModel):
    """血缘导出查询参数（query）。

    供治理/合规平台以开放格式消费血缘：OpenLineage 事件（L1+L2 表/字段级，
    标准 RunEvent 结构）或通用 JSON（原始边明细 + 元数据）。可按节点/方向/
    粒度/来源过滤后导出。
    """

    format: LineageExportFormat = "openlineage"
    node: str | None = Field(
        default=None, max_length=512, description="按节点过滤（仅返回该节点直接相关的边）"
    )
    direction: Literal["upstream", "downstream", "both"] = "both"
    granularity: Literal["L1", "L2", "L3", "all"] = "all"
    provenance: str | None = Field(default=None, max_length=32, description="按来源通道过滤")
    limit: int = Field(default=10_000, ge=1, le=100_000, description="返回边数上限")
    domain: str | None = Field(
        default=None,
        max_length=64,
        description="按业务域过滤（X-2 域边界：非 platform_admin 强制收敛到本域）",
    )


class OpenLineageFieldLineage(BaseModel):
    """OpenLineage SchemaFieldLineage：输出字段 → 输入字段（字段级血缘）。"""

    name: str = Field(description="输出字段名")
    input_fields: list[dict[str, str]] = Field(
        default_factory=list,
        description="输入字段 [{namespace, name, field}]（来源数据集与列）",
    )


class OpenLineageSchemaFacet(BaseModel):
    """OpenLineage SchemaDatasetFacet：数据集字段清单 + 字段级血缘（lineage 子 facet）。"""

    fields: list[dict[str, str]] = Field(
        default_factory=list, description="字段 [{name, type}]（类型未知时为 unknown）"
    )
    lineage: list[OpenLineageFieldLineage] | None = Field(
        default=None, description="字段级血缘（存在 L2 边时填充）"
    )


class OpenLineageDataset(BaseModel):
    """OpenLineage Dataset（RunEvent 的 input/output）。"""

    namespace: str = Field(description="命名空间（如 unisense）")
    name: str = Field(description="数据集名（去前缀的节点 id，如 db.tbl）")
    facets: dict[str, OpenLineageSchemaFacet] = Field(default_factory=dict)


class OpenLineageRunEvent(BaseModel):
    """OpenLineage RunEvent：一次表级数据流转（源数据集 → 目标数据集）。

    对齐 OpenLineage 2-0-0 规范（``schemaURL`` 指向官方 spec JSON）；每条 L1
    表级边生成一个事件，L2 字段级血缘以 schema facet 的 ``lineage`` 子 facet
    挂到目标数据集上。

    规范字段为 camelCase（``eventType``/``eventTime``/``schemaURL``）：内部用
    snake_case 属性 + ``alias`` 映射，序列化时 ``model_dump(by_alias=True)``
    输出标准 OpenLineage 事件结构。
    """

    model_config = ConfigDict(populate_by_name=True)

    event_type: str = Field(default="COMPLETE", alias="eventType", description="OL 事件类型")
    event_time: str = Field(alias="eventTime", description="事件时间（ISO8601 UTC）")
    producer: str = Field(description="生产者标识（本平台 URI）")
    schema_url: str = Field(
        default="https://openlineage.io/spec/2-0-0/OpenLineage.json",
        alias="schemaURL",
        description="OpenLineage spec 版本 URL",
    )
    run: dict[str, Any] = Field(default_factory=lambda: {"runId": ""})
    job: dict[str, Any] = Field(default_factory=dict)
    inputs: list[OpenLineageDataset] = Field(default_factory=list)
    outputs: list[OpenLineageDataset] = Field(default_factory=list)


class LineageJsonExportResponse(BaseModel):
    """通用 JSON 血缘导出（原始边明细 + 元数据，供开放 API 消费）。"""

    format: str = Field(default="json")
    producer: str = Field(description="生产者标识")
    exported_at: str = Field(description="导出时间（ISO8601 UTC）")
    edge_count: int = Field(description="导出边数")
    edges: list[dict[str, Any]] = Field(
        default_factory=list, description="边明细（id/source_node/target_node/edge_type/...）"
    )


class LineageScanRequest(BaseModel):
    """库级扫描请求（企业级血缘重建：扫描 SQL 目录批量解析）。"""

    path: str = Field(description="待扫描目录（容器内绝对路径）")
    dialect: str | None = Field(default=None, description="强制方言；None=按文件内容启发式推断")
    dry_run: bool = Field(default=True, description="True 仅统计不落库；False 批量幂等写入血缘")
    extensions: str = Field(default=".sql,.hql,.ddl", description="逗号分隔的文件扩展名过滤")
    limit: int = Field(default=500, ge=1, le=5000, description="最大扫描文件数")


class LineageScanFileResult(BaseModel):
    """单文件扫描明细。"""

    path: str = Field(description="文件路径")
    statements: int = Field(default=0, description="语句数")
    table_edges: int = Field(default=0, description="表级边数")
    field_edges: int = Field(default=0, description="字段级边数")
    ddl_edges: int = Field(default=0, description="DDL 血缘边数")
    error: str | None = Field(default=None, description="解析异常（整文件失败时）")


class LineageScanResponse(BaseModel):
    """库级扫描结果汇总。"""

    files: int = Field(description="扫描文件数")
    statements: int = Field(description="总语句数")
    table_edges: int = Field(description="表级血缘边总数")
    field_edges: int = Field(description="字段级血缘边总数")
    ddl_edges: int = Field(description="DDL 血缘边总数")
    succeeded: int = Field(description="成功文件数")
    failed: int = Field(description="失败文件数")
    dry_run: bool = Field(description="是否仅统计（未落库）")
    graph_written: bool = Field(default=False, description="非 dry_run 时图谱是否写入")
    files_detail: list[LineageScanFileResult] = Field(default_factory=list)
