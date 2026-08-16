"""lineage repository 单测（内存假 db）。

覆盖：幂等 upsert + 变更历史快照、环检测、指标级边、断链登记、BFS 影响分析与软删。
"""

from __future__ import annotations

import json
import re
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
    ) -> None:
        self._rows: list[Any] = list(rows)
        self._meta_rows: list[Any] = list(meta_rows or [])
        self._history_rows: list[Any] = list(history_rows or [])
        self.added: list[Any] = []
        self.flushed = False

    async def execute(self, stmt: object) -> _Result:
        # SQLAlchemy 编译产物含换行缩进：折叠空白以便按子串匹配分支
        sql = re.sub(r"\s+", " ", str(stmt.compile(compile_kwargs={"literal_binds": True})))
        # 节点元数据：metric 表
        if " FROM metric " in sql:
            return _Result([r for r in self._meta_rows if r.table == "metric"])
        # 节点元数据：db_catalog join data_source
        if " FROM db_catalog " in sql or " JOIN data_source " in sql:
            return _Result([r for r in self._meta_rows if r.table == "catalog"])
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
    assert sources == ["dp_csv", "sqlglot"]
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
