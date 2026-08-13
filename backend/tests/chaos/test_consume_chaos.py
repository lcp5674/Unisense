"""consume 服务混沌测试：OLAP 降级（503）、漏桶限流（429）、密钥校验（401）。

对齐 tests/chaos/test_governance_chaos.py：不依赖真实 MySQL / OLAP，通过内存依赖覆盖
与 repository / 密码校验 patch 走通真实 get_consume_client 鉴权路径。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import consume as consume_api
from app.api.deps import get_db_session
from app.core.error_codes import ErrorCode
from app.core.exceptions import BusinessError
from app.main import app
from app.models.consume import ApiClientStatus
from app.services.consume import service as consume_svc
from app.services.consume.repository import ApiClientRepo
from app.services.consume.schemas import QueryResponse

# 共享的内存接入方（X-Api-Key: cli_chaos:secret），QPS=1 用于触发限流。
_FAKE_CLIENT = MagicMock()
_FAKE_CLIENT.client_id = "cli_chaos"
_FAKE_CLIENT.scope_domain = "M1"
_FAKE_CLIENT.metric_whitelist = ["M1"]
_FAKE_CLIENT.qps = 1
_FAKE_CLIENT.daily_quota = 1000
_FAKE_CLIENT.status = ApiClientStatus.ACTIVE
_FAKE_CLIENT.client_secret_ref = "x"


class _BoomService(consume_svc.ConsumeService):
    """execute_query 恒失败的 service（模拟 OLAP 不可用）。"""

    def __init__(self, db, rate_limiter=None):  # noqa: ANN001
        super().__init__(db)

    async def execute_query(self, req, client):  # noqa: ANN001
        raise BusinessError("boom", error_code=ErrorCode.DEPENDENCY_DEGRADED_ENGINE)


def _patch_client_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """让真实 get_consume_client 在内存环境下完成鉴权 + 限流。"""

    async def _fake_get_by_client_id(cid):  # noqa: ANN001
        return _FAKE_CLIENT

    monkeypatch.setattr(ApiClientRepo, "get_by_client_id", staticmethod(_fake_get_by_client_id))

    async def _fake_verify_password(secret, ref):  # noqa: ANN001
        return secret == "secret"

    monkeypatch.setattr(consume_svc, "verify_password", _fake_verify_password)
    # 限流器经 get_rate_limiter() 获取（对齐 C6：运行期动态查询，非模块属性）
    from app.services.consume.rate_limiter import get_rate_limiter

    get_rate_limiter()._buckets.clear()


@pytest.fixture
async def api_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    """真实鉴权路径 + execute_query 成功（用于限流测试）。"""
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def fake_db():
        yield db

    app.dependency_overrides[get_db_session] = fake_db
    _patch_client_store(monkeypatch)

    async def fake_exec(req, cli):  # noqa: ANN001
        return QueryResponse(metric_code="M1", data={"value": 1})

    monkeypatch.setattr(consume_api.ConsumeService, "execute_query", staticmethod(fake_exec))

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
async def boom_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    """真实鉴权路径 + execute_query 恒失败（用于 503 降级测试）。"""
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def fake_db():
        yield db

    app.dependency_overrides[get_db_session] = fake_db
    _patch_client_store(monkeypatch)
    monkeypatch.setattr(consume_api, "ConsumeService", _BoomService)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_query_returns_503_on_olap_failure(boom_client: httpx.AsyncClient) -> None:
    """OLAP 不可用时查询必须降级 503，而非 200/500。"""
    resp = await boom_client.post(
        "/api/v1/consume/query",
        json={"metric_code": "M1", "date_range": "2024"},
        headers={"X-Api-Key": "cli_chaos:secret"},
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == ErrorCode.DEPENDENCY_DEGRADED_ENGINE.value


async def test_rate_limit_rejects_over_quota(api_client: httpx.AsyncClient) -> None:
    """接入方 QPS=1 时，超过限额的第二次请求必须被拒（429）。"""
    headers = {"X-Api-Key": "cli_chaos:secret"}
    body = {"metric_code": "M1", "date_range": "2024"}
    first = await api_client.post("/api/v1/consume/query", json=body, headers=headers)
    assert first.status_code == 200
    second = await api_client.post("/api/v1/consume/query", json=body, headers=headers)
    assert second.status_code == 429
    assert second.json()["code"] == ErrorCode.RATE_LIMITED.value


async def test_bad_secret_is_rejected(api_client: httpx.AsyncClient) -> None:
    """密钥校验失败必须拒绝（401）。"""
    resp = await api_client.post(
        "/api/v1/consume/query",
        json={"metric_code": "M1", "date_range": "2024"},
        headers={"X-Api-Key": "cli_chaos:wrong"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == ErrorCode.AUTH_APIKEY_INVALID.value
