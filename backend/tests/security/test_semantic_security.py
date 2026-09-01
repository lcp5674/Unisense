"""semantic 写端点审计 + PII 复核禁自审 安全测试（D10 §6.3 闭环）。

对齐 dimension/glossary 同款审查标准：写端点须落审计、PII 复核禁 Owner 自审。
依赖 conftest.client（默认角色 metric_owner，id=1）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import make_create_payload, make_metric

from app.api import metrics as metrics_module
from app.main import app


def _mock_service() -> MagicMock:
    svc = MagicMock()
    svc.create_metric = AsyncMock(return_value=make_metric())
    svc.update_metric = AsyncMock(return_value=make_metric())
    svc.publish_metric = AsyncMock(return_value=make_metric())
    svc.approve_metric = AsyncMock(return_value=make_metric())
    svc.deprecate_metric = AsyncMock(return_value=make_metric())
    svc.review_compliance = AsyncMock(return_value=make_metric())
    svc.run_lineage_post_commit = AsyncMock()
    return svc


async def test_pii_review_forbidden_for_metric_owner(client):
    # conftest 默认 client 角色 = metric_owner，须被 _PII_REVIEW_ROLES 拒绝
    with patch.object(metrics_module, "MetricService", return_value=_mock_service()):
        resp = await client.post("/api/v1/metric-definitions/foo/pii-review")
    assert resp.status_code == 403


async def test_create_metric_writes_audit(client):
    svc = _mock_service()
    with (
        patch.object(metrics_module, "MetricService", return_value=svc),
        patch.object(metrics_module, "write_audit", AsyncMock()) as wa,
        patch.object(metrics_module, "client_ip", return_value="test"),
    ):
        resp = await client.post("/api/v1/metric-definitions", json=make_create_payload())
        assert resp.status_code == 201
        wa.assert_awaited()


async def test_update_metric_writes_audit(client):
    svc = _mock_service()
    with (
        patch.object(metrics_module, "MetricService", return_value=svc),
        patch.object(metrics_module, "write_audit", AsyncMock()) as wa,
        patch.object(metrics_module, "client_ip", return_value="test"),
    ):
        resp = await client.put(
            "/api/v1/metric-definitions/foo", json={"change_reason": "修正口径说明"}
        )
        assert resp.status_code == 200
        wa.assert_awaited()


async def test_publish_metric_writes_audit(client, monkeypatch):
    """B3（审查修复）：/publish 收紧为 platform_admin 门禁——审计仍落。"""
    from unittest.mock import MagicMock

    from app.api import deps

    # 覆盖当前用户为平台管理员（B3 门禁：/publish 仅 platform_admin）
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role="platform_admin",
        roles_all=lambda: ["platform_admin"],
        has_role=lambda r: r == "platform_admin",
    )
    svc = _mock_service()
    with (
        patch.object(metrics_module, "MetricService", return_value=svc),
        patch.object(metrics_module, "write_audit", AsyncMock()) as wa,
        patch.object(metrics_module, "client_ip", return_value="test"),
    ):
        resp = await client.post(
            "/api/v1/metric-definitions/foo/publish",
            json={"version": 1, "change_reason": "首次发布说明"},
        )
        assert resp.status_code == 200
        wa.assert_awaited()


async def test_deprecate_metric_writes_audit(client):
    svc = _mock_service()
    with (
        patch.object(metrics_module, "MetricService", return_value=svc),
        patch.object(metrics_module, "write_audit", AsyncMock()) as wa,
        patch.object(metrics_module, "client_ip", return_value="test"),
    ):
        resp = await client.post(
            "/api/v1/metric-definitions/foo/deprecate", json={"successor_code": "bar"}
        )
        assert resp.status_code == 200
        wa.assert_awaited()
