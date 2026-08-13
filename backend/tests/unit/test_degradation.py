"""降级事件记录单测（对齐 TD §4.13 + §5.2.4/§5.2.5）。

验证：
- record_degradation 持久化 DegradationEvent 并 publish degradation.state_changed 事件。
- DB / EventBus 不可用时 best-effort，绝不抛异常（降级路径自身不能再降级）。
- update_dependency_health UPSERT、_persist_degradation_and_health 双表联动、
  fire_and_forget 调度、健康态读取与播种。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import degradation
from app.core.resilience import DegradationSignal
from app.models.degradation_event import DegradationEvent


@pytest.fixture
def patched(monkeypatch):
    """每个测试用全新 session/eventbus mock，避免 side_effect 跨测试泄漏。"""
    session = MagicMock()
    session.commit = AsyncMock()

    @asynccontextmanager
    async def _fake_session() -> MagicMock:
        yield session

    eb = MagicMock()
    eb.publish = AsyncMock()
    monkeypatch.setattr(degradation, "async_session_factory", _fake_session)
    monkeypatch.setattr(degradation, "get_eventbus", lambda: eb)
    return eb, session


async def test_record_degradation_persists_and_publishes(patched):
    eb, session = patched
    await degradation.record_degradation("OLAP", "olap", "DEGRADED", "olap_not_configured")

    # 持久化降级事件
    assert session.add.call_count == 1
    added = session.add.call_args[0][0]
    assert isinstance(added, DegradationEvent)
    assert added.dependency_type == "OLAP"
    assert added.dependency_id == "olap"
    assert added.state == "DEGRADED"
    assert added.reason == "olap_not_configured"
    assert session.commit.await_count == 1

    # 事件总线发出恢复/降级信号
    eb.publish.assert_awaited_once()
    assert eb.publish.call_args[0][0] == "degradation.state_changed"
    assert eb.publish.call_args[0][1]["state"] == "DEGRADED"


async def test_record_degradation_db_failure_is_best_effort(patched):
    eb, session = patched
    session.commit.side_effect = RuntimeError("db down")
    # DB 失败不应抛出，且事件总线仍应发出（告警通道独立）
    await degradation.record_degradation("OLAP", "olap", "DEGRADED", "x")
    eb.publish.assert_awaited_once()


async def test_record_degradation_eventbus_failure_is_best_effort(patched):
    eb, session = patched
    eb.publish.side_effect = RuntimeError("bus down")
    # 事件总线失败不应抛出，且仍尝试持久化
    await degradation.record_degradation("OLAP", "olap", "DEGRADED", "x")
    assert session.add.call_count == 1
    assert session.commit.await_count == 1


def _signal(event_state: str, **overrides) -> DegradationSignal:
    return DegradationSignal(
        dependency_type="OLAP",
        event_state=event_state,
        reason="circuit_open",
        circuit_state="OPEN",
        consecutive_failures=3,
        opened_at=None,
        **overrides,
    )


class TestSignalToHealthParams:
    def test_healthy_state_maps_to_closed(self) -> None:
        params = degradation._signal_to_health_params(_signal("HEALTHY"))
        assert params == {
            "status": "HEALTHY",
            "circuit_state": "CLOSED",
            "consecutive_failures": 0,
            "circuit_opened_at": None,
        }

    def test_probing_state_maps_to_half_open_degraded(self) -> None:
        params = degradation._signal_to_health_params(_signal("PROBING"))
        assert params["status"] == "DEGRADED"
        assert params["circuit_state"] == "HALF_OPEN"
        assert params["consecutive_failures"] == 3
        assert params["circuit_opened_at"] is None

    def test_degraded_state_maps_to_open(self) -> None:
        params = degradation._signal_to_health_params(_signal("DEGRADED"))
        assert params["status"] == "DEGRADED"
        assert params["circuit_state"] == "OPEN"
        assert params["consecutive_failures"] == 3
        assert params["circuit_opened_at"] is not None


class TestUpdateDependencyHealth:
    async def test_upsert_executes_and_commits(self, patched):
        _, session = patched
        session.execute = AsyncMock()
        await degradation.update_dependency_health(
            "OLAP", "olap", status="DEGRADED", circuit_state="OPEN", consecutive_failures=2
        )
        session.execute.assert_awaited_once()
        assert session.commit.await_count == 1

    async def test_db_failure_is_best_effort(self, patched):
        _, session = patched
        session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        await degradation.update_dependency_health(
            "OLAP", "olap", status="HEALTHY", circuit_state="CLOSED"
        )


class TestPersistDegradationAndHealth:
    async def test_degraded_state_writes_event_and_health(self, monkeypatch):
        recorded = MagicMock()
        recorded.coro = None

        async def fake_record(*args, **kwargs):
            recorded.coro = (args, kwargs)

        health_updated = MagicMock()

        async def fake_update(*args, **kwargs):
            health_updated.coro = (args, kwargs)

        monkeypatch.setattr(degradation, "record_degradation", fake_record)
        monkeypatch.setattr(degradation, "update_dependency_health", fake_update)

        await degradation._persist_degradation_and_health(
            "OLAP", "olap", "DEGRADED", "reason", circuit_state="OPEN"
        )
        # DEGRADED 写审计事件
        assert recorded.coro is not None
        assert recorded.coro[0][2] == "DEGRADED"
        # 健康态始终更新
        assert health_updated.coro is not None

    async def test_probing_state_skips_event(self, monkeypatch):
        recorded: list = []

        async def fake_record(*args, **kwargs):
            recorded.append(args)

        health_updated: list = []

        async def fake_update(*args, **kwargs):
            health_updated.append(args)

        monkeypatch.setattr(degradation, "record_degradation", fake_record)
        monkeypatch.setattr(degradation, "update_dependency_health", fake_update)

        await degradation._persist_degradation_and_health(
            "OLAP", "olap", "PROBING", "half_open", circuit_state="HALF_OPEN"
        )
        # PROBING 仅更新健康态，不写审计事件
        assert recorded == []
        assert len(health_updated) == 1

    async def test_unknown_state_defaults_to_degraded(self, monkeypatch):
        health_updated = MagicMock()

        async def fake_update(*args, **kwargs):
            health_updated.coro = kwargs

        monkeypatch.setattr(degradation, "update_dependency_health", fake_update)
        await degradation._persist_degradation_and_health("OLAP", "olap", "WEIRD", "x")
        assert health_updated.coro["status"] == "DEGRADED"


class TestFireAndForget:
    async def test_fire_degradation_event_schedules_persist(self, monkeypatch, patched):
        captured: list = []
        monkeypatch.setattr(degradation, "_schedule_persist", lambda coro: captured.append(coro))
        degradation.fire_degradation_event("OLAP", "olap", "DEGRADED", "reason")
        assert len(captured) == 1
        # 执行被调度的协程，验证最终落库 + 发事件
        await captured[0]
        eb, session = patched
        eb.publish.assert_awaited_once()
        assert session.commit.await_count == 1

    async def test_handle_circuit_signal_wires_signal_fields(self, monkeypatch):
        captured: list = []
        monkeypatch.setattr(degradation, "_schedule_persist", lambda coro: captured.append(coro))
        signal = _signal("DEGRADED")
        degradation.handle_circuit_signal(signal)
        assert len(captured) == 1
        # 未执行的协程显式关闭，避免 unawaited coroutine 告警
        captured[0].close()


def test_handle_circuit_signal_uses_real_dependency_id(monkeypatch):
    calls: list = []
    monkeypatch.setattr(degradation, "fire_degradation_event", lambda *a, **k: calls.append((a, k)))
    sig = DegradationSignal(
        "OLAP", "DEGRADED", "circuit_open", "OPEN", 3, 1.0, dependency_id="olap-x"
    )
    degradation.handle_circuit_signal(sig)
    args, _ = calls[0]
    assert args[1] == "olap-x"  # 真实实例 id 优先于 type.lower()


def test_handle_circuit_signal_falls_back_to_type_lower(monkeypatch):
    calls: list = []
    monkeypatch.setattr(degradation, "fire_degradation_event", lambda *a, **k: calls.append((a, k)))
    # dependency_id 缺省空串 -> 回退 dependency_type.lower()（兼容单实例依赖）
    sig = DegradationSignal("GRAPH", "DEGRADED", "circuit_open", "OPEN", 3, 1.0)
    degradation.handle_circuit_signal(sig)
    args, _ = calls[0]
    assert args[1] == "graph"

    async def test_schedule_persist_creates_task_and_cleans_up(self):
        done: list[int] = []

        async def coro():
            done.append(1)

        degradation._schedule_persist(coro())
        assert len(degradation._in_flight_tasks) == 1
        await asyncio.gather(*list(degradation._in_flight_tasks))
        assert done == [1]
        # done_callback 将任务从集合移除
        assert not degradation._in_flight_tasks

    def test_schedule_persist_without_running_loop(self):
        # 无运行中的事件循环（同步上下文）→ 静默跳过，不抛异常
        async def coro():
            return None

        c = coro()
        degradation._schedule_persist(c)
        assert not degradation._in_flight_tasks
        c.close()  # 显式关闭，避免 unawaited coroutine 告警


class _ScalarResult:
    """模拟 SQLAlchemy Result：同步链 scalars().all()。"""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list:
        return self._rows


class _ReadSession:
    """仅覆盖读路径的 AsyncSession 替身：execute 返回同步 _ScalarResult。"""

    def __init__(self, rows: list, exc: Exception | None = None) -> None:
        self._rows = rows
        self._exc = exc

    async def execute(self, _stmt: object) -> _ScalarResult:
        if self._exc:
            raise self._exc
        return _ScalarResult(self._rows)


def _health_row(**overrides) -> SimpleNamespace:
    now = datetime.now(UTC)
    base = {
        "dependency_type": "OLAP",
        "dependency_id": "olap",
        "status": "DEGRADED",
        "circuit_state": "OPEN",
        "consecutive_failures": 2,
        "last_check_at": now,
        "latency_p95_ms": 120,
        "error_rate_pct": 0.5,
        "circuit_opened_at": now,
        "meta": {"k": "v"},
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestReadDependencyHealth:
    @asynccontextmanager
    async def _read_session(self, monkeypatch, rows, exc=None):
        @asynccontextmanager
        async def _factory() -> _ReadSession:
            yield _ReadSession(rows, exc=exc)

        monkeypatch.setattr(degradation, "async_session_factory", _factory)
        yield

    async def test_read_returns_dicts(self, monkeypatch):
        row = _health_row()
        async with self._read_session(monkeypatch, [row]):
            result = await degradation.read_dependency_health("OLAP")
        assert len(result) == 1
        assert result[0]["dependency_type"] == "OLAP"
        assert result[0]["status"] == "DEGRADED"
        # 时间为 ISO8601 字符串
        assert isinstance(result[0]["last_check_at"], str)
        assert result[0]["meta"] == {"k": "v"}

    async def test_read_without_filter_returns_all(self, monkeypatch):
        row = _health_row(dependency_type="ES", dependency_id="es")
        async with self._read_session(monkeypatch, [row]):
            result = await degradation.read_dependency_health()
        assert result[0]["dependency_id"] == "es"

    async def test_db_failure_returns_empty(self, monkeypatch):
        async with self._read_session(monkeypatch, [], exc=RuntimeError("db down")):
            result = await degradation.read_dependency_health()
        assert result == []

    def test_row_to_dict_handles_none_timestamps(self):
        row = _health_row(
            last_check_at=None, circuit_opened_at=None, created_at=None, updated_at=None
        )
        out = degradation._row_to_dict(row)
        assert out["last_check_at"] is None
        assert out["circuit_opened_at"] is None
        assert out["created_at"] is None
        assert out["updated_at"] is None


class TestEnsureSeed:
    async def test_seed_inserts_three_dependencies(self, patched):
        _, session = patched
        session.execute = AsyncMock()
        await degradation.ensure_dependency_health_seed()
        # OLAP/GRAPH/ES 三个依赖各执行一次 INSERT
        assert session.execute.await_count == 3
        assert session.commit.await_count == 1

    async def test_seed_db_failure_is_best_effort(self, patched):
        _, session = patched
        session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        await degradation.ensure_dependency_health_seed()  # 不抛异常


class TestFireAndForgetRobustness:
    async def test_schedule_persist_cleans_up_after_exception(self):
        async def boom() -> None:
            raise RuntimeError("persist failed")

        degradation._schedule_persist(boom())
        assert len(degradation._in_flight_tasks) == 1
        # 即便协程抛错，done_callback 仍将其从集合移除，并取回异常避免未捕获告警
        await asyncio.gather(*list(degradation._in_flight_tasks), return_exceptions=True)
        assert not degradation._in_flight_tasks


class TestRecordDegradationBoundary:
    async def test_invalid_state_is_dropped(self, patched):
        eb, session = patched
        # PROBING 非合法审计态（仅 DEGRADED/HEALTHY），边界校验应丢弃，不发布不落库
        await degradation.record_degradation("OLAP", "olap", "PROBING", "half_open")
        eb.publish.assert_not_called()
        session.add.assert_not_called()
        session.commit.assert_not_called()
