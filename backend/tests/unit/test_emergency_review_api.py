"""紧急发布补审 API 契约测试（FR-022 闭环，P1-6）。

覆盖：
- 管理角色补审 → 200，返回指标
- 非管理角色（metric_owner）→ 403
- 非紧急发布指标 → 409 NOT_EMERGENCY_PUBLISHED
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport
from tests.conftest import make_metric

from app.api import deps
from app.core.exceptions import ConflictError
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
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


def _metric(code: str = "sales_gmv_d"):
    return make_metric(
        metric_code=code,
        status="PUBLISHED",
        emergency_publish=True,
        emergency_reason="生产系统故障",
    )


async def test_complete_emergency_review_success(metrics_client: httpx.AsyncClient) -> None:
    """管理角色补审 → 200 并返回指标。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.complete_emergency_review = AsyncMock(return_value=_metric())
        resp = await metrics_client.post("/api/v1/metric-definitions/sales_gmv_d/emergency-review")

    assert resp.status_code == 200
    assert resp.json()["data"]["metric_code"] == "sales_gmv_d"
    mock_svc.return_value.complete_emergency_review.assert_awaited_once_with(
        "sales_gmv_d", actor_id=1, role="platform_admin"
    )


async def test_complete_emergency_review_non_admin_forbidden(
    metrics_client: httpx.AsyncClient,
) -> None:
    """metric_owner 补审 → 403（端点角色门禁）。"""
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=2,
        role="metric_owner",
        domain="sales",
        roles_all=lambda: ["metric_owner"],
        has_role=lambda r: r == "metric_owner",
    )
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.complete_emergency_review = AsyncMock(return_value=_metric())
        resp = await metrics_client.post("/api/v1/metric-definitions/sales_gmv_d/emergency-review")

    assert resp.status_code == 403


async def test_complete_emergency_review_not_emergency(
    metrics_client: httpx.AsyncClient,
) -> None:
    """非紧急发布指标 → 409 NOT_EMERGENCY_PUBLISHED。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.complete_emergency_review = AsyncMock(
            side_effect=ConflictError(
                "该指标非紧急发布，无需补审", error_code="NOT_EMERGENCY_PUBLISHED"
            )
        )
        resp = await metrics_client.post("/api/v1/metric-definitions/sales_gmv_d/emergency-review")

    assert resp.status_code == 409
    assert resp.json()["code"] == "NOT_EMERGENCY_PUBLISHED"
