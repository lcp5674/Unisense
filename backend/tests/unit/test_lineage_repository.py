"""lineage repository 单测（内存假 db）。

覆盖：幂等 upsert + 变更历史快照、环检测、指标级边、断链登记、BFS 影响分析与软删。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from app.models.lineage import LineageEdge, LineageEdgeHistory, LineageIngestRun
from app.services.lineage.repository import LineageRepository


class _Row:
    def __init__(
        self,
        edge_id: int,
        source_node: str,
        target_node: str,
        edge_type: str = "DERIVED_FROM",
        granularity: str = "L1",
        confidence: float = 1.0,
        provenance: str = "sqlglot",
        pii_inherited: bool = False,
        owner: str | None = None,
        deleted_at: Any | None = None,
    ) -> None:
        self.id = edge_id
        self.source_node = source_node
        self.target_node = target_node
        self.edge_type = edge_type
        self.granularity = granularity
        self.confidence = confidence
        self.provenance = provenance
        self.pii_inherited = pii_inherited
        self.owner = owner
        self.deleted_at = deleted_at


class _HistRow:
    """血缘边变更历史行（edge_history_by_key 用）。"""

    def __init__(
        self,
        edge_id: int,
        source_node: str,
        target_node: str,
        edge_type: str = "DERIVED_FROM",
        granularity: str = "L1",
        confidence: float = 1.0,
        provenance: str = "sqlglot",
        pii_inherited: bool = False,
        change_reason: str = "manual",
        created_at: Any | None = None,
    ) -> None:
        self.id = edge_id
        self.source_node = source_node
        self.target_node = target_node
        self.edge_type = edge_type
        self.granularity = granularity
        self.confidence = confidence
        self.provenance = provenance
        self.pii_inherited = pii_inherited
        self.change_reason = change_reason
        self.created_at = created_at


class _Result:
    def __init__(
        self,
        rows: list[Any] | None = None,
        *,
        scalar: Any | None = None,
        rowcount: int | None = None,
    ) -> None:
        self._rows = rows or []
        self._scalar = scalar
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> Any | None:
        return self._scalar

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self._rows


def _extract(sql: str, column: str) -> str | None:
    m = re.search(rf"\b{column}\s*=\s*'((?:[^'\\]|\\.)*)'", sql)
    return m.group(1) if m else None


def _extract_in_list(sql: str, column: str) -> list[str] | None:
    """提取 ``column IN ('a', 'b')`` 列表（P2 分层 BFS 批量边查询用）。"""
    m = re.search(rf"\b{column}\s+IN\s*\(([^)]*)\)", sql, re.IGNORECASE)
    if not m:
        return None
    items = [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]
    return items or None


class _MetaRow:
    """节点元数据查询行（resolve_node_meta 用）：metric 或 db_catalog 行。"""

    def __init__(
        self,
        *,
        table: str,
        metric_code: str | None = None,
        domain: str | None = None,
        pii_flag: bool = False,
        owner_id: int | None = None,
        catalog_id: int | None = None,
        entity_name: str | None = None,
        sensitivity_level: str | None = None,
    ) -> None:
        self.table = table
        self.metric_code = metric_code
        self.domain = domain
        self.pii_flag = pii_flag
        self.owner_id = owner_id
        self.id = catalog_id
        self.entity_name = entity_name
        self.sensitivity_level = sensitivity_level


class _FakeDB:
    def __init__(
        self,
        rows: list[_Row],
        meta_rows: list[_MetaRow] | None = None,
        history_rows: list[_HistRow] | None = None,
        ingest_runs: list[LineageIngestRun] | None = None,
    ) -> None:
        self._rows: list[Any] = list(rows)
        self._meta_rows: list[Any] = list(meta_rows or [])
        self._history_rows: list[Any] = list(history_rows or [])
        self._ingest_runs: list[Any] = list(ingest_runs or [])
        self.added: list[Any] = []
        self.flushed = False

    async def execute(self, stmt: object) -> _Result:
        # SQLAlchemy 编译产物含换行缩进：折叠空白以便按子串匹配分支
        sql = re.sub(r"\s+", " ", str(stmt.compile(compile_kwargs={"literal_binds": True})))
        # 节点元数据：metric 表（单值等值条件按 metric_code 过滤并给 scalar）
        if " FROM metric " in sql:
            rows = [r for r in self._meta_rows if r.table == "metric"]
            code = _extract(sql, "metric_code")
            if code is not None:
                rows = [r for r in rows if getattr(r, "metric_code", None) == code]
            return _Result(rows, scalar=rows[0] if len(rows) == 1 else None)
        # 节点元数据：db_catalog join data_source（单值等值条件按 entity_name 过滤）
        if " FROM db_catalog " in sql or " JOIN data_source " in sql:
            rows = [r for r in self._meta_rows if r.table == "catalog"]
            name = _extract(sql, "entity_name")
            if name is not None:
                rows = [r for r in rows if getattr(r, "entity_name", None) == name]
            return _Result(rows, scalar=rows[0] if len(rows) == 1 else None)
        # 健康度聚合：stale_edge_count（count + stale 过滤）与全量 count
        if " FROM lineage_edge " in sql and "count(" in sql.lower():
            stale_only = "stale" in sql.lower()
            n = sum(
                1
                for r in self._rows
                if getattr(r, "deleted_at", None) is None
                and (not stale_only or getattr(r, "stale", False))
            )
            return _Result([], scalar=n)
        # 健康度表节点数：distinct table: 节点（source/target union，repository 端去重过滤）
        if " FROM lineage_edge " in sql and "distinct" in sql.lower():
            col = "source_node" if "source_node" in sql else "target_node"
            return _Result([(getattr(r, col),) for r in self._rows])
        # 健康度新鲜度：max(run_at) over lineage_ingest_run
        if "from lineage_ingest_run" in sql.lower() and "max(" in sql.lower():
            run_at: Any = None
            for r in self._ingest_runs:
                at = getattr(r, "run_at", None)
                if at is not None and (run_at is None or at > run_at):
                    run_at = at
            return _Result([], scalar=run_at)
        # 边变更历史（Task D：edge_history_by_key）按唯一键过滤
        if " FROM lineage_edge_history " in sql:
            sn = _extract(sql, "source_node")
            tn = _extract(sql, "target_node")
            et = _extract(sql, "edge_type")
            gr = _extract(sql, "granularity")
            return _Result(
                [
                    h
                    for h in self._history_rows
                    if getattr(h, "source_node", None) == sn
                    and getattr(h, "target_node", None) == tn
                    and getattr(h, "edge_type", None) == et
                    and getattr(h, "granularity", None) == gr
                ]
            )
        # 按主键取未删除边（Task D：get_edge）
        if (
            " FROM lineage_edge " in sql
            and "deleted_at IS NULL" in sql
            and " UNION " not in sql
            and "id =" in sql
        ):
            m = re.search(r"lineage_edge\.id\s*=\s*(\d+)", sql)
            eid = int(m.group(1)) if m else None
            matched = [r for r in self._rows if getattr(r, "id", None) == eid]
            return _Result(matched, scalar=matched[0] if matched else None)
        # 血缘导出/全量列表（P4 list_export_edges / list_all_edges）：
        # SELECT ... FROM lineage_edge WHERE deleted_at IS NULL [AND 过滤] ORDER BY id
        if " FROM lineage_edge " in sql and "order by lineage_edge.id" in sql.lower():
            rows = [r for r in self._rows if getattr(r, "deleted_at", None) is None]
            gran = _extract(sql, "granularity")
            if gran is not None:
                rows = [r for r in rows if getattr(r, "granularity", None) == gran]
            prov = _extract(sql, "provenance")
            if prov is not None:
                rows = [r for r in rows if getattr(r, "provenance", None) == prov]
            src = _extract(sql, "source_node")
            dst = _extract(sql, "target_node")
            if src is not None and dst is not None:
                rows = [r for r in rows if r.source_node == src or r.target_node == src]
            elif src is not None:
                rows = [r for r in rows if r.source_node == src]
            elif dst is not None:
                rows = [r for r in rows if r.target_node == dst]
            m_limit = re.search(r"\blimit\s+(\d+)", sql, re.IGNORECASE)
            if m_limit:
                rows = rows[: int(m_limit.group(1))]
            return _Result(list(rows))
        if sql.lstrip().upper().startswith("DELETE"):
            node = _extract(sql, "source_node")
            matched = [
                r
                for r in self._rows
                if getattr(r, "source_node", None) == node
                or getattr(r, "target_node", None) == node
            ]
            for r in matched:
                self._rows.remove(r)
            return _Result([], rowcount=len(matched))
        # 软删：UPDATE ... SET deleted_at 从活跃行移除（软删语义）
        #  - soft_delete_edge_by_key：WHERE 含 (source AND target AND edge_type)，精确单条
        #  - soft_delete_by_node：WHERE 仅 source/target（无 edge_type），级联整节点
        if sql.lstrip().upper().startswith("UPDATE") and "deleted_at" in sql:
            etype = _extract(sql, "edge_type")
            if etype is not None:
                src = _extract(sql, "source_node")
                dst = _extract(sql, "target_node")
                matched = [
                    r
                    for r in self._rows
                    if getattr(r, "source_node", None) == src
                    and getattr(r, "target_node", None) == dst
                    and getattr(r, "edge_type", None) == etype
                ]
            else:
                node = _extract(sql, "source_node")
                matched = [
                    r
                    for r in self._rows
                    if getattr(r, "source_node", None) == node
                    or getattr(r, "target_node", None) == node
                ]
            # 恢复（SET deleted_at = NULL）：仅统计命中行，不移除（与软删对称）
            if "DELETED_AT=NULL" in sql.upper().replace(" ", ""):
                return _Result([], rowcount=len(matched))
            for r in matched:
                self._rows.remove(r)
            return _Result([], rowcount=len(matched))
        # upsert 精确查找：WHERE 同时含 edge_type/granularity 字面量等值条件
        if "edge_type = '" in sql and "granularity = '" in sql:
            src = _extract(sql, "source_node")
            dst = _extract(sql, "target_node")
            etype = _extract(sql, "edge_type")
            gran = _extract(sql, "granularity")
            rows = [
                r
                for r in self._rows
                if r.source_node == src
                and r.target_node == dst
                and r.edge_type == etype
                and r.granularity == gran
            ]
            return _Result(rows, scalar=rows[0] if len(rows) == 1 else None)
        src = _extract(sql, "source_node")
        if src is not None:
            dst = _extract(sql, "target_node")
            etype = _extract(sql, "edge_type")
            if dst is not None and etype is not None:
                # soft_delete_edge_by_key：按 (source, target, edge_type) 精确取单条并给 scalar
                rows = [
                    r
                    for r in self._rows
                    if r.source_node == src
                    and r.target_node == dst
                    and r.edge_type == etype
                    and getattr(r, "deleted_at", None) is None
                ]
                return _Result(rows, scalar=rows[0] if len(rows) == 1 else None)
            # edges_for_node 等：按 source 匹配（deleted_at IS NULL 已由 WHERE 表达）
            return _Result(
                [
                    r
                    for r in self._rows
                    if r.source_node == src and getattr(r, "deleted_at", None) is None
                ]
            )
        # P2（审查修复）：批量下游边 source_node IN (...) 查询（分层 BFS）
        if " FROM lineage_edge " in sql and "source_node in" in sql.lower():
            src_in = _extract_in_list(sql, "source_node")
            return _Result(
                [
                    r
                    for r in self._rows
                    if getattr(r, "source_node", None) in (src_in or [])
                    and getattr(r, "deleted_at", None) is None
                ]
            )
        dst = _extract(sql, "target_node")
        if dst is not None:
            return _Result(
                [
                    r
                    for r in self._rows
                    if r.target_node == dst and getattr(r, "deleted_at", None) is None
                ]
            )
        return _Result([])

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        # 让插入的边对后续 upsert/查询可见（幂等查找用）
        if isinstance(obj, LineageEdge):
            self._rows.append(obj)

    async def flush(self) -> None:
        self.flushed = True
        # 模拟 SQLAlchemy flush 持久化：软删行（deleted_at 已置）从活跃集合移除
        self._rows = [r for r in self._rows if getattr(r, "deleted_at", None) is None]


def _histories(db: _FakeDB) -> list[LineageEdgeHistory]:
    return [a for a in db.added if isinstance(a, LineageEdgeHistory)]


def _edges(db: _FakeDB) -> list[LineageEdge]:
    return [a for a in db.added if isinstance(a, LineageEdge)]


async def test_upsert_creates_new() -> None:
    db = _FakeDB([])
    repo = LineageRepository(db)
    edge = await repo.upsert_edge(
        source_node="table:a", target_node="table:t", edge_type="DERIVED_FROM", granularity="L1"
    )
    assert edge.source_node == "table:a"
    assert len(db.added) == 1
    assert not _histories(db)


async def test_upsert_writes_history_on_change() -> None:
    db = _FakeDB(
        [
            _Row(
                1,
                "table:a",
                "table:t",
                edge_type="DERIVED_FROM",
                granularity="L1",
                confidence=0.8,
            )
        ]
    )
    repo = LineageRepository(db)
    edge = await repo.upsert_edge(
        source_node="table:a",
        target_node="table:t",
        edge_type="DERIVED_FROM",
        granularity="L1",
        confidence=0.9,
    )
    assert edge.confidence == 0.9
    histories = _histories(db)
    assert len(histories) == 1
    assert histories[0].confidence == 0.8  # 变更前的快照
    assert histories[0].source_node == "table:a"
    assert histories[0].target_node == "table:t"
    assert histories[0].edge_type == "DERIVED_FROM"
    assert histories[0].change_reason == "reparse"


async def test_upsert_idempotent_no_duplicate_history() -> None:
    db = _FakeDB([])
    repo = LineageRepository(db)
    await repo.upsert_edge(
        source_node="table:a", target_node="table:t", edge_type="DERIVED_FROM", granularity="L1"
    )
    await repo.upsert_edge(
        source_node="table:a", target_node="table:t", edge_type="DERIVED_FROM", granularity="L1"
    )
    assert len(_edges(db)) == 1
    assert not _histories(db)  # 值未变化不重复写历史


async def test_upsert_pii_inherited_change_writes_history() -> None:
    db = _FakeDB(
        [
            _Row(
                1,
                "table:a",
                "table:t",
                edge_type="DERIVED_FROM",
                granularity="L1",
                pii_inherited=False,
            )
        ]
    )
    repo = LineageRepository(db)
    await repo.upsert_edge(
        source_node="table:a",
        target_node="table:t",
        edge_type="DERIVED_FROM",
        granularity="L1",
        pii_inherited=True,
    )
    histories = _histories(db)
    assert len(histories) == 1
    assert histories[0].pii_inherited is False


async def test_record_edge_history_direct() -> None:
    db = _FakeDB([])
    repo = LineageRepository(db)
    edge = await repo.upsert_edge(
        source_node="table:a", target_node="table:t", edge_type="DERIVED_FROM", granularity="L1"
    )
    history = await repo.record_edge_history(edge, "schema_drift")
    assert isinstance(history, LineageEdgeHistory)
    assert history.change_reason == "schema_drift"
    assert history.source_node == "table:a"
    assert history.target_node == "table:t"
    assert history.provenance == "sqlglot"
    assert history.confidence == 1.0


class _ReviveDB:
    """保留软删行的假 db（模拟真实 MySQL：软删行留存于表内，SELECT 按 deleted_at 过滤）。

    ``_FakeDB.flush`` 会把软删行从集合移除，无法覆盖「软删行残留在唯一索引、
    重新解析须复活」的 P0-2 场景，故本测试用此专用 fake。
    """

    def __init__(self, active: list[_Row], tombstones: list[_Row]) -> None:
        self._active = list(active)
        self._tombstones = list(tombstones)
        self.added: list[Any] = []
        self.flushed = False

    async def execute(self, stmt: object) -> _Result:
        sql = re.sub(r"\s+", " ", str(stmt.compile(compile_kwargs={"literal_binds": True})))
        src = _extract(sql, "source_node")
        dst = _extract(sql, "target_node")
        etype = _extract(sql, "edge_type")
        gran = _extract(sql, "granularity")

        def _match(r: _Row) -> bool:
            return (
                r.source_node == src
                and r.target_node == dst
                and r.edge_type == etype
                and r.granularity == gran
            )

        if "deleted_at IS NOT NULL" in sql:
            rows = [r for r in self._tombstones if _match(r)]
            return _Result(rows, scalar=rows[0] if len(rows) == 1 else None)
        if "deleted_at IS NULL" in sql:
            rows = [r for r in self._active if _match(r)]
            return _Result(rows, scalar=rows[0] if len(rows) == 1 else None)
        return _Result([])

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed = True


async def test_upsert_revives_soft_deleted_edge() -> None:
    """P0-2：软删边重新解析/扫描时应复活而非重插，避免撞 uq_lineage_edge 1062。"""
    tombstone = _Row(
        7,
        "table:a",
        "table:t",
        edge_type="DERIVED_FROM",
        granularity="L1",
        confidence=0.8,
        deleted_at=datetime.now(),
    )
    db = _ReviveDB(active=[], tombstones=[tombstone])
    repo = LineageRepository(db)
    edge, created = await repo._upsert_with_created(
        source_node="table:a",
        target_node="table:t",
        edge_type="DERIVED_FROM",
        granularity="L1",
        confidence=0.95,
        change_reason="reparse",
    )
    assert created is False  # 复用软删行，非新插入
    assert edge is tombstone  # 同一行复活
    assert edge.deleted_at is None  # 软删标记清除
    assert edge.confidence == 0.95  # 新值应用
    histories = [a for a in db.added if isinstance(a, LineageEdgeHistory)]
    assert len(histories) == 1
    assert histories[0].change_reason == "revive:reparse"
    assert histories[0].confidence == 0.8  # 复活前快照


async def test_upsert_inserts_when_no_tombstone() -> None:
    """无活跃行也无软删行时仍走新建路径（created=True）。"""
    db = _ReviveDB(active=[], tombstones=[])
    repo = LineageRepository(db)
    edge, created = await repo._upsert_with_created(
        source_node="table:a",
        target_node="table:t",
        edge_type="DERIVED_FROM",
        granularity="L1",
    )
    assert created is True
    assert len(db.added) == 1
    assert edge.source_node == "table:a"


async def test_would_create_cycle_returns_true() -> None:
    rows = [
        _Row(1, "table:a", "table:b"),
        _Row(2, "table:b", "table:c"),
    ]
    db = _FakeDB(rows)
    repo = LineageRepository(db)
    edge = LineageEdge(
        source_node="table:c", target_node="table:a", edge_type="DERIVED_FROM", granularity="L1"
    )
    assert await repo.would_create_cycle(edge) is True


async def test_would_create_cycle_returns_false() -> None:
    rows = [
        _Row(1, "table:a", "table:b"),
        _Row(2, "table:b", "table:c"),
    ]
    db = _FakeDB(rows)
    repo = LineageRepository(db)
    edge = LineageEdge(
        source_node="table:d", target_node="table:a", edge_type="DERIVED_FROM", granularity="L1"
    )
    assert await repo.would_create_cycle(edge) is False


async def test_would_create_cycle_self_loop_is_cycle() -> None:
    db = _FakeDB([])
    repo = LineageRepository(db)
    edge = LineageEdge(
        source_node="table:x", target_node="table:x", edge_type="DERIVED_FROM", granularity="L1"
    )
    assert await repo.would_create_cycle(edge) is True


async def test_would_create_cycle_ignores_non_derived() -> None:
    rows = [
        _Row(1, "table:a", "table:b", edge_type="LINEAGE_DOWN", granularity="L1"),
    ]
    db = _FakeDB(rows)
    repo = LineageRepository(db)
    edge = LineageEdge(
        source_node="table:b", target_node="table:a", edge_type="LINEAGE_DOWN", granularity="L1"
    )
    assert await repo.would_create_cycle(edge) is False


async def test_upsert_metric_edge() -> None:
    db = _FakeDB([])
    repo = LineageRepository(db)
    edge = await repo.upsert_metric_edge(from_metric="gmv", to_metric="gmv_yoy")
    assert edge.source_node == "metric:gmv"
    assert edge.target_node == "metric:gmv_yoy"
    assert edge.edge_type == "DERIVED_FROM"
    assert edge.granularity == "L3"
    assert edge.provenance == "sqlglot"


async def test_upsert_metric_edge_idempotent() -> None:
    db = _FakeDB([])
    repo = LineageRepository(db)
    await repo.upsert_metric_edge(from_metric="gmv", to_metric="gmv_yoy")
    await repo.upsert_metric_edge(from_metric="gmv", to_metric="gmv_yoy")
    assert len(_edges(db)) == 1
    assert not _histories(db)


async def test_register_break_downstream() -> None:
    db = _FakeDB([])
    repo = LineageRepository(db)
    edge = await repo.register_break(node="table:orders", external_system="hive", owner="alice")
    assert edge.source_node == "table:orders"
    assert edge.target_node == "external:hive"
    assert edge.edge_type == "EXTERNAL_BREAK"
    assert edge.granularity == "L1"
    assert edge.owner == "alice"


async def test_register_break_upstream() -> None:
    db = _FakeDB([])
    repo = LineageRepository(db)
    edge = await repo.register_break(
        node="table:orders", external_system="hive", owner="bob", direction="upstream"
    )
    assert edge.source_node == "external:hive"
    assert edge.target_node == "table:orders"


async def test_register_break_idempotent() -> None:
    db = _FakeDB([])
    repo = LineageRepository(db)
    await repo.register_break(node="table:orders", external_system="hive", owner="alice")
    await repo.register_break(node="table:orders", external_system="hive", owner="alice")
    assert len(_edges(db)) == 1
    assert not _histories(db)


async def test_register_break_owner_change_writes_history() -> None:
    db = _FakeDB(
        [
            _Row(
                1,
                "table:orders",
                "external:hive",
                edge_type="EXTERNAL_BREAK",
                granularity="L1",
                provenance="manual",
                owner=None,
            )
        ]
    )
    repo = LineageRepository(db)
    await repo.register_break(node="table:orders", external_system="hive", owner="carol")
    histories = _histories(db)
    assert len(histories) == 1
    assert histories[0].change_reason == "manual"
    assert histories[0].source_node == "table:orders"  # 快照记录的是边当前值
    assert histories[0].target_node == "external:hive"


async def test_query_impact_bfs_downstream() -> None:
    rows = [
        _Row(1, "table:a", "table:b"),
        _Row(2, "table:b", "table:c"),
        _Row(3, "table:c", "table:d"),
    ]
    db = _FakeDB(rows)
    repo = LineageRepository(db)
    out = await repo.query_impact("table:a", direction="downstream", max_hops=5)
    assert len(out) == 3
    nodes = {e.target_node for e in out} | {e.source_node for e in out}
    assert {"table:b", "table:c", "table:d"} <= nodes


async def test_query_impact_upstream() -> None:
    rows = [
        _Row(1, "table:a", "table:b"),
        _Row(2, "table:b", "table:c"),
    ]
    db = _FakeDB(rows)
    repo = LineageRepository(db)
    out = await repo.query_impact("table:c", direction="upstream", max_hops=5)
    assert len(out) == 2
    assert {e.source_node for e in out} == {"table:b", "table:a"}


async def test_query_impact_both() -> None:
    rows = [
        _Row(1, "table:a", "table:b"),
        _Row(2, "table:b", "table:c"),
    ]
    db = _FakeDB(rows)
    repo = LineageRepository(db)
    out = await repo.query_impact("table:b", direction="both", max_hops=5)
    assert len(out) == 2


async def test_query_impact_respects_max_hops() -> None:
    rows = [
        _Row(1, "table:a", "table:b"),
        _Row(2, "table:b", "table:c"),
    ]
    db = _FakeDB(rows)
    repo = LineageRepository(db)
    out = await repo.query_impact("table:a", direction="downstream", max_hops=1)
    assert len(out) == 1
    assert out[0].target_node == "table:b"


async def test_soft_delete_by_node() -> None:
    rows = [
        _Row(1, "table:a", "table:b"),
        _Row(2, "table:b", "table:c"),
    ]
    db = _FakeDB(rows)
    repo = LineageRepository(db)
    n = await repo.soft_delete_by_node("table:b")
    assert n == 2
    assert db.flushed is True
    assert len(db._rows) == 0


async def test_restore_by_node() -> None:
    """restore_by_node 与 soft_delete_by_node 对称：仅清软删行的 deleted_at，统计命中数。"""
    rows = [
        _Row(1, "metric:a", "table:t"),
        _Row(2, "table:t", "metric:b"),
    ]
    db = _FakeDB(rows)
    repo = LineageRepository(db)
    n = await repo.restore_by_node("metric:a")
    assert n == 1
    assert db.flushed is True
    # 恢复不移除行（软删语义对称：活跃行集合不变）
    assert len(db._rows) == 2


# ---- 增量采集 / 失效管理（mark_seen / mark_missing）----


class _StaleDB:
    """面向 mark_seen/mark_missing 的假 db：execute 返回预设 LineageEdge 列表。"""

    def __init__(self, edges: list[LineageEdge]) -> None:
        self.edges = edges
        self.flushes = 0

    async def execute(self, stmt: object) -> _Result:
        return _Result(rows=list(self.edges))

    async def flush(self) -> None:
        self.flushes += 1


def _l1_edge(
    source: str,
    target: str,
    *,
    provenance: str = "dp_csv",
    last_seen: Any | None = None,
    missing: int = 0,
    stale: bool = False,
) -> LineageEdge:
    edge = LineageEdge(
        source_node=f"table:{source}",
        target_node=f"table:{target}",
        edge_type="DERIVED_FROM",
        granularity="L1",
        provenance=provenance,
    )
    edge.last_seen_at = last_seen
    edge.missing_count = missing
    edge.stale = stale
    return edge


async def test_mark_seen_refreshes_and_restores() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    stale_edge = _l1_edge("a", "b", last_seen=now, missing=2, stale=True)
    fresh = _l1_edge("c", "d", last_seen=now, missing=0, stale=False)
    repo = LineageRepository(_StaleDB([stale_edge, fresh]))

    confirmed, restored = await repo.mark_seen("dp_csv", {("table:a", "table:b")})

    assert confirmed == 1
    assert restored == 1
    assert stale_edge.stale is False
    assert stale_edge.missing_count == 0
    assert stale_edge.stale_since is None
    assert stale_edge.last_seen_at is not None
    # 未命中的边不受影响
    assert fresh.missing_count == 0
    assert fresh.stale is False


async def test_mark_missing_flags_stale_at_threshold() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    below = _l1_edge("a", "b", last_seen=now, missing=1, stale=False)
    at_threshold = _l1_edge("c", "d", last_seen=now, missing=2, stale=False)
    never_seen = _l1_edge("e", "f", last_seen=None, missing=0, stale=False)
    repo = LineageRepository(_StaleDB([below, at_threshold, never_seen]))

    missing, flagged = await repo.mark_missing("dp_csv", set(), threshold=3)

    assert missing == 2
    assert flagged == 1
    assert below.missing_count == 2 and below.stale is False
    assert at_threshold.missing_count == 3 and at_threshold.stale is True
    # 从未被确认过的边不参与失效检测
    assert never_seen.missing_count == 0 and never_seen.stale is False


async def test_mark_missing_skips_seen_pairs() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    edge = _l1_edge("a", "b", last_seen=now, missing=0, stale=False)
    repo = LineageRepository(_StaleDB([edge]))

    missing, flagged = await repo.mark_missing("dp_csv", {("table:a", "table:b")}, threshold=3)

    assert missing == 0
    assert flagged == 0
    assert edge.missing_count == 0


# ---- 采集通道总览 / 候选节点（list_channels / list_nodes）----


class _AggRow:
    """支持索引与迭代解包的聚合结果行（对齐 SQLAlchemy Row 访问约定）。"""

    def __init__(self, *values: object) -> None:
        self._values = values

    def __getitem__(self, index: int) -> object:
        return self._values[index]

    def __iter__(self):
        return iter(self._values)


class _AggregateDB:
    """按 SQL 特征返回聚合行的假 db（list_channels / list_nodes 用）。"""

    def __init__(
        self,
        *,
        edge_rows: list[tuple[str, int, int]] | None = None,
        node_rows: list[tuple[str, str]] | None = None,
        run: Any | None = None,
        nodes: list[tuple[str, int]] | None = None,
    ) -> None:
        self.edge_rows = edge_rows or []
        self.node_rows = node_rows or []
        self.run = run
        self.nodes = nodes or []
        self.sqls: list[str] = []
        self.flushes = 0

    async def execute(self, stmt: object) -> _Result:
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.sqls.append(sql)
        if "lineage_ingest_run" in sql:
            return _Result(rows=[self.run] if self.run else [], scalar=self.run)
        if "anon_1.node" in sql:
            # list_nodes：候选节点聚合（node, count）
            return _Result(rows=[_AggRow(n, c) for n, c in self.nodes])
        if "anon_1.p" in sql:
            # list_channels 节点数聚合（provenance, count）
            return _Result(rows=[_AggRow(p, c) for p, c in self.node_rows])
        if "lineage_edge.provenance" in sql:
            return _Result(rows=[_AggRow(*r) for r in self.edge_rows])
        return _Result(rows=[])

    async def flush(self) -> None:
        self.flushes += 1


async def test_list_channels_includes_known_channels_when_empty() -> None:
    """0 边时内置已知通道（dp_csv/sqlglot）仍展示，保证采集通道来源全景完整。"""
    db = _AggregateDB()
    repo = LineageRepository(db)
    channels = await repo.list_channels()
    sources = [c["source"] for c in channels]
    assert sources == ["dp_csv", "dp_sql", "sqlglot"]
    assert all(c["edge_count"] == 0 for c in channels)
    assert all(c["node_count"] == 0 for c in channels)


async def test_list_channels_merges_known_channels_with_existing() -> None:
    """有边来源（如 sqlglot）保留计数，缺失的已知通道（dp_csv）补全为 0。"""
    db = _AggregateDB(
        edge_rows=[("sqlglot", 5, 1)],
        node_rows=[("sqlglot", 3)],
    )
    repo = LineageRepository(db)
    channels = {c["source"]: c for c in await repo.list_channels()}
    assert channels["sqlglot"]["edge_count"] == 5
    assert channels["sqlglot"]["node_count"] == 3
    assert channels["sqlglot"]["stale_count"] == 1
    assert channels["dp_csv"]["edge_count"] == 0
    # 有边来源排前（edge_count 倒序）
    assert (await repo.list_channels())[0]["source"] == "sqlglot"


async def test_list_nodes_aggregates_and_filters() -> None:
    """候选节点：源∪目标去重聚合，带 kw 时按节点 id 模糊过滤。"""
    db = _AggregateDB(nodes=[("table:orders", 12), ("table:users", 3)])
    repo = LineageRepository(db)
    rows = await repo.list_nodes(kw="tab", limit=10)
    assert rows == [("table:orders", 12), ("table:users", 3)]
    # 断言 SQL 含 LIKE 模糊过滤与 LIMIT 上限
    assert any("LIKE" in s for s in db.sqls)
    assert any("LIMIT 10" in s for s in db.sqls)


# ---- 运行记录详情快照（detail_json / get_ingest_run）----


class _RunDB:
    """面向 finish_ingest_run / get_ingest_run 的假 db。"""

    def __init__(self, run: LineageIngestRun | None = None) -> None:
        self._run = run
        self.flushes = 0

    async def execute(self, stmt: object) -> _Result:
        return _Result([self._run] if self._run else [], scalar=self._run)

    async def flush(self) -> None:
        self.flushes += 1


def _ingest_run() -> LineageIngestRun:
    run = LineageIngestRun(source="sqlglot", status="running")
    run.id = 1
    return run


async def test_finish_ingest_run_writes_detail_json() -> None:
    """finish_ingest_run 把详情快照序列化写入 detail_json 列。"""
    run = _ingest_run()
    repo = LineageRepository(_RunDB())
    await repo.finish_ingest_run(
        run,
        status="success",
        total_edges=2,
        added=1,
        updated=1,
        detail={
            "kind": "sql_parse",
            "sql": "SELECT 1",
            "table_lineage": [{"source": "a", "target": "t"}],
        },
    )
    assert run.status == "success"
    assert run.added_count == 1
    assert run.detail_json is not None
    payload = json.loads(run.detail_json)
    assert payload["kind"] == "sql_parse"
    assert payload["sql"] == "SELECT 1"


async def test_finish_ingest_run_without_detail_leaves_null() -> None:
    """未传详情时 detail_json 保持 None（兼容既有采集路径）。"""
    run = _ingest_run()
    repo = LineageRepository(_RunDB())
    await repo.finish_ingest_run(run, status="success", total_edges=1)
    assert run.detail_json is None


async def test_get_ingest_run_returns_run() -> None:
    """get_ingest_run 按主键返回运行记录（含 detail_json）。"""
    run = _ingest_run()
    run.detail_json = '{"kind":"batch","added_edges":[["table:a","table:b"]]}'
    repo = LineageRepository(_RunDB(run))
    got = await repo.get_ingest_run(1)
    assert got is not None
    assert got.id == 1
    assert got.source == "sqlglot"
    assert '"added_edges"' in (got.detail_json or "")


async def test_get_ingest_run_missing_returns_none() -> None:
    """不存在的运行记录返回 None。"""
    repo = LineageRepository(_RunDB(None))
    assert await repo.get_ingest_run(999) is None


# ---- 节点元数据解析（resolve_node_meta）----


async def test_resolve_node_meta_metric_and_table() -> None:
    """metric 查 metric 表、table 查 db_catalog join data_source（entity_id/域/PII/Owner）。"""
    db = _FakeDB(
        [],
        meta_rows=[
            _MetaRow(table="metric", metric_code="gmv", domain="sales", pii_flag=True, owner_id=7),
            _MetaRow(
                table="catalog",
                catalog_id=42,
                entity_name="orders",
                domain="sales",
                sensitivity_level="PII-HIGH",
                owner_id=3,
            ),
        ],
    )
    repo = LineageRepository(db)
    out = await repo.resolve_node_meta({"metric:gmv", "table:orders"})
    assert out["metric:gmv"] == {
        "id": "metric:gmv",
        "type": "metric",
        "label": "gmv",
        "entity_id": None,
        "pii": True,
        "domain": "sales",
        "owner": "7",
    }
    assert out["table:orders"]["entity_id"] == 42
    assert out["table:orders"]["pii"] is True  # PII-HIGH 含 PII
    assert out["table:orders"]["domain"] == "sales"
    assert out["table:orders"]["owner"] == "3"


async def test_resolve_node_meta_field_inherits_table_domain() -> None:
    """field 节点展示 表.列 并由所属表继承业务域；external/未知节点仅类型与 label。"""
    db = _FakeDB(
        [],
        meta_rows=[
            _MetaRow(
                table="catalog",
                catalog_id=10,
                entity_name="orders",
                domain="sales",
                sensitivity_level="INTERNAL",
            ),
        ],
    )
    repo = LineageRepository(db)
    out = await repo.resolve_node_meta(
        {"field:orders.amount", "field:orders.user_id", "external:api", "plain_node"}
    )
    assert out["field:orders.amount"]["type"] == "field"
    assert out["field:orders.amount"]["label"] == "orders.amount"
    # 所属表 orders 有目录元数据 → 继承域 sales
    assert out["field:orders.amount"]["domain"] == "sales"
    assert out["field:orders.amount"]["entity_id"] is None
    # 另一个字段同样继承
    assert out["field:orders.user_id"]["domain"] == "sales"
    # external / 未知：仅类型与 label，无目录元数据
    assert out["external:api"]["type"] == "external"
    assert out["external:api"]["label"] == "api"
    assert out["external:api"]["domain"] is None
    assert out["plain_node"]["type"] == "other"
    assert out["plain_node"]["label"] == "plain_node"


async def test_resolve_node_meta_empty_and_unknown() -> None:
    """空节点集返回空；无目录元数据的 metric/table 仅类型与 label（不抛错）。"""
    db = _FakeDB([])
    repo = LineageRepository(db)
    assert await repo.resolve_node_meta(set()) == {}
    out = await repo.resolve_node_meta({"metric:ghost", "table:nonexistent"})
    assert out["metric:ghost"] == {
        "id": "metric:ghost",
        "type": "metric",
        "label": "ghost",
        "entity_id": None,
        "pii": False,
        "domain": None,
        "owner": None,
    }
    assert out["table:nonexistent"] == {
        "id": "table:nonexistent",
        "type": "table",
        "label": "nonexistent",
        "entity_id": None,
        "pii": False,
        "domain": None,
        "owner": None,
    }


# ---- Task D：血缘边详情（get_edge + edge_history_by_key）----


async def test_get_edge_by_id_and_missing() -> None:
    """get_edge 按主键取未删除边；缺失返回 None。"""
    db = _FakeDB([_Row(1, "metric:a", "table:t", "DERIVED_FROM", "L3")])
    repo = LineageRepository(db)
    edge = await repo.get_edge(1)
    assert edge is not None and edge.id == 1
    assert await repo.get_edge(999) is None


async def test_edge_history_by_key() -> None:
    """edge_history_by_key 按边唯一键取变更历史快照。"""
    hist = [
        _HistRow(10, "metric:a", "table:t", "DERIVED_FROM", "L3", change_reason="rename"),
        _HistRow(11, "metric:b", "table:t", "DERIVED_FROM", "L3", change_reason="manual"),
    ]
    db = _FakeDB([], history_rows=hist)
    repo = LineageRepository(db)
    rows = await repo.edge_history_by_key("metric:a", "table:t", "DERIVED_FROM", "L3")
    assert len(rows) == 1
    assert rows[0].change_reason == "rename"


async def test_sync_metric_dimension_edges_removes_stale_and_adds_new() -> None:
    """sync_metric_dimension_edges 差异同步：软删不再声明的维度边、注册新增边。"""
    db = _FakeDB(
        [
            _Row(
                1,
                "metric:m",
                "dimension:dim_store",
                "USES_DIMENSION",
                "L3",
                provenance="metric_definition",
            ),
            _Row(
                2,
                "metric:m",
                "dimension:dim_region",
                "USES_DIMENSION",
                "L3",
                provenance="metric_definition",
            ),
            # 非维度边（其他边类型）不受维度差异同步影响
            _Row(
                3,
                "metric:m",
                "table:dwd_order",
                "DERIVED_FROM",
                "L3",
                provenance="metric_definition",
            ),
        ]
    )
    repo = LineageRepository(db)
    deleted, added = await repo.sync_metric_dimension_edges("m", ["dim_store", "dim_new"])
    assert deleted == 1  # dim_region 不再声明 → 软删
    assert added == 1  # dim_new 新增（dim_store 已存在不计）
    remaining = [r for r in db._rows if r.source_node == "metric:m"]
    targets = sorted(r.target_node for r in remaining)
    # dim_region 已删；dim_store 保留；dim_new 新增；DERIVED_FROM 表边不受影响
    assert targets == ["dimension:dim_new", "dimension:dim_store", "table:dwd_order"]


async def test_sync_metric_dimension_edges_empty_current_clears_all() -> None:
    """sync 空声明集：清理全部残留维度边（指标不再声明任何维度）。"""
    db = _FakeDB(
        [
            _Row(
                1,
                "metric:m",
                "dimension:dim_store",
                "USES_DIMENSION",
                "L3",
                provenance="metric_definition",
            ),
            _Row(
                2,
                "metric:m",
                "dimension:dim_region",
                "USES_DIMENSION",
                "L3",
                provenance="metric_definition",
            ),
        ]
    )
    repo = LineageRepository(db)
    deleted, added = await repo.sync_metric_dimension_edges("m", [])
    assert deleted == 2
    assert added == 0
    assert not [r for r in db._rows if r.source_node == "metric:m"]


async def test_sync_metric_column_edges_removes_stale_and_adds_new() -> None:
    """sync_metric_column_edges 差异同步：软删不再声明的字段边、注册新增字段边。"""
    db = _FakeDB(
        [
            # 入边：column:{table}.{col} → metric:m（READS_COLUMN）
            _Row(
                1,
                "column:dws.gmv.amount",
                "metric:m",
                "READS_COLUMN",
                "L3",
                provenance="metric_definition",
            ),
            _Row(
                2,
                "column:dws.gmv.cnt",
                "metric:m",
                "READS_COLUMN",
                "L3",
                provenance="metric_definition",
            ),
            # 其他边类型不受影响
            _Row(
                3, "metric:other", "metric:m", "DERIVED_FROM", "L3", provenance="metric_definition"
            ),
        ]
    )
    repo = LineageRepository(db)
    deleted, added = await repo.sync_metric_column_edges(
        "m", [("dws.gmv", "amount"), ("dws.gmv", "revenue")]
    )
    assert deleted == 1  # cnt 不再声明 → 软删
    assert added == 1  # revenue 新增（amount 已存在不计）
    remaining = [r for r in db._rows if r.target_node == "metric:m"]
    sources = sorted(r.source_node for r in remaining)
    assert sources == ["column:dws.gmv.amount", "column:dws.gmv.revenue", "metric:other"]


async def test_sync_metric_column_edges_empty_current_clears_all() -> None:
    """sync 空字段集：清理全部残留字段边（指标不再声明任何字段）。"""
    db = _FakeDB(
        [
            _Row(
                1,
                "column:dws.gmv.amount",
                "metric:m",
                "READS_COLUMN",
                "L3",
                provenance="metric_definition",
            ),
            _Row(
                2,
                "column:dws.gmv.cnt",
                "metric:m",
                "READS_COLUMN",
                "L3",
                provenance="metric_definition",
            ),
        ]
    )
    repo = LineageRepository(db)
    deleted, added = await repo.sync_metric_column_edges("m", [])
    assert deleted == 2
    assert added == 0
    assert not [
        r for r in db._rows if r.target_node == "metric:m" and r.edge_type == "READS_COLUMN"
    ]


async def test_sync_metric_table_edges_removes_stale_and_adds_new() -> None:
    """sync_metric_table_edges 差异同步：软删不再声明的落地表/源表边、注册新增。"""
    db = _FakeDB(
        [
            # 落地表边：metric:m → table  (downstream)
            _Row(
                1,
                "metric:m",
                "table:dws.gmv_v1",
                "DERIVED_FROM",
                "L3",
                provenance="metric_definition",
            ),
            _Row(
                2,
                "metric:m",
                "table:dws.gmv_v2",
                "DERIVED_FROM",
                "L3",
                provenance="metric_definition",
            ),
            # 源表边：table → metric:m  (upstream)
            _Row(
                3,
                "table:ods.order",
                "metric:m",
                "DERIVED_FROM",
                "L3",
                provenance="metric_definition",
            ),
            _Row(
                4,
                "table:ods.user",
                "metric:m",
                "DERIVED_FROM",
                "L3",
                provenance="metric_definition",
            ),
            # 指标依赖边（metric:* 节点）不应被表差异同步误删
            _Row(
                5, "metric:dep_a", "metric:m", "DERIVED_FROM", "L3", provenance="metric_definition"
            ),
        ]
    )
    repo = LineageRepository(db)
    deleted, added = await repo.sync_metric_table_edges(
        "m", "dws.gmv_v2", ["ods.order", "ods.item"]
    )
    assert deleted == 2  # 落地表 gmv_v1 + 源表 ods.user 不再声明 → 软删
    assert added == 1  # 源表 ods.item 新增（gmv_v2 与 ods.order 已存在不计）
    remaining = [r for r in db._rows if r.source_node == "metric:m" or r.target_node == "metric:m"]
    keys = sorted((r.source_node, r.target_node) for r in remaining)
    # gmv_v2(落地表) + ods.order(源表) + ods.item(源表) + 依赖边 metric:dep_a 保留
    assert keys == [
        ("metric:dep_a", "metric:m"),
        ("metric:m", "table:dws.gmv_v2"),
        ("table:ods.item", "metric:m"),
        ("table:ods.order", "metric:m"),
    ]


async def test_sync_metric_table_edges_with_downstream_tables() -> None:
    """downstream_tables（下游使用表）并入 current_down：软删缺失 + 注册新增。

    落地表与下游使用表同向（metric → table），差异同步一并管理——指标编辑
    增删下游消费表后，血缘图不残留旧使用表边。
    """
    db = _FakeDB(
        [
            # 落地表边（仍声明 → 保留）
            _Row(
                1, "metric:m", "table:dws.gmv", "DERIVED_FROM", "L3",
                provenance="metric_definition",
            ),
            # 旧下游使用表（不再声明 → 软删）
            _Row(
                2, "metric:m", "table:ads.old_report", "DERIVED_FROM", "L3",
                provenance="metric_definition",
            ),
            # 已声明的下游使用表（仍声明 → 保留）
            _Row(
                3, "metric:m", "table:ads.gmv_report", "DERIVED_FROM", "L3",
                provenance="metric_definition",
            ),
        ]
    )
    repo = LineageRepository(db)
    deleted, added = await repo.sync_metric_table_edges(
        "m", "dws.gmv", ["ods.order"], ["ads.gmv_report", "ads.new_report"]
    )
    assert deleted == 1  # ads.old_report 不再声明 → 软删
    assert added == 2  # ods.order + ads.new_report 新增（gmv/ads.gmv_report 已存在不计）
    remaining = [r for r in db._rows if r.source_node == "metric:m"]
    targets = sorted(r.target_node for r in remaining)
    assert targets == ["table:ads.gmv_report", "table:ads.new_report", "table:dws.gmv"]


async def test_sync_metric_table_edges_no_tables_clears_all() -> None:
    """sync 无声明表：清理全部残留表边（指标不再声明任何表）。"""
    db = _FakeDB(
        [
            _Row(
                1,
                "metric:m",
                "table:dws.gmv_v1",
                "DERIVED_FROM",
                "L3",
                provenance="metric_definition",
            ),
            _Row(
                2,
                "table:ods.order",
                "metric:m",
                "DERIVED_FROM",
                "L3",
                provenance="metric_definition",
            ),
        ]
    )
    repo = LineageRepository(db)
    deleted, added = await repo.sync_metric_table_edges("m", None, [])
    assert deleted == 2
    assert added == 0
    assert not [
        r
        for r in db._rows
        if (r.source_node == "metric:m" and r.target_node.startswith("table:"))
        or (r.target_node == "metric:m" and r.source_node.startswith("table:"))
    ]


async def test_sync_metric_edges_preserves_manual_edges() -> None:
    """差异同步仅清理自动注册边(provenance=metric_definition)，保留手动/导入边。"""
    db = _FakeDB(
        [
            # 自动注册的落地表边（应被差异同步清理）
            _Row(
                1,
                "metric:m",
                "table:dws.gmv_v1",
                "DERIVED_FROM",
                "L3",
                provenance="metric_definition",
            ),
            # 手动登记的落地表边（应保留，即使不在声明集中）
            _Row(
                2,
                "metric:m",
                "table:dws.legacy_manual",
                "DERIVED_FROM",
                "L3",
                provenance="manual",
            ),
            # 手动登记的维度边（应保留）
            _Row(
                3,
                "metric:m",
                "dimension:dim_manual",
                "USES_DIMENSION",
                "L3",
                provenance="manual",
            ),
        ]
    )
    repo = LineageRepository(db)
    # 表差异同步：新落地表 gmv_v2（v1 自动边被清理、manual 边保留）
    deleted, _ = await repo.sync_metric_table_edges("m", "dws.gmv_v2", [])
    assert deleted == 1  # 仅自动注册的 gmv_v1 被清理
    # 维度差异同步：空声明（自动维度边被清理、manual 维度边保留）
    deleted2, _ = await repo.sync_metric_dimension_edges("m", [])
    assert deleted2 == 0  # 无自动维度边，manual 边保留
    targets = sorted(r.target_node for r in db._rows if r.source_node == "metric:m")
    assert targets == ["dimension:dim_manual", "table:dws.gmv_v2", "table:dws.legacy_manual"]


# ---- 健康度（P2）与路径查询（P3）----


def _ingest_run_with(*, run_at: Any) -> LineageIngestRun:
    run = LineageIngestRun(source="sqlglot", status="success", run_at=run_at)
    return run


async def test_stale_edge_count_counts_only_stale() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    db = _FakeDB(
        [
            _l1_edge("a", "b", last_seen=now, missing=2, stale=True),
            _l1_edge("c", "d", last_seen=now, missing=0, stale=False),
            _l1_edge("e", "f", last_seen=now, missing=1, stale=True),
        ]
    )
    repo = LineageRepository(db)
    assert await repo.stale_edge_count() == 2


async def test_latest_ingest_run_time_returns_max() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    db = _FakeDB(
        [],
        ingest_runs=[
            _ingest_run_with(run_at=now - timedelta(days=2)),
            _ingest_run_with(run_at=now),
            _ingest_run_with(run_at=now - timedelta(days=5)),
        ],
    )
    repo = LineageRepository(db)
    assert await repo.latest_ingest_run_time() == now


async def test_latest_ingest_run_time_none_when_empty() -> None:
    repo = LineageRepository(_FakeDB([]))
    assert await repo.latest_ingest_run_time() is None


async def test_find_paths_single_path() -> None:
    db = _FakeDB([_l1_edge("a", "b"), _l1_edge("b", "c")])
    repo = LineageRepository(db)
    paths = await repo.find_paths("table:a", "table:c", max_hops=5)
    assert len(paths) == 1
    assert [e.source_node for e in paths[0]] == ["table:a", "table:b"]
    assert [e.target_node for e in paths[0]] == ["table:b", "table:c"]


async def test_find_paths_multiple_and_hops_limit() -> None:
    db = _FakeDB(
        [
            _l1_edge("a", "b"),
            _l1_edge("a", "c"),
            _l1_edge("b", "d"),
            _l1_edge("c", "d"),
        ]
    )
    repo = LineageRepository(db)
    paths = await repo.find_paths("table:a", "table:d", max_hops=5)
    assert len(paths) == 2
    # max_hops=1：a→d 需 2 跳，超出上限 → 无路径
    assert await repo.find_paths("table:a", "table:d", max_hops=1) == []


async def test_find_paths_no_path_and_cycle_safe() -> None:
    # 环 a→b→a 不应死循环；a→c 仍有唯一路径
    db = _FakeDB([_l1_edge("a", "b"), _l1_edge("b", "a"), _l1_edge("b", "c")])
    repo = LineageRepository(db)
    assert len(await repo.find_paths("table:a", "table:c", max_hops=5)) == 1
    assert await repo.find_paths("table:a", "table:z", max_hops=5) == []


async def test_find_terminals_finds_dead_ends() -> None:
    db = _FakeDB([_l1_edge("a", "b"), _l1_edge("b", "c")])
    repo = LineageRepository(db)
    assert await repo.find_terminals("table:a", max_hops=5) == [
        ("table:c", ["table:a", "table:b", "table:c"])
    ]


async def test_find_terminals_multiple_and_depth_limit() -> None:
    db = _FakeDB(
        [
            _l1_edge("a", "b"),
            _l1_edge("a", "c"),
            _l1_edge("b", "d"),
            _l1_edge("c", "e"),
        ]
    )
    repo = LineageRepository(db)
    terminals = sorted(t for t, _ in await repo.find_terminals("table:a", max_hops=5))
    assert terminals == ["table:d", "table:e"]
    # max_hops=1：a 一步到 b/c，但 b/c 有下游非死端 → 无终止节点
    assert await repo.find_terminals("table:a", max_hops=1) == []


async def test_entity_exists_metric_and_table() -> None:
    db = _FakeDB(
        [],
        meta_rows=[
            _MetaRow(table="metric", metric_code="gmv", domain="sales"),
            _MetaRow(table="catalog", entity_name="dws.orders", catalog_id=1),
        ],
    )
    repo = LineageRepository(db)
    assert await repo.entity_exists("metric:gmv") is True
    assert await repo.entity_exists("metric:nope") is False
    assert await repo.entity_exists("table:dws.orders") is True
    assert await repo.entity_exists("table:dws.zzz") is False
    # field/external 等派生节点中性返回 True（不构成断链）
    assert await repo.entity_exists("field:a.b.c") is True


async def test_table_nodes_in_edges_unions_sources_and_targets() -> None:
    db = _FakeDB(
        [
            _l1_edge("a", "b"),
            _l1_edge("c", "d"),
            _l1_edge("b", "e"),
        ]
    )
    repo = LineageRepository(db)
    # source: a/c/b、target: b/d/e → union {a,b,c,d,e} = 5
    assert await repo.table_nodes_in_edges() == 5


async def test_table_nodes_in_edges_empty() -> None:
    repo = LineageRepository(_FakeDB([]))
    assert await repo.table_nodes_in_edges() == 0


# ---- P4：血缘导出过滤查询（list_export_edges）----


def _export_rows() -> list[_Row]:
    """导出测试数据：2 张表级边 + 1 张字段级边 + 1 张软删边 + 1 张其他来源。"""
    return [
        _Row(1, "table:ods.a", "table:dws.t", granularity="L1", provenance="dp_csv"),
        _Row(2, "field:ods.a.id", "field:dws.t.id", granularity="L2", provenance="dp_csv"),
        _Row(3, "table:ods.b", "table:dws.t", granularity="L1", provenance="sqlglot"),
        _Row(4, "table:ods.c", "table:dws.u", granularity="L1", provenance="dp_csv"),
        _Row(5, "table:ods.old", "table:dws.t", granularity="L1", deleted_at=datetime(2026, 1, 1)),
    ]


async def test_list_export_edges_all_filters_none() -> None:
    """无过滤：返回全部未删除边（按 id 升序，软删排除）。"""
    repo = LineageRepository(_FakeDB(_export_rows()))
    edges = await repo.list_export_edges()
    assert [e.id for e in edges] == [1, 2, 3, 4]
    assert all(e.deleted_at is None for e in edges)


async def test_list_export_edges_granularity_filter() -> None:
    """按粒度过滤：L1 只留表级边。"""
    repo = LineageRepository(_FakeDB(_export_rows()))
    edges = await repo.list_export_edges(granularity="L1")
    assert [e.id for e in edges] == [1, 3, 4]
    assert all(e.granularity == "L1" for e in edges)


async def test_list_export_edges_provenance_filter() -> None:
    """按来源过滤：sqlglot 只留 SQL 解析通道边。"""
    repo = LineageRepository(_FakeDB(_export_rows()))
    edges = await repo.list_export_edges(provenance="sqlglot")
    assert [e.id for e in edges] == [3]


async def test_list_export_edges_node_direction() -> None:
    """按节点+方向过滤：downstream=源为该节点 / upstream=目标为该节点 / both=任一。"""
    repo = LineageRepository(_FakeDB(_export_rows()))
    # downstream：source_node == table:dws.t 无（dws.t 是目标）
    assert await repo.list_export_edges(node="table:dws.t", direction="downstream") == []
    # upstream：target_node == table:dws.t → id 1、3（软删的 5 排除）
    up = await repo.list_export_edges(node="table:dws.t", direction="upstream")
    assert [e.id for e in up] == [1, 3]
    # both：source 或 target 为该节点（边 2 是 field 节点，不匹配 table:ods.a）
    both = await repo.list_export_edges(node="table:ods.a", direction="both")
    assert [e.id for e in both] == [1]
    # limit 截断
    limited = await repo.list_export_edges(node="table:dws.t", direction="upstream", limit=1)
    assert [e.id for e in limited] == [1]


async def test_invalidate_dropped_table_soft_deletes_edges() -> None:
    """``invalidate_dropped_table``：DROP TABLE 依赖失效——软删除以该表为源或目标的边。"""
    rows = [
        _Row(1, "table:ods.s", "table:dws.t"),
        _Row(2, "table:dws.t", "table:dws.u"),
        _Row(3, "table:ods.x", "table:dws.y"),  # 无关边保留
    ]
    db = _FakeDB(rows)
    repo = LineageRepository(db)
    n = await repo.invalidate_dropped_table("table:dws.t")
    assert n == 2  # 边 1（dws.t 是目标）+ 边 2（dws.t 是源）
    remaining = [r.id for r in db._rows]
    assert remaining == [3]


async def test_affected_asset_owners_collects_downstream_owners() -> None:
    """``affected_asset_owners``：沿下游收集受影响资产（表/指标）的 Owner 去重，无 Owner 排除。"""
    rows = [
        _Row(1, "table:ods.t", "table:dwd.b"),
        _Row(2, "table:dwd.b", "metric:m1"),
        _Row(3, "table:dwd.b", "table:dm.c"),
    ]
    meta = [
        _MetaRow(table="catalog", entity_name="ods.t", owner_id=10),
        _MetaRow(table="catalog", entity_name="dwd.b", owner_id=20),
        _MetaRow(table="metric", metric_code="m1", owner_id=30),
        _MetaRow(table="catalog", entity_name="dm.c", owner_id=None),  # 无 Owner 不计入
    ]
    repo = LineageRepository(_FakeDB(rows, meta_rows=meta))
    owners = await repo.affected_asset_owners("table:ods.t")
    # 自身(10) + 下游 dwd.b(20) + metric:m1(30)；dm.c 无 Owner 排除
    assert owners == {"10", "20", "30"}


async def test_affected_asset_owners_respects_max_hops() -> None:
    """深度限制：max_hops=1 只收集直接下游（含自身）。"""
    rows = [
        _Row(1, "table:ods.t", "table:dwd.b"),
        _Row(2, "table:dwd.b", "metric:m1"),
    ]
    meta = [
        _MetaRow(table="catalog", entity_name="ods.t", owner_id=10),
        _MetaRow(table="catalog", entity_name="dwd.b", owner_id=20),
        _MetaRow(table="metric", metric_code="m1", owner_id=30),
    ]
    repo = LineageRepository(_FakeDB(rows, meta_rows=meta))
    owners = await repo.affected_asset_owners("table:ods.t", max_hops=1)
    assert owners == {"10", "20"}


async def test_affected_asset_owners_empty_when_no_owners() -> None:
    """无 Owner 且无下游有 Owner 资产 → 空集合（不产生通知）。"""
    rows = [_Row(1, "table:ods.t", "table:dwd.b")]
    meta = [
        _MetaRow(table="catalog", entity_name="ods.t", owner_id=None),
        _MetaRow(table="catalog", entity_name="dwd.b", owner_id=None),
    ]
    repo = LineageRepository(_FakeDB(rows, meta_rows=meta))
    assert await repo.affected_asset_owners("table:ods.t") == set()


# ---- metric_referrers（deprecate 被引用拦截）----


class _ReferrerDB:
    """面向 metric_referrers 的假 db：source_node 等值 + 存活 + edge_type IN 过滤。"""

    def __init__(self, edges: list[LineageEdge]) -> None:
        self.edges = edges

    async def execute(self, stmt: object) -> _Result:
        sql = re.sub(r"\s+", " ", str(stmt.compile(compile_kwargs={"literal_binds": True})))
        sn = _extract(sql, "source_node")
        rows = [
            e
            for e in self.edges
            if getattr(e, "deleted_at", None) is None
            and not getattr(e, "stale", False)
            and e.source_node == sn
            and e.edge_type in ("DERIVED_FROM", "CONSUMED_BY")
        ]
        seen: set[tuple[str, str]] = set()
        out: list[tuple[str, str]] = []
        for e in rows:
            key = (e.target_node, e.edge_type)
            if key not in seen:
                seen.add(key)
                out.append((e.target_node, e.edge_type))
        return _Result(rows=out)


def _ref_edge(
    source: str,
    target: str,
    *,
    edge_type: str = "DERIVED_FROM",
    stale: bool = False,
    deleted: bool = False,
) -> LineageEdge:
    edge = LineageEdge(
        source_node=f"metric:{source}",
        target_node=target,
        edge_type=edge_type,
        granularity="L1",
        provenance="manual",
    )
    edge.stale = stale
    if deleted:
        edge.deleted_at = datetime.now()
    return edge


async def test_metric_referrers_returns_active_references() -> None:
    """返回引用指定指标的活跃 DERIVED_FROM/CONSUMED_BY 引用者（deprecate 拦截用）。

    过滤：stale 失效边、软删边、非 metric: source、非引用类型边不计入。
    """
    edges = [
        _ref_edge("sales_gmv_daily", "metric:sales_gmv_derived", edge_type="DERIVED_FROM"),
        _ref_edge("sales_gmv_daily", "metric:sales_gmv_ratio", edge_type="DERIVED_FROM"),
        _ref_edge("sales_gmv_daily", "consumer:bi_report", edge_type="CONSUMED_BY"),
        # 失效 / 软删 / 反向 / 无关类型均不计入
        _ref_edge("sales_gmv_daily", "metric:stale_ref", edge_type="DERIVED_FROM", stale=True),
        _ref_edge("sales_gmv_daily", "metric:deleted_ref", edge_type="DERIVED_FROM", deleted=True),
        _ref_edge("other", "metric:sales_gmv_daily", edge_type="DERIVED_FROM"),  # source 不是它
        _ref_edge("sales_gmv_daily", "table:landing", edge_type="LINEAGE_UP"),  # 非引用类型
    ]
    repo = LineageRepository(_ReferrerDB(edges))
    refs = await repo.metric_referrers("sales_gmv_daily")
    got = {(r["node"], r["edge_type"]) for r in refs}
    assert got == {
        ("metric:sales_gmv_derived", "DERIVED_FROM"),
        ("metric:sales_gmv_ratio", "DERIVED_FROM"),
        ("consumer:bi_report", "CONSUMED_BY"),
    }
    # 无引用 → 空列表
    assert await repo.metric_referrers("not_exist") == []


class _ReferrerBatchDB:
    """面向 metric_referrers_batch 的假 db：source_node IN (...) + 存活 + edge_type 过滤。"""

    def __init__(self, edges: list[LineageEdge]) -> None:
        self.edges = edges

    async def execute(self, stmt: object) -> _Result:
        sql = re.sub(r"\s+", " ", str(stmt.compile(compile_kwargs={"literal_binds": True})))
        m = re.search(r"source_node IN \(([^)]*)\)", sql)
        assert m is not None, f"batch 查询应使用 IN，实际 SQL: {sql}"
        codes = {c.strip().strip("'") for c in m.group(1).split(",")}
        rows = [
            e
            for e in self.edges
            if getattr(e, "deleted_at", None) is None
            and not getattr(e, "stale", False)
            and e.source_node in codes
            and e.edge_type in ("DERIVED_FROM", "CONSUMED_BY")
        ]
        seen: set[tuple[str, str, str]] = set()
        out: list[tuple[str, str, str]] = []
        for e in rows:
            key = (e.source_node, e.target_node, e.edge_type)
            if key not in seen:
                seen.add(key)
                out.append((e.source_node, e.target_node, e.edge_type))
        return _Result(rows=out)


async def test_metric_referrers_batch_returns_per_code() -> None:
    """批量下游审查：一次 IN 查询返回每指标引用者，无引用指标为空列表。"""
    edges = [
        _ref_edge("sales_gmv_daily", "metric:sales_gmv_derived", edge_type="DERIVED_FROM"),
        _ref_edge("sales_gmv_daily", "consumer:bi_report", edge_type="CONSUMED_BY"),
        _ref_edge("sales_uv_daily", "metric:sales_uv_derived", edge_type="DERIVED_FROM"),
        # 失效 / 软删 / 无关类型不计入
        _ref_edge("sales_gmv_daily", "metric:stale_ref", edge_type="DERIVED_FROM", stale=True),
        _ref_edge("sales_uv_daily", "metric:deleted_ref", edge_type="DERIVED_FROM", deleted=True),
        _ref_edge("sales_gmv_daily", "table:landing", edge_type="LINEAGE_UP"),
    ]
    repo = LineageRepository(_ReferrerBatchDB(edges))
    got = await repo.metric_referrers_batch(["sales_gmv_daily", "sales_uv_daily", "no_ref"])
    assert got["sales_gmv_daily"] == [
        {"node": "metric:sales_gmv_derived", "edge_type": "DERIVED_FROM"},
        {"node": "consumer:bi_report", "edge_type": "CONSUMED_BY"},
    ]
    assert got["sales_uv_daily"] == [
        {"node": "metric:sales_uv_derived", "edge_type": "DERIVED_FROM"}
    ]
    # 无引用指标 → 空列表（入参必有键）
    assert got["no_ref"] == []
    # 空入参 → 空 dict
    assert await repo.metric_referrers_batch([]) == {}


async def test_merge_provenances() -> None:
    """provenance 多来源合并 helper：去重、顺序保留（P2-7）。"""
    from app.services.lineage.repository import merge_provenances

    assert merge_provenances(None, "dp_sql") == "dp_sql"
    assert merge_provenances("hive", "dp_sql") == "hive+dp_sql"
    assert merge_provenances("hive+dp_sql", "hive") == "hive+dp_sql"
    assert merge_provenances("dp_sql", "hive") == "dp_sql+hive"


async def test_upsert_merges_provenance_instead_of_overwrite() -> None:
    """同一边被第二通道写入时 provenance 合并而非覆盖（P2-7 修复归属漂移）。

    回归：此前 dp_sql 后写会覆盖 hive 建立的边 provenance，使 hive 通道失去
    该边治理权（mark_seen/mark_missing 按 provenance 精确匹配查不到）。
    """
    db = _FakeDB(
        [
            _Row(
                1,
                "table:ods.a",
                "table:dwd.b",
                edge_type="DERIVED_FROM",
                granularity="L1",
                provenance="hive",
            )
        ]
    )
    repo = LineageRepository(db)
    edge, created = await repo.upsert_edge_with_status(
        source_node="table:ods.a",
        target_node="table:dwd.b",
        edge_type="DERIVED_FROM",
        granularity="L1",
        provenance="dp_sql",
        change_reason="dp_sync",
    )
    assert created is False
    assert edge.provenance == "hive+dp_sql"
