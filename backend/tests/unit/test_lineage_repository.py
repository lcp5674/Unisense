"""lineage repository 单测（内存假 db）。

覆盖：幂等 upsert + 变更历史快照、环检测、指标级边、断链登记、BFS 影响分析与软删。
"""

from __future__ import annotations

import re
from typing import Any

from app.models.lineage import LineageEdge, LineageEdgeHistory
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


class _FakeDB:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows: list[Any] = list(rows)
        self.added: list[Any] = []
        self.flushed = False

    async def execute(self, stmt: object) -> _Result:
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
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
        # 软删：UPDATE ... SET deleted_at 按 source/target 匹配（软删语义=从活跃行移除）
        if sql.lstrip().upper().startswith("UPDATE") and "deleted_at" in sql:
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
            return _Result([r for r in self._rows if r.source_node == src])
        dst = _extract(sql, "target_node")
        if dst is not None:
            return _Result([r for r in self._rows if r.target_node == dst])
        return _Result([])

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        # 让插入的边对后续 upsert/查询可见（幂等查找用）
        if isinstance(obj, LineageEdge):
            self._rows.append(obj)

    async def flush(self) -> None:
        self.flushed = True


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
