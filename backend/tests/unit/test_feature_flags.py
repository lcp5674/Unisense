"""特性开关单测（OPS-09: 特性开关框架）。

覆盖：
- register_flag / is_feature_enabled 的全局/定向域/定向用户规则
- 未注册开关默认 False（安全侧）
- is_feature_enabled_or_default 未注册默认 True（存量能力非破坏）
- update_flag / get_all_flags
- refresh_from_redis（命中/空/异常）
- 单例 get/init
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.core.feature_flags import (
    FeatureFlag,
    FeatureFlagManager,
    get_feature_flag_manager,
    is_feature_enabled,
    is_feature_enabled_or_default,
)


class TestRegisterAndQuery:
    def test_register_flag(self) -> None:
        m = FeatureFlagManager()
        flag = m.register_flag("emergency_publish", enabled=True, description="紧急发布")
        assert flag.name == "emergency_publish"
        assert m.get_flag("emergency_publish") is flag

    def test_unregistered_defaults_false(self) -> None:
        m = FeatureFlagManager()
        assert m.is_feature_enabled("nonexistent") is False

    def test_global_enabled_true(self) -> None:
        m = FeatureFlagManager()
        m.register_flag("quickbi", enabled=True)
        assert m.is_feature_enabled("quickbi") is True

    def test_global_disabled_false(self) -> None:
        m = FeatureFlagManager()
        m.register_flag("quickbi", enabled=False)
        assert m.is_feature_enabled("quickbi") is False

    def test_domain_targeting(self) -> None:
        m = FeatureFlagManager()
        m.register_flag("f", enabled=True, target_domains=["sales"])
        assert m.is_feature_enabled("f", domain="sales") is True
        assert m.is_feature_enabled("f", domain="finance") is False
        assert m.is_feature_enabled("f") is False

    def test_user_targeting(self) -> None:
        m = FeatureFlagManager()
        m.register_flag("f", enabled=True, target_users=[1, 2])
        assert m.is_feature_enabled("f", user_id=1) is True
        assert m.is_feature_enabled("f", user_id=3) is False
        assert m.is_feature_enabled("f") is False

    def test_no_targets_allows_all(self) -> None:
        m = FeatureFlagManager()
        m.register_flag("f", enabled=True, target_domains=[], target_users=[])
        assert m.is_feature_enabled("f", domain="x", user_id=9) is True


class TestUpdate:
    def test_update_existing(self) -> None:
        m = FeatureFlagManager()
        m.register_flag("f", enabled=False)
        flag = m.update_flag("f", enabled=True, description="updated")
        assert flag is not None
        assert flag.enabled is True
        assert flag.description == "updated"

    def test_update_missing_returns_none(self) -> None:
        m = FeatureFlagManager()
        assert m.update_flag("nope", enabled=True) is None

    def test_get_all_flags(self) -> None:
        m = FeatureFlagManager()
        m.register_flag("a", enabled=True)
        m.register_flag("b", enabled=False)
        assert len(m.get_all_flags()) == 2


class TestDefaultHelper:
    def test_unregistered_returns_default_true(self) -> None:
        # 未注册 → 默认开启（存量能力非破坏）
        assert is_feature_enabled_or_default("emergency_publish") is True

    def test_unregistered_respects_custom_default(self) -> None:
        assert is_feature_enabled_or_default("f", default=False) is False

    def test_registered_disabled_overrides_default(self) -> None:
        m = get_feature_flag_manager()
        m.register_flag("gate_test", enabled=False)
        try:
            assert is_feature_enabled_or_default("gate_test") is False
        finally:
            m._flags.pop("gate_test", None)

    def test_registered_enabled(self) -> None:
        m = get_feature_flag_manager()
        m.register_flag("gate_test", enabled=True)
        try:
            assert is_feature_enabled_or_default("gate_test") is True
        finally:
            m._flags.pop("gate_test", None)

    def test_module_level_is_feature_enabled(self) -> None:
        assert is_feature_enabled("definitely_missing") is False


class TestRedisRefresh:
    def test_refresh_within_ttl_skips(self) -> None:
        m = FeatureFlagManager()
        redis = MagicMock()
        m._cache_at = 999999.0
        m.refresh_from_redis(redis)
        redis.hkeys.assert_not_called()

    def test_refresh_populates_flags(self) -> None:
        m = FeatureFlagManager()
        redis = MagicMock()
        redis.hkeys.return_value = ["ff_a"]
        redis.hget.return_value = json.dumps({"enabled": True, "description": "d"})
        m._cache_at = 0.0
        m.refresh_from_redis(redis)
        flag = m.get_flag("ff_a")
        assert flag is not None
        assert flag.enabled is True

    def test_refresh_redis_error_silent(self) -> None:
        m = FeatureFlagManager()
        redis = MagicMock()
        redis.hkeys.side_effect = RuntimeError("redis down")
        m._cache_at = 0.0
        m.refresh_from_redis(redis)  # 不应抛异常


class TestFeatureFlagModel:
    def test_to_dict(self) -> None:
        f = FeatureFlag("x", enabled=True, target_domains=["s"], target_users=[1])
        d = f.to_dict()
        assert d["name"] == "x"
        assert d["enabled"] is True
        assert d["target_domains"] == ["s"]
        assert d["target_users"] == [1]


class TestSingleton:
    def test_get_returns_singleton(self, monkeypatch) -> None:
        from app.core import feature_flags as ff_module

        old = ff_module._manager
        monkeypatch.setattr(ff_module, "_manager", None)
        try:
            a = ff_module.get_feature_flag_manager()
            b = ff_module.get_feature_flag_manager()
            assert a is b
        finally:
            monkeypatch.setattr(ff_module, "_manager", old)

    def test_init_replaces_singleton(self, monkeypatch) -> None:
        from app.core import feature_flags as ff_module

        old = ff_module._manager
        monkeypatch.setattr(ff_module, "_manager", None)
        try:
            inst = ff_module.init_feature_flag_manager()
            assert ff_module.get_feature_flag_manager() is inst
        finally:
            monkeypatch.setattr(ff_module, "_manager", old)
