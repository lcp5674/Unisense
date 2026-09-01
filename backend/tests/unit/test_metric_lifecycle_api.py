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
        resp = await metrics_client.get("/api/v1/metric-definitions/sales_gmv_d/archived")
    assert resp.status_code == 200
    assert resp.json()["data"]["metric"]["metric_code"] == "sales_gmv_d"


async def test_archive_metric(metrics_client: httpx.AsyncClient) -> None:
    """归档（DELETE）→ 200，调用携带角色（管理员或原 Owner）。"""
    with (
        patch("app.api.metrics.ConflictRepository") as mock_conflict,
        patch("app.api.metrics.MetricService") as mock_svc,
    ):
        mock_conflict.return_value.count_open_for_metric = AsyncMock(return_value=0)
        mock_svc.return_value.delete_metric = AsyncMock(return_value=_metric())
        mock_svc.return_value.run_lineage_post_commit = AsyncMock()
        resp = await metrics_client.delete("/api/v1/metric-definitions/sales_gmv_d")
    assert resp.status_code == 200
    mock_svc.return_value.delete_metric.assert_awaited_once_with(
        "sales_gmv_d", actor_id=1, role="platform_admin"
    )


async def test_archive_metric_blocked_by_pending_conflict(
    metrics_client: httpx.AsyncClient,
) -> None:
    """删除被未决冲突引用的指标 → 400 CONFLICT_EXISTS（悬空冲突防护）。"""
    with patch("app.api.metrics.ConflictRepository") as mock_conflict:
        mock_conflict.return_value.count_open_for_metric = AsyncMock(return_value=2)
        resp = await metrics_client.delete("/api/v1/metric-definitions/sales_gmv_d")
    assert resp.status_code == 400
    assert resp.json()["code"] == "CONFLICT_EXISTS"


async def test_restore_metric(metrics_client: httpx.AsyncClient) -> None:
    """恢复归档指标 → 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.restore_metric = AsyncMock(return_value=_metric())
        mock_svc.return_value.run_lineage_post_commit = AsyncMock()
        resp = await metrics_client.post("/api/v1/metric-definitions/sales_gmv_d/restore")
    assert resp.status_code == 200
    mock_svc.return_value.restore_metric.assert_awaited_once()


async def test_purge_metric(metrics_client: httpx.AsyncClient) -> None:
    """彻底删除归档指标（仅平台管理员）→ 200 + 审计落库。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.purge_metric = AsyncMock()
        resp = await metrics_client.post("/api/v1/metric-definitions/sales_gmv_d/purge")
    assert resp.status_code == 200
    mock_svc.return_value.purge_metric.assert_awaited_once_with(
        "sales_gmv_d", actor_id=1, role="platform_admin"
    )


async def test_promote_metric(metrics_client: httpx.AsyncClient) -> None:
    """灰度全量发布 → 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.promote_metric = AsyncMock(return_value=_metric())
        resp = await metrics_client.post("/api/v1/metric-definitions/sales_gmv_d/promote")
    assert resp.status_code == 200
    mock_svc.return_value.promote_metric.assert_awaited_once_with(
        "sales_gmv_d", actor_id=1, role="platform_admin", user_domain=None
    )


async def test_rollback_metric(metrics_client: httpx.AsyncClient) -> None:
    """灰度回滚 → 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.rollback_metric = AsyncMock(return_value=_metric())
        resp = await metrics_client.post("/api/v1/metric-definitions/sales_gmv_d/rollback")
    assert resp.status_code == 200
    mock_svc.return_value.rollback_metric.assert_awaited_once_with(
        "sales_gmv_d", actor_id=1, role="platform_admin", user_domain=None
    )


async def test_emergency_publish_metric(metrics_client: httpx.AsyncClient) -> None:
    """紧急发布（特性开关默认开启）→ 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.emergency_publish_metric = AsyncMock(return_value=_metric())
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
                "candidates": [{"metric_code": "c1", "status": "DRAFT", "validation_errors": None}],
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
        mock_svc.return_value.recover_source_dropped = AsyncMock(return_value=_metric())
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/recover-source-dropped"
        )
    assert resp.status_code == 200
    mock_svc.return_value.recover_source_dropped.assert_awaited_once()


async def test_reactivate_metric(metrics_client: httpx.AsyncClient) -> None:
    """单条重新启用已废弃指标（DEPRECATED → DRAFT）→ 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.reactivate_metric = AsyncMock(return_value=_metric())
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/reactivate"
        )
    assert resp.status_code == 200
    mock_svc.return_value.reactivate_metric.assert_awaited_once()


