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


async def test_es_client_constructor_failure_disables(monkeypatch):
    """构造客户端失败（如 URL 非法/认证异常）→ 客户端禁用而非崩溃（90-91 分支）。"""

    class BoomES:
        def __init__(self, *a, **k):
            raise RuntimeError("init boom")

    monkeypatch.setattr(es_client, "_ESClientClass", BoomES)
    monkeypatch.setattr(es_client.settings, "es_url", "http://localhost:9200")
    client = EsClient()
    assert client.enabled is False


async def test_es_client_search_when_disabled():
    """客户端禁用时 search 抛 SearchUnavailableError（106 分支）。"""
    client = EsClient()  # 无 client 注入且未配置 → 禁用
    assert client.enabled is False
    with pytest.raises(SearchUnavailableError):
        await client.search("metrics", {})


async def test_es_client_index_success(monkeypatch):
    """index 文档成功 → 返回响应并复位熔断（129-136 分支）。"""
    captured: dict[str, Any] = {}

    class FakeES:
        async def index(self, index, document, id=None):  # noqa: A002 - 透传 es_client id= 命名参数
            captured.update(index=index, document=document, id=id)
            return {"result": "created"}

        async def close(self):
            return None

    fake = FakeES()
    breaker = es_client.CircuitBreaker(failure_threshold=3, reset_timeout=30.0)
    client = EsClient(client=fake, breaker=breaker)
    resp = await client.index("metrics", {"name": "gmv"}, doc_id="m1")
    assert resp == {"result": "created"}
    assert captured["index"] == "metrics"
    assert captured["document"] == {"name": "gmv"}
    assert captured["id"] == "m1"
    assert breaker.state == "closed"


async def test_es_client_index_failure_opens_circuit():
    """index 失败 → SearchUnavailableError + 熔断计数（137-139 分支）。"""

    class BoomES:
        async def index(self, index, document, id=None):  # noqa: A002 - 透传 es_client id= 命名参数
            raise RuntimeError("index boom")

        async def close(self):
            return None

    breaker = es_client.CircuitBreaker(failure_threshold=1, reset_timeout=30.0)
    client = EsClient(client=BoomES(), breaker=breaker)
    with pytest.raises(SearchUnavailableError):
        await client.index("m", {})
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        await client.index("m", {})


async def test_es_client_health_when_disabled():
    """客户端禁用时 health() 返回 False（144 分支）。"""
    client = EsClient()
    assert await client.health() is False


async def test_es_client_health_circuit_open():
    """熔断开启时 health() 返回 False（146 分支）。"""

    class FakeES:
        async def ping(self):
            return None

        async def close(self):
            return None

    breaker = es_client.CircuitBreaker(failure_threshold=1, reset_timeout=30.0)
    breaker._open = True  # 强制熔断打开
    client = EsClient(client=FakeES(), breaker=breaker)
    assert await client.health() is False


async def test_es_client_health_exception_records_failure():
    """ping 抛异常 → health() False + 熔断计数（151-153 分支）。"""

    class BoomES:
        async def ping(self):
            raise RuntimeError("ping boom")

        async def close(self):
            return None

    breaker = es_client.CircuitBreaker(failure_threshold=1, reset_timeout=30.0)
    client = EsClient(client=BoomES(), breaker=breaker)
    assert await client.health() is False
    assert breaker.state == "open"


async def test_es_client_close_exception_best_effort():
    """close 抛异常 → best-effort 不向外抛（157-161 分支）。"""

    class BoomES:
        async def close(self):
            raise RuntimeError("close boom")

    client = EsClient(client=BoomES())
    await client.close()  # 不应抛异常


async def test_es_client_index_when_disabled():
    """客户端禁用时 index 抛 SearchUnavailableError（130 行分支）。"""
    client = EsClient()
    with pytest.raises(SearchUnavailableError):
        await client.index("metrics", {"name": "gmv"})


async def test_es_client_import_error_guard(monkeypatch):
    """elasticsearch 包缺失时模块守卫生效：_ESClientClass 回退 None（30 行分支）。

    通过 monkeypatch builtins.__import__ 模拟 import 失败后 reload 模块验证守卫。
    注意：本测试置于文件末尾——reload 会重建模块内的类对象，避免影响其余用例。
    """
    import builtins
    import importlib

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "elasticsearch":
            raise ImportError("No module named 'elasticsearch'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # reload 重新执行模块代码（含 try/except import），fake_import 使 elasticsearch 导入失败
    reloaded = importlib.reload(es_client)
    assert reloaded._ESClientClass is None

    # 阶段2：包存在可导入 → _ESClientClass 被正确赋值（覆盖 try 内赋值分支）
    class FakeESModule:
        AsyncElasticsearch = object

    def fake_import_present(name: str, *args, **kwargs):
        if name == "elasticsearch":
            return FakeESModule
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import_present)
    reloaded = importlib.reload(es_client)
    assert reloaded._ESClientClass is FakeESModule.AsyncElasticsearch

    # 阶段3：恢复真实 import 后 reload：不应抛异常，模块仍可用（包是否安装由环境决定）
    monkeypatch.setattr(builtins, "__import__", real_import)
    importlib.reload(es_client)
    assert hasattr(es_client, "EsClient")
