"""lineage graph 客户端单测（mock driver/session，不连真实 Neo4j）。

聚焦读路径 ``query_impact`` 与已有写路径 ``write_edges`` 的降级语义：
未配置 / 熔断打开 / 驱动异常 → None 或 False，成功 → 边列表或 True。
"""

from __future__ import annotations

import pytest

from app.services.lineage import graph


class _TrackingBreaker:
    """记录成功/失败调用的熔断替身，避免污染全局 neo4j_breaker 状态。"""

    def __init__(self, allow: bool = True) -> None:
        self._allow = allow
        self.successes = 0
        self.failures = 0

    def allow(self) -> bool:
        return self._allow

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self) -> None:
        self.failures += 1


class _FakeRecord:
    def __init__(self, src: str, tgt: str, edge_type: str) -> None:
        self._data = {"src": src, "tgt": tgt, "edge_type": edge_type}

    def __getitem__(self, key: str) -> str:
        return self._data[key]


class _FakeResult:
    """模拟 ``AsyncResult``：支持 ``async for`` 迭代。"""

    def __init__(self, records: list[_FakeRecord]) -> None:
        self._records = list(records)

    def __aiter__(self) -> object:
        async def _gen() -> _FakeRecord:
            for record in self._records:
                yield record

        return _gen()


class _FakeSession:
    def __init__(self, result: _FakeResult) -> None:
        self._result = result
        self.runs: list[tuple[str, dict[str, object]]] = []

    async def run(self, query: str, **params: object) -> _FakeResult:
        self.runs.append((query, params))
        return self._result


class _FakeSessionCtx:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakeDriver:
    def __init__(self, result: _FakeResult | None = None) -> None:
        self._result = result
        self.last_session: _FakeSession | None = None

    def session(self) -> _FakeSessionCtx:
        session = _FakeSession(self._result or _FakeResult([]))
        self.last_session = session
        return _FakeSessionCtx(session)


class _RaisingDriver:
    def session(self) -> object:
        raise RuntimeError("neo4j unreachable")


class _DisposableDriver:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _make_client() -> graph.LineageGraphClient:
    return graph.LineageGraphClient(uri="bolt://fake:7687")


def _patch_breaker(monkeypatch: pytest.MonkeyPatch, allow: bool = True) -> _TrackingBreaker:
    breaker = _TrackingBreaker(allow=allow)
    monkeypatch.setattr(graph, "neo4j_breaker", breaker)
    return breaker


async def test_query_impact_returns_none_when_unconfigured() -> None:
    client = _make_client()
    client._uri = ""  # 模拟未配置 URI（env 为空串）
    assert await client.query_impact("table:a") is None


async def test_query_impact_returns_none_when_breaker_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_breaker(monkeypatch, allow=False)
    client = _make_client()
    assert await client.query_impact("table:a") is None


async def test_query_impact_returns_none_on_driver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    breaker = _patch_breaker(monkeypatch)
    client = _make_client()
    client._driver = _RaisingDriver()
    assert await client.query_impact("table:a") is None
    assert breaker.failures == 1


async def test_query_impact_returns_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    breaker = _patch_breaker(monkeypatch)
    client = _make_client()
    client._driver = _FakeDriver(
        _FakeResult(
            [
                _FakeRecord("table:a", "table:b", "DERIVED_FROM"),
                _FakeRecord("table:b", "table:c", "DERIVED_FROM"),
            ]
        )
    )
    edges = await client.query_impact("table:a")
    assert edges == [
        ("table:a", "table:b", "DERIVED_FROM"),
        ("table:b", "table:c", "DERIVED_FROM"),
    ]
    assert breaker.successes == 1
    session = client._driver.last_session
    assert session is not None
    query, params = session.runs[0]
    assert "(s:Asset {id:$node})-[:LINEAGE*1..5]->(t:Asset)" in query
    assert "LIMIT $max_edges" in query
    assert params == {"node": "table:a", "max_edges": 5000}


async def test_query_impact_empty_result_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    breaker = _patch_breaker(monkeypatch)
    client = _make_client()
    client._driver = _FakeDriver(_FakeResult([]))
    assert await client.query_impact("table:a") == []
    assert breaker.successes == 1


