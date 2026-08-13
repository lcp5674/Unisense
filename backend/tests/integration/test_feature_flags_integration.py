"""OPS-09 特性开关集成测试。"""
from app.core.feature_flags import FeatureFlagManager


def test_feature_flag_eval():
    mgr = FeatureFlagManager()
    mgr.register_flag("test_flag", enabled=True)
    assert mgr.is_feature_enabled("test_flag") is True
    assert mgr.is_feature_enabled("nonexistent") is False

def test_feature_flag_targeted():
    mgr = FeatureFlagManager()
    mgr.register_flag("flag_a", enabled=True, target_domains=["finance"])
    assert mgr.is_feature_enabled("flag_a", domain="finance") is True
    assert mgr.is_feature_enabled("flag_a", domain="growth") is False
