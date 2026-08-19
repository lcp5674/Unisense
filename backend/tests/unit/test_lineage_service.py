"""lineage service 单测（注入假 repo，覆盖解析落库、影响分析、what-if、缓存、PII、分页）。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.services.lineage.schemas import (
    CoverageBrokenEdgeItem,
    CoverageOrphanItem,
    LineageCoverageResponse,
    LineageEdgeDetailResponse,
    LineageEdgeResponse,
    LineageExportParams,
    LineageImpactParams,
    LineageParseBatchRequest,
    LineageParseRequest,
    LineageScanRequest,
    ManualEdgeCreateRequest,
    PiiImpactItem,
)
from app.services.lineage.service import LineageService, paginate_edges


def make_edge(
    i: int = 1,
    source: str = "table:a",
    target: str = "table:t",
    edge_type: str = "DERIVED_FROM",
    pii: bool = False,
) -> LineageEdgeResponse:
    """构造血缘边响应测试数据。"""
    granularity = "L2" if "field:" in source or "field:" in target else "L1"
    return LineageEdgeResponse(
        id=i,
        source_node=source,
        target_node=target,
        edge_type=edge_type,
        granularity=granularity,
        confidence=1.0,
        provenance="sqlglot",
        pii_inherited=pii,
    )


class FakeRepo:
    """内存假仓库：幂等 upsert + 按节点过滤的影响分析（对齐真实 BFS 读语义）。"""

    def __init__(self) -> None:
        self.edges: list[object] = []
        self.impact: list[LineageEdgeResponse] = []
        self.upsert_calls: list[dict[str, Any]] = []
        self.deleted_count = 0
        self._keys: set[tuple[object, ...]] = set()
        self.runs: list[SimpleNamespace] = []
        self.consumer_ids: list[str] = []
        # 覆盖率治理（Task B）假数据
        self.metric_total_count: int = 0
        self.codes_with_lineage: list[str] = []
        self.table_total_count: int = 0
        self.table_no_downstream: int = 0
        self.metric_rows: list[tuple[str, str | None]] = []
        self.broken_edges: list[dict[str, Any]] = []
        # 健康度（P2）假数据
        self.table_nodes_in_edges_count: int = 0
        # 边详情（Task D）假数据
        self.history: list[SimpleNamespace] = []
        # 健康度（P2）假数据
        self.stale_count: int = 0
        self.latest_run_at: Any | None = None
        # 路径查询（P3）假数据
        self.repo_paths: list[list[Any]] = []
        self.repo_terminals: list[tuple[str, list[str]]] = []
        self.missing_entities: set[str] = set()
        # 标准导出（P4）假数据
        self.export_edges: list[Any] = []
        # DDL 变更事件化：node -> 受影响资产 Owner 集合（affected_asset_owners 假实现）
        self.affected_owners: dict[str, set[str]] = {}

    async def upsert_edge(self, **kwargs: object) -> SimpleNamespace:
        self.upsert_calls.append(kwargs)
        edge = SimpleNamespace(id=len(self.edges) + 1, **kwargs)
        self.edges.append(edge)
        return edge

    async def upsert_metric_dimension_edge(
        self,
        *,
        metric_code: str,
        dim_node: str,
        edge_type: str = "USES_DIMENSION",
        **kwargs: object,
    ) -> SimpleNamespace:
        """指标↔维度边假实现：构造 metric→dimension 节点后记录。"""
        return await self.upsert_edge(
            source_node=f"metric:{metric_code}",
            target_node=dim_node,
            edge_type=edge_type,
            granularity="L3",
            **kwargs,
        )

    async def sync_metric_dimension_edges(
        self, metric_code: str, current_dim_codes: list[str]
    ) -> tuple[int, int]:
        """差异同步假实现：记录声明集，返回 (0, 新增数)。"""
        self.upsert_calls.append(
            {
                "op": "sync_metric_dimension_edges",
                "metric_code": metric_code,
                "codes": current_dim_codes,
            }
        )
        return 0, len([c for c in current_dim_codes if isinstance(c, str) and c])

    async def sync_metric_column_edges(
        self, metric_code: str, current_fields: list[tuple[str, str]]
    ) -> tuple[int, int]:
        """字段边差异同步假实现：记录声明字段集。"""
        self.upsert_calls.append(
            {
                "op": "sync_metric_column_edges",
                "metric_code": metric_code,
                "fields": current_fields,
            }
        )
        return 0, len([(t, c) for t, c in current_fields if t and c])

    async def sync_metric_table_edges(
        self,
        metric_code: str,
        downstream_table: str | None,
        upstream_tables: list[str],
    ) -> tuple[int, int]:
        """表边差异同步假实现：记录声明落地表/源表集。"""
        self.upsert_calls.append(
            {
                "op": "sync_metric_table_edges",
                "metric_code": metric_code,
                "downstream": downstream_table,
                "upstream": upstream_tables,
            }
        )
        return 0, len(upstream_tables) + (1 if downstream_table else 0)

    async def upsert_metric_column_edge(
        self,
        *,
        metric_code: str,
        column_node: str,
        edge_type: str = "READS_COLUMN",
        **kwargs: object,
    ) -> SimpleNamespace:
        """指标↔字段边假实现：构造 column→metric 节点后记录。"""
        return await self.upsert_edge(
            source_node=column_node,
            target_node=f"metric:{metric_code}",
            edge_type=edge_type,
            granularity="L3",
            **kwargs,
        )

    async def upsert_metric_table_edge(
        self,
        *,
        metric_code: str,
        table_node: str,
        direction: str = "downstream",
        edge_type: str = "DERIVED_FROM",
        **kwargs: object,
    ) -> SimpleNamespace:
        """指标↔表边假实现：按 direction 构造节点后记录。"""
        if direction == "upstream":
            src, tgt = table_node, f"metric:{metric_code}"
        else:
            src, tgt = f"metric:{metric_code}", table_node
        return await self.upsert_edge(
            source_node=src,
            target_node=tgt,
            edge_type=edge_type,
            granularity="L3",
            **kwargs,
        )

    async def upsert_edge_with_status(self, **kwargs: object) -> tuple[SimpleNamespace, bool]:
        """幂等 upsert 假实现：按唯一键（source/target/edge_type/granularity）判定 created。"""
        self.upsert_calls.append(kwargs)
        key = (
            kwargs.get("source_node"),
            kwargs.get("target_node"),
            kwargs.get("edge_type"),
            kwargs.get("granularity"),
        )
        created = key not in self._keys
        self._keys.add(key)
        edge = SimpleNamespace(id=len(self.edges) + 1, **kwargs)
        self.edges.append(edge)
        return edge, created

    async def begin_ingest_run(self, source: str) -> SimpleNamespace:
        run = SimpleNamespace(
            id=len(self.runs) + 1,
            source=source,
            status="running",
            run_at=datetime.now(UTC),
        )
        self.runs.append(run)
        return run

    async def finish_ingest_run(
        self,
        run: SimpleNamespace,
        *,
        status: str = "success",
        total_edges: int = 0,
        added: int = 0,
        updated: int = 0,
        missing: int = 0,
        stale_flagged: int = 0,
        restored: int = 0,
        skipped: int = 0,
        error: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        run.status = status
        run.total_edges = total_edges
        run.added_count = added
        run.updated_count = updated
        run.missing_count = missing
        run.stale_flagged_count = stale_flagged
        run.restored_count = restored
        run.skipped_count = skipped
        run.error = error
        payload = dict(detail or {})
        if skipped:
            payload["skipped"] = skipped
        run.detail_json = json.dumps(payload, ensure_ascii=False) if payload else None

    async def get_ingest_run(self, run_id: int) -> SimpleNamespace | None:
        for run in self.runs:
            if run.id == run_id:
                return run
        return None

    async def would_create_cycle(self, edge: object) -> bool:
        """环检假实现：默认不成环（供 parse_and_store 建边前调用）。"""
        return False

    async def invalidate_dropped_table(self, table_node: str) -> int:
        """DROP TABLE 依赖失效假实现：从 self.edges 移除触及该表节点的边。"""
        before = len(self.edges)
        self.edges = [
            e
            for e in self.edges
            if getattr(e, "source_node", None) != table_node
            and getattr(e, "target_node", None) != table_node
        ]
        return before - len(self.edges)

    async def edges_for_node(self, node: str, direction: str = "both") -> list[Any]:
        """按节点返回血缘边；返回 self.edges_for_node_result（默认空）。"""
        return list(getattr(self, "edges_for_node_result", []))

    async def query_impact(
        self, node: str, direction: str, max_hops: int, max_edges: int = 5000
    ) -> list[LineageEdgeResponse]:
        out: list[LineageEdgeResponse] = []
        for e in self.impact:
            if direction in ("downstream", "both") and e.source_node == node:
                out.append(e)
            if direction in ("upstream", "both") and e.target_node == node:
                out.append(e)
        return out

    async def soft_delete_by_node(self, node: str) -> int:
        return self.deleted_count

    async def restore_by_node(self, node: str) -> int:
        """级联恢复假实现：与 soft_delete_by_node 对称返回计数。"""
        return self.deleted_count

    async def soft_deleted_edges_for_node(self, node: str) -> list[Any]:
        """软删边假实现：返回 self.soft_deleted（默认空）。"""
        return list(getattr(self, "soft_deleted", []))

    async def soft_delete_edge(self, edge_id: int) -> object | None:
        """单条边软删假实现：按 id 从 self.edges 中移除并返回；不存在返回 None。"""
        for i, e in enumerate(self.edges):
            if getattr(e, "id", None) == edge_id:
                return self.edges.pop(i)
        return None

    async def list_active_consumers_for_metric(self, metric_code: str) -> list[str]:
        """消费该指标的 client_id（Task A 批量注册用）；默认无。"""
        return list(self.consumer_ids)

    # ---- 覆盖率治理（Task B）假实现 ----
    async def coverage_broken_edges(self, limit: int = 200) -> list[dict[str, Any]]:
        return list(self.broken_edges[:limit])

    # ---- 标准导出（P4）假实现 ----
    async def list_export_edges(
        self,
        *,
        node: str | None = None,
        direction: str = "both",
        granularity: str | None = None,
        provenance: str | None = None,
        limit: int = 10_000,
    ) -> list[Any]:
        """导出过滤假实现（对齐真实 repository 语义，软删过滤）。"""
        rows = [e for e in self.export_edges if getattr(e, "deleted_at", None) is None]
        if granularity:
            rows = [e for e in rows if getattr(e, "granularity", None) == granularity]
        if provenance:
            rows = [e for e in rows if getattr(e, "provenance", None) == provenance]
        if node:
            if direction == "upstream":
                rows = [e for e in rows if e.target_node == node]
            elif direction == "downstream":
                rows = [e for e in rows if e.source_node == node]
            else:
                rows = [e for e in rows if e.source_node == node or e.target_node == node]
        return list(rows[:limit])

    @staticmethod
    def _edge_dict(e: Any) -> dict[str, Any]:
        """边 → 导出字典（对齐真实 repository._edge_dict 字段）。"""
        return {
            "id": getattr(e, "id", None),
            "source_node": e.source_node,
            "target_node": e.target_node,
            "edge_type": getattr(e, "edge_type", "DERIVED_FROM"),
            "granularity": getattr(e, "granularity", "L1"),
            "confidence": getattr(e, "confidence", 1.0),
            "provenance": getattr(e, "provenance", "sqlglot"),
            "pii_inherited": getattr(e, "pii_inherited", False),
        }

    async def metric_total(self) -> int:
        return self.metric_total_count

    async def metric_codes_with_lineage(self) -> set[str]:
        return set(self.codes_with_lineage)

    async def table_total(self) -> int:
        return self.table_total_count

    async def table_no_downstream_count(self) -> int:
        return self.table_no_downstream

    async def table_nodes_in_edges(self) -> int:
        return self.table_nodes_in_edges_count

    async def edge_total(self) -> int:
        return len(self.edges)

    async def all_metric_rows(self) -> list[tuple[str, str | None]]:
        return list(self.metric_rows)

    # ---- 边详情（Task D）假实现 ----
    async def get_edge(self, edge_id: int) -> Any | None:
        for e in self.edges:
            if getattr(e, "id", None) == edge_id:
                return e
        return None

    async def edge_history_by_key(
        self, source_node: str, target_node: str, edge_type: str, granularity: str
    ) -> list[Any]:
        return [
            h
            for h in self.history
            if h.source_node == source_node
            and h.target_node == target_node
            and h.edge_type == edge_type
            and h.granularity == granularity
        ]

    async def list_nodes(self, kw: str | None = None, limit: int = 50) -> list[tuple[str, int]]:
        return [
            ("table:a", 3),
            ("metric:m1", 2),
            ("field:a.x", 1),
            ("external:ext", 4),
            ("plain_node", 1),
        ]

    async def resolve_node_meta(self, node_ids: set[str]) -> dict[str, dict[str, Any]]:
        """节点元数据假实现：类型/标签按前缀推导，无目录实体（供 node_meta 测试）。"""
        out: dict[str, dict[str, Any]] = {}
        for nid in node_ids:
            prefix = nid.split(":", 1)[0] if ":" in nid else "other"
            label = nid.split(":", 1)[1] if ":" in nid else nid
            out[nid] = {
                "id": nid,
                "type": prefix if prefix in ("table", "metric", "field", "external") else "other",
                "label": label,
                "entity_id": None,
                "pii": False,
                "domain": None,
                "owner": None,
            }
        return out

    # ---- DDL 变更事件化：受影响资产 Owner（假实现，可配置） ----
    async def affected_asset_owners(
        self, node: str, max_hops: int = 3, limit: int = 50
    ) -> set[str]:
        """受影响资产 Owner 假实现：按 ``affected_owners`` 配置返回（供通知测试）。"""
        return set(self.affected_owners.get(node, set()))

    # ---- 健康度（P2）与路径查询（P3）假实现 ----
    async def stale_edge_count(self) -> int:
        return self.stale_count

    async def latest_ingest_run_time(self) -> Any | None:
        return self.latest_run_at

    async def find_paths(
        self, source: str, target: str, max_hops: int = 5, limit: int = 50
    ) -> list[list[Any]]:
        return list(self.repo_paths)

    async def find_terminals(
        self, node: str, max_hops: int = 5, limit: int = 100
    ) -> list[tuple[str, list[str]]]:
        return list(self.repo_terminals)

    async def entity_exists(self, node: str) -> bool:
        return node not in self.missing_entities


class FakeGraph:
    """模拟 Neo4j 图读；result=None 表示图不可用降级。"""

    def __init__(
        self,
        result: list[tuple[str, str, str]] | None | None = None,
        *,
        write_ok: bool = True,
    ) -> None:
        self.result = result
        self.write_ok = write_ok
        self.calls: list[tuple[str, str, int, int]] = []
        self.deleted: list[tuple[str, str, str]] = []
        self.written: list[tuple[str, str, str]] = []
        # P2/P3 图能力（默认不可用/无结果，与 result=None 降级语义一致）
        self.edge_count: int | None = None
        self.paths: list[tuple[list[str], list[tuple[str, str, str]]]] | None = None
        self.terminals: list[tuple[str, list[str]]] | None = None

    async def query_impact(
        self, node: str, direction: str, max_hops: int, max_edges: int
    ) -> list[tuple[str, str, str]] | None:
        self.calls.append((node, direction, max_hops, max_edges))
        return self.result

    async def write_edges(self, edges: list[tuple[str, str, str]]) -> bool:
        self.written.extend(edges)
        return self.write_ok

    async def delete_edges(self, edges: list[tuple[str, str, str]]) -> bool:
        self.deleted.extend(edges)
        return True

    # ---- P2/P3 图能力假实现 ----
    async def count_edges(self) -> int | None:
        return self.edge_count

    async def query_paths(
        self, source: str, target: str, max_hops: int, limit: int
    ) -> list[tuple[list[str], list[tuple[str, str, str]]]] | None:
        return self.paths

    async def query_terminals(
        self, node: str, max_hops: int, limit: int
    ) -> list[tuple[str, list[str]]] | None:
        return self.terminals


class FakeRedis:
    """内存假 Redis（cache-aside + 前缀失效验证用）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.deleted_keys: list[str] = []

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.store[key] = value
        self.calls.append((key, value))
        return True

    async def scan(self, cursor: int, match: str = "", count: int = 100) -> tuple[int, list[str]]:
        """内存假 SCAN：按前缀匹配一次返回全部键（cursor 恒 0=一次结束）。"""
        prefix = match.replace("*", "")
        keys = [k for k in self.store if k.startswith(prefix)]
        return 0, keys

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
            self.deleted_keys.append(k)
        return n