async def test_query_impact_upstream_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_breaker(monkeypatch)
    client = _make_client()
    client._driver = _FakeDriver(_FakeResult([_FakeRecord("table:b", "table:a", "DERIVED_FROM")]))
    edges = await client.query_impact("table:a", direction="upstream")
    assert edges == [("table:b", "table:a", "DERIVED_FROM")]
    session = client._driver.last_session
    assert session is not None
    assert "(s:Asset {id:$node})<-[:LINEAGE*1..5]-(t:Asset)" in session.runs[0][0]


async def test_query_impact_both_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_breaker(monkeypatch)
    client = _make_client()
    client._driver = _FakeDriver(_FakeResult([]))
    await client.query_impact("table:a", direction="both")
    session = client._driver.last_session
    assert session is not None
    assert "(s:Asset {id:$node})-[:LINEAGE*1..5]-(t:Asset)" in session.runs[0][0]


async def test_query_impact_unknown_direction_defaults_downstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_breaker(monkeypatch)
    client = _make_client()
    client._driver = _FakeDriver(_FakeResult([]))
    await client.query_impact("table:a", direction="sideways")
    session = client._driver.last_session
    assert session is not None
    assert "(s:Asset {id:$node})-[:LINEAGE*1..5]->(t:Asset)" in session.runs[0][0]


async def test_query_impact_zero_hops_returns_empty() -> None:
    client = _make_client()
    assert await client.query_impact("table:a", max_hops=0) == []
    assert await client.query_impact("table:a", max_edges=0) == []


async def test_query_impact_truncates_to_max_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_breaker(monkeypatch)
    client = _make_client()
    records = [
        _FakeRecord(f"table:n{i}", f"table:n{i + 1}", "DERIVED_FROM") for i in range(10)
    ]
    client._driver = _FakeDriver(_FakeResult(records))
    edges = await client.query_impact("table:a", max_edges=3)
    assert len(edges) == 3


async def test_write_edges_returns_false_when_unconfigured() -> None:
    client = _make_client()
    client._uri = ""
    assert await client.write_edges([("table:a", "table:b", "DERIVED_FROM")]) is False


async def test_write_edges_returns_false_when_breaker_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_breaker(monkeypatch, allow=False)
    client = _make_client()
    assert await client.write_edges([("table:a", "table:b", "DERIVED_FROM")]) is False


async def test_write_edges_success(monkeypatch: pytest.MonkeyPatch) -> None:
    breaker = _patch_breaker(monkeypatch)
    client = _make_client()
    client._driver = _FakeDriver(_FakeResult([]))
    assert await client.write_edges([("table:a", "table:b", "DERIVED_FROM")]) is True
    assert breaker.successes == 1


async def test_write_edges_returns_false_on_driver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    breaker = _patch_breaker(monkeypatch)
    client = _make_client()
    client._driver = _RaisingDriver()
    assert await client.write_edges([("table:a", "table:b", "DERIVED_FROM")]) is False
    assert breaker.failures == 1


async def test_query_impact_creates_driver_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_breaker(monkeypatch)
    from neo4j import AsyncGraphDatabase

    fake_driver = _FakeDriver(_FakeResult([_FakeRecord("table:a", "table:b", "DERIVED_FROM")]))

    def _factory(*args: object, **kwargs: object) -> _FakeDriver:
        return fake_driver

    monkeypatch.setattr(AsyncGraphDatabase, "driver", _factory)
    client = _make_client()
    edges = await client.query_impact("table:a")
    assert edges == [("table:a", "table:b", "DERIVED_FROM")]
    assert client._driver is fake_driver


async def test_write_edges_creates_driver_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    breaker = _patch_breaker(monkeypatch)
    from neo4j import AsyncGraphDatabase

    fake_driver = _FakeDriver(_FakeResult([]))

    def _factory(*args: object, **kwargs: object) -> _FakeDriver:
        return fake_driver

    monkeypatch.setattr(AsyncGraphDatabase, "driver", _factory)
    client = _make_client()
    assert await client.write_edges([("table:a", "table:b", "DERIVED_FROM")]) is True
    assert breaker.successes == 1
    assert client._driver is fake_driver


async def test_dispose_closes_driver() -> None:
    client = _make_client()
    driver = _DisposableDriver()
    client._driver = driver
    await client.dispose()
    assert driver.closed
    assert client._driver is None


async def test_dispose_noop_without_driver() -> None:
    client = _make_client()
    client._driver = None
    await client.dispose()  # 不抛异常
