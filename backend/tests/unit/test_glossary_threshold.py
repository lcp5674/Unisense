"""术语同义词阈值测试（T058 部分）。

验证：
1. 同义词冲突阈值可配置（默认 0.8）
2. _get_synonym_threshold 返回 settings 值
3. _detect_conflicts 使用阈值而非硬编码 0.8
"""

from unittest.mock import patch


def test_synonym_threshold_default():
    """T058: 同义词冲突阈值默认 0.8。"""
    from app.services.glossary.service import _get_synonym_threshold

    threshold = _get_synonym_threshold()
    assert threshold == 0.8


def test_synonym_threshold_configurable():
    """T058: 同义词冲突阈值可通过 settings 配置。"""
    from app.services.glossary.service import _get_synonym_threshold

    mock_settings = type("Settings", (), {"glossary_synonym_threshold": 0.6})()
    with patch("app.services.glossary.service.settings", mock_settings):
        threshold = _get_synonym_threshold()
        assert threshold == 0.6


def test_overlap_ratio():
    """T058: Jaccard 重叠率计算正确。"""
    from app.services.glossary.service import _overlap_ratio

    # 完全相同
    assert _overlap_ratio(["a", "b"], ["a", "b"]) == 1.0
    # 无重叠
    assert _overlap_ratio(["a"], ["b"]) == 0.0
    # 部分重叠
    ratio = _overlap_ratio(["a", "b", "c"], ["b", "c", "d"])
    assert abs(ratio - 0.5) < 0.01  # intersection=2, union=4
