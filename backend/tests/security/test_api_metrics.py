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
        instance.approve_metric = AsyncMock(return_value=metric)

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
        session = AsyncMock()
        session.add = MagicMock()
        yield session

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
        instance.review_compliance.assert_awaited_once_with(
            "sales_gmv_daily", actor_id=99, role="domain_admin"
        )
    finally:
        app.dependency_overrides.pop(deps.get_current_user, None)


async def test_get_metric_versions_success(client):
    """版本历史接口：调用 service.get_version_responses_with_meta（返回指标实体
    供 PII 分级脱敏），并对非 PII 指标不做脱敏处理。"""
    from app.models.metric import MetricVersion

    metric = make_metric(pii_flag=False)
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
        instance.get_version_responses_with_meta = AsyncMock(
            return_value=(metric, [version])
        )

        resp = await client.get("/api/v1/metric-definitions/sales_gmv_daily/versions")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "OK"
    instance.get_version_responses_with_meta.assert_awaited_once_with(
        "sales_gmv_daily", actor_id=1, role="metric_owner"
    )
    assert body["data"][0]["version"] == 1


async def test_metric_write_endpoints_commit():
    """回归（H-1）：指标全部 5 个写端点必须真实 ``await db.commit()``。

    原缺陷：写端点只 flush 不 commit，事务随 ``get_db_session`` 关闭被回滚，
    生产环境所有指标写入静默丢失。用可感知 await 的 AsyncMock 会话捕获 commit。
    """
    session = AsyncMock()
    session.add = MagicMock()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="domain_admin")
    metric = make_metric()
    try:
        with patch("app.api.metrics.MetricService") as mock_svc:
            instance = mock_svc.return_value
            instance.create_metric = AsyncMock(return_value=metric)
            instance.update_metric = AsyncMock(return_value=metric)
            instance.approve_metric = AsyncMock(return_value=metric)
            instance.deprecate_metric = AsyncMock(return_value=metric)
            instance.review_compliance = AsyncMock(return_value=metric)
            # 写端点 commit 后触发血缘后置任务（lineage_post_commit），mock 须可 await
            instance.run_lineage_post_commit = AsyncMock()

            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                responses = [
                    await c.post("/api/v1/metric-definitions", json=make_create_payload()),
                    await c.put(
                        "/api/v1/metric-definitions/sales_gmv_daily",
                        json={"change_reason": "修正口径说明"},
                    ),
                    await c.post(
                        "/api/v1/metric-definitions/sales_gmv_daily/publish",
                        json={"version": 1, "change_reason": "首次发布"},
                    ),
                    await c.post(
                        "/api/v1/metric-definitions/sales_gmv_daily/deprecate",
                        json={"successor_code": "bar"},
                    ),
                    await c.post("/api/v1/metric-definitions/sales_gmv_daily/pii-review"),
                ]
        for resp in responses:
            assert resp.status_code < 300, resp.text
        assert session.commit.await_count >= 5, "指标写端点未全部提交事务"
    finally:
        app.dependency_overrides.clear()
