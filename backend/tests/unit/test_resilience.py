"""熔断器单测（对齐 TD §5.2 降级矩阵）。

验证：
- 状态切换（open/close）仅各上报一次降级信号（DEGRADED/HEALTHY），半开上报 PROBING，不重复刷。
- 信号携带实时熔断态（circuit_state）与连续失败数，供实时健康表使用。
- 半开窗口单飞（single-flight）仍生效（FR-06 已修复行为的回归锁）。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

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


@pytest.fixture(autouse=True)
def _reset_default_store():
    """每个测试前重置模块级默认存储，避免 store=None 的熔断器跨测试读到残留 OPEN 态。"""
    resilience.set_default_circuit_breaker_store(resilience.LocalCircuitBreakerStore())
    yield


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
    # 错误率滑动窗口阈值对齐 TD §5.2a（OLAP/GRAPH=5%，ES=10%）
    assert resilience.olap_breaker._error_rate_threshold == 0.05
    assert resilience.neo4j_breaker._error_rate_threshold == 0.05
    assert resilience.es_breaker._error_rate_threshold == 0.10


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


def test_error_rate_property_reflects_bounded_window():
    """error_rate 反映有界滑动窗口失败率（0~1），样本不足返回 0.0。"""
    b = resilience.CircuitBreaker(name="OLAP", error_rate_window=5)
    assert b.error_rate == 0.0
    for _ in range(4):
        b.record_failure()
    b.record_success()
    assert b.error_rate == 0.8  # 5 个样本里 4 失败
    assert b._recent_outcomes.maxlen == 5


def test_breaker_opens_on_error_rate_exceeding_threshold(captured):
    """错误率超阈（窗口样本充足）应切 OPEN 并上报 DEGRADED，与连续失败互补。"""
    b = resilience.CircuitBreaker(
        name="OLAP",
        failure_threshold=100,  # 抬高连续失败阈值，隔离错误率触发
        reset_timeout=30.0,
        error_rate_threshold=0.5,
        error_rate_window=10,
    )
    for _ in range(6):
        b.record_failure()
    for _ in range(4):
        b.record_success()
    # 窗口=10，失败率=0.6 > 0.5 → allow() 打开熔断
    assert b.allow() is False
    assert b.state == "open"
    assert len(captured) == 1
    assert captured[0].event_state == "DEGRADED"
    assert captured[0].reason == "circuit_open"


def test_breaker_stays_closed_when_error_rate_below_threshold(captured):
    """错误率未超阈（失败率=0.3<0.5）保持 CLOSED，不误开。"""
    b = resilience.CircuitBreaker(
        name="OLAP",
        failure_threshold=100,
        reset_timeout=30.0,
        error_rate_threshold=0.5,
        error_rate_window=10,
    )
    for _ in range(3):
        b.record_failure()
    for _ in range(7):
        b.record_success()
    assert b.allow() is True
    assert b.state == "closed"
    assert len(captured) == 0


def test_breaker_error_rate_ignores_small_sample(captured):
    """样本数未达窗口时不依错误率误开（避免小样本抖动触发熔断）。"""
    b = resilience.CircuitBreaker(
        name="OLAP",
        failure_threshold=100,
        reset_timeout=30.0,
        error_rate_threshold=0.5,
        error_rate_window=10,
    )
    for _ in range(3):
        b.record_failure()  # 失败率 1.0，但样本仅 3 < 窗口 10
    assert b.allow() is True
    assert b.state == "closed"
    assert len(captured) == 0


# ---------------------------------------------------------------------------
# 熔断态共享存储（跨 worker / 副本协调，TD §5.2a，gap #2）
# ---------------------------------------------------------------------------


def test_local_store_roundtrip():
    store = resilience.LocalCircuitBreakerStore()
    assert store.load_state("k") is None
    store.save_state("k", "OPEN", 1.0, 40.0)
    assert store.load_state("k") == ("OPEN", 1.0)


def test_redis_store_save_and_load():
    fake = MagicMock()
    fake.get.side_effect = ["OPEN", "123.4"]
    store = resilience.RedisCircuitBreakerStore("redis://test")
    store._client = fake
    assert store.load_state("OLAP:olap") == ("OPEN", 123.4)
    fake.get.side_effect = [None]
    assert store.load_state("OLAP:olap") is None


def test_redis_store_probe_single_flight():
    fake = MagicMock()
    fake.set.side_effect = [True, False]  # 首次获取成功，二次被持锁
    store = resilience.RedisCircuitBreakerStore("redis://test")
    store._client = fake
    assert store.try_acquire_probe("OLAP:olap", 5.0) is True
    assert store.try_acquire_probe("OLAP:olap", 5.0) is False


def test_redis_store_save_state_writes_ttl_and_clears_on_recover():
    fake = MagicMock()
    store = resilience.RedisCircuitBreakerStore("redis://test")
    store._client = fake
    store.save_state("OLAP:olap", "OPEN", 123.0, 40.0)
    # OPEN 且 opened_at 非 None → state + opened_at 各带 ex 写入，不删
    assert fake.set.call_count == 2
    assert fake.delete.call_count == 0
    fake.reset_mock()
    store.save_state("OLAP:olap", "CLOSED", None, 40.0)
    # 恢复 → 仅写 state 一次并清理 opened_at
    assert fake.set.call_count == 1
    assert fake.delete.call_count == 1


def test_breaker_coordinates_open_state_across_instances(captured):
    """任一 worker 打开熔断 → 共享态 OPEN → 其余 worker 拒绝（集群级防雪崩）。"""
    store = resilience.LocalCircuitBreakerStore()
    b1 = resilience.CircuitBreaker(
        name="OLAP",
        dependency_id="olap",
        store=store,
        failure_threshold=2,
        reset_timeout=10.0,
    )
    b2 = resilience.CircuitBreaker(
        name="OLAP",
        dependency_id="olap",
        store=store,
        failure_threshold=100,
        reset_timeout=10.0,  # 本地阈值抬高，隔离为集群协调
    )
    b1.record_failure()
    b1.record_failure()  # b1 打开并写共享态
    assert b1.state == "open"
    assert b2._open is False  # b2 本地仍关闭
    assert b2.allow() is False  # 集群级：peer 已开 → 拒绝（不重复上报）
    assert len(captured) == 1  # 仅 b1 上报一次 DEGRADED
    b1.record_success()  # 恢复 → 共享态切 CLOSED
    b2._shared_cache = None  # 模拟本地缓存到期，重新读共享态
    assert b2.allow() is True


def test_breaker_respects_redis_probe_lock(captured):
    """半开窗口内若集群探针锁已被持（另一 worker 探测中），本 worker 拒绝不重复探测。"""
    fake = MagicMock()
    fake.set.return_value = False  # 探针锁已被持
    store = resilience.RedisCircuitBreakerStore("redis://test")
    store._client = fake
    b = resilience.CircuitBreaker(
        name="OLAP",
        dependency_id="olap",
        store=store,
        failure_threshold=1,
        reset_timeout=0.0,
        probe_timeout=5.0,
    )
    b.record_failure()  # 打开
    assert b.allow() is False  # store 拒绝探针锁 → 本 worker 不探测
    assert b._probing is False


def test_init_circuit_breaker_store_falls_back_without_redis():
    """Redis 不可用（pool=None）时降级为进程内 Local 存储，不抛异常。"""
    original = resilience.get_default_circuit_breaker_store()
    try:
        resilience.init_circuit_breaker_store(None)
        assert isinstance(
            resilience.get_default_circuit_breaker_store(), resilience.LocalCircuitBreakerStore
        )
    finally:
        resilience.set_default_circuit_breaker_store(original)


# ---------------------------------------------------------------------------
# 补覆盖：监听器注册/异常兜底、抽象基类、Redis store 懒建/clear、共享态异常路径
# ---------------------------------------------------------------------------


def test_register_degradation_listener_appends(monkeypatch):
    """register_degradation_listener 将监听器追加到全局列表。"""
    seen: list[resilience.DegradationSignal] = []
    fn: resilience.DegradationListener = seen.append  # noqa: F821
    monkeypatch.setattr(resilience, "_degradation_listeners", [])
    resilience.register_degradation_listener(fn)
    assert resilience._degradation_listeners == [fn]


def test_emitter_swallows_listener_exception(monkeypatch):
    """监听器抛异常不应阻断熔断主流程（best-effort 兜底，记 warning 继续）。"""

    def _boom(_sig: resilience.DegradationSignal) -> None:
        raise RuntimeError("listener boom")

    signals: list[resilience.DegradationSignal] = []
    monkeypatch.setattr(
        resilience,
        "_degradation_listeners",
        [_boom, lambda s: signals.append(s)],
    )
    b = resilience.CircuitBreaker(name="OLAP", failure_threshold=1, reset_timeout=10.0)
    b.record_failure()  # 打开触发 DEGRADED；_boom 抛异常被吞
    assert len(signals) == 1
    assert b.state == "open"


def test_abstract_store_raises_not_implemented():
    """抽象基类三个接口方法直接调用应抛 NotImplementedError（引导实现者）。"""
    store = resilience.CircuitBreakerStore()
    with pytest.raises(NotImplementedError):
        store.load_state("k")
    with pytest.raises(NotImplementedError):
        store.save_state("k", "OPEN", 1.0, 40.0)
    with pytest.raises(NotImplementedError):
        store.try_acquire_probe("k", 5.0)
    store.clear_probe("k")  # 有默认空实现（no-op，不抛）


def test_redis_store_lazy_client_creation(monkeypatch):
    """Redis store 首次访问时经 redis.Redis.from_url 懒建客户端，二次复用。"""
    fake_client = MagicMock()
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **k: fake_client)
    store = resilience.RedisCircuitBreakerStore("redis://test")
    assert store._client is None
    assert store._get_client() is fake_client
    assert store._get_client() is fake_client  # 复用不新建


def test_redis_store_clear_probe():
    fake = MagicMock()
    store = resilience.RedisCircuitBreakerStore("redis://test")
    store._client = fake
    store.clear_probe("OLAP:olap")
    fake.delete.assert_called_once_with("cb:OLAP:olap:probing")


def test_init_circuit_breaker_store_with_redis(monkeypatch):
    """Redis 可用（pool 非空）→ 构建 Redis store 并切换为默认存储。"""
    original = resilience.get_default_circuit_breaker_store()
    try:
        monkeypatch.setattr(resilience.settings, "redis_url", "redis://localhost:6379/0")
        resilience.init_circuit_breaker_store(object())
        assert isinstance(
            resilience.get_default_circuit_breaker_store(), resilience.RedisCircuitBreakerStore
        )
    finally:
        resilience.set_default_circuit_breaker_store(original)


def test_init_circuit_breaker_store_redis_init_failure(monkeypatch):
    """Redis store 构建抛异常 → 降级为 Local（best-effort，不阻断启动）。"""
    original = resilience.get_default_circuit_breaker_store()
    try:
        monkeypatch.setattr(resilience.settings, "redis_url", "redis://localhost:6379/0")

        def _boom(*a: object, **k: object) -> None:
            raise RuntimeError("redis init boom")

        monkeypatch.setattr(resilience, "RedisCircuitBreakerStore", _boom)
        resilience.init_circuit_breaker_store(object())
        assert isinstance(
            resilience.get_default_circuit_breaker_store(), resilience.LocalCircuitBreakerStore
        )
    finally:
        resilience.set_default_circuit_breaker_store(original)


def test_failures_property_reflects_counter():
    """failures 属性返回当前连续失败次数（实时健康表/看板用）。"""
    b = resilience.CircuitBreaker(name="OLAP", failure_threshold=3, reset_timeout=10.0)
    assert b.failures == 0
    b.record_failure()
    assert b.failures == 1


def test_shared_state_handles_none_store(monkeypatch):
    """默认存储为 None 时，共享态读写/探针锁/释放均安全 no-op（不抛）。"""
    b = resilience.CircuitBreaker(
        name="OLAP", dependency_id="olap", failure_threshold=1, reset_timeout=10.0
    )
    monkeypatch.setattr(resilience, "_DEFAULT_STORE", None)
    assert b._load_shared_state() is None
    b._save_shared_state("OPEN", 1.0)  # no-op
    assert b._acquire_probe_lock() is True  # 无 store 默认放行（安全侧）
    b._release_probe_lock()  # no-op


def test_load_shared_state_cache_hit():
    """共享态本地缓存 TTL 内命中，不重复调用 store.load_state。"""
    store = MagicMock()
    store.load_state.return_value = ("OPEN", 1.0)
    b = resilience.CircuitBreaker(name="OLAP", dependency_id="olap", store=store)
    assert b._load_shared_state() == ("OPEN", 1.0)
    store.load_state.assert_called_once()
    # 缓存命中（TTL 内）→ 不再调用 store
    assert b._load_shared_state() == ("OPEN", 1.0)
    assert store.load_state.call_count == 1


def test_load_shared_state_exception_falls_back():
    """store.load_state 抛异常 → 返回 None（best-effort 降级），不阻断。"""
    store = MagicMock()
    store.load_state.side_effect = RuntimeError("redis down")
    b = resilience.CircuitBreaker(name="OLAP", dependency_id="olap", store=store)
    assert b._load_shared_state() is None


def test_save_shared_state_exception_best_effort():
    """store.save_state 抛异常 → 静默降级（记 warning，不抛）。"""
    store = MagicMock()
    store.save_state.side_effect = RuntimeError("redis down")
    b = resilience.CircuitBreaker(name="OLAP", dependency_id="olap", store=store)
    b._save_shared_state("OPEN", 1.0)  # 不抛


def test_acquire_probe_lock_exception_falls_open():
    """store.try_acquire_probe 抛异常 → 默认放行（安全侧，避免永久拒绝）。"""
    store = MagicMock()
    store.try_acquire_probe.side_effect = RuntimeError("redis down")
    b = resilience.CircuitBreaker(name="OLAP", dependency_id="olap", store=store)
    assert b._acquire_probe_lock() is True


def test_release_probe_lock_exception_best_effort():
    """store.clear_probe 抛异常 → 静默降级，不抛。"""
    store = MagicMock()
    store.clear_probe.side_effect = RuntimeError("redis down")
    b = resilience.CircuitBreaker(name="OLAP", dependency_id="olap", store=store)
    b._release_probe_lock()  # 不抛


def test_tcp_alive_unreachable_returns_false(monkeypatch):
    """TCP 探活不可达 → False（OSError 兜底，不抛异常）。"""

    def _boom(*a: object, **k: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(resilience.socket, "create_connection", _boom)
    assert resilience._tcp_alive("127.0.0.1", 9, timeout=0.1) is False


def test_parse_host_port_no_match():
    """连接串无协议/端口 → None（调用方视依赖未启用）。"""
    assert resilience._parse_host_port("not-a-url") is None
    assert resilience._parse_host_port("localhost") is None


# ---------------------------------------------------------------------------
# 补覆盖：冷却期拒绝、TCP 探活成功、host:port 解析、可选依赖探活遍历
# ---------------------------------------------------------------------------


def test_breaker_rejects_while_open_before_reset(captured):
    """本地已打开且冷却未到 → allow() 拒绝（不进入半开探测）。"""
    b = resilience.CircuitBreaker(name="OLAP", failure_threshold=1, reset_timeout=30.0)
    b.record_failure()  # 打开
    assert b.state == "open"
    assert b.allow() is False  # 冷却 30s 未到 → 拒绝


def test_tcp_alive_reachable_returns_true(monkeypatch):
    """TCP 探活可达 → True。"""

    class _FakeSock:
        def __enter__(self) -> _FakeSock:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    monkeypatch.setattr(resilience.socket, "create_connection", lambda *a, **k: _FakeSock())
    assert resilience._tcp_alive("127.0.0.1", 9, timeout=0.1) is True


def test_parse_host_port_parses():
    """连接串成功解析 host:port（支持 bolt/http/mysql 等 scheme）。"""
    assert resilience._parse_host_port("bolt://neo4j:7687") == ("neo4j", 7687)
    assert resilience._parse_host_port("http://localhost:19200") == ("localhost", 19200)


async def test_optional_dependency_status(monkeypatch):
    """探活可选依赖：未配置跳过、解析失败记 False、成功记 True。"""
    monkeypatch.setattr(resilience.settings, "neo4j_url", "bolt://neo4j:7687")
    monkeypatch.setattr(resilience.settings, "es_url", "http://es:9200")
    monkeypatch.setattr(resilience.settings, "olap_url", "")  # 未配置 → 跳过

    def _fake_tcp(host: str, port: int, timeout: float = 0.5) -> bool:
        return host != "neo4j"  # neo4j 不可达，es 可达

    async def _fake_tcp_async(host: str, port: int, timeout: float = 0.5) -> bool:
        return host != "neo4j"

    monkeypatch.setattr(resilience, "_tcp_alive", _fake_tcp)
    monkeypatch.setattr(resilience, "_tcp_alive_async", _fake_tcp_async)
    status = await resilience.optional_dependency_status()
    assert status == {"neo4j": False, "elasticsearch": True}
