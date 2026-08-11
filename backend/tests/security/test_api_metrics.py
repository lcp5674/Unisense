"""指标 API 集成测试（ASGI，覆盖统一信封与错误格式）。

通过覆盖 DB/用户依赖并 mock MetricService，验证 HTTP 层与统一响应结构。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport
from tests.conftest import make_create_payload, make_metric

from app.api import deps
from app.core.exceptions import ConflictError
from app.main import app


async def test_create_metric_success(client):
    metric = make_metric()
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.create_metric = AsyncMock(return_value=metric)

        resp = await client.post("/api/v1/metric-definitions", json=make_create_payload())

    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "OK"
    assert body["data"]["metric_code"] == "sales_gmv_daily"
    assert body["trace_id"]


async def test_create_metric_conflict_returns_409(client):
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.create_metric = AsyncMock(
            side_effect=ConflictError("指标编码已存在: sales_gmv_daily")
        )

        resp = await client.post("/api/v1/metric-definitions", json=make_create_payload())

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "CONFLICT"
    assert body["trace_id"]


async def test_publish_metric_success(client):
    metric = make_metric(status="PUBLISHED")
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.publish_metric = AsyncMock(return_value=metric)

        resp = await client.post(
            "/api/v1/metric-definitions/sales_gmv_daily/publish",
            json={"version": 1, "change_reason": "首次发布"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"] == "PUBLISHED"


async def test_list_metrics_success(client):
    metric = make_metric()
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.list_metrics = AsyncMock(return_value=([metric], 1))

        resp = await client.get("/api/v1/metric-definitions?page=1&page_size=20")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["metric_code"] == "sales_gmv_daily"


@pytest.fixture
async def viewer_client():
    """以 viewer 角色注入的客户端（应被写操作 RBAC 拦截）。"""

    async def fake_db():
        yield MagicMock()

    def fake_user():
        return MagicMock(id=2, role="viewer")

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = fake_user
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_create_metric_forbidden_for_viewer(viewer_client):
    resp = await viewer_client.post(
        "/api/v1/metric-definitions",
        json=make_create_payload(),
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "FORBIDDEN"
    assert "platform_admin" in body["message"]


async def test_pii_review_forbidden_for_metric_owner(client):
    # 默认 client 角色 = metric_owner，须被 _PII_REVIEW_ROLES 拒绝（禁 Owner 自审，对齐 COMPL-2）
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.review_compliance = AsyncMock(return_value=make_metric(pii_flag=True))

        resp = await client.post("/api/v1/metric-definitions/sales_gmv_daily/pii-review")

    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_pii_review_succeeds_for_domain_admin(client):
    # 合规/域管理员可执行 PII 复核
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=99, role="domain_admin")
    metric = make_metric(pii_flag=True, compliance_reviewed=True)
    try:
        with patch("app.api.metrics.MetricService") as mock_svc:
            instance = mock_svc.return_value
            instance.review_compliance = AsyncMock(return_value=metric)

            resp = await client.post("/api/v1/metric-definitions/sales_gmv_daily/pii-review")

        assert resp.status_code == 200
        assert resp.json()["data"]["compliance_reviewed"] is True
        instance.review_compliance.assert_awaited_once_with("sales_gmv_daily", actor_id=99)
    finally:
        app.dependency_overrides.pop(deps.get_current_user, None)


async def test_get_metric_versions_success(client):
    """版本历史接口：必须调用 service.get_versions（锁定此前误写为
    get_metric_versions 的运行时 500 缺陷）。"""
    from app.models.metric import MetricVersion

    version = MetricVersion(
        id=1,
        metric_id=1,
        version=1,
        change_type="CREATE",
        definition_json={},
        diff_json=None,
        status="DRAFT",
        change_reason="初始创建",
        created_by=1,
        published_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.get_versions = AsyncMock(return_value=[version])

        resp = await client.get("/api/v1/metric-definitions/sales_gmv_daily/versions")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "OK"
    instance.get_versions.assert_awaited_once_with("sales_gmv_daily")
    assert body["data"][0]["version"] == 1