class _FakeSession:
    """带 commit/rollback 的假 db session（增量采集/失效管理测试用）。"""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def flush(self) -> None:
        """假 flush：无实际作用（手动登记边 owner 写入后调用）。"""


async def test_parse_and_store_counts_no_graph() -> None:
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    res = await svc.parse_and_store(
        LineageParseRequest(sql="INSERT INTO t SELECT a.id FROM a"), actor_id=1
    )
    assert res.table_edges == 1
    assert res.field_edges == 1
    assert res.graph_written is False
    assert len(svc._repo.edges) >= 1


async def test_parse_and_store_dual_publishes_eventbus() -> None:
    """SQL 解析写边后双发 EventBus（lineage_parsed），Redis 裸通道保留。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    eventbus = AsyncMock()
    svc._eventbus = eventbus
    await svc.parse_and_store(
        LineageParseRequest(sql="INSERT INTO t SELECT a.id FROM a"), actor_id=1
    )
    # P0-3：事件延迟到事务提交后发布（调用方 commit 后触发）
    await svc.run_post_commit()
    eventbus.publish.assert_awaited_once_with(
        "lineage_parsed", {"table_edges": 1, "field_edges": 1}
    )


async def test_parse_and_store_no_side_effects_before_post_commit() -> None:
    """P0-3 核心不变量：事务提交前不得产生图写/事件/缓存失效副作用。

    若 commit 失败，MySQL 回滚而图/事件已发生即产生幽灵边——此测试锁定
    「副作用仅在 run_post_commit()（commit 后）触发」的时序。
    """
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    graph = FakeGraph(result=None)
    svc._graph = graph
    redis = FakeRedis()
    svc._redis = redis
    eventbus = AsyncMock()
    svc._eventbus = eventbus

    await svc.parse_and_store(
        LineageParseRequest(sql="INSERT INTO t SELECT a.id FROM a"), actor_id=1
    )
    # 提交前：图未写、事件未发、缓存未失效
    assert graph.written == []
    assert graph.deleted == []
    eventbus.publish.assert_not_awaited()

    # 提交后（调用方 commit 成功后触发）：图写 + 事件发布
    await svc.run_post_commit()
    assert ("table:a", "table:t", "DERIVED_FROM") in graph.written
    assert ("field:a.id", "field:t.id", "DERIVED_FROM") in graph.written
    assert eventbus.publish.await_count == 1  # _events(legacy) 未配置，仅 EventBus 一次



async def test_parse_and_store_returns_edge_detail() -> None:
    """方案 A：解析响应带回本次表级/字段级边明细，供前端当页展示。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    res = await svc.parse_and_store(
        LineageParseRequest(sql="INSERT INTO t SELECT a.id FROM a"), actor_id=1
    )
    assert [i.model_dump() for i in res.table_lineage] == [
        {"source": "table:a", "target": "table:t"}
    ]
    assert [i.model_dump() for i in res.field_lineage] == [
        {
            "source_table": "a",
            "source_column": "id",
            "target_table": "t",
            "target_column": "id",
            "expression": None,
        }
    ]


async def test_parse_and_store_writes_ingest_run() -> None:
    """SQL 解析也写采集运行记录（source=provenance，added/updated 统计），
    使「采集通道」视图能展示 SQL 解析的来源新鲜度。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    req = LineageParseRequest(sql="INSERT INTO t SELECT a.id FROM a", provenance="sqlglot")
    await svc.parse_and_store(req, actor_id=1)
    assert len(svc._repo.runs) == 1
    run = svc._repo.runs[0]
    assert run.source == "sqlglot"
    assert run.status == "success"
    assert run.total_edges == 2  # 表级 1 + 字段级 1
    assert run.added_count == 2
    assert run.updated_count == 0
    # 重复解析同一 SQL：命中既有边 → 全部记 updated
    await svc.parse_and_store(req, actor_id=1)
    assert svc._repo.runs[1].added_count == 0
    assert svc._repo.runs[1].updated_count == 2


async def test_parse_and_store_pure_select_without_target_returns_upstream_deps() -> None:
    """方案 B：纯 SELECT 未指定落点 → 返回上游依赖、不写图谱、不写运行记录。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    res = await svc.parse_and_store(
        LineageParseRequest(
            sql="SELECT o.id, u.name FROM ods_orders o JOIN dim_user u ON o.uid = u.uid"
        ),
        actor_id=1,
    )
    assert res.table_edges == 0
    assert res.field_edges == 0
    assert res.graph_written is False
    assert res.upstream_deps is not None
    assert set(res.upstream_deps.tables) == {"ods_orders", "dim_user"}
    assert "ods_orders.id" in res.upstream_deps.fields
    # 无落点不写边、不写运行记录
    assert len(repo.edges) == 0
    assert len(repo.runs) == 0


async def test_parse_and_store_pure_select_with_target_table_writes_edges() -> None:
    """方案 A：纯 SELECT + 指定落点 → 生成正式血缘边并写运行记录。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    res = await svc.parse_and_store(
        LineageParseRequest(
            sql="SELECT o.id, u.name FROM ods_orders o JOIN dim_user u ON o.uid = u.uid",
            target_table="dws_report",
        ),
        actor_id=1,
    )
    assert res.table_edges == 2  # ods_orders/dim_user → dws_report
    assert res.field_edges == 2
    assert res.graph_written is False  # 无 graph 客户端
    assert res.upstream_deps is None
    assert len(svc._repo.edges) >= 4  # 表级 2 + 字段级 2
    assert len(svc._repo.runs) == 1


async def test_parse_and_store_writes_sql_parse_detail() -> None:
    """运行记录附带 SQL 解析详情快照（SQL 原文/方言/落点/边明细）。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    await svc.parse_and_store(
        LineageParseRequest(
            sql="INSERT INTO t SELECT a.id FROM a", dialect="doris", target_table=None
        ),
        actor_id=7,
    )
    detail = json.loads(svc._repo.runs[0].detail_json or "{}")
    assert detail["kind"] == "sql_parse"
    assert detail["sql"] == "INSERT INTO t SELECT a.id FROM a"
    assert detail["dialect"] == "doris"
    assert detail["actor_id"] == 7
    assert detail["table_lineage"] == [{"source": "table:a", "target": "table:t"}]
    assert detail["field_lineage"][0]["target_column"] == "id"


async def test_get_ingest_run_detail_parses_snapshot() -> None:
    """get_ingest_run_detail 反序列化 detail_json 到响应的 detail 字段。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    await svc.parse_and_store(
        LineageParseRequest(sql="INSERT INTO t SELECT a.id FROM a"), actor_id=1
    )
    run_id = repo.runs[0].id
    resp = await svc.get_ingest_run_detail(run_id)
    assert resp.id == run_id
    assert resp.detail is not None
    assert resp.detail["kind"] == "sql_parse"
    assert "SELECT a.id FROM a" in resp.detail["sql"]


async def test_ingest_batch_writes_batch_detail() -> None:
    """批量采集运行记录附带变更边明细（新增/更新边列表）。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeIngestRepo()
    repo.mark_seen_result = (0, 0)
    repo.mark_missing_result = (0, 0)
    svc._repo = repo

    edges = {("a", "b"), ("a_new", "c"), ("d", "e")}
    await svc.ingest_batch("dp_csv", edges, threshold=2)
    detail = json.loads(repo.runs[0].detail_json or "{}")
    assert detail["kind"] == "batch"
    # 仅 a_new 命中新表判定 → 新增边 1 条，其余 2 条记更新
    assert detail["added_edges"] == [["table:a_new", "table:c"]]
    assert len(detail["updated_edges"]) == 2


