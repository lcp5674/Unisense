"""血缘服务（领域编排）。

对齐 TD §12.2（血缘解析）与 DEV_GUIDE §9a（编排层在 Service 内聚合 Repository/图/事件）。
解析器为纯函数（services/lineage/parser.py）；边以 MySQL 为权威存储，Neo4j 为可选图存储。
影响分析读路径图优先（Neo4j），图不可用/降级时回退 MySQL BFS；结果经 cache-aside
缓存（Redis，TTL 60s），Redis 不可用时直接回源，不阻塞核心链路。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.config import settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.lineage import LineageEdge
from app.models.metric import Metric
from app.services.lineage.events import LineageEventPublisher
from app.services.lineage.graph import LineageGraphClient
from app.services.lineage.parser import (
    DDLEdge,
    extract_ddl_lineage,
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
    MANUAL_EDGE_TYPES,
    MANUAL_NODE_PREFIXES,
    BatchParseStatementResult,
    CoverageBrokenEdgeItem,
    CoverageOrphanItem,
    DDLEdgeItem,
    EdgeDeleteResult,
    FieldLineageItem,
    HealthDimension,
    ImpactPreviewResponse,
    LineageChannelResponse,
    LineageCoverageResponse,
    LineageEdgeDetailResponse,
    LineageEdgeHistoryResponse,
    LineageEdgeResponse,
    LineageExportParams,
    LineageHealthResponse,
    LineageImpactParams,
    LineageIngestRunResponse,
    LineageNodeInfo,
    LineageNodeResponse,
    LineageParseBatchRequest,
    LineageParseBatchResponse,
    LineageParseRequest,
    LineageParseResponse,
    LineagePathEdge,
    LineagePathItem,
    LineagePathResponse,
    LineageScanFileResult,
    LineageScanRequest,
    LineageScanResponse,
    LineageTerminalItem,
    LineageTerminalsResponse,
    ManualEdgeCreateRequest,
    ManualEdgeCreateResponse,
    OpenLineageDataset,
    OpenLineageFieldLineage,
    OpenLineageRunEvent,
    OpenLineageSchemaFacet,
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
#: 库级扫描单文件大小上限（字节，5MB）——防超大文件整读耗尽内存（P1 加固）。
_MAX_SCAN_FILE_BYTES = 5 * 1024 * 1024
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
#: 健康度「采集新鲜度」维度的满分新鲜天数：距最近采集超过该天数即线性衰减到 0 分。
_FRESHNESS_FULL_DAYS = 30
#: 标准导出（P4）：OpenLineage 生产者标识（平台 URI）、默认 namespace 与 spec URL。
#: producer 遵循 OpenLineage 规范要求（标识产生血缘事件的系统 URI）。
_OL_PRODUCER = "https://openlineage.io/namespace/unisense"
_OL_NAMESPACE = "unisense"
_OL_SCHEMA_URL = "https://openlineage.io/spec/2-0-0/OpenLineage.json"


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

    @staticmethod
    def _post_commit_queue(session: AsyncSession) -> list[Callable[[], Awaitable[None]]]:
        """取 session 上挂载的提交后副作用队列（P0-3）。

        队列挂载在**共享 session** 而非 service 实例上：同一事务内可能创建多个
        ``LineageService`` 实例（semantic/dimension 清理血缘时的临时实例），
        共用同一队列保证 commit 后统一触发，不因实例析构而丢失副作用。
        """
        q = getattr(session, "_unisense_lineage_post_commit", None)
        if q is None:
            q = []
            # B010 建议直接赋值，但 AsyncSession 未声明该属性会触发 mypy；setattr 绕开
            setattr(session, "_unisense_lineage_post_commit", q)  # noqa: B010
        return q

    def _defer(self, coro_factory: Callable[[], Awaitable[None]]) -> None:
        """注册提交后副作用（图写/缓存失效/事件），由调用方在 commit 后触发。"""
        self._post_commit_queue(self._db).append(coro_factory)

    async def run_post_commit(self) -> None:
        """执行已注册的提交后副作用（幂等：执行后清空；单侧失败不阻断其余）。

        调用约定：每个调用方在 ``await db.commit()`` 之后调用本方法，确保
        图/缓存/事件仅在事务成功落库后发生（P0-3 幽灵边根治）。
        """
        q = self._post_commit_queue(self._db)
        pending, q[:] = q[:], []
        for factory in pending:
            try:
                await factory()
            except Exception as exc:  # noqa: BLE001 - 提交后副作用 best-effort，不阻断调用方
                logger.warning("lineage_post_commit_side_effect_failed", error=str(exc))


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
        ddl_edges = extract_ddl_lineage(req.sql, req.dialect)
        if not table_edges and not field_edges and not ddl_edges:
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

        # DDL 血缘（结构变更/依赖，区别于 DML 数据流转）：结构复制/表重命名按表级边写入、
        # 列重命名按字段级边写入；DROP TABLE 触发依赖失效（软删该表上下游边），
        # ADD/DROP/MODIFY COLUMN 仅标记（响应 ddl_edges 展示，不产伪数据流转边）。
        ddl_items: list[DDLEdgeItem] = []
        for d in ddl_edges:
            if d.ddl_type in ("create_like", "create_as_copy", "rename_table"):
                if not (d.source and d.target):
                    continue
                sn = node_table(d.source)
                tn = node_table(d.target)
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
                    provenance="ddl",
                    change_reason="ddl",
                )
                stored_table += 1
                if created:
                    added += 1
                else:
                    updated += 1
                table_lineage.append(TableLineageItem(source=sn, target=tn))
                graph_edges.append((sn, tn, "DERIVED_FROM"))
            elif d.ddl_type == "rename_column" and d.table and d.source_column and d.target_column:
                sn = node_field(d.table, d.source_column)
                tn = node_field(d.table, d.target_column)
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
                    provenance="ddl",
                    change_reason="ddl",
                )
                stored_field += 1
                if created:
                    added += 1
                else:
                    updated += 1
                field_lineage.append(
                    FieldLineageItem(
                        source_table=d.table,
                        source_column=d.source_column,
                        target_table=d.table,
                        target_column=d.target_column,
                    )
                )
                graph_edges.append((sn, tn, "DERIVED_FROM"))
            elif d.ddl_type == "drop_table" and d.table:
                await self._repo.invalidate_dropped_table(node_table(d.table))
            ddl_items.append(
                DDLEdgeItem(
                    ddl_type=d.ddl_type,
                    source=d.source,
                    target=d.target,
                    table=d.table,
                    source_column=d.source_column,
                    target_column=d.target_column,
                    column=d.column,
                )
            )

        # P0-3：图写/缓存失效/事件延迟到事务提交后执行（调用方 commit 后调 run_post_commit）
        graph_written = self._graph is not None and bool(graph_edges)
        _edges_snapshot = list(graph_edges)
        _ddl_snapshot = list(ddl_edges)
        self._defer(
            lambda: self._sync_graph(
                _edges_snapshot, delete=False, context=f"parse_and_store:{req.provenance}"
            )
        )
        for sn, tn, _etype in _edges_snapshot:
            self._defer(lambda sn=sn: self._invalidate_impact_cache(sn))
            self._defer(lambda tn=tn: self._invalidate_impact_cache(tn))
        # DDL 变更事件化：破坏性 DDL（重命名/DROP）定向通知受影响资产 Owner
        self._defer(lambda: self._notify_ddl_change(_ddl_snapshot))
        # 双发：保留 Redis 裸通道（历史兼容），同时发 EventBus 供通知中心消费（best-effort）
        parsed_payload = {"table_edges": stored_table, "field_edges": stored_field}
        if self._events is not None:
            self._defer(
                lambda payload=parsed_payload: self._events.publish(
                    "lineage_parsed", payload
                )
            )
        self._defer(
            lambda payload=parsed_payload: self._eventbus.publish(
                "lineage_parsed", payload
            )
        )
        detail = {
            "kind": "sql_parse",
            "sql": req.sql,
            "dialect": req.dialect,
            "target_table": req.target_table,
            "source_node": req.source_node,
            "actor_id": actor_id,
            "table_lineage": [i.model_dump() for i in table_lineage],
            "field_lineage": [i.model_dump() for i in field_lineage],
            "ddl_edges": [i.model_dump() for i in ddl_items],
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
            ddl_edges=ddl_items,
        )

    async def parse_batch(
        self, req: LineageParseBatchRequest, actor_id: int
    ) -> LineageParseBatchResponse:
        """批量解析多条 SQL 并幂等写入血缘（企业级批量导入）。

        与 ``parse_and_store`` 单条语义对齐，但面向「一次导入一批 ETL 血缘」：
        - 逐条**独立**解析——单条语法不支持/解析异常仅标记该条 ``error``，不阻断批次；
        - 产出的边统一在**单个事务**内幂等 upsert（新增/更新计数）；
        - 循环依赖的边**跳过计数**而非抛错（批量导入场景尽量多写入）；
        - 整批写一条 ``lineage_ingest_run``（kind=batch_parse，含逐条明细快照），
          同步图存储 + 失效两端影响缓存 + 发布 ``lineage_batch_parsed`` 事件。

        ``text`` 多语句文本块与 ``statements`` 数组二选一；纯 SELECT 可经
        ``target_table`` 指定整批共用落点（方案 A+B）。
        """
        statements = req.resolved_statements
        results: list[BatchParseStatementResult] = []
        table_edges: list[Any] = []
        field_edges: list[Any] = []
        for idx, sql in enumerate(statements):
            result, te, fe = self._parse_batch_statement(idx, sql, req.dialect, req.target_table)
            results.append(result)
            table_edges.extend(te)
            field_edges.extend(fe)
        run = await self._repo.begin_ingest_run(req.provenance)
        try:
            added, updated, skipped, graph_edges = await self._store_batch_edges(
                table_edges, field_edges, req.provenance
            )
            total = added + updated
            # P0-3：图写/缓存失效/事件延迟到事务提交后执行
            graph_written = self._graph is not None and bool(graph_edges)
            _edges_snapshot = list(graph_edges)
            self._defer(
                lambda: self._sync_graph(
                    _edges_snapshot, delete=False, context=f"parse_batch:{req.provenance}"
                )
            )
            for sn, tn, _etype in _edges_snapshot:
                self._defer(lambda sn=sn: self._invalidate_impact_cache(sn))
                self._defer(lambda tn=tn: self._invalidate_impact_cache(tn))
            succeeded = sum(
                1 for r in results if r.error is None and (r.table_edges or r.field_edges)
            )
            failed = sum(1 for r in results if r.error is not None)
            payload = {
                "statements": len(statements),
                "succeeded": succeeded,
                "failed": failed,
                "added": added,
                "updated": updated,
                "skipped": skipped,
            }
            if self._events is not None:
                self._defer(
                    lambda p=payload: self._events.publish("lineage_batch_parsed", p)
                )
            self._defer(lambda p=payload: self._eventbus.publish("lineage_batch_parsed", p))
            # 运行详情快照：只保留前 N 条语句明细（detail_json 为 TEXT 列，防 64KB 超限）
            detail = {
                "kind": "batch_parse",
                "dialect": req.dialect,
                "target_table": req.target_table,
                "actor_id": actor_id,
                "statement_count": len(statements),
                "statements": [s.model_dump() for s in results][:_DETAIL_EDGE_SAMPLE],
            }
            await self._repo.finish_ingest_run(
                run,
                status="success",
                total_edges=total,
                added=added,
                updated=updated,
                skipped=skipped,
                detail=detail,
            )
            await self._db.commit()
            return LineageParseBatchResponse(
                total_statements=len(statements),
                succeeded=succeeded,
                failed=failed,
                total_edges=total,
                added=added,
                updated=updated,
                skipped=skipped,
                graph_written=graph_written,
                statements=results,
            )
        except Exception as exc:
            await self._db.rollback()
            await self._repo.finish_ingest_run(run, status="failed", error=str(exc))
            await self._db.commit()
            raise

    @staticmethod
    def _parse_batch_statement(
        idx: int, sql: str, dialect: str | None, target_table: str | None
    ) -> tuple[BatchParseStatementResult, list[Any], list[Any]]:
        """解析单条语句，返回（明细结果, 表级边, 字段级边）。

        解析异常不抛出——降级为带 ``error`` 的明细，由调用方继续处理后续语句。
        """
        try:
            te = extract_table_lineage(sql, dialect, target_table=target_table)
            fe = extract_field_lineage(sql, dialect, target_table=target_table)
        except Exception as exc:  # pragma: no cover - 防御（parser 内部已降级）
            return BatchParseStatementResult(index=idx, sql=sql, error=str(exc)), [], []
        t_items = [TableLineageItem(source=e.source, target=e.target) for e in te]
        f_items = [
            FieldLineageItem(
                source_table=e.source_table,
                source_column=e.source_column,
                target_table=e.target_table,
                target_column=e.target_column,
                expression=e.expression,
            )
            for e in fe
            if e.source_table and e.source_column and e.target_table and e.target_column
        ]
        result = BatchParseStatementResult(
            index=idx, sql=sql, table_edges=t_items, field_edges=f_items
        )
        return result, te, fe

    async def _store_batch_edges(
        self,
        table_edges: list[Any],
        field_edges: list[Any],
        provenance: str,
    ) -> tuple[int, int, int, list[tuple[str, str, str]]]:
        """事务内幂等写入批次边，返回 (added, updated, skipped, graph_edges)。

        循环依赖边跳过计数（不抛错）；与 ``parse_and_store`` 的粒度/原因约定一致。
        """
        added = 0
        updated = 0
        skipped = 0
        graph_edges: list[tuple[str, str, str]] = []
        for e in table_edges:
            sn, tn = node_table(e.source), node_table(e.target)
            probe = LineageEdge(
                source_node=sn, target_node=tn, edge_type="DERIVED_FROM", granularity="L1"
            )
            if await self._repo.would_create_cycle(probe):
                skipped += 1
                continue
            _, created = await self._repo.upsert_edge_with_status(
                source_node=sn,
                target_node=tn,
                edge_type="DERIVED_FROM",
                granularity="L1",
                provenance=provenance,
                change_reason="reparse",
            )
            if created:
                added += 1
            else:
                updated += 1
            graph_edges.append((sn, tn, "DERIVED_FROM"))
        for e in field_edges:
            if not (e.source_table and e.source_column and e.target_table and e.target_column):
                continue
            sn = node_field(e.source_table, e.source_column)
            tn = node_field(e.target_table, e.target_column)
            probe = LineageEdge(
                source_node=sn, target_node=tn, edge_type="DERIVED_FROM", granularity="L2"
            )
            if await self._repo.would_create_cycle(probe):
                skipped += 1
                continue
            _, created = await self._repo.upsert_edge_with_status(
                source_node=sn,
                target_node=tn,
                edge_type="DERIVED_FROM",
                granularity="L2",
                provenance=provenance,
                change_reason="reparse",
            )
            if created:
                added += 1
            else:
                updated += 1
            graph_edges.append((sn, tn, "DERIVED_FROM"))
        return added, updated, skipped, graph_edges

    async def scan_directory(
        self, req: LineageScanRequest, actor_id: int | None = None
    ) -> LineageScanResponse:
        """库级扫描：递归扫描 SQL 目录并解析血缘（企业级批量重建）。

        - 遍历目录下匹配扩展名的文件（上限 ``limit``），逐文件解析表级/字段级/DDL 血缘；
        - 方言：显式给定则全量使用，否则按文件内容启发式推断（LATERAL VIEW→hive、
          ARRAY JOIN/SETTINGS→clickhouse、DISTRIBUTED BY/WITH LABEL→doris 等）；
        - ``dry_run=True`` 仅统计不落库（返回逐文件明细）；False 批量幂等写入血缘
          （单事务 + 图同步 + 失效两端缓存 + 写 ``kind=scan`` 运行记录），
          结构性 DDL 边（LIKE/COPY OF/RENAME）一并写入，DROP TABLE 触发依赖失效。

        Args:
            req: 扫描请求（path/dialect/dry_run/extensions/limit）。
            actor_id: 执行人（定时任务传 None，归因 NULL）。

        Returns:
            ``LineageScanResponse`` 汇总（文件/语句/边数/成功失败/逐文件明细）。
        """
        # 路径沙箱：先查原始路径含 ``..`` 组件（abspath 会归一化掉它），再确认目录存在
        if ".." in re.split(r"[\\/]", req.path) or not os.path.isdir(req.path):
            raise ValidationError(
                f"扫描路径无效或不存在: {req.path}", error_code=ErrorCode.VALIDATION_ERROR
            )
        # P1 加固：以 realpath 确立沙箱根，防止树内符号链接指向目录外文件（信息泄露/越权读取）
        sandbox_root = os.path.realpath(req.path)
        if not os.path.isdir(sandbox_root):
            raise ValidationError(
                f"扫描路径无效或不存在: {req.path}", error_code=ErrorCode.VALIDATION_ERROR
            )
        exts = {e.strip().lower() for e in req.extensions.split(",") if e.strip()}
        files: list[str] = []
        for root, _dirs, names in os.walk(sandbox_root):
            for name in sorted(names):
                full = os.path.join(root, name)
                if not any(name.lower().endswith(e) for e in exts):
                    continue
                # 符号链接沙箱：文件 realpath 必须在沙箱根内，否则跳过（防链接逃逸）
                real = os.path.realpath(full)
                if not (real == sandbox_root or real.startswith(sandbox_root + os.sep)):
                    logger.warning("lineage_scan_skip_outside_symlink", path=full)
                    continue
                files.append(full)
                if len(files) >= req.limit:
                    break
            if len(files) >= req.limit:
                break
        files = files[: req.limit]

        table_edges: list[Any] = []
        field_edges: list[Any] = []
        ddl_all: list[Any] = []
        file_results: list[LineageScanFileResult] = []
        succeeded = 0
        for path in files:
            try:
                # P1 加固：文件大小上限，防止超大文件整读耗尽内存（CPU DoS）
                if os.path.getsize(path) > _MAX_SCAN_FILE_BYTES:
                    file_results.append(
                        LineageScanFileResult(
                            path=path, error=f"文件超限（>{_MAX_SCAN_FILE_BYTES} 字节），已跳过"
                        )
                    )
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError as exc:
                file_results.append(LineageScanFileResult(path=path, error=f"读取失败: {exc}"))
                continue
            stmts = self._count_statements(content)
            dialect = req.dialect or self._infer_scan_dialect(content)
            try:
                te = extract_table_lineage(content, dialect)
                fe = extract_field_lineage(content, dialect)
                de = extract_ddl_lineage(content, dialect)
            except Exception as exc:  # pragma: no cover - 防御（parser 内部已降级）
                file_results.append(
                    LineageScanFileResult(path=path, statements=stmts, error=str(exc))
                )
                continue
            table_edges.extend(te)
            field_edges.extend(fe)
            ddl_all.extend(de)
            file_results.append(
                LineageScanFileResult(
                    path=path,
                    statements=stmts,
                    table_edges=len(te),
                    field_edges=len(fe),
                    ddl_edges=len(de),
                )
            )
            succeeded += 1

        graph_written = False
        if not req.dry_run and (table_edges or field_edges or ddl_all):
            graph_written = await self._scan_persist(
                table_edges, field_edges, ddl_all, actor_id=actor_id
            )
        return LineageScanResponse(
            files=len(files),
            statements=sum(r.statements for r in file_results),
            table_edges=len(table_edges),
            field_edges=len(field_edges),
            ddl_edges=len(ddl_all),
            succeeded=succeeded,
            failed=len(files) - succeeded,
            dry_run=req.dry_run,
            graph_written=graph_written,
            files_detail=file_results,
        )

    @staticmethod
    def _count_statements(sql: str) -> int:
        """统计语句数（剥注释 + 分号拆分；异常回退为 1）。"""
        try:
            import sqlparse

            cleaned = sqlparse.format(sql, strip_comments=True, reindent=False)
            return len([s for s in sqlparse.split(cleaned) if s and s.strip()])
        except Exception:  # noqa: BLE001 - 统计失败不影响主流程
            return 1

    @staticmethod
    def _infer_scan_dialect(sql: str) -> str | None:
        """按文件内容启发式推断方言（显式指定时不走此逻辑）。"""
        low = sql.lower()
        if "lateral view" in low:
            return "hive"
        if "array join" in low or re.search(r"\bsettings\b", low):
            return "clickhouse"
        if "distributed by" in low or "with label" in low or "properties(" in low:
            return "doris"
        if re.search(r"\binsert\s+top\s*\(", low):
            return "tsql"
        return None

    async def _scan_persist(
        self,
        table_edges: list[Any],
        field_edges: list[Any],
        ddl_edges: list[Any],
        actor_id: int | None,
    ) -> bool:
        """扫描结果落库：批量写入 DML 边 + 结构性 DDL 边 + DROP 依赖失效 + 图同步。"""
        run = await self._repo.begin_ingest_run("scan")
        try:
            added, updated, skipped, graph_edges = await self._store_batch_edges(
                table_edges, field_edges, "scan"
            )
            for d in ddl_edges:
                if d.ddl_type in ("create_like", "create_as_copy", "rename_table"):
                    if not (d.source and d.target):
                        continue
                    sn = node_table(d.source)
                    tn = node_table(d.target)
                    probe = LineageEdge(
                        source_node=sn,
                        target_node=tn,
                        edge_type="DERIVED_FROM",
                        granularity="L1",
                    )
                    if await self._repo.would_create_cycle(probe):
                        skipped += 1
                        continue
                    _, created = await self._repo.upsert_edge_with_status(
                        source_node=sn,
                        target_node=tn,
                        edge_type="DERIVED_FROM",
                        granularity="L1",
                        provenance="ddl",
                        change_reason="scan",
                    )
                    added += 1 if created else 0
                    updated += 0 if created else 1
                    graph_edges.append((sn, tn, "DERIVED_FROM"))
                elif d.ddl_type == "drop_table" and d.table:
                    await self._repo.invalidate_dropped_table(node_table(d.table))
            total = added + updated
            # P0-3：图写/缓存失效/事件延迟到事务提交后执行
            graph_written = self._graph is not None and bool(graph_edges)
            _edges_snapshot = list(graph_edges)
            _ddl_snapshot = list(ddl_edges)
            self._defer(
                lambda: self._sync_graph(
                    _edges_snapshot, delete=False, context="scan_directory"
                )
            )
            for sn, tn, _etype in _edges_snapshot:
                self._defer(lambda sn=sn: self._invalidate_impact_cache(sn))
                self._defer(lambda tn=tn: self._invalidate_impact_cache(tn))
            # DDL 变更事件化：扫描到的破坏性 DDL 定向通知受影响资产 Owner
            self._defer(lambda: self._notify_ddl_change(_ddl_snapshot))
            detail = {
                "kind": "scan",
                "actor_id": actor_id,
                "total_edges": total,
                "table_edges": len(table_edges),
                "field_edges": len(field_edges),
                "ddl_edges": [e.ddl_type for e in ddl_edges],
            }
            await self._repo.finish_ingest_run(
                run,
                status="success",
                total_edges=total,
                added=added,
                updated=updated,
                skipped=skipped,
                detail=detail,
            )
            await self._db.commit()
            return graph_written
        except Exception as exc:
            await self._db.rollback()
            await self._repo.finish_ingest_run(run, status="failed", error=str(exc))
            await self._db.commit()
            raise

    async def query_impact(self, params: LineageImpactParams) -> list[LineageEdgeResponse]:
        """影响分析：图(Neo4j)优先读，图不可用时回退 MySQL；结果 cache-aside。

        读取顺序：Redis 缓存 -> Neo4j 图遍历 -> MySQL BFS。缓存/图任一不可用
        均静默降级，不抛错、不阻塞主流程（对齐 TD §11 韧性）。

        无前缀输入（如裸指标编码/表名）自动展开为 ``metric:/table:/field:``
        候选节点逐个查询合并——用户常直接输入指标编码而不带前缀（读取侧容错，
        写入侧手动登记仍强制前缀校验，两者不冲突）。

        Args:
            params: 影响分析参数（node/direction/max_hops）。

        Returns:
            血缘边响应列表（含 ``pii_inherited``）。
        """
        candidates = self._resolve_query_nodes(params.node)
        if len(candidates) > 1:
            # 无前缀容错路径：逐个候选查询合并去重，不写缓存（避免空结果污染主键）。
            merged: list[LineageEdgeResponse] = []
            seen: set[tuple[str, str, str]] = set()
            for cand in candidates:
                sub = LineageImpactParams(
                    node=cand, direction=params.direction, max_hops=params.max_hops
                )
                for e in await self._query_impact_sources(sub):
                    key = (e.source_node, e.target_node, e.edge_type)
                    if key not in seen:
                        seen.add(key)
                        merged.append(e)
            return merged
        cache_key = self._impact_cache_key(params.node, params.direction, params.max_hops)
        cached = await self._impact_cache_get(cache_key)
        if cached is not None:
            return cached
        edges = await self._query_impact_sources(params)
        await self._impact_cache_set(cache_key, edges)
        return edges

    @staticmethod
    def _resolve_query_nodes(node: str) -> list[str]:
        """查询节点归一化：无前缀输入展开为候选前缀节点（``metric:/table:/field:``）。

        带前缀的输入（``metric:xxx``/``table:db.t``）原样返回；无前缀时返回
        候选列表供查询侧逐个尝试。与手动登记/解析的「写入侧强制前缀」校验解耦。
        """
        if not node or ":" in node:
            return [node]
        return [f"metric:{node}", f"table:{node}", f"field:{node}"]

    async def path_query(
        self, source: str, target: str, max_hops: int = 5, limit: int = 50
    ) -> LineagePathResponse:
        """A→B 血缘路径查询（P3）：图(Neo4j)全路径优先，空/不可达回退 MySQL DFS。

        无前缀输入（如裸指标编码）自动展开为 ``metric:/table:/field:`` 候选节点
        对逐个尝试（与影响分析读侧容错一致）；带前缀则只查单对。返回路径按边数
        升序，``shortest_hops`` 为最短路径跳数。

        Args:
            source: 起点节点（可带 ``metric:/table:/field:`` 前缀）。
            target: 终点节点（同上）。
            max_hops: 最大跳数。
            limit: 返回路径条数上限。

        Returns:
            ``LineagePathResponse``（has_path/path_count/shortest_hops/paths/truncated）。
        """
        sources = self._resolve_query_nodes(source)
        targets = self._resolve_query_nodes(target)
        collected: list[tuple[list[str], list[LineagePathEdge]]] = []
        for s in sources:
            if len(collected) >= limit:
                break
            for t in targets:
                if len(collected) >= limit:
                    break
                paths = await self._path_between(s, t, max_hops, limit - len(collected))
                for nodes, edges in paths:
                    if len(collected) >= limit:
                        break
                    collected.append((nodes, edges))
        collected.sort(key=lambda item: len(item[1]))
        items = [
            LineagePathItem(nodes=nodes, edges=edges, hops=len(edges)) for nodes, edges in collected
        ]
        return LineagePathResponse(
            source=source,
            target=target,
            has_path=bool(items),
            path_count=len(items),
            shortest_hops=min((len(edges) for _, edges in collected), default=None),
            paths=items,
            truncated=len(collected) >= limit,
        )

    async def _path_between(
        self, source: str, target: str, max_hops: int, limit: int
    ) -> list[tuple[list[str], list[LineagePathEdge]]]:
        """单节点对 A→B 路径：Neo4j 全路径优先，空/不可达回退 MySQL DFS。

        Neo4j 可能只同步了部分边（L1 表级 + 指标边），``field:``/``external:``
        等 L2/L3 边仅 MySQL 权威存储——图返回空结果同样回退 MySQL 兜底。
        """
        if self._graph is not None:
            graph_paths = await self._graph.query_paths(source, target, max_hops, limit)
            if graph_paths:
                return [
                    (
                        nodes,
                        [
                            LineagePathEdge(source=s, target=t, edge_type=etype)
                            for s, t, etype in edges
                        ],
                    )
                    for nodes, edges in graph_paths
                ]
        edge_paths = await self._repo.find_paths(source, target, max_hops, limit)
        result: list[tuple[list[str], list[LineagePathEdge]]] = []
        for ep in edge_paths:
            nodes = [ep[0].source_node] + [e.target_node for e in ep]
            edges = [
                LineagePathEdge(source=e.source_node, target=e.target_node, edge_type=e.edge_type)
                for e in ep
            ]
            result.append((nodes, edges))
        return result

    async def terminal_nodes(
        self, node: str, max_hops: int = 5, limit: int = 100
    ) -> LineageTerminalsResponse:
        """下游终止节点（P3 断链定位）：从节点下游可达的无下游死端。

        图(Neo4j)优先，空/不可达回退 MySQL DFS。每个终止节点标注对应实体在
        权威库中的存在性（``entity_exists``）——实体已不存在但仍有边引用即为
        断链嫌疑（如采集目录已删、指标已删但历史边残留）。

        Args:
            node: 起点节点（可带前缀；无前缀自动展开候选，取首个有结果的）。
            max_hops: 最大搜索深度（跳数）。
            limit: 返回终止节点数上限。

        Returns:
            ``LineageTerminalsResponse``（terminals + terminal_count + truncated）。
        """
        candidates = self._resolve_query_nodes(node)
        terminals: list[tuple[str, list[str]]] = []
        for cand in candidates:
            if self._graph is not None:
                graph_terminals = await self._graph.query_terminals(cand, max_hops, limit)
                if graph_terminals is not None and graph_terminals:
                    terminals = graph_terminals
                    break
        if not terminals:
            for cand in candidates:
                rows = await self._repo.find_terminals(cand, max_hops, limit)
                if rows:
                    terminals = rows
                    break
        items: list[LineageTerminalItem] = []
        for tnode, path in terminals:
            items.append(
                LineageTerminalItem(
                    node=tnode,
                    path=path,
                    hops=max(0, len(path) - 1),
                    node_type=self._node_type_of(tnode),
                    entity_exists=await self._repo.entity_exists(tnode),
                )
            )
        return LineageTerminalsResponse(
            node=node,
            terminal_count=len(items),
            terminals=items,
            truncated=len(items) >= limit,
        )

    @staticmethod
    def _node_type_of(node: str) -> str:
        """从节点 id 前缀判定类型（table/metric/field/external/other）。"""
        for prefix, ntype in (
            ("table:", "table"),
            ("metric:", "metric"),
            ("field:", "field"),
            ("external:", "external"),
        ):
            if node.startswith(prefix):
                return ntype
        return "other"

    async def list_edges(self, node: str, direction: str = "both") -> list[LineageEdgeResponse]:
        """列出与某节点直接相关的血缘边（一跳，含 ``pii_inherited``）。

        与 ``query_impact`` 一致，对无前缀输入展开候选节点合并，避免裸编码查空。
        """
        merged: list[LineageEdgeResponse] = []
        seen: set[tuple[str, str, str]] = set()
        for cand in self._resolve_query_nodes(node):
            edges = await self._repo.query_impact(cand, direction, max_hops=1, max_edges=_MAX_EDGES)
            for e in edges:
                resp = LineageEdgeResponse.model_validate(e)
                key = (resp.source_node, resp.target_node, resp.edge_type)
                if key not in seen:
                    seen.add(key)
                    merged.append(resp)
        return merged

    async def export_lineage(self, params: LineageExportParams) -> Any:
        """标准血缘导出（P4）：OpenLineage RunEvent 列表或通用 JSON 边明细。

        供治理/合规平台以开放格式消费血缘。数据源为 MySQL 权威存储
        （``list_export_edges`` 过滤查询），按节点/方向/粒度/来源过滤后：
        - ``openlineage``：L1 表级边 → 每条一个 RunEvent（inputs=源数据集、
          outputs=目标数据集，schema facet 携带字段清单与字段级血缘 lineage 子
          facet）；L2 字段级边按目标表聚合；L3 指标/维度/消费方边是平台扩展
          语义（数据集之外），保留在 JSON 导出中。
        - ``json``：原始边明细（含 id/source_node/target_node/edge_type/...）+ 元数据
          （导出时间/边数/生产者）。

        Args:
            params: 导出参数（format/node/direction/granularity/provenance/limit）。

        Returns:
            OpenLineage 格式返回 RunEvent dict 列表；JSON 格式返回
            ``{format, producer, exported_at, edge_count, edges}``。
        """
        edges = await self._repo.list_export_edges(
            node=params.node,
            direction=params.direction,
            granularity=None if params.granularity == "all" else params.granularity,
            provenance=params.provenance,
            limit=params.limit,
        )
        if params.format == "json":
            return {
                "format": "json",
                "producer": _OL_PRODUCER,
                "exported_at": datetime.now(UTC).isoformat(),
                "edge_count": len(edges),
                "edges": [self._repo._edge_dict(e) for e in edges],
            }
        return self._export_openlineage(edges)

    @staticmethod
    def _ol_dataset_from_node(node: str) -> tuple[str, str] | None:
        """节点 id → ``(数据集名, 表名)``；仅 ``table:``/``field:`` 构成数据集。

        ``table:db.tbl`` → ``("db.tbl", "db.tbl")``；``field:db.tbl.col`` →
        ``("db.tbl", "db.tbl")``。``metric:``/``dimension:``/``consumer:``/
        ``external:`` 等前缀非数据集语义（OpenLineage 数据集是物理表/字段），
        返回 ``None``（不参与 OpenLineage 导出）。
        """
        prefix, _, value = node.partition(":")
        if prefix == "table":
            return value, value
        if prefix == "field":
            parts = value.split(".")
            if len(parts) >= 2:
                table = ".".join(parts[:-1])
                return table, table
        return None

    def _export_openlineage(self, edges: list[LineageEdge]) -> list[dict[str, Any]]:
        """血缘边 → OpenLineage RunEvent 列表（L1 表级 + L2 字段级血缘 facet）。"""
        l1 = [e for e in edges if e.granularity == "L1"]
        l2 = [e for e in edges if e.granularity == "L2"]
        # L2 按目标表分组 → 字段级血缘（schema facet 的 lineage 子 facet）
        field_lineage_by_table: dict[str, list[OpenLineageFieldLineage]] = {}
        for e in l2:
            src = self._ol_dataset_from_node(e.source_node)
            tgt = self._ol_dataset_from_node(e.target_node)
            if src is None or tgt is None:
                continue
            tgt_table, _ = tgt
            field_lineage_by_table.setdefault(tgt_table, []).append(
                OpenLineageFieldLineage(
                    name=e.target_node.split(".")[-1],
                    input_fields=[
                        {
                            "namespace": _OL_NAMESPACE,
                            "name": src[0],
                            "field": e.source_node.split(".")[-1],
                        }
                    ],
                )
            )
        events: list[dict[str, Any]] = []
        covered_targets: set[str] = set()
        for e in l1:
            src = self._ol_dataset_from_node(e.source_node)
            tgt = self._ol_dataset_from_node(e.target_node)
            if src is None or tgt is None:
                continue
            tgt_table, _ = tgt
            covered_targets.add(tgt_table)
            events.append(
                self._ol_run_event({src[0]}, tgt_table, field_lineage_by_table.get(tgt_table))
            )
        # 仅有 L2 边而无对应 L1 表级边的目标表：输入为该表各字段边的源表集合（兜底）
        for tgt_table, fls in field_lineage_by_table.items():
            if tgt_table in covered_targets:
                continue
            sources = {i["name"] for fl in fls for i in fl.input_fields}
            events.append(self._ol_run_event(sources, tgt_table, fls))
        return events

    @staticmethod
    def _ol_run_event(
        source_tables: set[str],
        target_table: str,
        fls: list[OpenLineageFieldLineage] | None,
    ) -> dict[str, Any]:
        """构造单条 RunEvent：输入数据集（源表集）+ 输出数据集（目标表 + schema 血缘）。

        ``fls`` 非空时输出数据集携带 ``schema`` facet：``fields`` 为目标列清单
        （类型未知标记 unknown），``lineage`` 为字段级血缘（输出列 → 输入列）。
        """
        facets: dict[str, OpenLineageSchemaFacet] = {}
        if fls:
            facets["schema"] = OpenLineageSchemaFacet(
                fields=[{"name": fl.name, "type": "unknown"} for fl in fls],
                lineage=fls,
            )
        return OpenLineageRunEvent(
            event_time=datetime.now(UTC).isoformat(),
            producer=_OL_PRODUCER,
            schema_url=_OL_SCHEMA_URL,
            run={"runId": uuid.uuid4().hex},
            job={
                "namespace": _OL_NAMESPACE,
                "name": f"lineage:{','.join(sorted(source_tables))}->{target_table}",
            },
            inputs=[
                OpenLineageDataset(namespace=_OL_NAMESPACE, name=t) for t in sorted(source_tables)
            ],
            outputs=[OpenLineageDataset(namespace=_OL_NAMESPACE, name=target_table, facets=facets)],
        ).model_dump(by_alias=True)

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
        # 指标↔表：差异同步（软删不再声明的落地表/源表边 + 注册新增），
        # 编辑改 source_table/source_tables 后不留残留（纯追加此前导致旧表边残留）
        source_table = definition.get("source_table")
        source_table_clean = (
            source_table if isinstance(source_table, str) and source_table else None
        )
        upstream_tables = [
            t for t in (definition.get("source_tables") or []) if isinstance(t, str) and t
        ]
        await self._repo.sync_metric_table_edges(metric_code, source_table_clean, upstream_tables)
        if source_table_clean:
            edges.append(
                await self._repo.upsert_metric_table_edge(
                    metric_code=metric_code,
                    table_node=node_table(source_table_clean),
                    direction="downstream",
                    change_reason="metric_definition",
                )
            )
        # 指标↔维度：definition_json.dimensions（字符串数组或 {code,role} 对象数组）——
        # 差异同步（软删不再声明的维度边 + 注册新增），编辑减维度/清空不留残留
        dim_codes: list[str] = []
        for dim in definition.get("dimensions") or []:
            dim_code = dim.get("code") or dim.get("dim_code") if isinstance(dim, dict) else dim
            if isinstance(dim_code, str) and dim_code:
                dim_codes.append(dim_code)
        await self._repo.sync_metric_dimension_edges(metric_code, dim_codes)
        # 指标↔字段：measure_column + measures + source_table → column 节点（差异同步）
        current_fields: list[tuple[str, str]] = []
        if isinstance(source_table, str) and source_table:
            measure_column = definition.get("measure_column")
            if isinstance(measure_column, str) and measure_column:
                current_fields.append((source_table, measure_column))
            for m in definition.get("measures") or []:
                col = m.get("name") or m.get("column") if isinstance(m, dict) else m
                if isinstance(col, str) and col:
                    current_fields.append((source_table, col))
        deleted, added = await self._repo.sync_metric_column_edges(metric_code, current_fields)
        if deleted or added:
            logger.info(
                "metric_column_edges_synced",
                metric_code=metric_code,
                deleted=deleted,
                added=added,
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

    async def sync_metric_dimension_edges(
        self, metric_code: str, current_dim_codes: list[str]
    ) -> tuple[int, int]:
        """差异同步「指标↔维度」血缘边（软删缺失 + 注册新增）。

        供指标创建/编辑/发布时以 ``definition_json.dimensions`` 为唯一事实源同步
        血缘——编辑减维度/清空时清除陈旧 USES_DIMENSION 边（区别于纯追加的
        ``register_metric_dimension_edges``）。

        Args:
            metric_code: 指标编码。
            current_dim_codes: 当前声明的维度编码列表。

        Returns:
            ``(deleted_count, added_count)``（血缘变更不提交，交由调用方事务统一提交）。
        """
        return await self._repo.sync_metric_dimension_edges(metric_code, current_dim_codes)

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
        """级联软删某节点相关的全部血缘边（数据源删除时维护一致性）。

        删除后 best-effort 同步删除图存储（Neo4j）中的对应边，并失效该节点的
        影响分析缓存——避免删除后图/缓存仍残留已失效血缘（C3/m5）。
        """
        active = await self._repo.edges_for_node(node, "both")
        deleted = await self._repo.soft_delete_by_node(node)
        await self._db.flush()
        if deleted and active:
            # P0-3：图删除延迟到事务提交后执行
            _active_snapshot = [(e.source_node, e.target_node, e.edge_type) for e in active]
            self._defer(
                lambda: self._sync_graph(
                    _active_snapshot,
                    delete=True,
                    context=f"delete_by_node:{node}",
                )
            )
        self._defer(lambda: self._invalidate_impact_cache(node))
        return deleted

    async def restore_by_node(self, node: str) -> int:
        """级联恢复某节点相关的全部软删血缘边（指标回收站恢复时对称重建）。

        恢复后 best-effort 重建图存储（Neo4j）中的对应边，并失效影响分析缓存，
        保证恢复的指标血缘立即可见（C3/m5）。
        """
        soft_deleted = await self._repo.soft_deleted_edges_for_node(node)
        restored = await self._repo.restore_by_node(node)
        await self._db.flush()
        if restored and soft_deleted:
            # P0-3：图重建延迟到事务提交后执行
            _deleted_snapshot = [
                (e.source_node, e.target_node, e.edge_type) for e in soft_deleted
            ]
            self._defer(
                lambda: self._sync_graph(
                    _deleted_snapshot,
                    delete=False,
                    context=f"restore_by_node:{node}",
                )
            )
        self._defer(lambda: self._invalidate_impact_cache(node))
        return restored

    # ---- 人工治理：手动登记 / 单边删除（TD §12.2）----

    @staticmethod
    def _validate_manual_node(node: str, field_name: str) -> None:
        """校验手动登记节点格式：须带受支持前缀（``metric:``/``table:``/...）。

        节点命名约定与后端血缘节点格式一致（``parser.node_*`` / ``node_consumer``），
        每类节点承载的信息：
        - ``metric:{code}``          指标编码
        - ``table:{db}.{tbl}``       数据表（含 schema）
        - ``column:{db}.{tbl}.{col}`` 表字段
        - ``dimension:{code}``       维度编码
        - ``consumer:{client_id}``   消费方接入方 ID
        - ``external:{name}``        外部依赖标识
        """
        if ":" not in node:
            raise ValidationError(
                f"{field_name} 节点须带类型前缀（如 table:db.orders / metric:code）",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        prefix, _, value = node.partition(":")
        if prefix not in MANUAL_NODE_PREFIXES:
            raise ValidationError(
                f"{field_name} 节点前缀 {prefix!r} 不受支持，允许：{sorted(MANUAL_NODE_PREFIXES)}",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        if not value.strip():
            raise ValidationError(
                f"{field_name} 节点 {field_name} 缺少实体标识",
                error_code=ErrorCode.VALIDATION_ERROR,
            )

    async def add_manual_edge(
        self, req: ManualEdgeCreateRequest, actor_id: int
    ) -> ManualEdgeCreateResponse:
        """手动登记一条血缘边（provenance=manual, confidence=1.0, owner=登记人）。

        人工治理入口：覆盖自动解析不到的业务依赖（外部报表/文档记载/手工 ETL 之外
        的语义关系），并在登记时写入 ``LineageEdgeHistory``（change_reason 含备注）。
        """
        self._validate_manual_node(req.source_node, "source_node")
        self._validate_manual_node(req.target_node, "target_node")
        if req.edge_type not in MANUAL_EDGE_TYPES:
            raise ValidationError(
                f"边类型 {req.edge_type!r} 不受支持，允许：{sorted(MANUAL_EDGE_TYPES)}",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        if req.source_node == req.target_node:
            raise ValidationError(
                "上游与下游不能是同一节点（自环血缘无意义）",
                error_code=ErrorCode.VALIDATION_ERROR,
            )
        granularity = self._manual_granularity(req.source_node, req.target_node)
        note = req.note.strip() if req.note else ""
        edge, created = await self._repo.upsert_edge_with_status(
            source_node=req.source_node,
            target_node=req.target_node,
            edge_type=req.edge_type,
            granularity=granularity,
            confidence=1.0,
            provenance="manual",
            change_reason=f"manual: {note}" if note else "manual",
        )
        # owner 仅登记人工边（现有 upsert 不写 owner，手动补充；getattr 兼容替身/历史行）
        if getattr(edge, "owner", None) != str(actor_id):
            edge.owner = str(actor_id)
        await self._db.flush()
        # M1: 人工登记边写图 + 失效两端影响缓存——P0-3 延迟到事务提交后执行
        _pair = [(req.source_node, req.target_node, req.edge_type)]
        self._defer(
            lambda: self._sync_graph(
                _pair,
                delete=False,
                context=f"add_manual_edge:{req.source_node}->{req.target_node}",
            )
        )
        self._defer(lambda: self._invalidate_impact_cache(req.source_node))
        self._defer(lambda: self._invalidate_impact_cache(req.target_node))
        return ManualEdgeCreateResponse(
            edge=LineageEdgeResponse.model_validate(edge), created=created
        )

    @staticmethod
    def _manual_granularity(source: str, target: str) -> str:
        """按节点类型推断手动登记边的粒度：含字段节点→L2，含指标节点→L3，否则 L1。"""
        if "column:" in source or "column:" in target:
            return "L2"
        if "metric:" in source or "metric:" in target:
            return "L3"
        return "L1"

    async def edge_domains(self, edge_id: int) -> set[str]:
        """解析某条血缘边两端节点的业务域（P1 IDOR 归属校验用）。

        边本身无 domain 列，按节点解析：``metric:`` → metric 域、``table:`` →
        数据源继承域、``field:`` → 所属表继承域、external 等无目录实体不产生域。
        返回空集表示两端均无解析域（无法判属，调用方按不阻断处理）。
        """
        edge = await self._repo.get_edge(edge_id)
        if edge is None:
            raise NotFoundError(f"血缘边不存在或已删除: id={edge_id}")
        metas = await self._repo.resolve_node_meta(
            {edge.source_node, edge.target_node}
        )
        return {m.get("domain") for m in metas.values() if m.get("domain")}

    async def delete_edge_by_id(self, edge_id: int) -> EdgeDeleteResult:
        """按主键软删单条血缘边（人工治理：误登记/断链修复的单边删除）。

        删除后 best-effort 同步删除图存储中的对应边，并失效两端影响缓存（C3/m5）。
        """
        edge = await self._repo.soft_delete_edge(edge_id)
        if edge is None:
            raise NotFoundError(f"血缘边不存在或已删除: id={edge_id}")
        await self._db.flush()
        # P0-3：图删除延迟到事务提交后执行
        _pair = [(edge.source_node, edge.target_node, edge.edge_type)]
        self._defer(
            lambda: self._sync_graph(
                _pair,
                delete=True,
                context=f"delete_edge_by_id:{edge_id}",
            )
        )
        self._defer(lambda: self._invalidate_impact_cache(edge.source_node))
        self._defer(lambda: self._invalidate_impact_cache(edge.target_node))
        return EdgeDeleteResult(
            edge_id=edge.id,
            source_node=edge.source_node,
            target_node=edge.target_node,
        )

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

    async def health_score(self) -> LineageHealthResponse:
        """血缘平台综合健康度（P2 企业级治理看板核心）。

        五维评分（各 0-100，权重见 docstring）加权总分，维度独立可解释：
        - ``coverage``（40%）：指标血缘完整度 × 0.6 + 表端到端完整度 × 0.4
        - ``broken``（20%）：1 - 断链边数 / 边总数
        - ``stale``（15%）：1 - 失效边数 / 边总数
        - ``freshness``（15%）：距最近采集的天数线性衰减（30 天满分→0），无记录为 0
        - ``reconciliation``（10%）：图-库边数偏差（|差|/较大者），图不可达时该维度
          不参与总分（其余维度权重归一化）
        """
        cov = await self.coverage_stats()
        stale_count = await self._repo.stale_edge_count()
        latest_run = await self._repo.latest_ingest_run_time()

        metric_ratio = cov.metric_with_lineage / cov.metric_total if cov.metric_total else 1.0
        # 表端到端完整度：分母用血缘边内的 table 节点数（与 no_downstream 同口径），
        # 不用采集目录 table_total（不同口径，如血缘边含大量历史/外部表时会产生负值）。
        table_ratio = 1.0
        table_in_edges = await self._repo.table_nodes_in_edges()
        if table_in_edges:
            table_ratio = 1 - cov.table_no_downstream / table_in_edges
        coverage_score = 100.0 * (metric_ratio * 0.6 + table_ratio * 0.4)

        broken_score = 100.0 * (1 - cov.broken_edges / cov.edge_total) if cov.edge_total else 100.0
        stale_score = 100.0 * (1 - stale_count / cov.edge_total) if cov.edge_total else 100.0

        freshness_score = 100.0
        if latest_run is None and cov.edge_total == 0:
            pass  # 全新平台无采集活动：中性满分，不视为瑕疵
        elif latest_run is None:
            freshness_score = 0.0  # 有边但无任何采集运行记录
        else:
            # MySQL DATETIME 返回 offset-naive：统一按 UTC 解释后再与 now(UTC) 相减
            if latest_run.tzinfo is None:
                latest_run = latest_run.replace(tzinfo=UTC)
            days = max(0.0, (datetime.now(UTC) - latest_run).total_seconds() / 86400)
            freshness_score = max(0.0, 100.0 - days * (100.0 / _FRESHNESS_FULL_DAYS))

        dimensions: dict[str, HealthDimension] = {
            "coverage": HealthDimension(
                score=round(coverage_score, 1),
                weight=0.4,
                detail={
                    "metric_total": cov.metric_total,
                    "metric_with_lineage": cov.metric_with_lineage,
                    "metric_ratio": round(metric_ratio, 4),
                    "table_total": cov.table_total,
                    "table_no_downstream": cov.table_no_downstream,
                    "table_ratio": round(table_ratio, 4),
                },
            ),
            "broken": HealthDimension(
                score=round(broken_score, 1),
                weight=0.2,
                detail={"broken_edges": cov.broken_edges, "edge_total": cov.edge_total},
            ),
            "stale": HealthDimension(
                score=round(stale_score, 1),
                weight=0.15,
                detail={"stale_edges": stale_count, "edge_total": cov.edge_total},
            ),
            "freshness": HealthDimension(
                score=round(freshness_score, 1),
                weight=0.15,
                detail={
                    "latest_run_at": latest_run.isoformat() if latest_run else None,
                    "days_since_run": (
                        round(
                            max(
                                0.0,
                                (datetime.now(UTC) - latest_run.replace(tzinfo=UTC)).total_seconds()
                                / 86400,
                            ),
                            1,
                        )
                        if latest_run
                        else None
                    ),
                },
            ),
        }

        reconciliation_score: float | None = None
        if self._graph is not None:
            graph_edges = await self._graph.count_edges()
            if graph_edges is not None:
                denom = max(cov.edge_total, graph_edges)
                drift = abs(cov.edge_total - graph_edges) / denom if denom else 0.0
                reconciliation_score = max(0.0, 100.0 - drift * 100.0)
                dimensions["reconciliation"] = HealthDimension(
                    score=round(reconciliation_score, 1),
                    weight=0.1,
                    detail={
                        "mysql_edges": cov.edge_total,
                        "graph_edges": graph_edges,
                        "drift": round(drift, 4),
                    },
                )
            else:
                dimensions["reconciliation"] = HealthDimension(
                    score=0.0, weight=0.0, detail={"reason": "graph_unavailable"}
                )
        else:
            dimensions["reconciliation"] = HealthDimension(
                score=0.0, weight=0.0, detail={"reason": "graph_not_configured"}
            )

        if reconciliation_score is not None:
            total = sum(dimensions[k].score * dimensions[k].weight for k in dimensions)
        else:
            # 图不可达：reconciliation 权重 0.1 从分母剔除，其余权重归一化
            weighted = 0.4 + 0.2 + 0.15 + 0.15
            total = (
                sum(
                    dimensions[k].score * dimensions[k].weight
                    for k in ("coverage", "broken", "stale", "freshness")
                )
                / weighted
            )
        overall = round(total, 1)
        grade = (
            "excellent"
            if overall >= 90
            else "good"
            if overall >= 75
            else "fair"
            if overall >= 60
            else "poor"
        )
        return LineageHealthResponse(
            overall_score=overall,
            grade=grade,
            dimensions=dimensions,
            edge_total=cov.edge_total,
            metric_total=cov.metric_total,
            table_total=cov.table_total,
            evaluated_at=datetime.now(UTC).isoformat(),
        )

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
            # M1: 批量入库边同步写图（幂等 MERGE）——此前 ingest_batch 只落 MySQL，
            # 图存储完全缺失这批边，影响分析图路径长期回退 MySQL BFS。
            await self._sync_graph(
                [(s, t, "DERIVED_FROM") for s, t in sorted(seen)],
                delete=False,
                context=f"ingest_batch:{provenance}",
            )
            # m5: 新增边两端失效影响缓存，导入血缘立即在影响分析中可见
            for src, tgt in added_edges:
                await self._invalidate_impact_cache(src)
                await self._invalidate_impact_cache(tgt)
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
        """确认失效边：软删权威存储，并 best-effort 同步删除图存储 + 失效两端缓存。"""
        edge = await self._repo.get_edge(edge_id)
        if edge is None:
            raise NotFoundError(f"血缘边不存在或已删除: {edge_id}")
        await self._repo.confirm_stale(edge)
        await self._db.commit()
        await self._sync_graph(
            [(edge.source_node, edge.target_node, edge.edge_type)],
            delete=True,
            context=f"confirm_stale_edge:{edge_id}",
        )
        await self._invalidate_impact_cache(edge.source_node)
        await self._invalidate_impact_cache(edge.target_node)
        return StaleEdgeResponse.model_validate(edge)

    async def restore_stale_edge(self, edge_id: int) -> StaleEdgeResponse:
        """恢复失效边：清除失效标记与观察期计数，重新参与血缘查询。

        恢复后 best-effort 重建图存储中的对应边 + 失效两端影响缓存（C3/m5）。
        """
        edge = await self._repo.get_edge(edge_id)
        if edge is None:
            raise NotFoundError(f"血缘边不存在或已删除: {edge_id}")
        await self._repo.restore_stale(edge)
        await self._db.commit()
        await self._sync_graph(
            [(edge.source_node, edge.target_node, edge.edge_type)],
            delete=False,
            context=f"restore_stale_edge:{edge_id}",
        )
        await self._invalidate_impact_cache(edge.source_node)
        await self._invalidate_impact_cache(edge.target_node)
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
                    self._graph_edge_to_response(src, tgt, etype) for src, tgt, etype in graph_edges
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

    async def _sync_graph(
        self,
        edges: list[tuple[str, str, str]],
        *,
        delete: bool = False,
        context: str = "",
    ) -> bool:
        """best-effort 同步图存储，失败时记告警日志 + 发布事件（M2）。

        所有血缘变更路径统一经此写/删 Neo4j——此前 write_edges 失败静默降级、
        不告警、不入队，图存储长期漂移无人察觉。

        Args:
            edges: ``(source_node, target_node, edge_type)`` 三元组列表。
            delete: True=删除图边，False=写入（MERGE）图边。
            context: 调用场景标记（如 ``delete_by_node:table:a``），供告警定位。

        Returns:
            图可用且同步成功返回 True；图未配置/不可达/熔断返回 False（降级）。
        """
        if self._graph is None or not edges:
            return False
        ok = (
            await self._graph.delete_edges(edges)
            if delete
            else await self._graph.write_edges(edges)
        )
        if not ok:
            logger.error(
                "lineage_graph_sync_failed",
                context=context,
                action="delete" if delete else "write",
                edge_count=len(edges),
            )
            await self._eventbus.publish(
                "lineage.graph_sync_failed",
                {
                    "context": context,
                    "action": "delete" if delete else "write",
                    "edge_count": len(edges),
                },
            )
        return ok

    async def _invalidate_impact_cache(self, node: str) -> None:
        """失效某节点的全部影响分析缓存（按 ``lineage:impact:{node}:*`` 前缀）。

        m5: 血缘变更（删除/恢复/新增）后立即失效，避免 TTL（60s）内读到已变更
        的边。Redis 不可用时静默跳过，不阻塞主流程。
        """
        redis = self._redis
        if redis is None:
            return
        try:
            keys: list[Any] = []
            cursor = 0
            pattern = f"{_CACHE_KEY_PREFIX}{node}:*"
            while True:
                cursor, batch = await redis.scan(cursor, match=pattern, count=200)
                keys.extend(batch)
                if not cursor:
                    break
            if keys:
                await redis.delete(*keys)
        except Exception as exc:
            logger.warning("lineage_impact_cache_invalidate_failed", node=node, error=str(exc))

    async def _notify_ddl_change(self, ddl_edges: list[DDLEdge]) -> None:
        """DDL 变更事件化：破坏性 DDL（重命名/DROP）定向通知受影响资产 Owner。

        治理闭环：表/列重命名、DROP TABLE 会让下游资产血缘断裂/失效，仅靠缓存
        失效用户感知不到。对每条破坏性 DDL 收集「变更对象自身 + 下游受影响资产」
        的 Owner，经 ``NotifyService.notify_user`` 定向送达（IN_APP，不依赖订阅
        偏好），并把 ``lineage.ddl_changed`` 事件发布到 EventBus 供通知中心记录/
        订阅扇出。best-effort：Owner 解析/通知失败均不阻断血缘写入主流程。
        """
        impacted: list[dict[str, Any]] = []
        for d in ddl_edges:
            changed: str | None = None
            desc = ""
            if d.ddl_type == "rename_table" and d.source and d.target:
                changed = node_table(d.source)
                desc = f"表 {d.source} 重命名为 {d.target}"
            elif d.ddl_type == "rename_column" and d.table and d.source_column and d.target_column:
                changed = node_table(d.table)
                desc = f"表 {d.table} 列 {d.source_column} 重命名为 {d.target_column}"
            elif d.ddl_type == "drop_table" and d.table:
                changed = node_table(d.table)
                desc = f"表 {d.table} 已删除（DROP）"
            if not changed:
                continue
            try:
                owners = await self._repo.affected_asset_owners(changed)
            except Exception as exc:
                logger.warning("lineage_ddl_owner_resolve_failed", node=changed, error=str(exc))
                continue
            if not owners:
                continue
            impacted.append(
                {"owners": sorted(owners), "desc": desc, "node": changed, "ddl_type": d.ddl_type}
            )
        if not impacted:
            return
        owner_count = sum(len(i["owners"]) for i in impacted)
        title = f"血缘变更：{'；'.join(i['desc'] for i in impacted[:3])}"
        body = (
            f"本次 DDL 变更影响 {owner_count} 个资产 Owner，下游血缘可能断裂。"
            f"请到血缘视图查看受影响资产并核对依赖关系。"
        )
        payload = {
            "impacted": [
                {"ddl_type": i["ddl_type"], "node": i["node"], "desc": i["desc"]} for i in impacted
            ],
        }
        # 定向通知每个受影响资产 Owner（best-effort；notify 不 commit，不影响血缘事务）
        try:
            from app.services.notify.service import NotifyService

            svc = NotifyService(self._db)
            seen: set[int] = set()
            for item in impacted:
                for owner_id in item["owners"]:
                    oid = int(owner_id)
                    if oid in seen:
                        continue
                    seen.add(oid)
                    try:
                        await svc.notify_user(
                            user_id=oid,
                            event_type="lineage.ddl_changed",
                            title=title,
                            body=body,
                            payload=payload,
                        )
                    except Exception as exc:
                        logger.warning("lineage_ddl_notify_failed", owner_id=oid, error=str(exc))
        except Exception as exc:
            logger.warning("lineage_ddl_notify_service_failed", error=str(exc))
        # 发布事件（通知中心记录 + 订阅扇出；best-effort）
        try:
            if self._events is not None:
                await self._events.publish("lineage.ddl_changed", payload)
            await self._eventbus.publish("lineage.ddl_changed", payload)
        except Exception as exc:
            logger.warning("lineage_ddl_event_publish_failed", error=str(exc))

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