async def test_batch_reactivate_metrics(metrics_client: httpx.AsyncClient) -> None:
    """批量重新启用已废弃指标 → 200（返回 BatchResponse）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.reactivate_metric = AsyncMock(return_value=_metric())
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/batch-reactivate",
            json={"metric_codes": ["sales_gmv_d", "sales_order_cnt_d"]},
        )
    assert resp.status_code == 200
    assert mock_svc.return_value.reactivate_metric.await_count == 2


async def test_create_metric_commit_integrity_error() -> None:
    """单条创建并发竞态：commit 唯一键冲突 → 转 ConflictError(409)，不 500。

    覆盖审查遗留项「单条创建路径不捕 IntegrityError」——repository 层 flush 捕获
    已覆盖 metric/version 的直接唯一键冲突；此处验证端点 commit 兜底（血缘/冲突等
    延迟 flush 对象的唯一键冲突在 commit 才暴露，对齐 semantic.py 模板创建端点先例）。
    """
    from sqlalchemy.exc import IntegrityError

    from app.api.metrics import create_metric as create_metric_handler
    from app.core.exceptions import ConflictError

    db = MagicMock()
    db.commit = AsyncMock(
        side_effect=IntegrityError(
            "Duplicate entry 'sales_gmv_d' for key 'metric.metric_code'", None, None
        )
    )
    db.rollback = AsyncMock()
    user = MagicMock(id=1, role="platform_admin")

    with (
        patch("app.api.metrics.MetricService") as mock_svc,
        patch("app.api.metrics._register_metric_l3_lineage", new_callable=AsyncMock),
    ):
        mock_svc.return_value.create_metric = AsyncMock(return_value=_metric("sales_gmv_d"))
        with pytest.raises(ConflictError) as exc_info:
            await create_metric_handler(
                MagicMock(), db, user, trace_id="trace-1", http_req=MagicMock()
            )
    assert exc_info.value.error_code == "METRIC_CODE_EXISTS"
    assert "sales_gmv_d" in exc_info.value.message
    db.rollback.assert_awaited_once()


async def test_confirm_deprecate_dropped(metrics_client: httpx.AsyncClient) -> None:
    """确认退役（DSD → DEPRECATED）→ 200。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.confirm_deprecate_dropped = AsyncMock(return_value=_metric())
        mock_svc.return_value.run_lineage_post_commit = AsyncMock()
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
        id=2,
        role="metric_owner",
        domain="sales",
        roles_all=lambda: ["metric_owner"],
        has_role=lambda r: r == "metric_owner",
    )
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.mark_source_dropped = AsyncMock(return_value=0)
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/mark-source-dropped",
            json={"source_ids": ["src_a"]},
        )
    assert resp.status_code == 403
    mock_svc.return_value.mark_source_dropped.assert_not_awaited()


# ---- E1: 补齐 8 条零 API 测试路由（description/submit/approve/reject/版本三端点/health）----


async def test_update_metric_description(metrics_client: httpx.AsyncClient) -> None:
    """PUT /{code}/description → 200（治理补充，不触发版本）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.update_metric_description = AsyncMock(return_value=_metric())
        resp = await metrics_client.put(
            "/api/v1/metric-definitions/sales_gmv_d/description",
            json={"description": "日订单金额口径"},
        )
    assert resp.status_code == 200
    mock_svc.return_value.update_metric_description.assert_awaited_once()


async def test_submit_metric_api(metrics_client: httpx.AsyncClient) -> None:
    """POST /{code}/submit → 200（DRAFT→REVIEW）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.submit_metric = AsyncMock(return_value=_metric())
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/submit",
            json={"change_reason": "首次提交评审"},
        )
    assert resp.status_code == 200
    mock_svc.return_value.submit_metric.assert_awaited_once()


async def test_approve_metric_api(metrics_client: httpx.AsyncClient) -> None:
    """POST /{code}/approve → 200（REVIEW→PUBLISHED，标准发布）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.approve_metric = AsyncMock(return_value=_metric())
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/approve",
            json={"mode": "standard"},
        )
    assert resp.status_code == 200
    mock_svc.return_value.approve_metric.assert_awaited_once()


async def test_reject_metric_api(metrics_client: httpx.AsyncClient) -> None:
    """POST /{code}/reject → 200（REVIEW→DRAFT）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.reject_metric = AsyncMock(return_value=_metric())
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/reject",
            json={"reason": "口径与粒度不符，请修改后重提"},
        )
    assert resp.status_code == 200
    mock_svc.return_value.reject_metric.assert_awaited_once()


