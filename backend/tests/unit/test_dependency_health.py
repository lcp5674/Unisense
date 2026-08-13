"""依赖实时健康态单测（对齐 TD §4.13 dependency_health）。

验证：
- update_dependency_health 按 (dependency_type, dependency_id) UPSERT 到 dependency_health。
- _signal_to_health_params 将熔断器信号映射为正确的 health 参数（DEGRADED/HEALTHY/PROBING）。
- _persist_degradation_and_health 同时落 degradation_event 与 dependency_health。
- handle_circuit_signal 将信号正确映射并触发 fire_degradation_event（含 circuit_state）。
"""

from __future__ import annotations

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
    session.execute = AsyncMock()
    session.add = MagicMock()

    def _factory():
        @asynccontextmanager
        async def _cm():
            yield session

        return _cm()

    eb = MagicMock()
    eb.publish = AsyncMock()
    monkeypatch.setattr(degradation, "async_session_factory", _factory)
    monkeypatch.setattr(degradation, "get_eventbus", lambda: eb)
    return eb, session


async def test_update_dependency_health_upserts(patched):
    eb, session = patched
    await degradation.update_dependency_health(
        "OLAP", "olap", status="DEGRADED", circuit_state="OPEN", consecutive_failures=3
    )
    session.execute.assert_awaited_once()
    stmt = session.execute.call_args[0][0]
    assert stmt.table.name == "dependency_health"
    session.commit.assert_awaited()


async def test_upsert_preserves_unspecified_telemetry(patched_read):
    """熔断事件式调用（仅 status/circuit_state/consecutive_failures）不得清零
    P95 延迟 / 错误率 / 扩展信息等探针采集的遥测字段。"""
    from sqlalchemy.dialects import mysql as mysql_dialect

    await degradation.update_dependency_health(
        "OLAP", "olap", status="DEGRADED", circuit_state="OPEN", consecutive_failures=3
    )
    stmt = patched_read.execute.call_args_list[0][0][0]
    sql = str(stmt.compile(dialect=mysql_dialect.dialect()))
    update_part = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
    # 提供的字段必须出现在更新子句
    assert "status" in update_part
    assert "circuit_state" in update_part
    assert "consecutive_failures" in update_part
    # 未提供的遥测字段必须不出现（保护既有值，避免谎报错误率 0% / 丢失元数据）
    assert "latency_p95_ms" not in update_part
    assert "error_rate_pct" not in update_part
    assert "meta" not in update_part


async def test_upsert_includes_provided_telemetry(patched_read):
    """探针显式提供遥测字段时，应纳入更新子句。"""
    from sqlalchemy.dialects import mysql as mysql_dialect

    await degradation.update_dependency_health(
        "OLAP",
        "olap",
        status="DEGRADED",
        circuit_state="OPEN",
        latency_p95_ms=120,
        error_rate_pct=2.5,
        metadata={"threshold": 5},
    )
    stmt = patched_read.execute.call_args_list[0][0][0]
    sql = str(stmt.compile(dialect=mysql_dialect.dialect()))
    update_part = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
    assert "latency_p95_ms" in update_part
    assert "error_rate_pct" in update_part
    assert "meta" in update_part


async def test_persist_writes_event_and_health(patched):
    eb, session = patched
    await degradation._persist_degradation_and_health(
        "OLAP",
        "olap",
        "DEGRADED",
        "circuit_open",
        circuit_state="OPEN",
        consecutive_failures=3,
    )
    # degradation_event 写入
    assert session.add.call_count == 1
    added = session.add.call_args_list[0][0][0]
    assert isinstance(added, DegradationEvent)
    assert added.dependency_type == "OLAP"
    assert added.state == "DEGRADED"
    # dependency_health upsert 执行
    assert session.execute.await_count == 1
    # 事件总线发布
    eb.publish.assert_awaited_once()
    assert eb.publish.call_args[0][0] == "degradation.state_changed"


def test_signal_to_health_params():
    deg = DegradationSignal("OLAP", "DEGRADED", "circuit_open", "OPEN", 3, 1.0)
    hp = degradation._signal_to_health_params(deg)
    assert hp["status"] == "DEGRADED"
    assert hp["circuit_state"] == "OPEN"
    assert hp["consecutive_failures"] == 3
    assert hp["circuit_opened_at"] is not None

    rec = DegradationSignal("OLAP", "HEALTHY", "circuit_recovered", "CLOSED", 0, None)
    hp = degradation._signal_to_health_params(rec)
    assert hp["status"] == "HEALTHY"
    assert hp["circuit_state"] == "CLOSED"
    assert hp["consecutive_failures"] == 0
    assert hp["circuit_opened_at"] is None

    probe = DegradationSignal("OLAP", "PROBING", "circuit_half_open", "HALF_OPEN", 2, None)
    hp = degradation._signal_to_health_params(probe)
    assert hp["status"] == "DEGRADED"
    assert hp["circuit_state"] == "HALF_OPEN"


def test_handle_circuit_signal_maps_to_fire(monkeypatch):
    calls = []
    monkeypatch.setattr(degradation, "fire_degradation_event", lambda *a, **k: calls.append((a, k)))
    sig = DegradationSignal("OLAP", "DEGRADED", "circuit_open", "OPEN", 3, 1.0)
    degradation.handle_circuit_signal(sig)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("OLAP", "olap", "DEGRADED", "circuit_open")
    assert kwargs["circuit_state"] == "OPEN"
    assert kwargs["consecutive_failures"] == 3
    assert kwargs["circuit_opened_at"] is not None


@pytest.fixture
def patched_read(monkeypatch):
    """仅 mock session factory，供 read/seed 测试断言生成的 SQL 与调用次数。"""
    session = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()

    @asynccontextmanager
    async def _cm():
        yield session

    monkeypatch.setattr(degradation, "async_session_factory", _cm)
    return session


async def test_read_dependency_health(patched_read):
    row = SimpleNamespace(
        dependency_type="OLAP",
        dependency_id="olap",
        status="HEALTHY",
        circuit_state="CLOSED",
        consecutive_failures=0,
        last_check_at=datetime(2026, 1, 1, tzinfo=UTC),
        latency_p95_ms=12,
        error_rate_pct=0.0,
        circuit_opened_at=None,
        meta={"threshold": 5},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [row]
    patched_read.execute.return_value = result

    items = await degradation.read_dependency_health()
    assert len(items) == 1
    assert items[0]["dependency_type"] == "OLAP"
    assert items[0]["circuit_state"] == "CLOSED"
    assert items[0]["last_check_at"] == "2026-01-01T00:00:00+00:00"
    assert items[0]["meta"] == {"threshold": 5}


async def test_read_dependency_health_db_failure(patched_read):
    patched_read.execute.side_effect = RuntimeError("db down")
    items = await degradation.read_dependency_health()
    assert items == []


async def test_ensure_seed_insert_ignore(patched_read):
    await degradation.ensure_dependency_health_seed()
    # 三个受监控依赖（OLAP/GRAPH/ES）各一条 INSERT
    assert patched_read.execute.await_count == 3
    patched_read.commit.assert_awaited_once()
    # 使用 INSERT IGNORE：已存在则跳过，绝不覆盖真实运行态
    stmt = patched_read.execute.call_args_list[0][0][0]
    assert "INSERT IGNORE" in str(stmt).upper()
    # 重复播种不抛错、不重复插入
    await degradation.ensure_dependency_health_seed()
    assert patched_read.execute.await_count == 6
