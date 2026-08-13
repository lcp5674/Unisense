"""OPS-05 降级注册中心集成测试。"""

from app.core.degradation_registry import DegradationRegistry


def test_register_and_query():
    reg = DegradationRegistry()
    reg.register_degradation("redis", "connection_lost")
    assert reg.is_degraded("redis")
    assert len(reg.get_active_degradations()) == 1


def test_clear_degradation():
    reg = DegradationRegistry()
    reg.register_degradation("redis", "connection_lost")
    reg.clear_degradation("redis")
    assert not reg.is_degraded("redis")
