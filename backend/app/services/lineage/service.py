"""血缘服务（领域编排）。

对齐 TD §12.2（血缘解析）与 DEV_GUIDE §9a（编排层在 Service 内聚合 Repository/图/事件）。
解析器为纯函数（services/lineage/parser.py）；边以 MySQL 为权威存储，Neo4j 为可选图存储。
影响分析读路径图优先（Neo4j），图不可用/降级时回退 MySQL BFS；结果经 cache-aside
缓存（Redis，TTL 60s），Redis 不可用时直接回源，不阻塞核心链路。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.lineage import LineageEdge
from app.models.metric import Metric
from app.services.lineage.events import LineageEventPublisher
from app.services.lineage.graph import LineageGraphClient
from app.services.lineage.parser import (
    extract_field_lineage,
    extract_table_lineage,
    extract_upstream_deps,
    node_column,
    node_dimension,
    node_field,
    node_metric,
    node_table,
)
from app.services.lineage.repository import LineageRepository
from app.services.lineage.schemas import (
    CoverageBrokenEdgeItem,
    CoverageOrphanItem,
    FieldLineageItem,
    ImpactPreviewResponse,
    LineageChannelResponse,
    LineageCoverageResponse,
    LineageEdgeDetailResponse,
    LineageEdgeHistoryResponse,
    LineageEdgeResponse,
    LineageImpactParams,
    LineageIngestRunResponse,
    LineageNodeInfo,
    LineageNodeResponse,
    LineageParseRequest,
    LineageParseResponse,
    PiiImpactItem,
    StaleEdgeResponse,
    TableLineageItem,
    UpstreamDeps,
)

logger = get_logger("unisense.lineage.service")

#: 影响分析读缓存 TTL（秒）——what-if 预览与影响 API 共享，避免热点路径打爆 MySQL。
_CACHE_TTL = 60
#: 单次影响分析返回边数上限（图与 MySQL BFS 对齐）。
_MAX_EDGES = 5000
#: Redis 不可用的告警日志仅记一次，避免刷屏。
_CACHE_KEY_PREFIX = "lineage:impact:"

#: 变更类型中视为破坏性/高风险的取值（what-if 风险分级用）。
_RISKY_CHANGE_TYPES = frozenset({"BREAKING", "DROP", "DELETE", "REMOVE"})
#: 增量采集分批提交大小（控制单事务规模，大批量导入时分批 commit）。
_INGEST_COMMIT_BATCH = 500
#: 运行详情快照中保留的边明细示例条数（上限）。
#: detail_json 是 TEXT 列（64KB），全量边明细在大批量导入时会超长触发
#: MySQL 1406（Data too long），故只保留前 N 条示例 + 完整计数。
_DETAIL_EDGE_SAMPLE = 200
#: 覆盖率断链校验的边扫描上限（治理端点，避免超大批边集全量拉取）。
_MAX_COVERAGE_BROKEN_SCAN = 2000


def node_consumer(client_id: str) -> str:
    """构造消费方节点标识 ``consumer:{client_id}``（供应商=消费侧接入方）。

    与 ``parser.node_table`` / ``parser.node_metric`` 的节点约定对齐，供
    ``CONSUMED_BY`` 边两端的消费方节点（Task A）使用。
    """
    return f"consumer:{client_id}"


def paginate_edges(edges: list[LineageEdgeResponse], page: int, page_size: int) -> dict[str, Any]:
    """对血缘边列表做内存分页（图/MySQL 结果均先整体展开，再切片）。

    Args:
        edges: 完整血缘边列表。
        page: 页码（从 1 开始）。
        page_size: 每页条数。

    Returns:
        ``{items, total, page, page_size, has_more}`` 分页信封。
    """
    total = len(edges)
    start = (page - 1) * page_size
    items = edges[start : start + page_size]
    return {
        "items": [e.model_dump() for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": start + len(items) < total,
        # 节点元数据（默认空；API 层按需填充当前页节点的基础信息，供图谱点击侧边栏）
        "nodes": [],
    }


class LineageService(BaseService):
    """血缘解析与影响分析服务。"""

    def __init__(
        self,
        db: AsyncSession,
        *,
        graph: LineageGraphClient | None = None,
        events: LineageEventPublisher | None = None,
        redis: Any | None = None,
    ) -> None:
        super().__init__(db)
        self._repo = LineageRepository(db)
        self._graph = graph
        self._events = events
        self._redis = redis

    async def parse_and_store(
        self, req: LineageParseRequest, actor_id: int
    ) -> LineageParseResponse:
        """解析 SQL 并持久化血缘边（表级 + 字段级）。

        返回本次解析的边**明细**（``table_lineage`` / ``field_lineage``，供前端当页
        直接展示本次血缘效果）；并按来源通道写一条 ``lineage_ingest_run`` 运行记录
        （对齐模型注释：dp_csv / quickbi / 数据接口 / SQL 解析均记运行摘要），使
        「采集通道」视图能展示 SQL 解析的来源新鲜度与变更摘要，运行记录附带本次
        详情快照（SQL 原文/方言/落点/边明细），点击运行历史行可查看具体信息。

        纯 SELECT 语义（方案 A+B）：
        - 指定 ``target_table`` 时把查询读取的源表/投影列指向该落点，生成正式血缘并
          写入图谱（与自带 INSERT/CTAS 的解析结果一致）；
        - 无落点（未指定且 SQL 无写入目标）时**不写图谱、不写运行记录**，改为返回
          上游依赖清单（``upstream_deps``，只读展示该查询读取的表/字段）。
        """
        table_edges = extract_table_lineage(req.sql, req.dialect, target_table=req.target_table)
        field_edges = extract_field_lineage(req.sql, req.dialect, target_table=req.target_table)
        if not table_edges and not field_edges:
            # 纯 SELECT 无落点：不构成血缘边，仅返回上游依赖（方案 B），不写图谱/运行记录
            deps = extract_upstream_deps(req.sql, req.dialect)
            return LineageParseResponse(
                table_edges=0,
                field_edges=0,
                graph_written=False,
                upstream_deps=UpstreamDeps(tables=list(deps.tables), fields=list(deps.fields)),
            )

        stored_table = 0
        stored_field = 0
        added = 0
        updated = 0
        graph_edges: list[tuple[str, str, str]] = []
        table_lineage: list[TableLineageItem] = []
        field_lineage: list[FieldLineageItem] = []
        run = await self._repo.begin_ingest_run(req.provenance)

        for e in table_edges:
            sn = node_table(e.source)
            tn = node_table(e.target)
            probe = LineageEdge(
                source_node=sn, target_node=tn, edge_type="DERIVED_FROM", granularity="L1"
            )
            if await self._repo.would_create_cycle(probe):
                raise ConflictError(
                    f"血缘边 {sn} → {tn} 将形成循环依赖，已拒绝",
                    ctx={"source_node": sn, "target_node": tn},
                )
            _, created = await self._repo.upsert_edge_with_status(
                source_node=sn,
                target_node=tn,
                edge_type="DERIVED_FROM",
                granularity="L1",
                provenance=req.provenance,
                change_reason="reparse",
            )
            stored_table += 1
            if created:
                added += 1
            else:
                updated += 1
            table_lineage.append(TableLineageItem(source=sn, target=tn))
            graph_edges.append((sn, tn, "DERIVED_FROM"))

        for fe in field_edges:
            if not (fe.source_table and fe.source_column and fe.target_table and fe.target_column):
                continue
            sn = node_field(fe.source_table, fe.source_column)
            tn = node_field(fe.target_table, fe.target_column)
            probe = LineageEdge(
                source_node=sn, target_node=tn, edge_type="DERIVED_FROM", granularity="L2"
            )
            if await self._repo.would_create_cycle(probe):
                raise ConflictError(
                    f"血缘边 {sn} → {tn} 将形成循环依赖，已拒绝",
                    ctx={"source_node": sn, "target_node": tn},
                )
            _, created = await self._repo.upsert_edge_with_status(
                source_node=sn,
                target_node=tn,
                edge_type="DERIVED_FROM",
                granularity="L2",
                provenance=req.provenance,
                change_reason="reparse",
            )
            stored_field += 1
            if created:
                added += 1
            else:
                updated += 1
            field_lineage.append(
                FieldLineageItem(
                    source_table=fe.source_table,
                    source_column=fe.source_column,
                    target_table=fe.target_table,
                    target_column=fe.target_column,
                    expression=fe.expression,
                )
            )
            graph_edges.append((sn, tn, "DERIVED_FROM"))

        graph_written = False
        if self._graph is not None:
            graph_written = await self._graph.write_edges(graph_edges)
        # 双发：保留 Redis 裸通道（历史兼容），同时发 EventBus 供通知中心消费（best-effort）
        parsed_payload = {"table_edges": stored_table, "field_edges": stored_field}
        if self._events is not None:
            await self._events.publish("lineage_parsed", parsed_payload)
        await self._eventbus.publish("lineage_parsed", parsed_payload)
        detail = {
            "kind": "sql_parse",
            "sql": req.sql,
            "dialect": req.dialect,
            "target_table": req.target_table,
            "source_node": req.source_node,
            "actor_id": actor_id,
            "table_lineage": [i.model_dump() for i in table_lineage],
            "field_lineage": [i.model_dump() for i in field_lineage],
        }
        await self._repo.finish_ingest_run(
            run,
            status="success",
            total_edges=stored_table + stored_field,
            added=added,
            updated=updated,
            detail=detail,
        )
        return LineageParseResponse(
            table_edges=stored_table,
            field_edges=stored_field,
            graph_written=graph_written,
            table_lineage=table_lineage,
            field_lineage=field_lineage,
        )

    async def query_impact(self, params: LineageImpactParams) -> list[LineageEdgeResponse]:
        """影响分析：图(Neo4j)优先读，图不可用时回退 MySQL；结果 cache-aside。

        读取顺序：Redis 缓存 -> Neo4j 图遍历 -> MySQL BFS。缓存/图任一不可用
        均静默降级，不抛错、不阻塞主流程（对齐 TD §11 韧性）。

        Args:
            params: 影响分析参数（node/direction/max_hops）。

        Returns:
            血缘边响应列表（含 ``pii_inherited``）。
        """
        cache_key = self._impact_cache_key(params.node, params.direction, params.max_hops)
        cached = await self._impact_cache_get(cache_key)
        if cached is not None:
            return cached
        edges = await self._query_impact_sources(params)
        await self._impact_cache_set(cache_key, edges)
        return edges

    async def list_edges(self, node: str, direction: str = "both") -> list[LineageEdgeResponse]:
        """列出与某节点直接相关的血缘边（一跳，含 ``pii_inherited``）。"""
        edges = await self._repo.query_impact(node, direction, max_hops=1, max_edges=_MAX_EDGES)
        return [LineageEdgeResponse.model_validate(e) for e in edges]

    async def node_meta(self, node_ids: Iterable[str]) -> list[LineageNodeInfo]:
        """批量解析血缘节点基础信息（影响分析/边列表响应的 ``nodes`` 字段）。

        供前端血缘查询/影响分析图谱点击节点时在侧边栏展示具体信息（指标详情 /
        表详情），并使图节点具备 domain/pii/entity_id 属性（按业务域着色、
        PII 红色描边、表详情直达——与血缘图谱交互一致）。

        Args:
            node_ids: 血缘节点 id 集合（如 ``table:db.orders`` / ``metric:gmv``）。

        Returns:
            排序后的节点基础信息列表（无目录实体的 external/未知节点仅类型与 label）。
        """
        meta = await self._repo.resolve_node_meta(set(node_ids))
        return [LineageNodeInfo(**meta[nid]) for nid in sorted(meta)]

    # ---- 指标级（L3）血缘：注册与查询 ----

    async def get_metric_edges(
        self, metric_code: str, direction: str = "both"
    ) -> list[LineageEdgeResponse]:
        """给定指标编码返回其血缘边（一跳，上游/下游/双向）。

        内部将 ``metric_code`` 规范化为 ``metric:{code}`` 节点查询，供推荐模块血缘兜底
        与指标详情页血缘 Tab 使用（推荐方只需按 ``metric:{code}`` 节点查边即可命中）。
        """
        node = node_metric(metric_code)
        edges = await self._repo.edges_for_node(node, direction)
        return [LineageEdgeResponse.model_validate(e) for e in edges]

    async def register_metric_lineage(
        self, metric_code: str, source_table: str, *, commit: bool = True
    ) -> LineageEdge | None:
        """注册「指标→底表」血缘边（粒度 L3，幂等）。

        写入 ``metric:{code}`` → ``table:{source_table}``（DERIVED_FROM，
        provenance=metric_definition）；``source_table`` 为空/非字符串时静默跳过
        （返回 None），不抛错。

        Args:
            metric_code: 指标编码。
            source_table: 底表名（如 ``dws_metric_gmv``）。不带库前缀时按原样作为
                ``table:{source_table}`` 节点写入（与 ``scripts.sync_neo4j_assets``
                约定一致）；带 ``db.table`` 形式时直接沿用。
            commit: 是否立即提交（默认 True；批量注册时置 False 由调用方统一提交）。

        Returns:
            写入的血缘边；无有效底表名时返回 None。
        """
        if not isinstance(source_table, str) or not source_table.strip():
            logger.warning("metric_lineage_skip_empty_source_table", metric_code=metric_code)
            return None
        edge = await self._repo.upsert_metric_table_edge(
            metric_code=metric_code,
            table_node=node_table(source_table.strip()),
            change_reason="metric_definition",
        )
        if commit:
            await self._db.commit()
        return edge

    async def register_metric_from_definition(
        self, metric: Metric, *, commit: bool = True
    ) -> list[LineageEdge]:
        """从指标口径定义（``definition_json``）注册指标↔表血缘边（L3，幂等）。

        解析约定与 ``scripts.sync_neo4j_assets.parse_metric_edges`` 保持一致：
        - ``source_table``（落地/物化表）→ ``metric:{code}`` → ``table:{t}``；
        - ``source_tables``（上游源表）→ ``table:{t}`` → ``metric:{code}``。

        ``definition_json`` 缺键/类型异常/空值对应边静默跳过；返回本次写入的边列表
        （空列表表示无可注册的表血缘）。

        Args:
            metric: 指标 ORM 实体（读取 ``metric_code`` 与 ``definition_json``）。
            commit: 是否立即提交（默认 True）。
        """
        definition = metric.definition_json or {}
        if not isinstance(definition, dict):
            return []
        edges: list[LineageEdge] = []
        metric_code = metric.metric_code
        for table in definition.get("source_tables") or []:
            if isinstance(table, str) and table:
                edges.append(
                    await self._repo.upsert_metric_table_edge(
                        metric_code=metric_code,
                        table_node=node_table(table),
                        direction="upstream",
                        change_reason="metric_definition",
                    )
                )
        source_table = definition.get("source_table")
        if isinstance(source_table, str) and source_table:
            edges.append(
                await self._repo.upsert_metric_table_edge(
                    metric_code=metric_code,
                    table_node=node_table(source_table),
                    direction="downstream",
                    change_reason="metric_definition",
                )
            )
        # 指标↔维度：definition_json.dimensions（字符串数组或 {code,role} 对象数组）
        for dim in definition.get("dimensions") or []:
            dim_code = dim.get("code") or dim.get("dim_code") if isinstance(dim, dict) else dim
            if isinstance(dim_code, str) and dim_code:
                edges.append(
                    await self._repo.upsert_metric_dimension_edge(
                        metric_code=metric_code,
                        dim_node=node_dimension(dim_code),
                        change_reason="metric_definition",
                    )
                )
        # 指标↔字段：measure_column + measures + source_table → column 节点
        measure_column = definition.get("measure_column")
        if (
            isinstance(source_table, str)
            and source_table
            and isinstance(measure_column, str)
            and measure_column
        ):
            edges.append(
                await self._repo.upsert_metric_column_edge(
                    metric_code=metric_code,
                    column_node=node_column(source_table, measure_column),
                    change_reason="metric_definition",
                )
            )
        for m in definition.get("measures") or []:
            col = m.get("name") or m.get("column") if isinstance(m, dict) else m
            if isinstance(col, str) and col and isinstance(source_table, str) and source_table:
                edges.append(
                    await self._repo.upsert_metric_column_edge(
                        metric_code=metric_code,
                        column_node=node_column(source_table, col),
                        change_reason="metric_definition",
                    )
                )
        if commit and edges:
            await self._db.commit()
        return edges

    async def register_metric_dimension_edges(
        self, metric_code: str, dim_codes: list[str], *, commit: bool = True
    ) -> list[LineageEdge]:
        """注册「指标↔维度」血缘边（L3，幂等）。

        供维度绑定（``MetricDimension`` 落库处）与存量回填脚本调用；空编码静默跳过。

        Args:
            metric_code: 指标编码。
            dim_codes: 维度编码列表。
            commit: 是否立即提交（默认 True）。
        """
        edges: list[LineageEdge] = []
        for code in dim_codes:
            if isinstance(code, str) and code:
                edges.append(
                    await self._repo.upsert_metric_dimension_edge(
                        metric_code=metric_code,
                        dim_node=node_dimension(code),
                        change_reason="metric_dimension_binding",
                    )
                )
        if commit and edges:
            await self._db.commit()
        return edges

    async def register_metric_column_edge(
        self,
        metric_code: str,
        table: str,
        column: str,
        *,
        commit: bool = True,
    ) -> LineageEdge | None:
        """注册「指标↔字段」血缘边（L3，幂等）。

        表示指标来源于 ``table`` 表的 ``column`` 字段（度量列/维度列）。

        Args:
            metric_code: 指标编码。
            table: 底表名（如 ``dws_metric_gmv`` 或 ``db.table``）。
            column: 字段名。
            commit: 是否立即提交（默认 True）。

        Returns:
            写入的血缘边；表/字段为空时返回 None。
        """
        if not table or not column:
            return None
        edge = await self._repo.upsert_metric_column_edge(
            metric_code=metric_code,
            column_node=node_column(table, column),
            change_reason="metric_definition",
        )
        if commit:
            await self._db.commit()
        return edge

    async def delete_by_node(self, node: str) -> int:
        """级联软删某节点相关的全部血缘边（数据源删除时维护一致性）。"""
        return await self._repo.soft_delete_by_node(node)

    async def query_graph(
        self,
        domain: str | None = None,
        pii_only: bool = False,
        limit: int = 1000,
        provenance: str | None = None,
    ) -> dict[str, Any]:
        """血缘图谱：返回 ``nodes + edges``（力导向图渲染数据）。

        两种构建路径：
        - ``provenance`` 为空（默认）：复用资产地图的图谱拼接
          （``AssetMapRepository.graph_from_mysql``）——从 MySQL ``lineage_edge``
          + ``metric`` + ``db_catalog`` 拼装血缘专属图谱，节点以采集目录表 +
          指标为主。**注意**：DP 元数据导入的表（``wedw_dwd.tjhis_*`` 等）不在
          采集目录中时不会出现在该视图。
        - ``provenance`` 指定（如 ``dp_csv``）：从 ``lineage_edge`` 权威存储
          直接构建表级血缘图谱（``repo.graph_from_edges``），节点 = 血缘边两端的
          所有表/指标/字段（去重 + 目录元数据富集）——DP 同步 / SQL 解析等通道
          导入的表级血缘完整可见，不再受采集目录交集限制。

        Args:
            domain: 按业务域过滤节点（仅默认路径生效）。
            pii_only: 仅返回含 PII 标记的节点（仅默认路径生效）。
            limit: 返回边数软上限。
            provenance: 来源通道过滤（dp_csv / sqlglot / metric_definition）。

        Returns:
            ``{"nodes": [...], "edges": [...]}``——边为**自包含子图**（仅保留
            两端都在节点集内的边），保证返回边数与图谱实际渲染一致。
        """
        if provenance:
            # "all"=全通道表级血缘；具体通道名=仅该通道
            nodes, edges = await self._repo.graph_from_edges(
                provenance=None if provenance == "all" else provenance,
                limit=limit,
            )
            return {"nodes": nodes, "edges": edges}

        from app.services.assetmap.repository import AssetMapRepository

        nodes, edges = await AssetMapRepository(self._db).graph_from_mysql(domain, pii_only)
        # 自包含子图：仅保留两端都在节点集内的边，消除指向未渲染节点的悬空边
        # （如中间层 *_tmp / ads_* 表），避免界面“共 N 条边”与实际渲染不符。
        node_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]
        return {"nodes": nodes, "edges": edges[:limit]}

    async def impact_preview(self, metric_code: str, change_type: str) -> ImpactPreviewResponse:
        """变更影响预览（what-if）：估算变更影响面与风险等级。

        基于下游影响分析结果分类：``metric:`` 前缀计受影响指标，``table:`` 前缀
        计受影响物理表，``CONSUMED_BY`` 边计消费方；risk_level 按影响面与变更
        类型分级（critical >=20 / high >=10 或破坏性变更 / medium / low 无影响）。

        Args:
            metric_code: 拟变更的指标编码。
            change_type: 变更类型（用于风险升级判定）。

        Returns:
            ``ImpactPreviewResponse``，含 ``affected_metrics``（含 metric_code 与
            change_type）、``affected_tables``（物理表）、``affected_consumers``、
            ``risk_level``。
        """
        params = LineageImpactParams(
            node=f"metric:{metric_code}", direction="downstream", max_hops=5
        )
        edges = await self.query_impact(params)
        metrics: list[dict[str, str]] = []
        tables: list[str] = []
        consumers: set[str] = set()
        seen_metrics: set[str] = set()
        for e in edges:
            if e.edge_type == "CONSUMED_BY":
                consumers.add(e.target_node)
            if e.target_node.startswith("metric:"):
                mc = e.target_node[len("metric:") :]
                if mc not in seen_metrics:
                    seen_metrics.add(mc)
                    metrics.append({"metric_code": mc, "change_type": change_type})
            elif e.target_node.startswith("table:"):
                tables.append(e.target_node)
        risk_level = self._risk_level(len(metrics) + len(tables) + len(consumers), change_type)
        return ImpactPreviewResponse(
            affected_metrics=metrics,
            affected_tables=sorted(tables),
            affected_consumers=sorted(consumers),
            risk_level=risk_level,
        )

    async def propagate_pii(self, node: str, depth: int = 3) -> int:
        """PII 沿血缘传导：沿 ``DERIVED_FROM`` 下游遍历至多 ``depth`` 跳，标记边继承 PII。

        以 MySQL 为权威边存储逐跳展开，对命中的血缘边通过 ``repo.upsert_edge``
        重建并置 ``pii_inherited=True``（幂等，重复执行为空操作）。返回标记边数。

        Args:
            node: 起点节点（如 ``table:db.t``）。
            depth: 最大下探跳数（默认 3）。

        Returns:
            标记为 PII 继承的边数。
        """
        # TODO: repo.upsert_edge 当前未暴露 pii_inherited 形参，集成时统一对接
        # （在 repository 的 upsert_edge 增加 pii_inherited 关键字参数）；此处以
        # duck typing 透传，重建边即写库标记。
        upsert = self._repo.upsert_edge
        visited: set[str] = set()
        frontier = [node]
        marked = 0
        hops = 0
        while frontier and hops < depth:
            next_frontier: list[str] = []
            for n in frontier:
                rows = await self._repo.query_impact(
                    n, "downstream", max_hops=1, max_edges=_MAX_EDGES
                )
                for edge in rows:
                    if edge.edge_type != "DERIVED_FROM":
                        continue
                    await upsert(
                        source_node=edge.source_node,
                        target_node=edge.target_node,
                        edge_type=edge.edge_type,
                        granularity=edge.granularity,
                        confidence=edge.confidence,
                        provenance=edge.provenance,
                        pii_inherited=True,
                    )
                    marked += 1
                    if edge.target_node not in visited:
                        visited.add(edge.target_node)
                        next_frontier.append(edge.target_node)
            frontier = next_frontier
            hops += 1
        return marked

    # ---- 消费方节点注册（Task A）----

    async def register_metric_consumer(
        self, metric_code: str, client_id: str, *, commit: bool = True
    ) -> LineageEdge | None:
        """注册「指标→消费方」血缘边（CONSUMED_BY，粒度 L3，幂等）。

        写入 ``metric:{code}`` → ``consumer:{client_id}``；``client_id`` 为空/非字符串
        时静默跳过（返回 None），不抛错。由消费侧接入方向（任务说明：显式调用版）
        在授权指标订阅时调用。

        Args:
            metric_code: 指标编码。
            client_id: 消费方接入方 ID（X-Api-Key 用户名）。
            commit: 是否立即提交（默认 True；批量注册时置 False 由调用方统一提交）。

        Returns:
            写入的血缘边；无有效 client_id 时返回 None。
        """
        if not isinstance(client_id, str) or not client_id.strip():
            logger.warning("metric_consumer_skip_empty_client", metric_code=metric_code)
            return None
        edge = await self._repo.upsert_edge(
            source_node=f"metric:{metric_code}",
            target_node=node_consumer(client_id.strip()),
            edge_type="CONSUMED_BY",
            granularity="L3",
            confidence=1.0,
            provenance="metric_consumer",
            change_reason="metric_consumer",
        )
        if commit:
            await self._db.commit()
        return edge

    async def register_metric_consumers_from_db(self, metric_code: str) -> int:
        """从消费接入方表批量注册该指标的全部消费方血缘边，返回注册边数。

        查询 ApiClient 中活动且（白名单为空=域内全量 / 白名单含该指标）的接入方，
        逐条注册 ``metric:{code} → consumer:{client_id}``（CONSUMED_BY，L3，幂等）。
        供消费侧在指标上线/白名单变更时一次性补齐消费方血缘。

        Args:
            metric_code: 指标编码。

        Returns:
            注册的消费方边数。
        """
        client_ids = await self._repo.list_active_consumers_for_metric(metric_code)
        if not client_ids:
            return 0
        for client_id in client_ids:
            await self.register_metric_consumer(metric_code, client_id, commit=False)
        await self._db.commit()
        return len(client_ids)

    # ---- PII 影响面分析（Task C）----

    async def pii_impact(self, node: str, depth: int = 3) -> list[PiiImpactItem]:
        """PII 影响面分析：返回受 PII 影响的所有下游节点（供合规审计）。

        沿 ``DERIVED_FROM`` 下游遍历至多 ``depth`` 跳（数据血缘传导，与
        ``propagate_pii`` 语义一致），但**记录所有边类型**（含 CONSUMED_BY 等
        消费边）——PII 影响面报表需覆盖数据消费方。同一节点经不同路径可达时
        各记一条（审计要求保留路径），仅沿 DERIVED_FROM 继续展开以避免消费方
        子节点扩散。

        Args:
            node: 起点节点（如 ``table:db.t`` / ``metric:code``）。
            depth: 最大下探跳数（默认 3）。

        Returns:
            受 PII 影响的下游 ``PiiImpactItem`` 列表（node/edge_type/path/hops）。
        """
        items: list[PiiImpactItem] = []
        visited: set[str] = {node}
        frontier: list[tuple[str, list[str]]] = [(node, [node])]
        hops = 0
        while frontier and hops < depth:
            next_frontier: list[tuple[str, list[str]]] = []
            for n, path in frontier:
                rows = await self._repo.query_impact(
                    n, "downstream", max_hops=1, max_edges=_MAX_EDGES
                )
                for edge in rows:
                    target = edge.target_node
                    new_path = path + [target]
                    items.append(
                        PiiImpactItem(
                            node=target,
                            edge_type=edge.edge_type,
                            path=new_path,
                            hops=hops + 1,
                        )
                    )
                    # 仅沿 DERIVED_FROM（数据血缘）继续传导；消费/断链等边为终点
                    if edge.edge_type == "DERIVED_FROM" and target not in visited:
                        visited.add(target)
                        next_frontier.append((target, new_path))
            frontier = next_frontier
            hops += 1
        return items

    # ---- 血缘覆盖率治理（Task B）----

    async def coverage_stats(self) -> LineageCoverageResponse:
        """血缘覆盖率统计（治理看板核心）。

        聚合指标/表的血缘完整度、断链边数；孤儿指标数与断链明细另由
        ``coverage_orphan_metrics`` / ``coverage_broken_edges`` 提供。

        Returns:
            ``LineageCoverageResponse`` 各类计数。
        """
        broken = await self._repo.coverage_broken_edges(limit=_MAX_COVERAGE_BROKEN_SCAN)
        metric_total = await self._repo.metric_total()
        metric_with_lineage = len(await self._repo.metric_codes_with_lineage())
        return LineageCoverageResponse(
            metric_total=metric_total,
            metric_with_lineage=metric_with_lineage,
            metric_orphan=max(0, metric_total - metric_with_lineage),
            table_total=await self._repo.table_total(),
            table_no_downstream=await self._repo.table_no_downstream_count(),
            edge_total=await self._repo.edge_total(),
            broken_edges=len(broken),
        )

    async def coverage_orphan_metrics(self) -> list[CoverageOrphanItem]:
        """无任何血缘边的孤立指标清单（预案式治理对象）。

        Returns:
            ``[{metric_code, domain}]``。
        """
        with_lineage = await self._repo.metric_codes_with_lineage()
        return [
            CoverageOrphanItem(metric_code=code, domain=domain)
            for code, domain in await self._repo.all_metric_rows()
            if code not in with_lineage
        ]

    async def coverage_broken_edges(
        self, limit: int = _MAX_COVERAGE_BROKEN_SCAN
    ) -> list[CoverageBrokenEdgeItem]:
        """断链边明细（source 节点对应实体已不存在），供人工修复跳转。

        Args:
            limit: 返回条数上限。

        Returns:
            ``[CoverageBrokenEdgeItem, ...]``。
        """
        rows = await self._repo.coverage_broken_edges(limit=limit)
        return [CoverageBrokenEdgeItem.model_validate(r) for r in rows]

    # ---- 血缘边详情（Task D）----

    async def edge_detail(self, edge_id: int) -> LineageEdgeDetailResponse:
        """单条血缘边 + 其变更历史（边元数据查询）。

        按主键取未删除边；缺失抛 ``NotFoundError``。变更历史按该边唯一键
        （source/target/edge_type/granularity）倒序取快照。

        Args:
            edge_id: 血缘边主键。

        Returns:
            ``LineageEdgeDetailResponse``（edge 当前值 + history 变更历史）。
        """
        edge = await self._repo.get_edge(edge_id)
        if edge is None:
            raise NotFoundError(f"血缘边不存在或已删除: {edge_id}")
        history = await self._repo.edge_history_by_key(
            edge.source_node, edge.target_node, edge.edge_type, edge.granularity
        )
        return LineageEdgeDetailResponse(
            edge=LineageEdgeResponse.model_validate(edge),
            history=[LineageEdgeHistoryResponse.model_validate(h) for h in history],
        )

    # ---- 增量采集与采集通道（TD §12.2）----

    async def ingest_batch(
        self,
        provenance: str,
        edges: set[tuple[str, str]],
        *,
        threshold: int | None = None,
        change_reason: str = "ingest",
    ) -> dict[str, Any]:
        """增量采集一批表级血缘边，并记录运行摘要与失效观察。

        供各来源通道（dp_csv / quickbi / 数据接口）统一调用：
        1. 逐条幂等 upsert（返回 created 标记 → 新增/更新计数）；
        2. ``mark_seen`` 刷新已见边的 ``last_seen_at`` 并恢复既往失效边；
        3. ``mark_missing`` 对未再出现的边累加观察期计数，达到阈值进入失效队列
           （不直接删除，防"本次未采到"误删真实血缘）；
        4. 写一条 ``lineage_ingest_run`` 运行记录（变更摘要审计）。

        Args:
            provenance: 来源通道标识（如 ``dp_csv``）。
            edges: 本次采集确认存在的 ``(source_node, target_node)`` 集合。
            threshold: 失效观察期（连续未确认轮次）；缺省取配置
                ``lineage_stale_observation_runs``。
            change_reason: 变更历史原因标记（默认 ``ingest``）。

        Returns:
            变更摘要 ``{run_id, source, total_edges, added, updated, missing,
            stale_flagged, restored}``。
        """
        threshold = (
            threshold if threshold is not None else int(settings.lineage_stale_observation_runs)
        )
        run = await self._repo.begin_ingest_run(provenance)
        added = 0
        updated = 0
        added_edges: list[list[str]] = []
        updated_edges: list[list[str]] = []
        seen: set[tuple[str, str]] = set()
        try:
            for source, target in sorted(edges):
                src_node = node_table(source)
                tgt_node = node_table(target)
                _, created = await self._repo.upsert_edge_with_status(
                    source_node=src_node,
                    target_node=tgt_node,
                    edge_type="DERIVED_FROM",
                    granularity="L1",
                    confidence=1.0,
                    provenance=provenance,
                    change_reason=change_reason,
                )
                seen.add((src_node, tgt_node))
                if created:
                    added += 1
                    added_edges.append([src_node, tgt_node])
                else:
                    updated += 1
                    updated_edges.append([src_node, tgt_node])
                if (added + updated) % _INGEST_COMMIT_BATCH == 0:
                    await self._db.commit()
            confirmed, restored = await self._repo.mark_seen(provenance, seen)
            missing, stale_flagged = await self._repo.mark_missing(provenance, seen, threshold)
            # 运行详情快照：记录本次新增/更新的具体边明细，供「运行历史行 → 详情」查看。
            # 大批量导入只保留前 N 条示例（完整计数在 added/updated 字段），
            # 避免全量明细序列化超 detail_json TEXT 列上限（MySQL 1406）。
            detail = {
                "kind": "batch",
                "added_edges": added_edges[:_DETAIL_EDGE_SAMPLE],
                "updated_edges": updated_edges[:_DETAIL_EDGE_SAMPLE],
            }
            await self._repo.finish_ingest_run(
                run,
                status="success",
                total_edges=len(seen),
                added=added,
                updated=updated,
                missing=missing,
                stale_flagged=stale_flagged,
                restored=restored,
                detail=detail,
            )
            await self._db.commit()
            # 双发：保留 Redis 裸通道（历史兼容），同时发 EventBus 供通知中心消费（best-effort）
            ingested_payload = {
                "source": provenance,
                "added": added,
                "updated": updated,
                "missing": missing,
                "stale_flagged": stale_flagged,
                "restored": restored,
            }
            if self._events is not None:
                await self._events.publish("lineage_ingested", ingested_payload)
            await self._eventbus.publish("lineage_ingested", ingested_payload)
            return {
                "run_id": run.id,
                "source": provenance,
                "total_edges": len(seen),
                "added": added,
                "updated": updated,
                "missing": missing,
                "stale_flagged": stale_flagged,
                "restored": restored,
            }
        except Exception as exc:
            await self._db.rollback()
            await self._repo.finish_ingest_run(run, status="failed", error=str(exc))
            await self._db.commit()
            raise

    async def list_channels(self) -> list[LineageChannelResponse]:
        """血缘采集通道总览（按来源聚合边数/节点数/失效数/最近运行）。"""
        rows = await self._repo.list_channels()
        return [LineageChannelResponse(**r) for r in rows]

    async def list_ingest_runs(
        self, source: str, limit: int = 20
    ) -> list[LineageIngestRunResponse]:
        """某来源通道的采集运行历史（按时间倒序）。"""
        runs = await self._repo.list_ingest_runs(source, limit)
        return [LineageIngestRunResponse.model_validate(r) for r in runs]

    async def get_ingest_run_detail(self, run_id: int) -> LineageIngestRunResponse:
        """取单条采集运行记录（含详情快照），供「运行历史行 → 详情」展示。

        运行记录以 ``detail_json`` 文本列存结构化快照（SQL 解析：SQL 原文/方言/落点/
        边明细；批量采集：变更边明细），此处反序列化到响应的 ``detail`` 字段；
        快照缺失/损坏时降级为 None（仍返回计数摘要，不抛错）。
        """
        run = await self._repo.get_ingest_run(run_id)
        if run is None:
            raise NotFoundError(f"采集运行记录不存在: {run_id}")
        resp = LineageIngestRunResponse.model_validate(run)
        if run.detail_json:
            try:
                resp.detail = json.loads(run.detail_json)
            except (TypeError, ValueError):
                logger.warning("lineage_run_detail_decode_failed", run_id=run_id)
                resp.detail = None
        return resp

    async def list_stale(
        self, source: str | None = None, limit: int = 200
    ) -> list[StaleEdgeResponse]:
        """失效队列：连续未被确认、待人工处置的血缘边。"""
        edges = await self._repo.list_stale_edges(source, limit)
        return [StaleEdgeResponse.model_validate(e) for e in edges]

    async def list_nodes(self, kw: str | None = None, limit: int = 50) -> list[LineageNodeResponse]:
        """血缘候选节点（影响分析/血缘查询选项框预加载与关键词搜索）。

        无 ``kw`` 时返回参与边数最多的 top-N 节点（预加载常用节点）；带 ``kw`` 时
        按节点 id 模糊过滤，供用户输入关键词搜索指定节点。
        """
        rows = await self._repo.list_nodes(kw=kw, limit=limit)
        return [self._node_to_response(node, count) for node, count in rows]

    @staticmethod
    def _node_to_response(node: str, count: int) -> LineageNodeResponse:
        """将节点 id（如 ``table:db.orders``）映射为展示模型（去前缀 + 类型）。"""
        for prefix, ntype in (
            ("table:", "table"),
            ("metric:", "metric"),
            ("field:", "field"),
            ("external:", "external"),
        ):
            if node.startswith(prefix):
                return LineageNodeResponse(
                    id=node, label=node[len(prefix) :], type=ntype, count=count
                )
        return LineageNodeResponse(id=node, label=node, type="other", count=count)

    async def confirm_stale_edge(self, edge_id: int) -> StaleEdgeResponse:
        """确认失效边：软删权威存储，并 best-effort 同步删除图存储。"""
        edge = await self._repo.get_edge(edge_id)
        if edge is None:
            raise NotFoundError(f"血缘边不存在或已删除: {edge_id}")
        await self._repo.confirm_stale(edge)
        await self._db.commit()
        if self._graph is not None:
            await self._graph.delete_edges([(edge.source_node, edge.target_node, edge.edge_type)])
        return StaleEdgeResponse.model_validate(edge)

    async def restore_stale_edge(self, edge_id: int) -> StaleEdgeResponse:
        """恢复失效边：清除失效标记与观察期计数，重新参与血缘查询。"""
        edge = await self._repo.get_edge(edge_id)
        if edge is None:
            raise NotFoundError(f"血缘边不存在或已删除: {edge_id}")
        await self._repo.restore_stale(edge)
        await self._db.commit()
        return StaleEdgeResponse.model_validate(edge)

    # ---- 内部方法 ----

    async def _query_impact_sources(self, params: LineageImpactParams) -> list[LineageEdgeResponse]:
        """图优先 + MySQL 兜底的影响分析读路径（缓存未命中时调用）。

        图查询返回**空列表**（图可达但该节点在图中无数据，如仅写入 MySQL 的
        导入血缘）同样回退 MySQL——否则导入的边在前端永远不可见。仅当图不可达/
        熔断/异常（返回 None）才直接使用图结果语义，空结果一律回退权威 MySQL。
        """
        if self._graph is not None:
            graph_edges = await self._graph.query_impact(
                params.node, params.direction, params.max_hops, _MAX_EDGES
            )
            if graph_edges is not None and graph_edges:
                merged = [
                    self._graph_edge_to_response(src, tgt, etype)
                    for src, tgt, etype in graph_edges
                ]
                # 补充图存储未覆盖的「指标↔维度/字段」边（仅 MySQL 权威存储，L3）。
                # 图边以表/指标/消费方为主；维度/字段边由指标定义/回填写入 MySQL，
                # 图路径需合并权威库结果，否则影响分析与血缘图谱会漏掉这两类关系。
                seen = {(e.source_node, e.target_node, e.edge_type) for e in merged}
                for e in await self._repo.edges_for_node(params.node, params.direction):
                    if e.edge_type in ("USES_DIMENSION", "READS_COLUMN"):
                        resp = LineageEdgeResponse.model_validate(e)
                        key = (resp.source_node, resp.target_node, resp.edge_type)
                        if key not in seen:
                            seen.add(key)
                            merged.append(resp)
                return merged
        rows = await self._repo.query_impact(
            params.node, params.direction, params.max_hops, max_edges=_MAX_EDGES
        )
        return [LineageEdgeResponse.model_validate(e) for e in rows]

    @staticmethod
    def _graph_edge_to_response(source: str, target: str, edge_type: str) -> LineageEdgeResponse:
        """将图读路径返回的 ``(source, target, edge_type)`` 组装为响应。

        id 在图存储无意义，置 0；granularity 由节点前缀推断（``field:`` 视为 L2）。
        """
        granularity = "L2" if source.startswith("field:") or target.startswith("field:") else "L1"
        return LineageEdgeResponse(
            id=0,
            source_node=source,
            target_node=target,
            edge_type=edge_type,
            granularity=granularity,
            confidence=1.0,
            provenance="neo4j",
            pii_inherited=False,
        )

    @staticmethod
    def _impact_cache_key(node: str, direction: str, hops: int) -> str:
        return f"{_CACHE_KEY_PREFIX}{node}:{direction}:{hops}"

    async def _impact_cache_get(self, key: str) -> list[LineageEdgeResponse] | None:
        """读影响分析缓存；Redis 不可用/熔断/解析失败时返回 None（回源）。"""
        redis = self._redis
        if redis is None:
            return None
        try:
            raw = await redis.get(key)
        except Exception as exc:
            logger.warning("lineage_impact_cache_get_failed", key=key, error=str(exc))
            return None
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.warning("lineage_impact_cache_decode_failed", key=key, error=str(exc))
            return None
        return [LineageEdgeResponse.model_validate(d) for d in payload]

    async def _impact_cache_set(self, key: str, edges: list[LineageEdgeResponse]) -> None:
        """回写影响分析缓存；Redis 不可用/写失败时静默跳过，不阻断主流程。"""
        redis = self._redis
        if redis is None:
            return
        try:
            payload = json.dumps([e.model_dump(mode="json") for e in edges], ensure_ascii=False)
            await redis.set(key, payload, ex=_CACHE_TTL)
        except Exception as exc:
            logger.warning("lineage_impact_cache_set_failed", key=key, error=str(exc))

    @staticmethod
    def _risk_level(impact_count: int, change_type: str) -> str:
        """按影响面与变更类型分级风险：critical / high / medium / low。"""
        if impact_count == 0:
            return "low"
        if impact_count >= 20:
            return "critical"
        if impact_count >= 10:
            return "high"
        if change_type.upper() in _RISKY_CHANGE_TYPES:
            return "high"
        return "medium"
