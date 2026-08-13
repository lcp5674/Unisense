"""降级注册中心单测（OPS-05: 统一降级面板 + 健康检查降级状态）。

覆盖：
- register_degradation / register_down / clear_degradation
- get_all_degradations / get_active_degradations / get_degradation
- is_degraded / is_any_degraded / get_status_summary
- 重复注册降级（更新 reason，不重置 since）
- 单例 get/init
"""

from __future__ import annotations

from app.core.degradation_registry import (
    DEGRADED,
    DOWN,
    HEALTHY,
    DegradationEntry,
    DegradationRegistry,
)


class TestRegister:
    def test_register_degradation(self) -> None:
        r = DegradationRegistry()
        r.register_degradation("redis", "redis down")
        entry = r.get_degradation("redis")
        assert entry is not None
        assert entry.status == DEGRADED
        assert entry.since is not None

    def test_register_duplicate_updates_reason(self) -> None:
        r = DegradationRegistry()
        r.register_degradation("redis", "first")
        since1 = r.get_degradation("redis").since
        r.register_degradation("redis", "second")
        entry = r.get_degradation("redis")
        assert entry.reason == "second"
        # 重复注册不重置 since
        assert entry.since == since1

    def test_register_down(self) -> None:
        r = DegradationRegistry()
        r.register_down("neo4j", "unreachable")
        assert r.get_degradation("neo4j").status == DOWN

    def test_clear_degradation(self) -> None:
        r = DegradationRegistry()
        r.register_degradation("es", "fail")
        r.clear_degradation("es")
        entry = r.get_degradation("es")
        assert entry.status == HEALTHY
        assert entry.reason is None
        assert entry.since is None

    def test_clear_healthy_is_noop(self) -> None:
        r = DegradationRegistry()
        r.clear_degradation("never_registered")
        assert r.get_degradation("never_registered") is None


class TestQuery:
    def test_get_active_only_non_healthy(self) -> None:
        r = DegradationRegistry()
        r.register_degradation("redis", "down")
        r.register_down("neo4j", "down")
        r.clear_degradation("redis")
        active = r.get_active_degradations()
        assert [e.component for e in active] == ["neo4j"]

    def test_is_degraded(self) -> None:
        r = DegradationRegistry()
        assert r.is_degraded("redis") is False
        r.register_degradation("redis", "down")
        assert r.is_degraded("redis") is True
        r.clear_degradation("redis")
        assert r.is_degraded("redis") is False

    def test_is_any_degraded(self) -> None:
        r = DegradationRegistry()
        assert r.is_any_degraded() is False
        r.register_degradation("olap", "down")
        assert r.is_any_degraded() is True

    def test_status_summary_healthy(self) -> None:
        r = DegradationRegistry()
        s = r.get_status_summary()
        assert s["overall_status"] == "healthy"
        assert s["degraded_count"] == 0

    def test_status_summary_degraded(self) -> None:
        r = DegradationRegistry()
        r.register_degradation("redis", "down")
        s = r.get_status_summary()
        assert s["overall_status"] == "degraded"
        assert s["degraded_count"] == 1
        assert s["degraded_components"][0]["component"] == "redis"


class TestEntry:
    def test_to_dict(self) -> None:
        e = DegradationEntry("redis", status=DEGRADED, reason="down")
        d = e.to_dict()
        assert d["component"] == "redis"
        assert d["status"] == DEGRADED
        assert d["reason"] == "down"
        assert d["since"] is not None

    def test_healthy_entry_has_no_since(self) -> None:
        e = DegradationEntry("redis")
        assert e.status == HEALTHY
        assert e.since is None


class TestSingleton:
    def test_get_returns_singleton(self, monkeypatch) -> None:
        from app.core import degradation_registry as dr_module

        old = dr_module._registry
        monkeypatch.setattr(dr_module, "_registry", None)
        try:
            a = dr_module.get_degradation_registry()
            b = dr_module.get_degradation_registry()
            assert a is b
        finally:
            monkeypatch.setattr(dr_module, "_registry", old)

    def test_init_replaces_singleton(self, monkeypatch) -> None:
        from app.core import degradation_registry as dr_module

        old = dr_module._registry
        monkeypatch.setattr(dr_module, "_registry", None)
        try:
            inst = dr_module.init_degradation_registry()
            assert dr_module.get_degradation_registry() is inst
        finally:
            monkeypatch.setattr(dr_module, "_registry", old)
