"""指标批量治理端点测试（ASGI：batch-submit/approve/reject/deprecate）。

覆盖逐条收集结果（不整体失败）、成功/失败混合、错误信封透传。
对齐 TD §13 批量治理闭环。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import make_metric

from app.api import deps
from app.core.exceptions import BusinessError
from app.main import app


def _as_reviewer(role: str = "domain_admin"):
    """上下文：把当前用户角色覆盖为评审角色（client fixture 默认 metric_owner）。"""

    class _Ctx:
        def __enter__(self) -> None:
            app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
                id=1, role=role, domain="sales"
            )

        def __exit__(self, *exc: object) -> None:
            app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
                id=1, role="metric_owner"
            )

    return _Ctx()


async def test_batch_submit_mixed_results(client):
    """批量提交：逐条收集，成功与失败并存（单条失败不阻断其余）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        submitted = make_metric(status="REVIEW")

        async def fake_submit(code, request, **kwargs):
            if code == "bad_metric":
                raise BusinessError("指标不存在: bad_metric", error_code="NOT_FOUND")
            return submitted

        instance.submit_metric = AsyncMock(side_effect=fake_submit)

        resp = await client.post(
            "/api/v1/metric-definitions/batch-submit",
            json={
                "items": [
                    {"metric_code": "sales_gmv_daily", "change_reason": "首次提交审核"},
                    {"metric_code": "bad_metric", "change_reason": "首次提交审核"},
                ]
            },
        )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    assert body["fail_count"] == 1
    by_code = {r["metric_code"]: r for r in body["results"]}
    assert by_code["sales_gmv_daily"]["ok"] is True
    assert by_code["bad_metric"]["ok"] is False
    assert "指标不存在" in by_code["bad_metric"]["message"]


async def test_batch_submit_passes_reviewer_assignment(client):
    """批量提交透传评审指派字段（reviewer_id/reviewer_type）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        instance.submit_metric = AsyncMock(return_value=make_metric(status="REVIEW"))

        resp = await client.post(
            "/api/v1/metric-definitions/batch-submit",
            json={
                "items": [
                    {
                        "metric_code": "sales_gmv_daily",
                        "change_reason": "提交审核",
                        "reviewer_id": 7,
                        "reviewer_type": "user",
                    }
                ]
            },
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["ok_count"] == 1
    req = instance.submit_metric.call_args.args[1]
    assert req.reviewer_id == 7
    assert req.reviewer_type == "user"


async def test_batch_approve_mixed_results(client):
    """批量通过：逐条收集，成功与失败并存。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value
        published = make_metric(status="PUBLISHED")

        async def fake_approve(code, request, **kwargs):
            if code == "pii_metric":
                raise BusinessError("PII 指标须先通过合规审核", error_code="COMPLIANCE_BLOCKED")
            return published

        instance.approve_metric = AsyncMock(side_effect=fake_approve)

        with _as_reviewer():
            resp = await client.post(
                "/api/v1/metric-definitions/batch-approve",
                json={"metric_codes": ["sales_gmv_daily", "pii_metric"], "mode": "standard"},
            )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    assert body["fail_count"] == 1
    by_code = {r["metric_code"]: r for r in body["results"]}
    assert by_code["pii_metric"]["ok"] is False
    assert "合规" in by_code["pii_metric"]["message"]


async def test_batch_reject_mixed_results(client):
    """批量打回：逐条收集，成功与失败并存。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value

        async def fake_reject(code, request, **kwargs):
            if code == "x":
                raise BusinessError("指标不存在: x", error_code="NOT_FOUND")
            return make_metric(status="DRAFT")

        instance.reject_metric = AsyncMock(side_effect=fake_reject)

        with _as_reviewer():
            resp = await client.post(
                "/api/v1/metric-definitions/batch-reject",
                json={"metric_codes": ["sales_gmv_daily", "x"], "reason": "口径需调整"},
            )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    assert body["fail_count"] == 1


async def test_batch_deprecate_mixed_results(client):
    """批量下线：逐条收集，successor 未发布失败不影响其余。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        instance = mock_svc.return_value

        async def fake_deprecate(code, successor_code, **kwargs):
            if code == "no_successor":
                raise BusinessError(
                    f"替代指标 {successor_code} 未发布，无法作为替代",
                    error_code="VALIDATION_ERROR",
                )
            return make_metric(status="DEPRECATED")

        instance.deprecate_metric = AsyncMock(side_effect=fake_deprecate)

        resp = await client.post(
            "/api/v1/metric-definitions/batch-deprecate",
            json={
                "items": [
                    {"metric_code": "sales_gmv_daily", "successor_code": "sales_gmv_weekly"},
                    {"metric_code": "no_successor", "successor_code": "ghost_metric"},
                ]
            },
        )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["ok_count"] == 1
    assert body["fail_count"] == 1
    by_code = {r["metric_code"]: r for r in body["results"]}
    assert by_code["no_successor"]["ok"] is False
    assert "未发布" in by_code["no_successor"]["message"]


async def test_batch_submit_empty_items_422(client):
    """空 items 列表 → 422（schema 约束）。"""
    resp = await client.post(
        "/api/v1/metric-definitions/batch-submit", json={"items": []}
    )
    assert resp.status_code == 422
