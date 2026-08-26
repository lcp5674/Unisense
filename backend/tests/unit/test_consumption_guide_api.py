"""消费指南 API 契约测试（US11：GET 读 + PUT 人工维护写路径）。

覆盖：
- GET /semantics/consumption-guide/{code} → 200（读路径正常）
- PUT /semantics/consumption-guide/{code} → 200（写路径 + 审计 + commit）
- PUT 指标不存在 → 404
- PUT 结构校验失败（超长列表项）→ 422
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


async def test_get_consumption_guide_200(metrics_client: httpx.AsyncClient) -> None:
    """GET 消费指南 → 200。"""
    guide = {
        "metric_code": "sales_gmv_daily",
        "guide_source": "auto",
        "recommended_usage": ["适用 sales 域 daily 粒度分析"],
        "cautions": [],
        "related_metrics": [],
    }
    with patch("app.api.semantic.MetricService") as mock_svc:
        mock_svc.return_value.get_consumption_guide = AsyncMock(return_value=guide)
        resp = await metrics_client.get(
            "/api/v1/semantics/consumption-guide/sales_gmv_daily"
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["guide_source"] == "auto"


async def test_put_consumption_guide_200(metrics_client: httpx.AsyncClient) -> None:
    """PUT 消费指南 → 200，写路径 + 审计 + commit。"""
    result = {
        "metric_code": "sales_gmv_daily",
        "recommended_usage": ["按日分析"],
        "cautions": ["含退款前"],
        "related_metrics": ["sales_uv_daily"],
        "guide_source": "manual",
        "guide_updated_at": "2026-08-26T00:00:00+00:00",
    }
    with patch("app.api.semantic.MetricService") as mock_svc:
        mock_svc.return_value.update_consumption_guide = AsyncMock(return_value=result)
        resp = await metrics_client.put(
            "/api/v1/semantics/consumption-guide/sales_gmv_daily",
            json={
                "recommended_usage": ["按日分析"],
                "cautions": ["含退款前"],
                "related_metrics": ["sales_uv_daily"],
                "row_version": 3,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["guide_source"] == "manual"
    mock_svc.return_value.update_consumption_guide.assert_awaited_once()
    args, kwargs = mock_svc.return_value.update_consumption_guide.call_args
    assert kwargs["actor_id"] == 1
    assert kwargs["role"] == "platform_admin"


async def test_put_consumption_guide_404(metrics_client: httpx.AsyncClient) -> None:
    """PUT 指标不存在 → 404。"""
    from app.core.exceptions import NotFoundError

    with patch("app.api.semantic.MetricService") as mock_svc:
        mock_svc.return_value.update_consumption_guide = AsyncMock(
            side_effect=NotFoundError("指标不存在")
        )
        resp = await metrics_client.put(
            "/api/v1/semantics/consumption-guide/nonexistent",
            json={"recommended_usage": ["x"]},
        )
    assert resp.status_code == 404


async def test_put_consumption_guide_422_oversize(
    metrics_client: httpx.AsyncClient,
) -> None:
    """PUT 超长列表项 → 422（结构校验在 schema 层拦截）。"""
    with patch("app.api.semantic.MetricService") as mock_svc:
        resp = await metrics_client.put(
            "/api/v1/semantics/consumption-guide/sales_gmv_daily",
            json={"recommended_usage": ["x" * 201]},
        )
    assert resp.status_code == 422
    mock_svc.return_value.update_consumption_guide.assert_not_called()


async def test_put_consumption_guide_requires_write_role(
    metrics_client: httpx.AsyncClient,
) -> None:
    """写端点角色门禁：非写角色（viewer）→ 403。"""
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=2,
        role="viewer",
        domain=None,
        roles_all=lambda: ["viewer"],
        has_role=lambda r: r == "viewer",
    )
    try:
        resp = await metrics_client.put(
            "/api/v1/semantics/consumption-guide/sales_gmv_daily",
            json={"recommended_usage": ["x"]},
        )
    finally:
        app.dependency_overrides.pop(deps.get_current_user, None)
    assert resp.status_code == 403