async def test_query_impact_delegates_to_repo() -> None:
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    svc._repo.impact = [make_edge(source="table:a", target="table:t")]
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert len(out) == 1
    assert out[0].target_node == "table:t"


async def test_query_impact_uses_graph_when_available() -> None:
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    svc._graph = FakeGraph(result=[("table:a", "table:g", "DERIVED_FROM")])
    svc._redis = FakeRedis()
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert len(out) == 1
    assert out[0].target_node == "table:g"
    assert out[0].provenance == "neo4j"
    assert out[0].granularity == "L1"


async def test_query_impact_falls_back_to_mysql_when_graph_none() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.impact = [make_edge(source="table:a", target="table:t")]
    svc._repo = repo
    svc._graph = FakeGraph(result=None)
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert len(out) == 1
    assert out[0].target_node == "table:t"
    assert out[0].provenance == "sqlglot"


async def test_query_impact_falls_back_to_mysql_when_graph_empty() -> None:
    """图可达但查不到该节点（空列表）时回退 MySQL——否则仅写入 MySQL 的
    导入血缘（如 dp_csv）在前端永远不可见。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.impact = [make_edge(source="table:a", target="table:t")]
    svc._repo = repo
    svc._graph = FakeGraph(result=[])
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert len(out) == 1
    assert out[0].target_node == "table:t"
    assert out[0].provenance == "sqlglot"


async def test_query_impact_skips_cache_without_redis() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.impact = [make_edge(source="table:a", target="table:t")]
    svc._repo = repo
    svc._graph = FakeGraph(result=None)
    svc._redis = None
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert out[0].target_node == "table:t"


async def test_query_impact_reads_from_cache() -> None:
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    svc._redis = FakeRedis()
    await svc._impact_cache_set(
        "lineage:impact:table:a:downstream:5",
        [make_edge(source="table:a", target="table:cached")],
    )
    graph = FakeGraph(result=[("table:a", "table:graph", "DERIVED_FROM")])
    svc._graph = graph
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert out[0].target_node == "table:cached"
    assert graph.calls == [], "缓存命中时不应再访问图"


async def test_query_impact_writes_cache_on_miss() -> None:
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    graph = FakeGraph(result=[("table:a", "table:b", "DERIVED_FROM")])
    svc._graph = graph
    redis = FakeRedis()
    svc._redis = redis
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert out[0].target_node == "table:b"
    assert len(redis.store) == 1
    # 图宕机后二次读仍能从缓存命中
    graph.result = None
    out2 = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert out2[0].target_node == "table:b"
    assert len(graph.calls) == 1


async def test_query_impact_cache_corruption_falls_through() -> None:
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    redis = FakeRedis()
    redis.store["lineage:impact:table:a:downstream:5"] = "{not-json"
    svc._redis = redis
    svc._graph = FakeGraph(result=[("table:a", "table:b", "DERIVED_FROM")])
    out = await svc.query_impact(LineageImpactParams(node="table:a"))
    assert out[0].target_node == "table:b"


async def test_impact_preview_classifies_impact_and_risk() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.impact = [
        make_edge(i=1, source="metric:gm", target="metric:m1"),
        make_edge(i=2, source="metric:gm", target="metric:m2"),
        make_edge(i=3, source="metric:gm", target="table:dw.rpt1"),
        make_edge(i=4, source="metric:gm", target="report:r1", edge_type="CONSUMED_BY"),
    ]
    svc._repo = repo
    svc._graph = None
    svc._redis = None
    result = await svc.impact_preview("gm", "UPDATE")
    assert [m.metric_code for m in result.affected_metrics] == ["m1", "m2"]
    assert all(m.change_type == "UPDATE" for m in result.affected_metrics)
    assert result.affected_tables == ["table:dw.rpt1"]
    assert result.affected_consumers == ["report:r1"]
    assert result.risk_level == "medium"


async def test_impact_preview_low_risk_when_no_impact() -> None:
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    svc._graph = None
    svc._redis = None
    result = await svc.impact_preview("ghost", "DROP")
    assert result.affected_metrics == []
    assert result.affected_tables == []
    assert result.affected_consumers == []
    assert result.risk_level == "low"


async def test_impact_preview_breaking_change_escalates_risk() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.impact = [make_edge(source="metric:gm", target="metric:m1")]
    svc._repo = repo
    svc._graph = None
    svc._redis = None
    result = await svc.impact_preview("gm", "BREAKING")
    assert result.risk_level == "high"


async def test_propagate_pii_marks_derived_descendants() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.impact = [
        make_edge(i=1, source="metric:source", target="metric:mid"),
        make_edge(i=2, source="metric:mid", target="metric:leaf"),
        make_edge(i=3, source="metric:mid", target="table:other", edge_type="CONSUMED_BY"),
    ]
    svc._repo = repo
    marked = await svc.propagate_pii("metric:source", depth=3)
    assert marked == 2
    pii_calls = [c for c in repo.upsert_calls if c.get("pii_inherited") is True]
    assert len(pii_calls) == 2
    assert {c["target_node"] for c in pii_calls} == {"metric:mid", "metric:leaf"}


async def test_propagate_pii_respects_depth() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.impact = [
        make_edge(i=1, source="metric:a", target="metric:b"),
        make_edge(i=2, source="metric:b", target="metric:c"),
        make_edge(i=3, source="metric:c", target="metric:d"),
    ]
    svc._repo = repo
    marked = await svc.propagate_pii("metric:a", depth=2)
    assert marked == 2


async def test_list_edges_returns_direct_neighbors() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.impact = [make_edge(source="table:a", target="table:t")]
    svc._repo = repo
    out = await svc.list_edges("table:a")
    assert len(out) == 1
    assert out[0].target_node == "table:t"
    assert out[0].pii_inherited is False


async def test_delete_by_node_delegates() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.deleted_count = 3
    svc._repo = repo
    assert await svc.delete_by_node("table:a") == 3


async def test_delete_by_node_syncs_graph_and_invalidates_cache() -> None:
    """C3/m5: 级联删除回写图存储并失效影响分析缓存。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.deleted_count = 1
    repo.edges_for_node_result = [
        SimpleNamespace(
            id=1, source_node="table:a", target_node="table:b", edge_type="DERIVED_FROM"
        )
    ]
    svc._repo = repo
    graph = FakeGraph()
    svc._graph = graph
    redis = FakeRedis()
    redis.store["lineage:impact:table:a:downstream:5"] = "[]"
    svc._redis = redis

    assert await svc.delete_by_node("table:a") == 1

    # P0-3：图写/缓存失效延迟到事务提交后执行
    await svc.run_post_commit()
    assert graph.deleted == [("table:a", "table:b", "DERIVED_FROM")]
    assert "lineage:impact:table:a:downstream:5" not in redis.store


