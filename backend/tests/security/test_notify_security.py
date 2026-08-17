"""notify 安全测试（对齐 gateways security_reverse，TD §13）。"""

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


_EVENT_BODY = {
    "event_type": "metric.anomaly",
    "source": "quality",
    "payload": {"metric_id": "M1"},
    "level": "INFO",
}


@pytest.fixture
async def writer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(9, "metric_owner"):
        yield c


@pytest.fixture
async def viewer_client() -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(11, "viewer"):
        yield c


async def test_publish_event_requires_write_role_403(viewer_client: httpx.AsyncClient) -> None:
    """普通读者无权发布事件。"""
    resp = await viewer_client.post("/api/v1/notify/events", json=_EVENT_BODY)
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_list_notifications_blocks_sql_injection_400(
    writer_client: httpx.AsyncClient,
) -> None:
    resp = await writer_client.get("/api/v1/notify/notifications", params={"search": "' OR '1'='1"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


async def test_list_events_blocks_sql_injection_400(writer_client: httpx.AsyncClient) -> None:
    resp = await writer_client.get("/api/v1/notify/events", params={"status": "' OR '1'='1"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"


async def test_list_notifications_ignores_client_subscriber_id(
    writer_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PLAT-2: 即使 client 传入 subscriber_id=999，服务端也只查当前认证用户(9)。"""
    captured: dict[str, int] = {}

    # 端点当前调用 list_notifications_page（分页版，返回 (notifs, total)）；
    # 此前 patch 了 list_notifications 导致真实分页方法撞 MagicMock 会话报 500
    async def fake_list_page(self, subscriber_id: int, *args, **kwargs):
        captured["subscriber_id"] = subscriber_id
        return [], 0

    monkeypatch.setattr(
        __import__("app.services.notify.service", fromlist=["NotifyService"]).NotifyService,
        "list_notifications_page",
        fake_list_page,
    )
    resp = await writer_client.get("/api/v1/notify/notifications", params={"subscriber_id": 999})
    assert resp.status_code == 200
    assert captured["subscriber_id"] == 9


async def test_upsert_subscription_ignores_client_user_id(
    writer_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PLAT-2: 订阅绑定时忽略 client 传入的 user_id，使用认证身份(9)。"""
    captured: dict[str, int] = {}

    async def fake_upsert(self, data, actor_id=None):
        captured["user_id"] = actor_id
        # 返回带 id 的 SubscriptionPref 等价对象，供 SubscriptionResponse.from_model 序列化
        return MagicMock(
            id=1,
            user_id=actor_id,
            channel=data.channel,
            event_type=data.event_type,
            enabled=data.enabled,
            threshold=data.threshold,
        )

    monkeypatch.setattr(
        __import__("app.services.notify.service", fromlist=["NotifyService"]).NotifyService,
        "upsert_subscription",
        fake_upsert,
    )
    resp = await writer_client.put(
        "/api/v1/notify/subscriptions",
        json={"user_id": 999, "channel": "email", "event_type": "metric.anomaly"},
    )
    assert resp.status_code == 200
    assert captured["user_id"] == 9


async def test_publish_event_rejects_illegal_source_422(
    writer_client: httpx.AsyncClient,
) -> None:
    """PLAT-5: 非法的 source/level 被拒绝。"""
    resp = await writer_client.post(
        "/api/v1/notify/events",
        json={"event_type": "x", "source": "evil_hack", "level": "INFO"},
    )
    assert resp.status_code in (422, 400)


async def test_publish_event_rejects_illegal_level_422(
    writer_client: httpx.AsyncClient,
) -> None:
    """PLAT-5: 非法的 level 被拒绝。"""
    resp = await writer_client.post(
        "/api/v1/notify/events",
        json={"event_type": "x", "source": "quality", "level": "SUPREME"},
    )
    assert resp.status_code in (422, 400)
