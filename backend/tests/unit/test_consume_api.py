"""consume API 层契约测试：/consume/me/favorites 的 response_model 与 service 返回一致。

修复背景：多资产收藏重构（5efce3a）后 service.list_favorites 返回
list[dict]（{asset_type, asset_id}），但 API 层 response_model 漏改仍声明
list[str]，导致 GET /consume/me/favorites 恒 500（pydantic response 校验失败）。
本测试固化 API 层契约，防止回归。

双通道回归：/consume/query 与 /consume/query/dry-run 支持内部登录用户
（用户 JWT 通道，无需 consume 令牌）——查询工作台「指标查询」入口依赖此能力。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.api.consume import get_consume_or_internal_user
from app.main import app
from app.services.consume.schemas import DryRunResponse, QueryResponse


@pytest.fixture
async def favorites_client() -> AsyncIterator[httpx.AsyncClient]:
    async def fake_db():
        yield MagicMock()

    svc = MagicMock()
    svc.list_favorites = AsyncMock(
        return_value=[
            {"asset_type": "METRIC", "asset_id": "sales_gmv_day"},
            {"asset_type": "TABLE", "asset_id": "dw.sales"},
        ]
    )

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="metric_owner",
        roles_all=lambda: ["metric_owner"],
        has_role=lambda r: r == "metric_owner",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        with patch("app.api.consume.ConsumeService", return_value=svc):
            yield c
    app.dependency_overrides.clear()


async def test_get_favorites_returns_object_list(favorites_client: httpx.AsyncClient) -> None:
    """GET /consume/me/favorites 返回 {asset_type, asset_id} 对象列表（非 list[str]，修复 500）。"""
    resp = await favorites_client.get("/api/v1/consume/me/favorites")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == [
        {"asset_type": "METRIC", "asset_id": "sales_gmv_day"},
        {"asset_type": "TABLE", "asset_id": "dw.sales"},
    ]


@pytest.fixture
async def internal_query_client() -> AsyncIterator[httpx.AsyncClient]:
    """内部用户双通道：override get_consume_or_internal_user 返回 User（无 consume 令牌）。"""

    async def fake_db():
        session = MagicMock()
        session.commit = AsyncMock()
        yield session

    svc = MagicMock()
    svc.execute_query = AsyncMock(
        return_value=QueryResponse(metric_code="outp_visit_day", data={"rows": [], "total": 0})
    )
    svc.dry_run_query = AsyncMock(
        return_value=DryRunResponse(
            metric_code="outp_visit_day",
            status="ok",
            checks=[],
            execution_plan={},
            meta={},
        )
    )
    svc.record_query_log = AsyncMock()

    user = MagicMock(id=7, username="internal_analyst", roles_all=lambda: ["analyst"])

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[get_consume_or_internal_user] = lambda: user
    app.dependency_overrides[deps.get_current_user] = lambda: user
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        with patch("app.api.consume.ConsumeService", return_value=svc):
            yield c
    app.dependency_overrides.clear()


async def test_internal_user_query_via_dual_channel(
    internal_query_client: httpx.AsyncClient,
) -> None:
    """内部用户经 /consume/query 走用户 JWT 通道成功（无需 consume 令牌）。"""
    resp = await internal_query_client.post(
        "/api/v1/consume/query",
        json={"metric_code": "outp_visit_day", "date_range": "2026-08"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["metric_code"] == "outp_visit_day"


async def test_internal_user_dry_run_via_dual_channel(
    internal_query_client: httpx.AsyncClient,
) -> None:
    """内部用户经 /consume/query/dry-run 走用户 JWT 通道成功（口径校验，不执行）。"""
    resp = await internal_query_client.post(
        "/api/v1/consume/query/dry-run",
        json={"metric_code": "outp_visit_day", "date_range": "2026-08"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "ok"