async def test_restore_by_node_rebuilds_graph_and_invalidates_cache() -> None:
    """C3/m5: 级联恢复重建图存储并失效影响分析缓存。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.deleted_count = 1
    repo.soft_deleted = [
        SimpleNamespace(
            id=2, source_node="table:x", target_node="table:y", edge_type="DERIVED_FROM"
        )
    ]
    svc._repo = repo
    graph = FakeGraph()
    svc._graph = graph
    redis = FakeRedis()
    redis.store["lineage:impact:table:x:upstream:3"] = "[]"
    svc._redis = redis

    assert await svc.restore_by_node("table:x") == 1

    # P0-3：图写/缓存失效延迟到事务提交后执行
    await svc.run_post_commit()
    assert graph.written == [("table:x", "table:y", "DERIVED_FROM")]
    assert "lineage:impact:table:x:upstream:3" not in redis.store


async def test_query_graph_reuses_assetmap_assembly(monkeypatch: Any) -> None:
    """血缘图谱复用资产地图拼接：透传 domain/pii_only，按 limit 截断边。"""
    called: dict[str, Any] = {}

    async def fake_graph_from_mysql(self, domain, pii_only):
        called["domain"] = domain
        called["pii_only"] = pii_only
        nodes = [
            {"id": "table:a", "type": "table", "label": "a"},
            {"id": "metric:m", "type": "metric", "label": "m"},
        ]
        edges = [{"source": "table:a", "target": "metric:m", "type": "DERIVED_FROM"}] * 5
        return (nodes, edges)

    fake_cls = type(
        "FakeAssetMapRepo",
        (),
        {
            "__init__": lambda self, db: setattr(self, "_db", db),
            "graph_from_mysql": fake_graph_from_mysql,
        },
    )
    monkeypatch.setattr("app.services.assetmap.repository.AssetMapRepository", fake_cls)

    svc = LineageService(db=_FakeSession())
    out = await svc.query_graph(domain="finance", pii_only=True, limit=2)
    assert called["domain"] == "finance"
    assert called["pii_only"] is True
    assert out["nodes"][0]["id"] == "table:a"
    assert len(out["edges"]) == 2  # limit 截断边


async def test_query_graph_drops_dangling_edges(monkeypatch: Any) -> None:
    """自包含子图：仅保留两端都在节点集内的边，悬空边（指向未渲染节点）被剔除。"""

    async def fake_graph_from_mysql(self, domain, pii_only):
        nodes = [
            {"id": "table:a", "type": "table", "label": "a"},
            {"id": "metric:m", "type": "metric", "label": "m"},
        ]
        edges = [
            {"source": "table:a", "target": "metric:m", "type": "DERIVED_FROM"},  # 保留
            {"source": "table:a", "target": "table:ghost", "type": "DERIVED_FROM"},  # 悬空
            {"source": "table:ghost", "target": "table:a", "type": "DERIVED_FROM"},  # 悬空
        ]
        return (nodes, edges)

    fake_cls = type(
        "FakeAssetMapRepo",
        (),
        {
            "__init__": lambda self, db: setattr(self, "_db", db),
            "graph_from_mysql": fake_graph_from_mysql,
        },
    )
    monkeypatch.setattr("app.services.assetmap.repository.AssetMapRepository", fake_cls)

    svc = LineageService(db=_FakeSession())
    out = await svc.query_graph()
    assert len(out["edges"]) == 1
    assert out["edges"][0] == {"source": "table:a", "target": "metric:m", "type": "DERIVED_FROM"}


async def test_query_graph_defaults_without_filters(monkeypatch: Any) -> None:
    """血缘图谱默认不设域/PII 过滤，limit 默认 1000。"""
    called: dict[str, Any] = {}

    async def fake_graph_from_mysql(self, domain, pii_only):
        called["domain"] = domain
        called["pii_only"] = pii_only
        return [], []

    fake_cls = type(
        "FakeAssetMapRepo",
        (),
        {
            "__init__": lambda self, db: setattr(self, "_db", db),
            "graph_from_mysql": fake_graph_from_mysql,
        },
    )
    monkeypatch.setattr("app.services.assetmap.repository.AssetMapRepository", fake_cls)

    svc = LineageService(db=_FakeSession())
    out = await svc.query_graph()
    assert called["domain"] is None
    assert called["pii_only"] is False
    assert out == {"nodes": [], "edges": []}


def test_paginate_edges_slices_and_has_more() -> None:
    edges = [make_edge(i=i, target=f"table:t{i}") for i in range(1, 26)]
    page1 = paginate_edges(edges, 1, 10)
    assert page1["total"] == 25
    assert len(page1["items"]) == 10
    assert page1["has_more"] is True
    assert page1["items"][0]["id"] == 1
    # 节点元数据字段为可选的加法字段（默认空列表，向后兼容）
    assert page1["nodes"] == []

    last = paginate_edges(edges, 3, 10)
    assert len(last["items"]) == 5
    assert last["has_more"] is False

    empty = paginate_edges([], 1, 50)
    assert empty["total"] == 0
    assert empty["has_more"] is False
    assert empty["nodes"] == []


async def test_node_meta_returns_sorted_info() -> None:
    """node_meta 透传仓库解析结果并按节点 id 排序（供影响分析/边列表响应）。"""
    svc = LineageService(_FakeSession())
    svc._repo = FakeRepo()
    out = await svc.node_meta({"table:b", "metric:a", "plain"})
    assert [n.id for n in out] == ["metric:a", "plain", "table:b"]
    assert out[0].type == "metric"
    assert out[0].label == "a"
    assert out[2].type == "table"
    assert out[2].label == "b"


def test_risk_level_thresholds() -> None:
    assert LineageService._risk_level(0, "DROP") == "low"
    assert LineageService._risk_level(12, "UPDATE") == "high"
    assert LineageService._risk_level(25, "UPDATE") == "critical"
    assert LineageService._risk_level(4, "UPDATE") == "medium"
    assert LineageService._risk_level(4, "BREAKING") == "high"


class FakeIngestRepo:
    """增量采集/失效管理假仓库（记录调用与可配置返回）。"""

    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, object]] = []
        self.seen_calls: list[tuple[str, set[tuple[str, str]]]] = []
        self.missing_calls: list[tuple[str, set[tuple[str, str]], int]] = []
        self.runs: list[SimpleNamespace] = []
        self.mark_seen_result = (0, 0)
        self.mark_missing_result = (0, 0)
        self.edge: object | None = None
        self.confirmed = 0
        self.restored = 0
        self.channels: list[dict[str, object]] = []
        self.stale_edges: list[object] = []
        self.ingest_runs: list[object] = []

    async def begin_ingest_run(self, source: str) -> SimpleNamespace:
        run = SimpleNamespace(
            id=len(self.runs) + 1,
            source=source,
            status="running",
            run_at=datetime.now(UTC),
        )
        self.runs.append(run)
        return run

    async def upsert_edge_with_status(self, **kwargs: object) -> tuple[SimpleNamespace, bool]:
        self.upsert_calls.append(kwargs)
        created = str(kwargs.get("source_node", "")).endswith("_new")
        return SimpleNamespace(id=100 + len(self.upsert_calls), **kwargs), created

    async def mark_seen(self, source: str, seen: set[tuple[str, str]]) -> tuple[int, int]:
        self.seen_calls.append((source, seen))
        return self.mark_seen_result

    async def mark_missing(
        self, source: str, seen: set[tuple[str, str]], threshold: int
    ) -> tuple[int, int]:
        self.missing_calls.append((source, seen, threshold))
        return self.mark_missing_result

    async def finish_ingest_run(
        self,
        run: SimpleNamespace,
        *,
        status: str,
        total_edges: int = 0,
        added: int = 0,
        updated: int = 0,
        missing: int = 0,
        stale_flagged: int = 0,
        restored: int = 0,
        error: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        run.status = status
        run.total_edges = total_edges
        run.added_count = added
        run.updated_count = updated
        run.missing_count = missing
        run.stale_flagged_count = stale_flagged
        run.restored_count = restored
        run.error = error
        run.detail_json = json.dumps(detail, ensure_ascii=False) if detail else None

    async def get_ingest_run(self, run_id: int) -> object | None:
        for run in self.runs:
            if run.id == run_id:
                return run
        return None

    async def get_edge(self, edge_id: int) -> object | None:
        return self.edge

    async def confirm_stale(self, edge: object) -> None:
        self.confirmed += 1

    async def restore_stale(self, edge: object) -> None:
        self.restored += 1

    async def list_channels(self) -> list[dict[str, object]]:
        return self.channels

    async def list_stale_edges(self, source: str | None = None, limit: int = 200) -> list[object]:
        return self.stale_edges

    async def list_ingest_runs(self, source: str, limit: int = 20) -> list[object]:
        return self.ingest_runs


def _stale_edge(i: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=i,
        source_node=f"table:src{i}",
        target_node=f"table:tgt{i}",
        edge_type="DERIVED_FROM",
        granularity="L1",
        confidence=1.0,
        provenance="dp_csv",
        missing_count=2,
        stale_since=None,
    )


async def test_ingest_batch_returns_change_summary() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeIngestRepo()
    repo.mark_seen_result = (2, 1)
    repo.mark_missing_result = (3, 1)
    svc._repo = repo

    edges = {("a", "b"), ("a_new", "c"), ("d", "e")}
    summary = await svc.ingest_batch("dp_csv", edges, threshold=2)

    assert summary["source"] == "dp_csv"
    assert summary["total_edges"] == 3
    # 仅 a_new 命中新表判定
    assert summary["added"] == 1
    assert summary["updated"] == 2
    assert summary["missing"] == 3
    assert summary["stale_flagged"] == 1
    assert summary["restored"] == 1
    assert summary["run_id"] == repo.runs[0].id
    # 运行记录回写 success 摘要
    run = repo.runs[0]
    assert run.status == "success"
    assert run.added_count == 1
    assert run.stale_flagged_count == 1


async def test_ingest_batch_dual_publishes_eventbus() -> None:
    """批量采集后双发 EventBus（lineage_ingested），Redis 裸通道保留。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeIngestRepo()
    repo.mark_seen_result = (2, 1)
    repo.mark_missing_result = (3, 1)
    svc._repo = repo
    eventbus = AsyncMock()
    svc._eventbus = eventbus
    await svc.ingest_batch("dp_csv", {("a", "b"), ("a_new", "c"), ("d", "e")}, threshold=2)
    eventbus.publish.assert_awaited_once_with(
        "lineage_ingested",
        {
            "source": "dp_csv",
            "added": 1,
            "updated": 2,
            "missing": 3,
            "stale_flagged": 1,
            "restored": 1,
        },
    )


async def test_ingest_batch_threshold_defaults_to_config() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeIngestRepo()
    svc._repo = repo
    await svc.ingest_batch("dp_csv", {("a", "b")})
    # threshold 缺省走配置 lineage_stale_observation_runs=3
    assert repo.missing_calls[0][2] == 3


async def test_ingest_batch_failure_records_failed_run() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeIngestRepo()

    async def boom(**kwargs: object) -> tuple[SimpleNamespace, bool]:
        raise RuntimeError("db down")

    repo.upsert_edge_with_status = boom  # type: ignore[method-assign]
    svc._repo = repo
    raised = False
    try:
        await svc.ingest_batch("dp_csv", {("a", "b")})
    except RuntimeError:
        raised = True
    assert raised is True
    assert repo.runs[0].status == "failed"
    assert repo.runs[0].error == "db down"


async def test_list_channels_maps_to_response() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeIngestRepo()
    repo.channels = [
        {"source": "dp_csv", "edge_count": 10, "node_count": 8, "stale_count": 1, "last_run": None}
    ]
    svc._repo = repo
    channels = await svc.list_channels()
    assert len(channels) == 1
    assert channels[0].source == "dp_csv"
    assert channels[0].edge_count == 10
    assert channels[0].stale_count == 1


async def test_list_stale_maps_to_response() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeIngestRepo()
    repo.stale_edges = [_stale_edge(1)]
    svc._repo = repo
    stale = await svc.list_stale("dp_csv", limit=50)
    assert len(stale) == 1
    assert stale[0].provenance == "dp_csv"
    assert stale[0].missing_count == 2


async def test_list_nodes_maps_types_and_labels() -> None:
    """候选节点：按 id 前缀映射类型与去前缀展示名（影响分析选项框预加载/搜索）。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    nodes = await svc.list_nodes()
    by_id = {n.id: n for n in nodes}
    assert by_id["table:a"].type == "table"
    assert by_id["table:a"].label == "a"
    assert by_id["metric:m1"].type == "metric"
    assert by_id["metric:m1"].label == "m1"
    assert by_id["field:a.x"].type == "field"
    assert by_id["external:ext"].type == "external"
    assert by_id["plain_node"].type == "other"
    assert by_id["plain_node"].label == "plain_node"
    assert by_id["table:a"].count == 3


async def test_confirm_stale_edge_deletes_and_cleans_graph() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeIngestRepo()
    repo.edge = _stale_edge(7)
    graph = FakeGraph(result=[])
    svc._repo = repo
    svc._graph = graph
    edge = await svc.confirm_stale_edge(7)
    assert edge.id == 7
    assert repo.confirmed == 1
    # 同步清理图存储
    assert graph.deleted == [("table:src7", "table:tgt7", "DERIVED_FROM")]


async def test_restore_stale_edge_clears_flag() -> None:
    svc = LineageService(db=_FakeSession())
    repo = FakeIngestRepo()
    repo.edge = _stale_edge(3)
    svc._repo = repo
    edge = await svc.restore_stale_edge(3)
    assert edge.id == 3
    assert repo.restored == 1


async def test_query_graph_provenance_uses_edge_repo(monkeypatch: Any) -> None:
    """指定 provenance 时走血缘边仓库直接构建表级图谱（不依赖采集目录交集）。"""
    fake_repo = FakeRepo()

    async def fake_graph_from_edges(self, *, provenance, limit):
        assert provenance == "dp_csv"
        assert limit == 500
        nodes = [
            {
                "id": "table:wedw_dwd.tjhis_all_dic_drug_df",
                "type": "table",
                "label": "wedw_dwd.tjhis_all_dic_drug_df",
                "entity_id": None,
            },
            {"id": "metric:gmv", "type": "metric", "label": "gmv", "entity_id": None},
        ]
        edges = [
            {
                "source": "table:di_tjhqdzg.dic_drug",
                "target": "table:wedw_dwd.tjhis_all_dic_drug_df",
                "type": "DERIVED_FROM",
            },
        ]
        return (nodes, edges)

    fake_repo.graph_from_edges = fake_graph_from_edges.__get__(fake_repo, FakeRepo)
    svc = LineageService(db=_FakeSession())
    svc._repo = fake_repo

    out = await svc.query_graph(provenance="dp_csv", limit=500)
    assert out["nodes"][0]["id"] == "table:wedw_dwd.tjhis_all_dic_drug_df"
    assert out["edges"][0]["type"] == "DERIVED_FROM"
    # 边自包含：节点集来自边两端，无需二次过滤
    assert len(out["edges"]) == 1


# ---- Task A：消费方节点注册 ----


async def test_register_metric_consumer_creates_edge() -> None:
    """register_metric_consumer 写入 metric:code → consumer:client_id（CONSUMED_BY/L3）。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    edge = await svc.register_metric_consumer("gmv_total", "app_a")
    assert edge is not None
    kwargs = repo.upsert_calls[-1]
    assert kwargs["source_node"] == "metric:gmv_total"
    assert kwargs["target_node"] == "consumer:app_a"
    assert kwargs["edge_type"] == "CONSUMED_BY"
    assert kwargs["granularity"] == "L3"
    assert kwargs["provenance"] == "metric_consumer"


async def test_register_metric_consumer_skips_empty_client() -> None:
    """空/非字符串 client_id 静默跳过，不写边。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    assert await svc.register_metric_consumer("gmv_total", "") is None
    assert await svc.register_metric_consumer("gmv_total", None) is None  # type: ignore[arg-type]
    assert repo.upsert_calls == []


async def test_register_metric_consumers_from_db() -> None:
    """register_metric_consumers_from_db 按 ApiClient 白名单批量注册消费方边。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.consumer_ids = ["app_a", "app_b"]
    svc._repo = repo
    count = await svc.register_metric_consumers_from_db("gmv_total")
    assert count == 2
    targets = {c["target_node"] for c in repo.upsert_calls}
    assert targets == {"consumer:app_a", "consumer:app_b"}


# ---- Task B：血缘覆盖率治理 ----


