"""Elasticsearch 客户端单测（对齐 TD §4.13 / §5.2 降级矩阵 + 消除 es_breaker 死代码缺口）。

验证：
- 可选依赖守卫：elasticsearch 包缺失或未配置 es_url 时客户端自动禁用，绝不因缺包崩溃。
- 检索经 es_breaker 保护：成功复位、失败熔断、熔断开启拒绝（CircuitOpenError）。
- health() 真实探活并经熔断器统计，使 es_breaker 进入降级矩阵调用路径。
- get_es_client 返回进程内单例（复用连接池）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core import es_client
from app.core.es_client import (
    CircuitOpenError,
    EsClient,
    SearchUnavailableError,
    get_es_client,
)


def test_es_client_disabled_when_package_missing(monkeypatch):
    monkeypatch.setattr(es_client, "_ESClientClass", None)
    monkeypatch.setattr(es_client.settings, "es_url", "http://localhost:9200")
    client = EsClient()
    assert client.enabled is False


def test_es_client_disabled_when_no_url(monkeypatch):
    class FakeES:
        def __init__(self, *a, **k): ...

    monkeypatch.setattr(es_client, "_ESClientClass", FakeES)
    monkeypatch.setattr(es_client.settings, "es_url", "")
    client = EsClient()
    assert client.enabled is False


async def test_es_client_search_success_uses_breaker(monkeypatch):
    class FakeES:
        async def search(self, index, **kwargs):
            return {"hits": []}

        async def ping(self):
            return None

        async def close(self):
            return None

    monkeypatch.setattr(es_client.settings, "es_url", "http://localhost:9200")
    fake = FakeES()
    breaker = es_client.CircuitBreaker(failure_threshold=3, reset_timeout=30.0)
    client = EsClient(client=fake, breaker=breaker)
    assert client.enabled is True
    resp = await client.search("metrics", {"query": {}})
    assert resp == {"hits": []}
    assert breaker.state == "closed"


async def test_es_client_search_circuit_open(monkeypatch):
    class FakeES:
        async def search(self, index, **kwargs):
            raise RuntimeError("boom")

        async def ping(self):
            return None

        async def close(self):
            return None

    monkeypatch.setattr(es_client.settings, "es_url", "http://localhost:9200")
    fake = FakeES()
    breaker = es_client.CircuitBreaker(failure_threshold=1, reset_timeout=30.0)
    client = EsClient(client=fake, breaker=breaker)
    # 第一次失败 -> 熔断开启
    with pytest.raises(SearchUnavailableError):
        await client.search("m", {})
    assert breaker.state == "open"
    # 再次请求 -> 熔断拒绝（CircuitOpenError）
    with pytest.raises(CircuitOpenError):
        await client.search("m", {})


async def test_es_client_health(monkeypatch):
    class FakeES:
        async def ping(self):
            return None

        async def search(self, *a, **k):
            return {}

        async def close(self):
            return None

    monkeypatch.setattr(es_client.settings, "es_url", "http://localhost:9200")
    client = EsClient(client=FakeES())
    assert await client.health() is True


def test_get_es_client_singleton():
    c1 = get_es_client()
    c2 = get_es_client()
    assert c1 is c2


async def test_es_client_search_uses_8x_named_params(monkeypatch):
    """elasticsearch-py 8.x 移除了 body= 参数：必须将查询体展开为命名参数（from->from_）。"""
    captured: dict[str, Any] = {}

    class FakeES:
        async def search(self, index, **kwargs):
            captured.update(kwargs)
            return {"hits": []}

        async def ping(self):
            return None

        async def close(self):
            return None

    monkeypatch.setattr(es_client.settings, "es_url", "http://localhost:9200")
    client = EsClient(client=FakeES())
    await client.search("metrics", {"query": {"term": {"a": 1}}, "from": 0, "size": 5})
    # 8.x 不再接受 body=，而是把查询体展开为命名参数；from -> from_
    assert "body" not in captured
    assert captured["query"] == {"term": {"a": 1}}
    assert captured["from_"] == 0
    assert captured["size"] == 5


async def test_es_client_passes_request_timeout(monkeypatch):
    """工业级容错：构造客户端时必须透传 request_timeout，避免慢/挂的 ES 阻塞调用方。"""
    captured: dict[str, Any] = {}

    class FakeES:
        def __init__(self, hosts: str, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def ping(self):
            return None

        async def search(self, **kwargs):
            return {}

        async def close(self):
            return None

    monkeypatch.setattr(es_client, "_ESClientClass", FakeES)
    monkeypatch.setattr(es_client.settings, "es_url", "http://localhost:9200")
    monkeypatch.setattr(es_client.settings, "es_request_timeout", 2.5)
    EsClient()
    assert captured.get("request_timeout") == 2.5
