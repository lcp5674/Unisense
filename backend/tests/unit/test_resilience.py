"""熔断器单测（对齐 TD §5.2 降级矩阵）。

验证：
- 状态切换（open/close）仅各上报一次降级信号（DEGRADED/HEALTHY），半开上报 PROBING，不重复刷。
- 信号携带实时熔断态（circuit_state）与连续失败数，供实时健康表使用。
- 半开窗口单飞（single-flight）仍生效（FR-06 已修复行为的回归锁）。
"""

from __future__ import annotations

import time

import pytest

from app.core import resilience


@pytest.fixture
def captured(monkeypatch):
    """用受控监听器替换全局监听器列表，捕获 DegradationSignal（best-effort 不抛）。"""
    signals: list[resilience.DegradationSignal] = []
    monkeypatch.setattr(
        resilience,
        "_degradation_listeners",
        [lambda s: signals.append(s)],
    )
    return signals


def test_breaker_emits_open_signal_on_threshold_and_recovers(captured):
    b = resilience.CircuitBreaker(name="OLAP", failure_threshold=3, reset_timeout=10.0)
    for _ in range(3):
        b.record_failure()

    assert b.state == "open"
    # 仅在第 3 次（达到阈值）那一刻上报一次 DEGRADED/OPEN 信号
    assert len(captured) == 1
    sig = captured[0]
    assert sig.dependency_type == "OLAP"
    assert sig.event_state == "DEGRADED"
    assert sig.reason == "circuit_open"
    assert sig.circuit_state == "OPEN"
    assert sig.consecutive_failures == 3

    b.record_success()
    assert b.state == "closed"
    # 恢复时上报一次 HEALTHY/CLOSED 信号
    assert len(captured) == 2
    rec = captured[1]
    assert rec.event_state == "HEALTHY"
    assert rec.reason == "circuit_recovered"
    assert rec.circuit_state == "CLOSED"
    assert rec.consecutive_failures == 0


def test_breaker_does_not_reemit_while_stayed_open(captured):
    b = resilience.CircuitBreaker(name="GRAPH", failure_threshold=2, reset_timeout=10.0)
    b.record_failure()
    b.record_failure()  # 打开
    assert len(captured) == 1
    assert captured[0].event_state == "DEGRADED"
    # 打开态继续失败，不再重复上报
    b.record_failure()
    b.record_failure()
    assert len(captured) == 1


def test_breaker_emits_half_open_signal_on_probe(captured):
    # reset_timeout=0 -> 打开后立刻进入半开
    b = resilience.CircuitBreaker(name="OLAP", failure_threshold=1, reset_timeout=0.0)
    b.record_failure()
    assert b.state == "half-open"
    assert len(captured) == 1  # DEGRADED/OPEN
    assert b.allow() is True  # 首个探测放行 -> 半开信号
    assert b.allow() is False  # 探测窗口内其余请求拒绝（防恢复期雪崩）
    probe = captured[-1]
    assert probe.event_state == "PROBING"
    assert probe.reason == "circuit_half_open"
    assert probe.circuit_state == "HALF_OPEN"


def test_breaker_defaults_dependency_id_to_name_lower():
    b = resilience.CircuitBreaker(name="GRAPH", failure_threshold=2, reset_timeout=10.0)
    assert b._dependency_id == "graph"  # 单实例依赖：默认 name.lower() 对齐种子 id


def test_breaker_uses_explicit_dependency_id():
    # 多实例依赖（如多 DATASOURCE）显式传入实例 id
    b = resilience.CircuitBreaker(name="DATASOURCE", dependency_id="ds-123")
    assert b._dependency_id == "ds-123"


def test_emitted_signal_carries_dependency_id(captured):
    # 多实例依赖的实例 id 应原样进入信号，供 handle_circuit_signal 落到正确健康行
    b = resilience.CircuitBreaker(
        name="OLAP", failure_threshold=1, reset_timeout=1.0, dependency_id="olap-cluster-1"
    )
    b.record_failure()
    assert len(captured) == 1
    assert captured[0].dependency_id == "olap-cluster-1"


def test_breaker_recovers_from_lost_probe(captured):
    """探测结果丢失（调用方未回调 record_*）时，超过 probe_timeout 应允许重新探测，避免永久拒绝。"""
    b = resilience.CircuitBreaker(
        name="ES", failure_threshold=1, reset_timeout=5.0, probe_timeout=2.0
    )
    b.record_failure()
    assert b.state == "open"
    # 模拟 reset_timeout(5s) 已过 -> 进入 half-open，首个探测放行
    b._opened_at = time.monotonic() - 6.0
    assert b.allow() is True  # 首个探测放行 -> PROBING
    assert b._probing is True
    assert b.allow() is False  # 探测窗口内其余请求仍拒绝（防恢复期雪崩）
    # 模拟探测结果丢失：强制半开窗口已超时（_probing_since 远早于 probe_timeout）
    b._probing_since = time.monotonic() - 3.0  # 丢失 > probe_timeout(2s)
    assert b.allow() is True  # 重新放行探测
    b.record_success()  # 重新探测成功 -> 恢复
    assert b.state == "closed"
    # 事件序列：DEGRADED -> PROBING -> PROBING(重新放行) -> HEALTHY
    assert [s.event_state for s in captured] == ["DEGRADED", "PROBING", "PROBING", "HEALTHY"]


def test_predefined_breakers_match_td_thresholds():
    """预构建熔断器阈值对齐 TD §5.2a 各依赖类型阈值参数。"""
    assert resilience.olap_breaker._failure_threshold == 3
    assert resilience.olap_breaker._reset_timeout == 30.0
    assert resilience.neo4j_breaker._failure_threshold == 5
    assert resilience.neo4j_breaker._reset_timeout == 30.0
    assert resilience.es_breaker._failure_threshold == 5
    assert resilience.es_breaker._reset_timeout == 30.0


def test_get_circuit_breaker_returns_shared_instance():
    """get_circuit_breaker 跨调用返回同一实例，保证熔断状态共享（不每调用新建）。"""
    b1 = resilience.get_circuit_breaker("olap")
    b2 = resilience.get_circuit_breaker("olap")
    assert b1 is resilience.olap_breaker is b2  # 已知服务返回注册表单例
    # 未知服务跨调用返回同一实例（状态共享），而非每次新建导致熔断失效
    u1 = resilience.get_circuit_breaker("unknown-svc")
    u2 = resilience.get_circuit_breaker("unknown-svc")
    assert u1 is u2
    assert u1._name == "UNKNOWN-SVC"
