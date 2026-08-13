"""SEC-03 XFF 信任链回归测试。"""

from unittest.mock import MagicMock

from app.core.audit import client_ip


def test_untrusted_proxy_xff_ignored():
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "192.168.1.1"
    req.headers = {"X-Forwarded-For": "1.2.3.4"}
    ip = client_ip(req)
    assert ip == "192.168.1.1"