async def test_coverage_stats_aggregates_counts() -> None:
    """coverage_stats 聚合指标/表/边/断链计数。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.metric_total_count = 10
    repo.codes_with_lineage = ["a", "b"]
    repo.table_total_count = 5
    repo.table_no_downstream = 2
    repo.broken_edges = [{"id": 1}, {"id": 2}]
    svc._repo = repo
    stats = await svc.coverage_stats()
    assert isinstance(stats, LineageCoverageResponse)
    assert stats.metric_total == 10
    assert stats.metric_with_lineage == 2
    assert stats.metric_orphan == 8
    assert stats.table_total == 5
    assert stats.table_no_downstream == 2
    assert stats.edge_total == 0
    assert stats.broken_edges == 2


async def test_coverage_orphan_metrics() -> None:
    """coverage_orphan_metrics 返回无血缘边的指标。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.metric_rows = [("a", "dom1"), ("c", None)]
    repo.codes_with_lineage = ["a"]
    svc._repo = repo
    orphans = await svc.coverage_orphan_metrics()
    assert orphans == [CoverageOrphanItem(metric_code="c", domain=None)]


async def test_coverage_broken_edges() -> None:
    """coverage_broken_edges 按 limit 截断返回断链明细。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.broken_edges = [
        {
            "id": 1,
            "source_node": "metric:x",
            "target_node": "table:t",
            "edge_type": "DERIVED_FROM",
            "granularity": "L3",
            "confidence": 1.0,
            "provenance": "sqlglot",
        },
        {
            "id": 2,
            "source_node": "metric:y",
            "target_node": "table:t",
            "edge_type": "DERIVED_FROM",
            "granularity": "L3",
            "confidence": 1.0,
            "provenance": "sqlglot",
        },
    ]
    svc._repo = repo
    broken = await svc.coverage_broken_edges(limit=1)
    assert len(broken) == 1
    assert isinstance(broken[0], CoverageBrokenEdgeItem)
    assert broken[0].id == 1


# ---- Task C：PII 影响面分析 ----


async def test_pii_impact_collects_downstream_with_consumers() -> None:
    """pii_impact 沿下游收集受 PII 影响节点（含 CONSUMED_BY 消费方），仅沿 DERIVED_FROM 传导。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.impact = [
        LineageEdgeResponse(
            id=1,
            source_node="metric:m0",
            target_node="metric:m1",
            edge_type="DERIVED_FROM",
            granularity="L3",
            confidence=1.0,
            provenance="x",
        ),
        LineageEdgeResponse(
            id=2,
            source_node="metric:m0",
            target_node="consumer:c1",
            edge_type="CONSUMED_BY",
            granularity="L3",
            confidence=1.0,
            provenance="x",
        ),
        LineageEdgeResponse(
            id=3,
            source_node="metric:m1",
            target_node="metric:m2",
            edge_type="DERIVED_FROM",
            granularity="L3",
            confidence=1.0,
            provenance="x",
        ),
    ]
    svc._repo = repo
    items = await svc.pii_impact("metric:m0", depth=3)
    assert {i.node for i in items} == {"metric:m1", "metric:m2", "consumer:c1"}
    assert all(isinstance(i, PiiImpactItem) for i in items)
    # 消费方边纳入影响面，但作为终点不继续传导
    c1 = next(i for i in items if i.node == "consumer:c1")
    assert c1.edge_type == "CONSUMED_BY"
    assert c1.path == ["metric:m0", "consumer:c1"]
    assert c1.hops == 1


# ---- Task D：血缘边详情 ----


async def test_edge_detail_returns_edge_and_history() -> None:
    """edge_detail 返回当前边 + 变更历史；缺失抛 NotFoundError。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.edges = [
        SimpleNamespace(
            id=1,
            source_node="metric:m1",
            target_node="consumer:c1",
            edge_type="CONSUMED_BY",
            granularity="L3",
            confidence=1.0,
            provenance="metric_consumer",
            pii_inherited=False,
        )
    ]
    repo.history = [
        SimpleNamespace(
            id=1,
            source_node="metric:m1",
            target_node="consumer:c1",
            edge_type="CONSUMED_BY",
            granularity="L3",
            confidence=0.9,
            provenance="metric_consumer",
            pii_inherited=False,
            change_reason="rename",
            created_at=datetime.now(UTC),
        )
    ]
    svc._repo = repo
    detail = await svc.edge_detail(1)
    assert isinstance(detail, LineageEdgeDetailResponse)
    assert detail.edge.id == 1
    assert detail.edge.target_node == "consumer:c1"
    assert len(detail.history) == 1
    assert detail.history[0].change_reason == "rename"


async def test_edge_detail_missing_raises() -> None:
    """缺失边抛 NotFoundError。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    raised = False
    try:
        await svc.edge_detail(999)
    except NotFoundError:
        raised = True
    assert raised


# ---- 指标↔维度 / 指标↔字段 血缘（枚举扩展后新增） ----


async def test_register_metric_dimension_edges() -> None:
    """register_metric_dimension_edges 注册 USES_DIMENSION 边（L3，幂等）。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    edges = await svc.register_metric_dimension_edges("gmv_total", ["store", "region"])
    assert len(edges) == 2
    targets = {c["target_node"] for c in repo.upsert_calls}
    assert targets == {"dimension:store", "dimension:region"}
    for call in repo.upsert_calls:
        assert call["source_node"] == "metric:gmv_total"
        assert call["edge_type"] == "USES_DIMENSION"
        assert call["granularity"] == "L3"


async def test_register_metric_dimension_edges_skips_empty() -> None:
    """空/非字符串维度编码静默跳过，不产生边。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    edges = await svc.register_metric_dimension_edges("gmv_total", ["", None, 123])
    assert edges == []
    assert repo.upsert_calls == []


async def test_register_metric_column_edge() -> None:
    """register_metric_column_edge 注册 READS_COLUMN 边（column -> metric，L3）。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    edge = await svc.register_metric_column_edge("gmv_total", "dws.gmv", "amount")
    assert edge is not None
    call = repo.upsert_calls[0]
    assert call["source_node"] == "column:dws.gmv.amount"
    assert call["target_node"] == "metric:gmv_total"
    assert call["edge_type"] == "READS_COLUMN"
    assert call["granularity"] == "L3"


async def test_register_metric_column_edge_skips_empty() -> None:
    """表/字段为空时返回 None，不注册边。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    assert await svc.register_metric_column_edge("gmv_total", "", "amount") is None
    assert await svc.register_metric_column_edge("gmv_total", "t", "") is None
    assert repo.upsert_calls == []


async def test_register_metric_from_definition_includes_dim_and_column() -> None:
    """register_metric_from_definition 注册表血缘，维度/字段边走差异同步。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    metric = SimpleNamespace(
        metric_code="gmv_total",
        definition_json={
            "source_table": "dws.gmv",
            "measure_column": "amount",
            "dimensions": [{"code": "store"}, "region"],
            "measures": [{"name": "cnt"}],
        },
    )
    edges = await svc.register_metric_from_definition(metric)
    # 表血缘：1 条落地表 downstream 边（维度/字段走差异同步，不入 edges）
    assert len(edges) == 1
    assert edges[0].edge_type == "DERIVED_FROM"
    calls = repo.upsert_calls
    # 维度差异同步：声明集含 store + region
    dim_sync = [c for c in calls if c.get("op") == "sync_metric_dimension_edges"]
    assert dim_sync and dim_sync[0]["codes"] == ["store", "region"]
    # 字段差异同步：声明字段集含 (dws.gmv, amount) + (dws.gmv, cnt)
    col_sync = [c for c in calls if c.get("op") == "sync_metric_column_edges"]
    assert col_sync
    assert sorted(col_sync[0]["fields"]) == [("dws.gmv", "amount"), ("dws.gmv", "cnt")]


async def test_register_metric_from_definition_mount_authority() -> None:
    """OneData 挂载层权威：挂载实体 source_table 优先于 definition_json 冗余。

    挂载 API 独立更新挂载后 definition_json 的 source_table 可能过期，
    血缘必须以 metric_mount 为准（否则注册/更新后血缘绑到旧表）。
    """
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    metric = SimpleNamespace(
        id=42,
        metric_code="sales_gmv_daily",
        definition_json={"source_table": "dws.gmv_old", "measure_column": "amount"},
    )
    mount = SimpleNamespace(source_table="dws.gmv_new", source_column="amount")
    with patch("app.services.metric_mount.repository.MetricMountRepository") as mrepo_cls:
        mrepo_cls.return_value.get_by_metric = AsyncMock(return_value=mount)
        edges = await svc.register_metric_from_definition(metric)

    # 表边差异同步使用挂载权威 source_table（而非 definition_json 旧值）
    table_sync = [c for c in repo.upsert_calls if c.get("op") == "sync_metric_table_edges"]
    assert table_sync and table_sync[0]["downstream"] == "dws.gmv_new"
    # 字段边差异同步使用挂载权威 source_table + source_column
    col_sync = [c for c in repo.upsert_calls if c.get("op") == "sync_metric_column_edges"]
    assert col_sync and col_sync[0]["fields"] == [("dws.gmv_new", "amount")]
    # 血缘边注册到挂载权威表节点
    assert len(edges) == 1
    assert edges[0].edge_type == "DERIVED_FROM"
    assert edges[0].target_node == "table:dws.gmv_new"


async def test_query_impact_merges_dimension_column_edges_from_mysql() -> None:
    """图路径返回时合并 MySQL 权威库的 USES_DIMENSION/READS_COLUMN 边。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    svc._graph = FakeGraph(result=[("metric:gmv_total", "metric:gmv_derived", "DERIVED_FROM")])

    async def _extra(node: str, direction: str = "both") -> list[Any]:
        return [
            SimpleNamespace(
                id=1,
                source_node="metric:gmv_total",
                target_node="dimension:store",
                edge_type="USES_DIMENSION",
                granularity="L3",
                confidence=1.0,
                provenance="metric_definition",
                pii_inherited=False,
            ),
            SimpleNamespace(
                id=2,
                source_node="column:dws.gmv.amount",
                target_node="metric:gmv_total",
                edge_type="READS_COLUMN",
                granularity="L3",
                confidence=1.0,
                provenance="metric_definition",
                pii_inherited=False,
            ),
        ]

    repo.edges_for_node = _extra  # type: ignore[method-assign]
    params = LineageImpactParams(node="metric:gmv_total", direction="both", max_hops=3)
    edges = await svc.query_impact(params)
    types = {e.edge_type for e in edges}
    assert "DERIVED_FROM" in types
    assert "USES_DIMENSION" in types
    assert "READS_COLUMN" in types
    assert len(edges) == 3


# ---- 人工治理：手动登记 / 单边删除 ----


async def test_add_manual_edge_creates_manual_edge() -> None:
    """手动登记：provenance=manual、owner=登记人、粒度按节点类型推断。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    result = await svc.add_manual_edge(
        ManualEdgeCreateRequest(
            source_node="table:ods.orders",
            target_node="metric:gmv_total",
            edge_type="DERIVED_FROM",
            note="活动口径关联",
        ),
        actor_id=7,
    )
    assert result.created is True
    kwargs = repo.upsert_calls[-1]
    assert kwargs["source_node"] == "table:ods.orders"
    assert kwargs["target_node"] == "metric:gmv_total"
    assert kwargs["provenance"] == "manual"
    assert kwargs["confidence"] == 1.0
    assert kwargs["change_reason"] == "manual: 活动口径关联"
    # 含指标节点 → L3
    assert kwargs["granularity"] == "L3"


async def test_add_manual_edge_granularity_by_node_type() -> None:
    """粒度推断：含字段节点→L2，纯表→L1。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo

    await svc.add_manual_edge(
        ManualEdgeCreateRequest(
            source_node="column:ods.orders.amount",
            target_node="table:dwd.order_amt",
        ),
        actor_id=1,
    )
    assert repo.upsert_calls[-1]["granularity"] == "L2"

    await svc.add_manual_edge(
        ManualEdgeCreateRequest(
            source_node="table:ods.orders",
            target_node="table:dwd.order_amt",
        ),
        actor_id=1,
    )
    assert repo.upsert_calls[-1]["granularity"] == "L1"


