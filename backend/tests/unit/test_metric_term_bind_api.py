"""指标↔术语绑定 API 契约测试（P2-11 术语绑定写路径）。

覆盖：
- 管理/Owner 绑定术语 → 200 返回指标（term_id 写入）
- 解绑（term_id=null）→ 200
- 术语不存在 → 404 NOT_FOUND
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport
from tests.conftest import make_metric

from app.api import deps
from app.core.exceptions import NotFoundError
from app.main import app


@pytest.fixture
async def metrics_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（platform_admin，写端点放行）。"""

    async def fake_db() -> AsyncIterator[MagicMock]:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        domain=None,
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
        domains_all=lambda: None,
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_bind_metric_term_success(metrics_client: httpx.AsyncClient) -> None:
    """P2-11: 绑定术语 → 200，term_id 写入。"""
    bound = make_metric(term_id=55)
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.bind_metric_term = AsyncMock(return_value=bound)
        resp = await metrics_client.put(
            "/api/v1/metric-definitions/sales_gmv_daily/term",
            json={"term_id": 55},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["term_id"] == 55
    mock_svc.return_value.bind_metric_term.assert_awaited_once_with(
        "sales_gmv_daily", 55, actor_id=1, role="platform_admin", user_domains=None,
        term_ids=None,
    )


async def test_unbind_metric_term(metrics_client: httpx.AsyncClient) -> None:
    """P2-11: term_id=null 解绑 → 200。"""
    unbound = make_metric(term_id=None)
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.bind_metric_term = AsyncMock(return_value=unbound)
        resp = await metrics_client.put(
            "/api/v1/metric-definitions/sales_gmv_daily/term",
            json={"term_id": None},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["term_id"] is None


async def test_bind_metric_term_not_found(metrics_client: httpx.AsyncClient) -> None:
    """P2-11: 术语不存在 → 404 NOT_FOUND。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.bind_metric_term = AsyncMock(
            side_effect=NotFoundError("术语不存在: 999")
        )
        resp = await metrics_client.put(
            "/api/v1/metric-definitions/sales_gmv_daily/term",
            json={"term_id": 999},
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