async def test_confirm_version_api(metrics_client: httpx.AsyncClient) -> None:
    """POST /{code}/confirm-version → 200（消费方确认 PENDING_VERSION）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.confirm_version = AsyncMock(return_value=_metric())
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/confirm-version",
            json={"version": 1},
        )
    assert resp.status_code == 200
    mock_svc.return_value.confirm_version.assert_awaited_once_with("sales_gmv_d", 1, consumer_id=1)


async def test_reject_version_api(metrics_client: httpx.AsyncClient) -> None:
    """POST /{code}/reject-version → 200（消费方拒绝 PENDING_VERSION）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.reject_version = AsyncMock(return_value=_metric())
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/reject-version",
            json={"version": 1, "reason": "新口径存在口径错误，拒绝确认"},
        )
    assert resp.status_code == 200
    mock_svc.return_value.reject_version.assert_awaited_once_with(
        "sales_gmv_d", 1, reason="新口径存在口径错误，拒绝确认", consumer_id=1
    )


async def test_extend_version_api(metrics_client: httpx.AsyncClient) -> None:
    """POST /{code}/extend-version → 200（PENDING_VERSION 确认延期）。"""
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.extend_version = AsyncMock(return_value=_metric())
        resp = await metrics_client.post(
            "/api/v1/metric-definitions/sales_gmv_d/extend-version",
            json={"version": 1},
        )
    assert resp.status_code == 200
    mock_svc.return_value.extend_version.assert_awaited_once_with(
        "sales_gmv_d", 1, actor_id=1, role="platform_admin", user_domain=None
    )


async def test_get_metric_health_api(metrics_client: httpx.AsyncClient) -> None:
    """GET /{code}/health → 200（五维健康度评分）。"""
    from datetime import UTC, datetime

    from app.services.semantic.schemas import MetricHealthResponse

    health = MetricHealthResponse(
        metric_id=1,
        score=88,
        level="HEALTHY",
        completeness_score=90,
        activity_score=85,
        quality_score=95,
        owner_response_score=80,
        lineage_coverage_score=90,
        missing_dimensions=[],
        calculated_at=datetime.now(UTC),
    )
    with patch("app.api.metrics.MetricService") as mock_svc:
        mock_svc.return_value.get_metric_health = AsyncMock(return_value=health)
        resp = await metrics_client.get("/api/v1/metric-definitions/sales_gmv_d/health")
    assert resp.status_code == 200
    assert resp.json()["data"]["level"] == "HEALTHY"
    # P0-3 行级隔离：健康度读取透传 actor/role/user_domain（私有指标仅本人/管理可见）
    mock_svc.return_value.get_metric_health.assert_awaited_once_with(
        "sales_gmv_d", actor_id=1, role="platform_admin", user_domain=None
    )


async def test_create_allows_legit_sql_in_definition_json(
    metrics_client: httpx.AsyncClient,
) -> None:
    """单条创建：口径字段（definition_json/raw_sql）含合法 ETL SQL（-- 注释 /
    UNION SELECT / 块注释）不被注入守卫误伤（回归：此前 400 INJECTION_DETECTED）。
    """
    payload = {
        "name": "门诊收费金额",
        "domain": "e2e",
        "type": "atomic",
        "metric_code": "outp_feeamount_amt_day",
        # 单条创建数仓开发必填（PRD 4.5 口径三方责任）
        "dw_developer_id": 1,
        "definition_json": {
            "expression": "SUM(COALESCE(fee_amount, 0))",
            "sql": "SELECT month_id FROM t -- 行注释\nUNION ALL SELECT month_id FROM t2",
            "source_table": "wedw_dwd.outp_fee_di",
            "measure_column": "fee_amount",
        },
        "raw_sql": "insert overwrite table x select month_id from t -- 注释",
    }
    with (
        patch("app.api.metrics.MetricService") as mock_svc,
        patch("app.api.metrics._register_metric_l3_lineage", new_callable=AsyncMock),
        patch("app.api.metrics.write_audit", new_callable=AsyncMock),
    ):
        mock_svc.return_value.create_metric = AsyncMock(
            return_value=_metric("outp_feeamount_amt_day")
        )
        resp = await metrics_client.post("/api/v1/metric-definitions", json=payload)
    assert resp.status_code == 201
    mock_svc.return_value.create_metric.assert_awaited_once()


async def test_create_still_blocks_injection_in_non_sql_fields(
    metrics_client: httpx.AsyncClient,
) -> None:
    """单条创建：非口径字段（metric_code）仍被注入守卫拦截——豁免不削弱纵深防御。"""
    payload = {
        "name": "门诊收费金额",
        "domain": "e2e",
        "type": "atomic",
        "metric_code": "outp_feeamount_amt_day -- 注入",
        "definition_json": {"expression": "SUM(fee_amount)"},
    }
    resp = await metrics_client.post("/api/v1/metric-definitions", json=payload)
    assert resp.status_code == 400
    assert resp.json()["code"] == "INJECTION_DETECTED"
