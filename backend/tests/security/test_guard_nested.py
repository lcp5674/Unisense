"""SEC-05 嵌套深度拦截回归测试。"""
import pytest

from app.core.exceptions import BusinessError
from app.core.guard import _scan_deep


def test_deep_nesting_blocked():
    data = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": {"j": {"k": "evil"}}}}}}}}}}}
    with pytest.raises(BusinessError):
        _scan_deep(data)

def test_normal_nesting_passes():
    assert not _scan_deep({"name": "test"})