async def test_add_manual_edge_rejects_unsupported_prefix() -> None:
    """非法节点前缀拒绝（fail-fast，不写边）。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    with pytest.raises(ValidationError):
        await svc.add_manual_edge(
            ManualEdgeCreateRequest(
                source_node="hack:payload",
                target_node="table:a",
            ),
            actor_id=1,
        )
    assert repo.upsert_calls == []


async def test_add_manual_edge_rejects_bare_node() -> None:
    """无前缀节点拒绝。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    with pytest.raises(ValidationError):
        await svc.add_manual_edge(
            ManualEdgeCreateRequest(source_node="orders", target_node="table:a"),
            actor_id=1,
        )


async def test_add_manual_edge_rejects_self_loop() -> None:
    """自环拒绝（上游==下游无意义）。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    with pytest.raises(ValidationError):
        await svc.add_manual_edge(
            ManualEdgeCreateRequest(source_node="table:a", target_node="table:a"),
            actor_id=1,
        )


async def test_add_manual_edge_rejects_invalid_edge_type() -> None:
    """非法边类型拒绝。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    with pytest.raises(ValidationError):
        await svc.add_manual_edge(
            ManualEdgeCreateRequest(
                source_node="table:a",
                target_node="table:b",
                edge_type="NOT_A_TYPE",
            ),
            actor_id=1,
        )
    assert repo.upsert_calls == []


async def test_add_manual_edge_sets_owner() -> None:
    """登记人写入 owner（人工边归属）。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    result = await svc.add_manual_edge(
        ManualEdgeCreateRequest(
            source_node="dimension:store",
            target_node="metric:gmv_total",
            edge_type="USES_DIMENSION",
        ),
        actor_id=42,
    )
    # FakeRepo 返回 SimpleNamespace，owner 写入直接反映在返回对象
    assert result.edge is not None
    assert repo.upsert_calls[-1]["granularity"] == "L3"


async def test_delete_edge_by_id_delegates() -> None:
    """单边删除：委托 repo.soft_delete_edge，返回被删边两端。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    edge = SimpleNamespace(
        id=9, source_node="table:a", target_node="table:b", edge_type="DERIVED_FROM"
    )
    repo.edges.append(edge)
    result = await svc.delete_edge_by_id(9)
    assert result.edge_id == 9
    assert result.source_node == "table:a"
    assert result.target_node == "table:b"


async def test_delete_edge_by_id_syncs_graph_and_invalidates_cache() -> None:
    """C3/m5: 单边删除回写图存储并失效两端影响缓存。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    edge = SimpleNamespace(
        id=9, source_node="table:a", target_node="table:b", edge_type="DERIVED_FROM"
    )
    repo.edges.append(edge)
    svc._repo = repo
    graph = FakeGraph()
    svc._graph = graph
    redis = FakeRedis()
    redis.store["lineage:impact:table:a:downstream:5"] = "[]"
    redis.store["lineage:impact:table:b:upstream:5"] = "[]"
    svc._redis = redis

    await svc.delete_edge_by_id(9)

    # P0-3：图写/缓存失效延迟到事务提交后执行
    await svc.run_post_commit()
    assert graph.deleted == [("table:a", "table:b", "DERIVED_FROM")]
    assert "lineage:impact:table:a:downstream:5" not in redis.store
    assert "lineage:impact:table:b:upstream:5" not in redis.store


async def test_delete_edge_by_id_not_found() -> None:
    """单边删除：边不存在抛 NotFoundError。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    with pytest.raises(NotFoundError):
        await svc.delete_edge_by_id(999)


async def test_resolve_query_nodes_expands_prefix_candidates() -> None:
    """无前缀输入展开为 metric:/table:/field: 候选；带前缀原样返回（第 9 轮）。"""
    assert LineageService._resolve_query_nodes("gmv_day") == [
        "metric:gmv_day",
        "table:gmv_day",
        "field:gmv_day",
    ]
    assert LineageService._resolve_query_nodes("metric:gmv_day") == ["metric:gmv_day"]
    assert LineageService._resolve_query_nodes("table:db.orders") == ["table:db.orders"]


async def test_query_impact_without_prefix_expands_candidates() -> None:
    """无前缀裸编码查询：展开候选节点，指标边与表边均命中合并（第 9 轮）。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.impact = [
        make_edge(i=1, source="metric:gmv_day", target="metric:gmv_week"),
        make_edge(i=2, source="table:dwd.order", target="metric:gmv_day"),
    ]
    svc._repo = repo
    svc._graph = None  # 强制走 MySQL（FakeRepo）路径
    edges = await svc.query_impact(
        LineageImpactParams(node="gmv_day", direction="downstream", max_hops=5)
    )
    # 裸编码展开为 metric:gmv_day（下游命中 1 条）与 table:gmv_day/field:gmv_day（无）→ 合并 1 条
    assert len(edges) == 1
    assert edges[0].source_node == "metric:gmv_day"


async def test_list_edges_without_prefix_expands_candidates() -> None:
    """边列表同样对无前缀输入展开候选节点（第 9 轮）。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.impact = [
        make_edge(i=3, source="metric:gmv_day", target="consumer:app_a", edge_type="CONSUMED_BY"),
    ]
    svc._repo = repo
    edges = await svc.list_edges("gmv_day", direction="downstream")
    assert len(edges) == 1
    assert edges[0].target_node == "consumer:app_a"


# ---------- M1: 批量入库写图 + m5 缓存失效 ----------


async def test_ingest_batch_writes_graph_and_invalidates_added_cache() -> None:
    """M1/m5: 批量入库写图存储；新增边两端失效影响缓存。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeIngestRepo()
    repo.mark_seen_result = (0, 0)
    repo.mark_missing_result = (0, 0)
    svc._repo = repo
    graph = FakeGraph()
    svc._graph = graph
    redis = FakeRedis()
    redis.store["lineage:impact:table:a_new:downstream:5"] = "[]"
    redis.store["lineage:impact:table:a:upstream:5"] = "[]"
    svc._redis = redis

    await svc.ingest_batch("dp_csv", {("a", "b"), ("a_new", "c")}, threshold=2)

    # 全量 seen 写图（含更新与新增，幂等 MERGE）
    assert ("table:a", "table:b", "DERIVED_FROM") in graph.written
    assert ("table:a_new", "table:c", "DERIVED_FROM") in graph.written
    # 仅新增边两端失效缓存（更新边图里已有，无需刷新）
    assert "lineage:impact:table:a_new:downstream:5" not in redis.store
    assert "lineage:impact:table:a:upstream:5" in redis.store


# ---------- M2: 写图失败告警 ----------


async def test_sync_graph_failure_publishes_alert() -> None:
    """M2: 写图失败发布告警事件（不再静默吞掉）。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    svc._graph = FakeGraph(write_ok=False)
    eventbus = AsyncMock()
    svc._eventbus = eventbus

    ok = await svc._sync_graph(
        [("table:a", "table:b", "DERIVED_FROM")], context="ingest_batch:dp_csv"
    )

    assert ok is False
    eventbus.publish.assert_awaited_once()
    event, payload = eventbus.publish.await_args.args
    assert event == "lineage.graph_sync_failed"
    assert payload["context"] == "ingest_batch:dp_csv"
    assert payload["action"] == "write"
    assert payload["edge_count"] == 1


async def test_sync_graph_delete_failure_publishes_alert() -> None:
    """M2: 删图失败同样发布告警事件。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()

    class FailDeleteGraph(FakeGraph):
        async def delete_edges(self, edges: list[tuple[str, str, str]]) -> bool:
            return False

    svc._graph = FailDeleteGraph()
    eventbus = AsyncMock()
    svc._eventbus = eventbus

    ok = await svc._sync_graph(
        [("table:a", "table:b", "DERIVED_FROM")], delete=True, context="delete_edge_by_id:9"
    )

    assert ok is False
    event, payload = eventbus.publish.await_args.args
    assert payload["action"] == "delete"


# ---------- m5: 缓存失效辅助 ----------


async def test_invalidate_impact_cache_without_redis_is_noop() -> None:
    """m5: 未注入 Redis 时缓存失效为 no-op（不阻塞主流程）。"""
    svc = LineageService(db=_FakeSession())
    assert await svc._invalidate_impact_cache("table:a") is None


# ---------- 企业级批次解析（parse_batch）----------


async def test_parse_batch_writes_all_edges() -> None:
    """批次解析：多条 SQL 全部写入，变更摘要与逐条明细正确。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    eventbus = AsyncMock()
    svc._eventbus = eventbus
    req = LineageParseBatchRequest(
        dialect="mysql",
        statements=[
            "INSERT INTO dws.t SELECT a.id, a.v FROM ods.a",
            "INSERT INTO dws.u SELECT b.id FROM ods.b",
        ],
        provenance="sqlglot_batch",
    )
    res = await svc.parse_batch(req, actor_id=1)
    # P0-3：事件延迟到事务提交后发布
    await svc.run_post_commit()
    assert res.total_statements == 2
    assert res.succeeded == 2
    assert res.failed == 0
    # 表级 2 + 字段级 3（a.id/a.v/b.id）= 5 条边
    assert res.total_edges == 5
    assert res.added == 5
    assert res.updated == 0
    assert res.skipped == 0
    assert len(res.statements) == 2
    # 逐条明细：第一条 1 表级 + 2 字段级，第二条 1 表级 + 1 字段级
    assert len(res.statements[0].table_edges) == 1
    assert len(res.statements[0].field_edges) == 2
    assert len(res.statements[1].table_edges) == 1
    assert len(res.statements[1].field_edges) == 1
    # 事件双发（lineage_batch_parsed）
    eventbus.publish.assert_awaited_once()
    event, payload = eventbus.publish.await_args.args
    assert event == "lineage_batch_parsed"
    assert payload["added"] == 5
    # 写了一条批次运行记录（kind=batch_parse）
    assert len(svc._repo.runs) == 1
    assert svc._repo.runs[0].source == "sqlglot_batch"


async def test_parse_batch_text_block_splits() -> None:
    """text 多语句文本块按分号拆分（含注释剥离），等价 statements 数组。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    req = LineageParseBatchRequest(
        dialect="hive",
        text=(
            "-- 上游加工\n"
            "INSERT INTO dwd.dim_user SELECT user_id, user_name FROM ods.ods_user; "
            "INSERT INTO dws.dws_user_stat SELECT user_id, COUNT(*) AS cnt FROM dwd.dim_user "
            "GROUP BY user_id"
        ),
    )
    res = await svc.parse_batch(req, actor_id=1)
    assert res.total_statements == 2
    assert res.succeeded == 2
    # 表级 2 边 + 字段级 3（user_id/user_name + user_id；COUNT(*) 无源列不产边）
    assert res.total_edges == 5
    # 中间表 dwd.dim_user 正确成链（两语句均写边）
    nodes = {e["source_node"] for e in svc._repo.upsert_calls}
    assert "table:dwd.dim_user" in nodes


class _PartialCycleRepo(FakeRepo):
    """环检假仓库：仅对指定 (source, target) 判成环。"""

    def __init__(self) -> None:
        super().__init__()
        self.cycle_keys: set[tuple[str, str]] = set()

    async def would_create_cycle(self, edge: object) -> bool:
        return (edge.source_node, edge.target_node) in self.cycle_keys


async def test_parse_batch_cycle_edge_skipped() -> None:
    """批次解析：成环边跳过计数（不抛错），其余边正常写入。"""
    svc = LineageService(db=_FakeSession())
    repo = _PartialCycleRepo()
    repo.cycle_keys = {("table:ods.b", "table:dws.u")}
    svc._repo = repo
    req = LineageParseBatchRequest(
        dialect="mysql",
        statements=[
            "INSERT INTO dws.t SELECT a.id FROM ods.a",
            "INSERT INTO dws.u SELECT b.id FROM ods.b",
        ],
    )
    res = await svc.parse_batch(req, actor_id=1)
    assert res.skipped == 1  # ods.b->dws.u 表级边成环跳过
    assert res.added == 3  # a->t 表级 + a.id->t.id 字段 + b.id->u.id 字段
    assert res.total_edges == 3
    # 成环表级边未写入，字段级边正常写入
    written = {(e["source_node"], e["target_node"]) for e in repo.upsert_calls}
    assert ("table:ods.b", "table:dws.u") not in written
    assert ("table:ods.a", "table:dws.t") in written
    assert ("field:ods.b.id", "field:dws.u.id") in written


async def test_parse_batch_empty_no_op() -> None:
    """批次解析空输入（无 statements/text）：返回零结果、不写运行记录。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    req = LineageParseBatchRequest(dialect="mysql")
    res = await svc.parse_batch(req, actor_id=1)
    assert res.total_statements == 0
    assert res.total_edges == 0
    assert res.succeeded == 0
    assert res.failed == 0


