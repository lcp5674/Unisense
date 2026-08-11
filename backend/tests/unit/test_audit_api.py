"""审计查询 API 单测（补齐覆盖率）。

针对 api/audit.py 的 52% 覆盖率补充。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.models.audit import AuditLog


@pytest.fixture
async def audit_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（平台管理员）。"""

    async def fake_db():
        session = MagicMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            AuditLog(
                id=1,
                actor_id=1,
                action="CREATE",
                entity_type="metric",
                entity_id="M1",
                ip="127.0.0.1",
                trace_id="t1",
                pii_access=False,
            )
        ]
        session.execute = AsyncMock(return_value=mock_result)
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="platform_admin"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_list_audit_logs(audit_client: httpx.AsyncClient) -> None:
    resp = await audit_client.get("/api/v1/audit")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["items"]) == 1
    assert data["items"][0]["entity_type"] == "metric"
    assert data["items"][0]["entity_id"] == "M1"


async def test_list_audit_logs_with_filters(audit_client: httpx.AsyncClient) -> None:
    resp = await audit_client.get(
        "/api/v1/audit",
        params={"actor_id": 1, "entity_type": "metric", "pii_access": "false"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["page"] == 1
    assert resp.json()["data"]["page_size"] == 20
