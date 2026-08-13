"""采集 API 端到端单测（对齐 P1-4 drift-logs / P1-7 调度与立即采集分离）。

仅覆盖 http 路由层行为，DB/Redis 以依赖覆盖 + mock 注入，无外部依赖。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app


@pytest.fixture
async def collector_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（平台管理员），返回 httpx 异步客户端。"""

    async def fake_db() -> AsyncIterator[MagicMock]:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.flush = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="platform_admin"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_schedule_only_persists_config_does_not_collect(
    collector_client: httpx.AsyncClient,
) -> None:
    """P1-7: /schedule 只保存调度配置，不立即入队采集（schedule_collection 不被调用）。"""
    with patch(
        "app.api.collector.CollectorService.schedule_collection", new_callable=AsyncMock
    ) as mock_schedule:
        resp = await collector_client.post(
            "/api/v1/data-sources/s1/schedule",
            json={"cron": "0 0 * * *", "mode": "FULL"},
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["scheduled"] is True
    assert body["cron"] == "0 0 * * *"
    assert body["mode"] == "FULL"
    # 定时调度不应触发立即采集
    mock_schedule.assert_not_called()


async def test_collect_now_triggers_immediate_collection(
    collector_client: httpx.AsyncClient,
) -> None:
    """P1-7: /collect-now 立即入队采集，返回 job_id 且 schedule_collection 被调用。"""
    with patch(
        "app.api.collector.CollectorService.schedule_collection",
        new_callable=AsyncMock,
        return_value="job-immediate-1",
    ) as mock_schedule:
        resp = await collector_client.post(
            "/api/v1/data-sources/s1/collect-now",
            json={"mode": "FULL"},
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["job_id"] == "job-immediate-1"
    assert body["status"] == "QUEUED"
    assert body["mode"] == "FULL"
    mock_schedule.assert_awaited_once_with("s1", 1)


async def test_drift_logs_endpoint_returns_paged(
    collector_client: httpx.AsyncClient,
) -> None:
    """P1-4: GET /{source_id}/drift-logs 返回分页 drift 记录。"""
    from datetime import UTC, datetime

    fake_log = MagicMock(
        source_id="s1",
        entity_name="users",
        change_type="ADD_COLUMN",
        before_signature=None,
        after_signature="sig2",
        before_schema=None,
        after_schema={"columns": [{"name": "age", "type": "int"}]},
        diff_json={"added": ["age"], "removed": [], "changed": []},
        detected_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    with patch(
        "app.api.collector.CollectorService.list_drift_logs",
        new_callable=AsyncMock,
        return_value={
            "items": [
                {
                    "source_id": "s1",
                    "entity_name": "users",
                    "change_type": "ADD_COLUMN",
                    "before_signature": None,
                    "after_signature": "sig2",
                    "before_schema": None,
                    "after_schema": {"columns": [{"name": "age", "type": "int"}]},
                    "diff_json": {"added": ["age"], "removed": [], "changed": []},
                    "detected_at": "2026-02-01T00:00:00+00:00",
                }
            ],
            "total": 1,
            "page": 1,
            "page_size": 20,
        },
    ):
        resp = await collector_client.get("/api/v1/data-sources/s1/drift-logs")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["entity_name"] == "users"
    assert body["items"][0]["change_type"] == "ADD_COLUMN"
