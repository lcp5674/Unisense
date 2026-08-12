"""语义服务单元测试（对齐 DEV_GUIDE §8b：纯逻辑、可独立运行）。

使用 mock 替换 MetricRepository，覆盖状态机、乐观锁、PII 合规闸门、
破坏性变更判定与分页计算。无数据库依赖。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.conftest import make_create_payload, make_metric

from app.core.exceptions import (
    BusinessError,
    ConflictError,
    NotFoundError,
)
from app.services.semantic.schemas import (
    MetricCreateRequest,
    MetricListParams,
    MetricPublishRequest,
    MetricUpdateRequest,
)
from app.services.semantic.service import MetricService


def _svc_with_repo() -> tuple[MetricService, MagicMock]:
    """构造服务并替换其仓库为 mock，返回 (service, mock_repo_instance)。"""
    with patch("app.services.semantic.service.MetricRepository") as mock_repo:
        svc = MetricService(db=MagicMock())
        return svc, mock_repo.return_value


async def test_create_metric_happy_path():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    result = await svc.create_metric(MetricCreateRequest(**make_create_payload()), owner_id=1)

    repo.get_by_code.assert_awaited_once_with("sales_gmv_amount_daily")
    repo.create.assert_awaited_once()
    repo.create_version.assert_awaited_once()
    assert result.status == "DRAFT"
    assert result.version == 1
    assert result.row_version == 1


async def test_create_metric_duplicate_code_raises_conflict():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())

    with pytest.raises(ConflictError):
        await svc.create_metric(MetricCreateRequest(**make_create_payload()), owner_id=1)


async def test_get_metric_not_found():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError):
        await svc.get_metric("missing")


async def test_update_metric_creates_version_and_bumps():
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=1, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)
    updated = make_metric(status="DRAFT", row_version=2, version=2)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    repo.create_version = AsyncMock(return_value=MagicMock())

    result = await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            definition_json=existing.definition_json,
            change_reason="口径不变",
        ),
        actor_id=1,
        role="metric_owner",
    )

    # 乐观锁使用原始 row_version
    call_args = repo.update_with_optimistic_lock.call_args
    assert call_args.args[1] == 1
    # 提供 definition_json 时创建版本快照，change_type 为 UPDATE（非破坏性）
    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.change_type == "UPDATE"
    assert version_arg.version == 2
    assert result.row_version == 2
    assert result.version == 2


async def test_update_metric_breaking_change():
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="DRAFT",
        row_version=1,
        version=1,
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    updated = make_metric(status="DRAFT", row_version=2, version=2)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            definition_json={"expression": "SUM(refund_amount)"},
            change_reason="口径变更",
        ),
        actor_id=1,
        role="metric_owner",
    )

    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.change_type == "BREAKING"
    assert version_arg.version == 2


async def test_update_metric_invalid_status_rejected():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DEPRECATED"))

    with pytest.raises(BusinessError):
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(change_reason="修正名称"),
            actor_id=1,
            role="metric_owner",
        )


async def test_publish_metric_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="REVIEW", pii_flag=False))
    published = make_metric(status="PUBLISHED")
    repo.update_with_optimistic_lock = AsyncMock(return_value=published)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.mark_version_published = AsyncMock()

    result = await svc.publish_metric(
        "sales_gmv_daily",
        MetricPublishRequest(version=1, change_reason="首次发布"),
        actor_id=1,
        role="metric_owner",
    )

    assert result.status == "PUBLISHED"
    called = repo.update_with_optimistic_lock.call_args.kwargs
    assert called["status"] == "PUBLISHED"
    assert called["effective_version"] == 1
    repo.mark_version_published.assert_awaited_once()


async def test_publish_metric_pii_blocked_without_compliance():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="REVIEW", pii_flag=True, compliance_reviewed=False)
    )

    with pytest.raises(BusinessError) as exc:
        await svc.publish_metric(
            "sales_gmv_daily",
            MetricPublishRequest(version=1, change_reason="发布含 PII 指标"),
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "COMPLIANCE_BLOCKED"


async def test_publish_metric_invalid_status_rejected():
    svc, repo = _svc_with_repo()
    # DRAFT 直接发布非法（须先 submit→REVIEW，再 approve→PUBLISHED）
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT"))

    with pytest.raises(BusinessError):
        await svc.publish_metric(
            "sales_gmv_daily",
            MetricPublishRequest(version=1, change_reason="重复发布"),
            actor_id=1,
            role="metric_owner",
        )


async def test_deprecate_metric_success_sets_sunset():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    deprecated = make_metric(status="DEPRECATED", successor_code="sales_gmv_v2")
    repo.update_with_optimistic_lock = AsyncMock(return_value=deprecated)

    result = await svc.deprecate_metric(
        "sales_gmv_daily", "sales_gmv_v2", actor_id=1, role="metric_owner"
    )

    assert result.status == "DEPRECATED"
    called = repo.update_with_optimistic_lock.call_args.kwargs
    assert called["successor_code"] == "sales_gmv_v2"
    assert called["sunset_until"] is not None


async def test_deprecate_metric_already_deprecated_rejected():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DEPRECATED"))

    with pytest.raises(BusinessError):
        await svc.deprecate_metric("sales_gmv_daily", "x", actor_id=1, role="metric_owner")


async def test_list_metrics_pagination_offset():
    svc, repo = _svc_with_repo()
    repo.list_metrics = AsyncMock(return_value=([make_metric()], 1))

    await svc.list_metrics(MetricListParams(page=3, page_size=10))

    called = repo.list_metrics.call_args.kwargs
    assert called["limit"] == 10
    assert called["offset"] == 20  # (page-1)*page_size


async def test_is_breaking_change_detection():
    svc, _ = _svc_with_repo()
    old = {"expression": "SUM(a)", "dependencies": ["t1"]}
    same = {"expression": "SUM(a)", "dependencies": ["t1"]}
    diff = {"expression": "SUM(b)", "dependencies": ["t1"]}

    assert svc._is_breaking_change(old, same) is False
    assert svc._is_breaking_change(old, diff) is True


async def test_review_compliance_blocks_owner_self_review():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=7))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(owner_id=7))

    with pytest.raises(BusinessError) as exc:
        await svc.review_compliance("sales_gmv_daily", actor_id=7, role="domain_admin")
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"
    repo.update_with_optimistic_lock.assert_not_awaited()


async def test_review_compliance_allows_non_owner():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=7))
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(owner_id=7, compliance_reviewed=True)
    )

    result = await svc.review_compliance("sales_gmv_daily", actor_id=99, role="domain_admin")
    assert result.compliance_reviewed is True
    repo.update_with_optimistic_lock.assert_awaited_once()