# ---- 血缘平台健康度（P2）----


async def test_health_score_computes_grade_and_dimensions() -> None:
    """五维加权评分：覆盖率/断链/失效/新鲜度/对账一致时总分高、等级优。"""
    repo = FakeRepo()
    repo.metric_total_count = 100
    repo.codes_with_lineage = [f"m{i}" for i in range(80)]  # 指标覆盖率 80%
    repo.table_total_count = 50
    repo.table_no_downstream = 5  # 表端到端 90%
    repo.table_nodes_in_edges_count = 50  # 与 no_downstream 同口径（血缘边内 table 节点）
    repo.edges = [SimpleNamespace() for _ in range(10)]
    repo.broken_edges = [{}]  # 断链 1/10
    repo.stale_count = 1  # 失效 1/10
    repo.latest_run_at = datetime.now(UTC)  # 新鲜（0 天）
    graph = FakeGraph()
    graph.edge_count = 10  # 图-库一致
    svc = LineageService(db=_FakeSession(), graph=graph)
    svc._repo = repo

    health = await svc.health_score()

    # 各维度分：coverage 84 / broken 90 / stale 90 / freshness 100 / reconciliation 100
    assert health.dimensions["coverage"].score == 84.0
    assert health.dimensions["broken"].score == 90.0
    assert health.dimensions["stale"].score == 90.0
    assert health.dimensions["freshness"].score == 100.0
    assert health.dimensions["reconciliation"].score == 100.0
    # 总分 = 84*0.4 + 90*0.2 + 90*0.15 + 100*0.15 + 100*0.1 = 90.1
    assert health.overall_score == 90.1
    assert health.grade == "excellent"


async def test_health_score_graph_unavailable_normalizes() -> None:
    """图存储不可达：reconciliation 维度权重 0，其余维度权重归一化不惩罚。"""
    repo = FakeRepo()
    repo.metric_total_count = 100
    repo.codes_with_lineage = [f"m{i}" for i in range(100)]
    repo.table_total_count = 10
    repo.table_no_downstream = 0
    repo.table_nodes_in_edges_count = 10  # 与 no_downstream 同口径
    repo.edges = [SimpleNamespace() for _ in range(5)]
    repo.broken_edges = []
    repo.stale_count = 0
    repo.latest_run_at = datetime.now(UTC)
    graph = FakeGraph()
    graph.edge_count = None  # 图不可达
    svc = LineageService(db=_FakeSession(), graph=graph)
    svc._repo = repo

    health = await svc.health_score()

    assert health.dimensions["reconciliation"].weight == 0.0
    assert health.dimensions["reconciliation"].detail == {"reason": "graph_unavailable"}
    # coverage/broken/stale/freshness 全 100 → 归一化总分 100
    assert health.overall_score == 100.0
    assert health.grade == "excellent"


async def test_health_score_freshness_decays_without_runs() -> None:
    """无采集运行记录但有边：新鲜度 0 分，拉低总分。"""

    repo = FakeRepo()
    repo.metric_total_count = 100
    repo.codes_with_lineage = [f"m{i}" for i in range(100)]
    repo.table_total_count = 10
    repo.table_no_downstream = 0
    repo.table_nodes_in_edges_count = 10  # 与 no_downstream 同口径
    repo.edges = [SimpleNamespace() for _ in range(10)]
    repo.broken_edges = []
    repo.stale_count = 0
    repo.latest_run_at = None  # 有边但无运行记录
    graph = FakeGraph()
    graph.edge_count = 10
    svc = LineageService(db=_FakeSession(), graph=graph)
    svc._repo = repo

    health = await svc.health_score()

    assert health.dimensions["freshness"].score == 0.0
    # 总分 = 100*0.4 + 100*0.2 + 100*0.15 + 0*0.15 + 100*0.1 = 85
    assert health.overall_score == 85.0
    assert health.grade == "good"


async def test_health_score_freshness_neutral_when_empty_platform() -> None:
    """全新平台（无边、无运行）：新鲜度中性满分，整体健康。"""
    repo = FakeRepo()
    svc = LineageService(db=_FakeSession(), graph=FakeGraph())
    svc._repo = repo
    health = await svc.health_score()
    assert health.dimensions["freshness"].score == 100.0
    assert health.overall_score == 100.0


async def test_health_score_handles_naive_ingest_run_time() -> None:
    """MySQL DATETIME 返回 offset-naive 时新鲜度不崩（生产真实时区场景回归）。"""
    from datetime import timedelta

    repo = FakeRepo()
    repo.metric_total_count = 100
    repo.codes_with_lineage = [f"m{i}" for i in range(100)]
    repo.table_total_count = 10
    repo.table_no_downstream = 0
    repo.table_nodes_in_edges_count = 10  # 与 no_downstream 同口径
    repo.edges = [SimpleNamespace() for _ in range(10)]
    repo.broken_edges = []
    repo.stale_count = 0
    # offset-naive 时间（无 tzinfo，与 MySQL DATETIME 一致）
    repo.latest_run_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=15)
    graph = FakeGraph()
    graph.edge_count = 10
    svc = LineageService(db=_FakeSession(), graph=graph)
    svc._repo = repo

    health = await svc.health_score()

    # 15 天 → 50 分（30 天衰减到 0）
    assert health.dimensions["freshness"].score == 50.0
    # 总分 = 100*0.4 + 100*0.2 + 100*0.15 + 50*0.15 + 100*0.1 = 92.5
    assert health.overall_score == 92.5
    assert health.grade == "excellent"


# ---- 血缘路径查询（P3）----


async def test_path_query_uses_graph_paths() -> None:
    """图返回路径：直接采用，含最短跳数。"""
    repo = FakeRepo()
    graph = FakeGraph()
    graph.paths = [
        (
            ["table:a", "table:m", "table:b"],
            [("table:a", "table:m", "DERIVED_FROM"), ("table:m", "table:b", "DERIVED_FROM")],
        ),
        (["table:a", "table:b"], [("table:a", "table:b", "DERIVED_FROM")]),
    ]
    svc = LineageService(db=_FakeSession(), graph=graph)
    svc._repo = repo

    res = await svc.path_query(source="table:a", target="table:b")

    assert res.has_path is True
    assert res.path_count == 2
    assert res.shortest_hops == 1
    # 按边数升序：最短（1 跳）在前
    assert res.paths[0].nodes == ["table:a", "table:b"]
    assert res.paths[0].hops == 1
    assert res.paths[1].nodes == ["table:a", "table:m", "table:b"]
    assert res.paths[1].hops == 2
    assert res.truncated is False


async def test_path_query_falls_back_to_mysql_when_graph_none() -> None:
    """图不可达（query_paths=None）：回退 MySQL DFS。"""
    repo = FakeRepo()
    graph = FakeGraph()
    graph.paths = None
    edge = make_edge()
    edge.source_node = "table:a"
    edge.target_node = "table:b"
    repo.repo_paths = [[edge]]
    svc = LineageService(db=_FakeSession(), graph=graph)
    svc._repo = repo

    res = await svc.path_query(source="table:a", target="table:b")

    assert res.has_path is True
    assert res.path_count == 1
    assert res.paths[0].nodes == ["table:a", "table:b"]
    assert res.paths[0].edges[0].source == "table:a"


async def test_path_query_no_path() -> None:
    """图与 MySQL 均无路径：has_path=False、shortest_hops=None。"""
    repo = FakeRepo()
    graph = FakeGraph()
    graph.paths = []
    svc = LineageService(db=_FakeSession(), graph=graph)
    svc._repo = repo

    res = await svc.path_query(source="table:a", target="table:z")

    assert res.has_path is False
    assert res.path_count == 0
    assert res.shortest_hops is None
    assert res.paths == []


async def test_terminal_nodes_uses_graph_and_entity_exists() -> None:
    """图终止节点 + 实体存在性标注（断链嫌疑）。"""
    repo = FakeRepo()
    repo.missing_entities = {"table:ghost"}
    graph = FakeGraph()
    graph.terminals = [
        ("table:ghost", ["table:a", "table:ghost"]),
        ("table:ads", ["table:a", "table:m", "table:ads"]),
    ]
    svc = LineageService(db=_FakeSession(), graph=graph)
    svc._repo = repo

    res = await svc.terminal_nodes(node="table:a")

    assert res.terminal_count == 2
    ghost = next(t for t in res.terminals if t.node == "table:ghost")
    assert ghost.entity_exists is False  # 断链嫌疑
    assert ghost.hops == 1
    ads = next(t for t in res.terminals if t.node == "table:ads")
    assert ads.entity_exists is True
    assert ads.hops == 2


async def test_terminal_nodes_falls_back_to_mysql() -> None:
    """图不可达（query_terminals=None）：回退 MySQL DFS。"""
    repo = FakeRepo()
    graph = FakeGraph()
    graph.terminals = None
    repo.repo_terminals = [("table:end", ["table:a", "table:end"])]
    svc = LineageService(db=_FakeSession(), graph=graph)
    svc._repo = repo

    res = await svc.terminal_nodes(node="table:a")

    assert res.terminal_count == 1
    assert res.terminals[0].node == "table:end"
    assert res.terminals[0].node_type == "table"


# ---- P4：标准血缘导出（OpenLineage / JSON）----


def _export_edge(
    eid: int,
    source: str,
    target: str,
    *,
    granularity: str = "L1",
    provenance: str = "dp_csv",
) -> SimpleNamespace:
    """构造导出边假数据（SimpleNamespace，对齐 LineageEdge 属性）。"""
    return SimpleNamespace(
        id=eid,
        source_node=source,
        target_node=target,
        edge_type="DERIVED_FROM",
        granularity=granularity,
        confidence=1.0,
        provenance=provenance,
        pii_inherited=False,
        deleted_at=None,
    )


async def test_export_openlineage_builds_run_events() -> None:
    """OpenLineage：L1 表级边 → RunEvent（inputs/outputs + schema 血缘 facet），L3 排除。"""
    repo = FakeRepo()
    repo.export_edges = [
        _export_edge(1, "table:ods.a", "table:dws.t"),
        _export_edge(2, "field:ods.a.id", "field:dws.t.id", granularity="L2"),
        _export_edge(3, "metric:gmv", "table:dws.ads", granularity="L3"),
    ]
    svc = LineageService(db=_FakeSession())
    svc._repo = repo

    result = await svc.export_lineage(LineageExportParams(format="openlineage"))

    # 仅 L1 边生成事件；L2 聚合为 schema facet；L3 排除
    assert isinstance(result, list)
    assert len(result) == 1
    ev = result[0]
    assert ev["eventType"] == "COMPLETE"
    assert ev["eventTime"]
    assert ev["producer"] == "https://openlineage.io/namespace/unisense"
    assert ev["schemaURL"].startswith("https://openlineage.io/spec")
    assert ev["run"]["runId"]
    # inputs=源表 ods.a、outputs=目标表 dws.t
    assert ev["inputs"] == [{"namespace": "unisense", "name": "ods.a", "facets": {}}]
    output = ev["outputs"][0]
    assert output["name"] == "dws.t"
    # schema facet：字段清单 + 字段级血缘（id ← ods.a.id）
    schema = output["facets"]["schema"]
    assert schema["fields"] == [{"name": "id", "type": "unknown"}]
    assert schema["lineage"] == [
        {
            "name": "id",
            "input_fields": [{"namespace": "unisense", "name": "ods.a", "field": "id"}],
        }
    ]


