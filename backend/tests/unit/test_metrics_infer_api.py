"""指标描述推断 API 契约测试（FR-023 防重补齐）。

覆盖：首次并发（都还没有描述）时 in-flight 锁拒绝重复请求 → 409 LLM_INFER_IN_PROGRESS。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from tests.conftest import make_metric


@pytest.fixture
async def metrics_client() -> AsyncIterator[httpx.AsyncClient]:
    """覆盖 DB 会话与当前用户依赖（平台管理员，写端点放行）。"""

    async def fake_db() -> AsyncIterator[MagicMock]:
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="platform_admin", domain=None
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_infer_metric_description_inflight_conflict(
    metrics_client: httpx.AsyncClient,
) -> None:
    """FR-023: 指标推断进行中时，重复请求返回 409 LLM_INFER_IN_PROGRESS。

    关键场景：首次并发点击推断（都还没有描述、无法靠幂等短路拦截），
    必须有 in-flight 锁挡住第二个请求，避免双调 LLM。
    """
    mock_guard = MagicMock()
    mock_guard.acquire = AsyncMock(return_value=False)
    mock_guard.release = AsyncMock(return_value=True)
    with patch(
        "app.api.metrics.InferInflightGuard", return_value=mock_guard
    ):
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_daily/infer-description",
            params={"force": False},
        )
    assert resp.status_code == 409
    assert resp.json()["code"] == "LLM_INFER_IN_PROGRESS"
    # 锁以 (kind=metric, metric_code) 为键获取，owner 为随机值
    mock_guard.acquire.assert_awaited_once()
    args, kwargs = mock_guard.acquire.await_args
    assert args == ("metric", "sales_gmv_daily")
    assert "owner" in kwargs
    # 未获得锁 → 直接抛 409，不进入 try，release 不被调用
    mock_guard.release.assert_not_awaited()


async def test_get_metric_redacts_description_for_pii_non_sensitive(
    metrics_client: httpx.AsyncClient,
) -> None:
    """PII 合规：非敏感角色读取 PII 指标时，业务描述与口径同级脱敏。

    此前仅 definition_json 脱敏，业务描述（AI 生成可能引用敏感字段/口径上下文）
    原样返回——非敏感角色可绕过读分级看到敏感描述。修复后 description=None。
    """
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=2, role="viewer", domain=None
    )
    from app.services.semantic.schemas import MetricResponse

    pii_metric = MetricResponse(
        id=1,
        metric_code="sales_pii_daily",
        name="PII 测试",
        domain="sales",
        type="atomic",
        granularity="day",
        unit="元",
        currency=None,
        aggregation="SUM",
        time_semantics="PERIOD",
        freshness="T1",
        sla=None,
        dw_layer="DWS",
        metric_tier="T1",
        serving_mode="BATCH_ONLY",
        additivity="ADDITIVE",
        non_additive_dimensions=None,
        definition_json={"expression": "sum(mobile)"},
        version=1,
        row_version=1,
        status="PUBLISHED",
        owner_id=1,
        backup_owner_id=None,
        description="统计用户手机号与收货地址的敏感描述",
        pii_flag=True,
        compliance_reviewed=True,
        effective_version=1,
        consumption_guide=None,
        successor_code=None,
        deprecated_at=None,
        sunset_until=None,
        created_at="2026-08-01T00:00:00",
        updated_at="2026-08-01T00:00:00",
    )
    with patch(
        "app.api.metrics.MetricService.get_metric_public",
        new=AsyncMock(return_value=pii_metric),
    ) as mocked_get:
        resp = await metrics_client.get("/api/v1/metric-definitions/sales_pii_daily")
    assert resp.status_code == 200
    data = resp.json()["data"]
    # 口径脱敏保留键结构（叶子值 ***）
    assert data["definition_json"] == {"expression": "***"}
    # 业务描述与口径同级脱敏
    assert data["description"] is None
    mocked_get.assert_awaited_once()


async def test_update_metric_audit_records_governance_changed(
    metrics_client: httpx.AsyncClient,
) -> None:
    """治理字段变更时 UPDATE 审计 detail 记录 governance_changed（合规可追溯）。

    修复前：治理字段（数仓层/时效/分级等）编辑后审计只记 change_reason，无法追溯
    具体改了哪个治理字段、改成了什么——分层纠正/时效调整等治理动作在审计中不可见。
    """
    mock_svc = MagicMock()
    fake_metric = make_metric(metric_code="sales_gmv_daily")
    mock_svc.update_metric = AsyncMock(return_value=fake_metric)
    mock_audit = AsyncMock()
    with patch("app.api.metrics.MetricService", return_value=mock_svc), patch(
        "app.api.metrics.write_audit", mock_audit
    ), patch("app.api.metrics._register_metric_l3_lineage", AsyncMock(return_value=None)):
        resp = await metrics_client.put(
            "/api/v1/metric-definitions/sales_gmv_daily",
            json={
                "name": "新名称",
                "dw_layer": "DWS",
                "freshness": "T0",
                "change_reason": "治理字段调整：分层纠正+时效调整",
            },
        )
    assert resp.status_code == 200
    detail = mock_audit.call_args.kwargs["detail"]
    assert detail["governance_changed"] == {"dw_layer": "DWS", "freshness": "T0"}
    assert detail["change_reason"] == "治理字段调整：分层纠正+时效调整"


async def test_update_metric_audit_no_governance_when_unchanged(
    metrics_client: httpx.AsyncClient,
) -> None:
    """未提交治理字段时，审计 detail 不含 governance_changed（避免空记录噪音）。"""
    mock_svc = MagicMock()
    fake_metric = make_metric(metric_code="sales_gmv_daily")
    mock_svc.update_metric = AsyncMock(return_value=fake_metric)
    mock_audit = AsyncMock()
    with patch("app.api.metrics.MetricService", return_value=mock_svc), patch(
        "app.api.metrics.write_audit", mock_audit
    ), patch("app.api.metrics._register_metric_l3_lineage", AsyncMock(return_value=None)):
        resp = await metrics_client.put(
            "/api/v1/metric-definitions/sales_gmv_daily",
            json={"name": "新名称", "change_reason": "仅调整名称"},
        )
    assert resp.status_code == 200
    detail = mock_audit.call_args.kwargs["detail"]
    assert "governance_changed" not in detail

