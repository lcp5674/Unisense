"""lineage repository 单测（内存假 db，覆盖幂等 upsert 与 BFS 影响分析）。"""

from __future__ import annotations

import re

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
    ) -> None:
        self.id = edge_id
        self.source_node = source_node
        self.target_node = target_node
        self.edge_type = edge_type
        self.granularity = granularity
        self.confidence = confidence
        self.provenance = provenance


class _Result:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows
        self._scalar: _Row | None = None

    def scalar_one_or_none(self) -> _Row | None:
        return self._scalar

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[_Row]:
        return self._rows


class _FakeDB:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows
        self.added: list[_Row] = []
        self.flushed = False

    async def execute(self, stmt: object) -> _Result:
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        # upsert 查询的 WHERE 含 edge_type/granularity 的字面量等值条件；
        # 普通边查询仅在 SELECT 列表出现这些列名（无等值），据此区分
        if "edge_type = '" in sql and "granularity = '" in sql:
            return _Result([])
        node: str | None = None
        for m in re.finditer(r"(source_node|target_node)\s*=\s*'((?:[^'\\]|\\.)*)'", sql):
            node = m.group(2)
        if node is None:
            return _Result([])
        if "source_node" in sql:
            rows = [r for r in self._rows if r.source_node == node]
        else:
            rows = [r for r in self._rows if r.target_node == node]
        return _Result(rows)

    def add(self, obj: _Row) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed = True


async def test_upsert_creates_new() -> None:
    db = _FakeDB([])
    repo = LineageRepository(db)
    edge = await repo.upsert_edge(
        source_node="table:a", target_node="table:t", edge_type="DERIVED_FROM", granularity="L1"
    )
    assert edge.source_node == "table:a"
    assert len(db.added) == 1


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