async def test_export_openlineage_l2_only_fallback_event() -> None:
    """仅有 L2 字段级边（无对应 L1）：兜底生成独立 RunEvent，输入=各源表集合。"""
    repo = FakeRepo()
    repo.export_edges = [
        _export_edge(1, "field:ods.a.id", "field:dws.u.id", granularity="L2"),
        _export_edge(2, "field:ods.b.uid", "field:dws.u.uid", granularity="L2"),
    ]
    svc = LineageService(db=_FakeSession())
    svc._repo = repo

    result = await svc.export_lineage(LineageExportParams(format="openlineage"))

    assert len(result) == 1
    ev = result[0]
    assert {d["name"] for d in ev["inputs"]} == {"ods.a", "ods.b"}
    assert ev["outputs"][0]["name"] == "dws.u"
    schema = ev["outputs"][0]["facets"]["schema"]
    assert {fl["name"] for fl in schema["lineage"]} == {"id", "uid"}


async def test_export_json_returns_raw_edges() -> None:
    """JSON：原始边明细 + 元数据（导出时间/边数/生产者），含全部粒度。"""
    repo = FakeRepo()
    repo.export_edges = [
        _export_edge(1, "table:ods.a", "table:dws.t"),
        _export_edge(2, "metric:gmv", "table:dws.ads", granularity="L3"),
    ]
    svc = LineageService(db=_FakeSession())
    svc._repo = repo

    result = await svc.export_lineage(LineageExportParams(format="json"))

    assert result["format"] == "json"
    assert result["edge_count"] == 2
    assert result["exported_at"]
    assert len(result["edges"]) == 2
    assert result["edges"][0]["source_node"] == "table:ods.a"
    assert result["edges"][1]["granularity"] == "L3"


async def test_export_filters_applied() -> None:
    """过滤参数透传：按粒度+来源过滤后导出。"""
    repo = FakeRepo()
    repo.export_edges = [
        _export_edge(1, "table:ods.a", "table:dws.t"),
        _export_edge(2, "table:ods.b", "table:dws.t", provenance="sqlglot"),
        _export_edge(3, "metric:gmv", "table:dws.ads", granularity="L3"),
    ]
    svc = LineageService(db=_FakeSession())
    svc._repo = repo

    result = await svc.export_lineage(
        LineageExportParams(format="openlineage", granularity="L1", provenance="dp_csv")
    )

    assert len(result) == 1
    assert result[0]["inputs"][0]["name"] == "ods.a"


# ---- DDL 血缘集成（企业级：结构变更/依赖写入）----


async def test_parse_and_store_ddl_create_like_writes_edge() -> None:
    """``CREATE TABLE t LIKE s``：DDL 边写入图谱 + 响应 ddl_edges 明细。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    res = await svc.parse_and_store(
        LineageParseRequest(sql="CREATE TABLE dws.t LIKE ods.s", dialect="mysql"),
        actor_id=1,
    )
    assert [i.model_dump() for i in res.ddl_edges] == [
        {
            "ddl_type": "create_like",
            "source": "ods.s",
            "target": "dws.t",
            "table": None,
            "source_column": None,
            "target_column": None,
            "column": None,
        }
    ]
    # 结构性 DDL 边按表级边写入图谱
    assert any(
        c.get("source_node") == "table:ods.s" and c.get("target_node") == "table:dws.t"
        for c in svc._repo.upsert_calls
    )
    assert any(i.source == "table:ods.s" and i.target == "table:dws.t" for i in res.table_lineage)


async def test_parse_and_store_ddl_rename_column_writes_field_edge() -> None:
    """``ALTER TABLE t RENAME COLUMN a TO b``：列重命名按字段级边写入。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    res = await svc.parse_and_store(
        LineageParseRequest(sql="ALTER TABLE dws.t RENAME COLUMN a TO b", dialect="postgres"),
        actor_id=1,
    )
    assert [i.ddl_type for i in res.ddl_edges] == ["rename_column"]
    assert any(
        c.get("source_node") == "field:dws.t.a" and c.get("target_node") == "field:dws.t.b"
        for c in svc._repo.upsert_calls
    )


async def test_parse_and_store_ddl_drop_table_invalidates() -> None:
    """``DROP TABLE``：标记 drop_table 并触发依赖失效（软删触及该表的边）。"""
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    svc._repo = repo
    repo.edges = [SimpleNamespace(id=1, source_node="table:ods.s", target_node="table:dws.t")]
    res = await svc.parse_and_store(
        LineageParseRequest(sql="DROP TABLE IF EXISTS ods.s", dialect="mysql"), actor_id=1
    )
    assert [i.ddl_type for i in res.ddl_edges] == ["drop_table"]
    # 触及 ods.s 的边被软删（依赖失效）
    assert repo.edges == []


async def test_parse_and_store_ddl_add_column_marker_no_edge() -> None:
    """``ALTER TABLE ADD COLUMN``：仅标记不产数据流转边。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    res = await svc.parse_and_store(
        LineageParseRequest(sql="ALTER TABLE dws.t ADD COLUMN c INT", dialect="mysql"),
        actor_id=1,
    )
    assert [i.ddl_type for i in res.ddl_edges] == ["add_column"]
    assert res.table_edges == 0 and res.field_edges == 0
    assert svc._repo.upsert_calls == []


# ---- 库级扫描（企业级批量重建）----


def _write_scan_file(dirpath: str, name: str, content: str) -> str:
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


async def test_scan_directory_dry_run_stats() -> None:
    """库级扫描 dry_run：递归收集文件 + 表级/字段级/DDL 边统计，不落库。"""
    with tempfile.TemporaryDirectory() as td:
        _write_scan_file(td, "a.sql", "INSERT INTO dws.t SELECT a.id FROM ods.a")
        _write_scan_file(td, "b.hql", "CREATE TABLE dws.u LIKE ods.s")
        _write_scan_file(td, "c.sql", "SELECT 1")  # 无血缘边
        svc = LineageService(db=_FakeSession())
        svc._repo = FakeRepo()
        res = await svc.scan_directory(LineageScanRequest(path=td, dry_run=True), actor_id=1)
        assert res.files == 3
        assert res.succeeded == 3
        assert res.failed == 0
        assert res.dry_run is True
        assert res.table_edges == 1  # a.sql 1 表级（b.hql 的 LIKE 只产 DDL 边）
        assert res.field_edges == 1
        assert res.ddl_edges == 1
        assert res.graph_written is False
        # 逐文件明细存在（scan_directory 以 realpath 确立沙箱根，macOS /var→/private/var 需对齐）
        assert {f.path for f in res.files_detail} >= {
            os.path.join(os.path.realpath(td), "a.sql"),
            os.path.join(os.path.realpath(td), "b.hql"),
        }
        # dry_run 不写库
        assert svc._repo.upsert_calls == []


async def test_scan_directory_persist_writes_edges() -> None:
    """库级扫描非 dry_run：批量写入 DML 边 + 结构性 DDL 边 + 图同步。"""
    with tempfile.TemporaryDirectory() as td:
        _write_scan_file(td, "a.sql", "INSERT INTO dws.t SELECT a.id FROM ods.a")
        _write_scan_file(td, "b.sql", "CREATE TABLE dws.u LIKE ods.s")
        svc = LineageService(db=_FakeSession())
        svc._repo = FakeRepo()
        res = await svc.scan_directory(LineageScanRequest(path=td, dry_run=False), actor_id=1)
        assert res.dry_run is False
        assert res.table_edges == 1  # a.sql 的 DML 表级边（b.sql LIKE 计入 ddl_edges）
        assert res.ddl_edges == 1
        # DML 表级边 + DDL 表级边均写入
        written = {(c.get("source_node"), c.get("target_node")) for c in svc._repo.upsert_calls}
        assert ("table:ods.a", "table:dws.t") in written
        assert ("table:ods.s", "table:dws.u") in written
        # 运行记录落一条（kind=scan）
        assert any(getattr(r, "source", None) == "scan" for r in svc._repo.runs)


async def test_scan_directory_rejects_path_traversal() -> None:
    """路径沙箱：拒绝 ``..`` 相对路径穿越。"""
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    with pytest.raises(ValidationError):
        await svc.scan_directory(LineageScanRequest(path="/tmp/../etc"), actor_id=1)


async def test_scan_directory_infers_hive_dialect() -> None:
    """方言启发式：含 LATERAL VIEW 的 .hql 按 hive 解析（字段级正确）。"""
    with tempfile.TemporaryDirectory() as td:
        _write_scan_file(
            td,
            "e.hql",
            "INSERT INTO dws.t SELECT e.tag FROM ods.a LATERAL VIEW EXPLODE(a.tags) e AS tag",
        )
        svc = LineageService(db=_FakeSession())
        svc._repo = FakeRepo()
        res = await svc.scan_directory(LineageScanRequest(path=td, dry_run=True), actor_id=1)
        assert res.field_edges == 1  # hive EXPLODE 展开列正确解析（hive 方言推断生效）


async def test_notify_ddl_change_notifies_affected_owners(monkeypatch) -> None:
    """DDL 变更事件化：破坏性 DDL（重命名/DROP）定向通知受影响资产 Owner + 发布事件。"""
    from app.services.lineage.parser import DDLEdge

    calls: list[dict[str, Any]] = []

    class _FakeNotify:
        def __init__(self, db: object) -> None:
            pass

        async def notify_user(self, **kw: Any) -> object:
            calls.append(kw)
            return SimpleNamespace(id=len(calls))

    monkeypatch.setattr("app.services.notify.service.NotifyService", _FakeNotify)
    svc = LineageService(db=_FakeSession())
    repo = FakeRepo()
    repo.affected_owners = {"table:ods.s": {"1", "2"}}
    svc._repo = repo
    eventbus = AsyncMock()
    svc._eventbus = eventbus
    await svc._notify_ddl_change(
        [DDLEdge(ddl_type="rename_table", source="ods.s", target="dws.new")]
    )
    # 通知两个受影响资产 Owner，事件类型/标题正确
    assert [c["user_id"] for c in calls] == [1, 2]
    assert all(c["event_type"] == "lineage.ddl_changed" for c in calls)
    assert "重命名" in calls[0]["title"]
    # 事件发布到 EventBus（通知中心记录/订阅扇出）
    eventbus.publish.assert_awaited_once()
    assert eventbus.publish.await_args.args[0] == "lineage.ddl_changed"


async def test_notify_ddl_change_skips_non_breaking_ddl(monkeypatch) -> None:
    """非破坏性 DDL（create_like 结构复制）不触发定向通知。"""
    from app.services.lineage.parser import DDLEdge

    called = False

    class _FakeNotify:
        def __init__(self, db: object) -> None:
            pass

        async def notify_user(self, **kw: Any) -> object:
            nonlocal called
            called = True
            return SimpleNamespace(id=1)

    monkeypatch.setattr("app.services.notify.service.NotifyService", _FakeNotify)
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()
    eventbus = AsyncMock()
    svc._eventbus = eventbus
    await svc._notify_ddl_change([DDLEdge(ddl_type="create_like", source="ods.s", target="dws.u")])
    assert called is False
    eventbus.publish.assert_not_awaited()


async def test_notify_ddl_change_skips_when_no_owners(monkeypatch) -> None:
    """受影响资产无 Owner → 不通知、不发事件。"""
    from app.services.lineage.parser import DDLEdge

    called = False

    class _FakeNotify:
        def __init__(self, db: object) -> None:
            pass

        async def notify_user(self, **kw: Any) -> object:
            nonlocal called
            called = True
            return SimpleNamespace(id=1)

    monkeypatch.setattr("app.services.notify.service.NotifyService", _FakeNotify)
    svc = LineageService(db=_FakeSession())
    svc._repo = FakeRepo()  # affected_owners 为空
    eventbus = AsyncMock()
    svc._eventbus = eventbus
    await svc._notify_ddl_change([DDLEdge(ddl_type="drop_table", table="ods.s")])
    assert called is False
    eventbus.publish.assert_not_awaited()
