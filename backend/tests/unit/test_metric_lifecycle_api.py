"""指标生命周期 API 契约测试（P3-15：补齐零 API 测试路由）。

覆盖（ASGI + 模拟 MetricService，快速契约校验）：
- 归档查询 / 归档(删除) / 恢复（restore）
- 全量发布 promote / 回滚 rollback
- 紧急发布 emergency-publish（特性开关开启）
- 对比 compare / 批量注册 batch-register
- 数据源下线三端点：mark-source-dropped（管理角色门禁）/ recover / confirm-deprecate
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport
from tests.conftest import make_metric

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
        id=1, role="platform_admin", domain=None
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


def _metric(code: str = "sales_gmv_d"):
    return make_metric(metric_code=code)


async def test_get_archived_metric(metrics_client: httpx.AsyncClient) -> None:
    """归档指标查询 → 200。"""
    from app.services.semantic.schemas import MetricResponse

    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.get_archived_metric_public = AsyncMock(
            return_value={
                "metric": MetricResponse.model_validate(_metric()),
                "successor_code": None,
                "arbitration_mark": None,
            }
        )
        resp = await metrics_client.get(
            "/api/v1/metric-definitions/sales_gmv_d/archived"
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["metric"]["metric_code"] == "sales_gmv_d"


async def test_archive_metric(metrics_client: httpx.AsyncClient) -> None:
    """归档（DELETE）→ 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.delete_metric = AsyncMock(return_value=_metric())
        resp = await metrics_client.delete("/api/v1/metric-definitions/sales_gmv_d")
    assert resp.status_code == 200


async def test_restore_metric(metrics_client: httpx.AsyncClient) -> None:
    """恢复归档指标 → 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.restore_metric = AsyncMock(return_value=_metric())
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/restore"
        )
    assert resp.status_code == 200
    mock_svc.return_value.restore_metric.assert_awaited_once()


async def test_promote_metric(metrics_client: httpx.AsyncClient) -> None:
    """灰度全量发布 → 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.promote_metric = AsyncMock(
            return_value=_metric()
        )
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/promote"
        )
    assert resp.status_code == 200
    mock_svc.return_value.promote_metric.assert_awaited_once_with("sales_gmv_d", actor_id=1)


async def test_rollback_metric(metrics_client: httpx.AsyncClient) -> None:
    """灰度回滚 → 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.rollback_metric = AsyncMock(return_value=_metric())
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/rollback"
        )
    assert resp.status_code == 200
    mock_svc.return_value.rollback_metric.assert_awaited_once_with("sales_gmv_d", actor_id=1)


async def test_emergency_publish_metric(metrics_client: httpx.AsyncClient) -> None:
    """紧急发布（特性开关默认开启）→ 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.emergency_publish_metric = AsyncMock(
            return_value=_metric()
        )
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/emergency-publish",
            json={"reason": "生产故障需立即发布处理", "target_version": 1},
        )
    assert resp.status_code == 200
    mock_svc.return_value.emergency_publish_metric.assert_awaited_once()


async def test_compare_metrics(metrics_client: httpx.AsyncClient) -> None:
    """指标对比 → 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.compare_metrics = AsyncMock(
            return_value={"fields": {}, "definition": {"a": {}, "b": {}}}
        )
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/compare",
            json={"metric_codes": ["a_metric", "b_metric"]},
        )
    assert resp.status_code == 200


async def test_batch_register_metrics(metrics_client: httpx.AsyncClient) -> None:
    """批量注册 → 200（返回 candidates）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.batch_register_metrics = AsyncMock(
            return_value={
                "batch_id": "b1",
                "candidates": [
                    {"metric_code": "c1", "status": "DRAFT", "validation_errors": None}
                ],
            }
        )
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/batch-register",
            json={
                "source_table": "dwd.sales_detail",
                "measure_columns": ["gmv", "order_cnt"],
                "domain": "sales",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["batch_id"] == "b1"


async def test_recover_source_dropped(metrics_client: httpx.AsyncClient) -> None:
    """源已恢复（DSD → PUBLISHED）→ 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.recover_source_dropped = AsyncMock(
            return_value=_metric()
        )
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/recover-source-dropped"
        )
    assert resp.status_code == 200
    mock_svc.return_value.recover_source_dropped.assert_awaited_once()


async def test_confirm_deprecate_dropped(metrics_client: httpx.AsyncClient) -> None:
    """确认退役（DSD → DEPRECATED）→ 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.confirm_deprecate_dropped = AsyncMock(
            return_value=_metric()
        )
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/confirm-deprecate-dropped",
            json={"successor_code": None},
        )
    assert resp.status_code == 200
    mock_svc.return_value.confirm_deprecate_dropped.assert_awaited_once()


async def test_mark_source_dropped_admin_only(metrics_client: httpx.AsyncClient) -> None:
    """P0-1: mark-source-dropped 仅管理角色——platform_admin 放行。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.mark_source_dropped = AsyncMock(return_value=3)
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/mark-source-dropped",
            json={"source_ids": ["src_a"]},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["marked"] == 3


async def test_mark_source_dropped_owner_forbidden(
    metrics_client: httpx.AsyncClient,
) -> None:
    """P0-1: metric_owner 调用 mark-source-dropped → 403（越权收紧）。"""
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=2, role="metric_owner", domain="sales"
    )
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.mark_source_dropped = AsyncMock(return_value=0)
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/mark-source-dropped",
            json={"source_ids": ["src_a"]},
        )
    assert resp.status_code == 403
    mock_svc.return_value.mark_source_dropped.assert_not_awaited()
