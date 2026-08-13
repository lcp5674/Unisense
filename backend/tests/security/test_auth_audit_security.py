"""SEC-03~05 认证审计安全回归测试。"""
from unittest.mock import MagicMock

import pytest

from app.core.audit import client_ip
from app.core.exceptions import BusinessError
from app.core.guard import _is_suspicious, _scan_deep


def test_xff_ignored_from_untrusted_proxy():
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "10.0.0.1"
    req.headers = {"X-Forwarded-For": "1.2.3.4"}
    ip = client_ip(req)
    assert ip == "10.0.0.1"

def test_xff_used_from_trusted_proxy():
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "10.0.0.1"
    req.headers = {"X-Forwarded-For": "1.2.3.4, 10.0.0.1"}
    ip = client_ip(req)
    assert isinstance(ip, str)

def test_date_not_blocked_by_guard():
    assert not _is_suspicious("2024-01-01")
    assert not _is_suspicious("WHERE date = '2024-01-15'")
    assert not _is_suspicious("2024-12-31 23:59:59")

def test_sql_comment_still_blocked():
    assert _is_suspicious("1 -- drop table")
    assert _is_suspicious("'; -- ")

def test_deep_nested_payload_blocked():
    deep: dict = {"k": "'; DROP TABLE--"}
    for _ in range(10):
        deep = {"a": deep}
    with pytest.raises(BusinessError):
        _scan_deep(deep)

def test_normal_nesting_passes():
    normal = {"name": "test", "props": {"value": 42}}
    assert not _scan_deep(normal)
