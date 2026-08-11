"""observability 安全测试（对齐 gateways security_reverse，TD §13）。

feedback 写接口 WRITE_ROLES 含 viewer，故角色闸门对所有已认证角色开放；
安全边界体现在读端点的 SQL 注入守卫（纵深防御）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app


def _session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    s.flush = AsyncMock()
    s.refresh = MagicMock()
    s.execute = MagicMock()
    return s


async def _client(uid: int, role: str) -> AsyncIterator[httpx.AsyncClient]:
    session = _session()

    async def fake_db() -> AsyncIterator[MagicMock]:
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=uid, role=role, domain="sales"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def writer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(9, "metric_owner"):
        yield c


@pytest.fixture
async def viewer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_list_feedback_blocks_sql_injection_400(writer_client: httpx.AsyncClient) -> None:
    resp = await writer_client.get(
        "/api/v1/observability/feedback", params={"target_type": "' OR '1'='1"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


async def test_metrics_quality_blocks_sql_injection_400(writer_client: httpx.AsyncClient) -> None:
    resp = await writer_client.get(
        "/api/v1/observability/metrics/quality", params={"domain": "' OR '1'='1"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


async def test_feedback_metrics_blocks_unauthorized_role_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLAT-1: 不在 _READ_ROLES 中的角色被 RBAC 闸门拦截。"""

    async def fake_db():
        yield MagicMock()

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=99, role="guest", domain="sales"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/observability/metrics/quality")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_submit_feedback_ignores_client_user_id(
    writer_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PLAT-2: 反馈提交忽略 client 传入的 user_id=999，使用认证身份(9)。"""
    import app.services.observability.service as osvc

    captured: dict[str, int | None] = {}

    async def fake(self, data, actor_id=None):
        captured["actor_id"] = actor_id

        class _FB(dict):
            @property
            def id(self) -> int:
                return 1

        return _FB({"id": 1, "target_type": "metric"})

    monkeypatch.setattr(osvc.ObservabilityService, "submit_feedback", fake)
    resp = await writer_client.post(
        "/api/v1/observability/feedback",
        json={"user_id": 999, "target_type": "metric", "rating": 5},
    )
    assert resp.status_code == 201
    assert captured["actor_id"] == 9


async def test_submit_feedback_rejects_illegal_target_type_422(
    writer_client: httpx.AsyncClient,
) -> None:
    """observability M-1: 非法的 target_type 被拒绝。"""
    resp = await writer_client.post(
        "/api/v1/observability/feedback",
        json={"user_id": 9, "target_type": "evil", "rating": 5},
    )
    assert resp.status_code in (422, 400)
