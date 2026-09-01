"""notify API 层权限回归测试：广播收窄 + 用户自助放开（基线错位修复）。

- 广播 POST /notify/events：对齐前端 notifications:publish 基线（platform_admin/
  domain_admin），metric_owner 此前可绕过前端直调广播 API（_WRITE_ROLES 含之），现 403。
- 用户自助 POST /notify/notifications/{id}/read：任何登录用户可标记自己的通知已读
  （此前 _WRITE_ROLES 排除 reviewer/viewer/analyst/compliance_officer），viewer 现可 200。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app


def _make_user(role: str) -> MagicMock:
    from app.models.user import User

    u = MagicMock(spec=User, id=9, username=f"u_{role}")
    u.role = role
    u.roles_all.return_value = [role]
    u.has_role.side_effect = lambda r: r == role
    return u


def _client(user: MagicMock) -> httpx.AsyncClient:
    async def fake_db():
        s = MagicMock()
        s.commit = AsyncMock()
        yield s

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_broadcast_denied_for_metric_owner() -> None:
    """metric_owner 无 notifications:publish：广播被拒（403），不再可绕过前端直调。"""
    async with _client(_make_user("metric_owner")) as c:
        resp = await c.post(
            "/api/v1/notify/events",
            json={"event_type": "metric_created", "source": "metric", "payload": {"code": "x"}},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 403


async def test_broadcast_allowed_for_domain_admin() -> None:
    """domain_admin 有 notifications:publish：广播放行（201）。"""

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.api.notify.NotifyService",
            lambda db: MagicMock(
                publish_event=AsyncMock(
                    return_value={"event_id": 1, "event_type": "metric_created"}
                )
            ),
        )
        async with _client(_make_user("domain_admin")) as c:
            resp = await c.post(
                "/api/v1/notify/events",
                json={"event_type": "metric_created", "source": "metric", "payload": {"code": "x"}},
            )
    app.dependency_overrides.clear()
    assert resp.status_code == 201


async def test_mark_read_allowed_for_viewer() -> None:
    """viewer 无写角色但可标记自己的通知已读（用户自助，不再 403）。"""
    from types import SimpleNamespace


    read_row = SimpleNamespace(
        id=5, subscriber_id=9, channel="inapp", title="通知", body=None, status="READ",
        template_code=None, ref_type=None, ref_id=None, sent_at=None, payload=None,
        send_at=None, read_at=None, created_at=None, actor_id=None, actor_name=None,
        last_error=None, handled_at=None,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.api.notify.NotifyService",
            lambda db: MagicMock(mark_read=AsyncMock(return_value=read_row)),
        )
        async with _client(_make_user("viewer")) as c:
            resp = await c.post("/api/v1/notify/notifications/5/read")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
