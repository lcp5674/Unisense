"""语义服务单元测试（对齐 DEV_GUIDE §8b：纯逻辑、可独立运行）。

使用 mock 替换 MetricRepository，覆盖状态机、乐观锁、PII 合规闸门、
破坏性变更判定与分页计算。无数据库依赖。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.conftest import make_create_payload, make_metric

from app.core.exceptions import (
    AuthError,
    BusinessError,
    ConflictError,
    NotFoundError,
)
from app.services.semantic.schemas import (
    MetricApproveRequest,
    MetricCreateRequest,
    MetricEmergencyPublishRequest,
    MetricListParams,
    MetricPublishRequest,
    MetricRejectRequest,
    MetricSubmitRequest,
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


async def test_create_metric_marks_pending_conflict_on_precheck_hit():
    """真实预检命中相似口径 → 挂 pending_conflict 标记并持久化详情。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())
    # 已存在口径相同但编码不同 → 预检命中 SAME_DEF_DIFF_NAME（软冲突）
    existing = make_metric(
        id=9,
        metric_code="sales_gmv_amount_day",
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.list_metrics = AsyncMock(return_value=([existing], 1))
    updated = make_metric(
        pending_conflict=True,
        pending_conflict_detail={"conflict_type": "same_def_diff_name"},
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    result = await svc.create_metric(MetricCreateRequest(**make_create_payload()), owner_id=1)

    repo.update_with_optimistic_lock.assert_awaited_once()
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["pending_conflict"] is True
    assert kwargs["pending_conflict_detail"]["conflict_type"] == "same_def_diff_name"
    assert result.pending_conflict is True


async def test_create_metric_precheck_failure_is_best_effort():
    """预检依赖加载失败（list_metrics 抛错）→ 不阻断创建，也不抛异常。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())
    repo.list_metrics = AsyncMock(side_effect=RuntimeError("catalog down"))

    result = await svc.create_metric(MetricCreateRequest(**make_create_payload()), owner_id=1)

    assert result is created
    # 未挂冲突标记
    repo.update_with_optimistic_lock.assert_not_called()


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
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=7, pii_flag=True))
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(owner_id=7, compliance_reviewed=True, pii_flag=True)
    )

    result = await svc.review_compliance("sales_gmv_daily", actor_id=99, role="domain_admin")
    assert result.compliance_reviewed is True
    repo.update_with_optimistic_lock.assert_awaited_once()


# ---- T052: compare + batch_register 测试 ----


async def test_compare_metrics_identical():
    """两指标完全相同 → 所有字段 identical。"""
    svc, repo = _svc_with_repo()
    defn = {"expression": "SUM(x)", "dependencies": ["t1"]}
    m1 = make_metric(metric_code="m1", definition_json=defn)
    m2 = make_metric(metric_code="m2", definition_json=defn)
    repo.get_by_code = AsyncMock(side_effect=[m1, m2])

    result = await svc.compare_metrics("m1", "m2")
    # 实现契约（对齐前端 MetricCompare）：result["metrics"] + result["fields"][...]
    assert result["metrics"] == ["m1", "m2"]
    # 同口径应标记 identical
    defn_diff = result["fields"]["definition"]
    assert defn_diff["difference_level"] == "identical"


async def test_compare_metrics_different():
    """两指标口径不同 → 标记 different。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1", definition_json={"expression": "SUM(x)"})
    m2 = make_metric(metric_code="m2", definition_json={"expression": "SUM(y)"})
    repo.get_by_code = AsyncMock(side_effect=[m1, m2])

    result = await svc.compare_metrics("m1", "m2")
    defn_diff = result["fields"]["definition"]
    assert defn_diff["difference_level"] == "different"


async def test_batch_register_success():
    """批量注册：全部成功。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)  # 无重名
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    from app.services.semantic.schemas import MetricBatchRegisterRequest

    request = MetricBatchRegisterRequest(
        source_table="dwd.sales_detail",
        measure_columns=["gmv", "order_cnt"],
        dimension_mapping={"domain": "sales"},
        llm_prefill=True,
        domain="sales",
    )

    result = await svc.batch_register_metrics(request, actor_id=1)

    assert "batch_id" in result
    assert len(result["candidates"]) == 2
    # 实现契约：每条候选 {metric_code, status, validation_errors}，成功为 DRAFT
    assert all(c["status"] == "DRAFT" for c in result["candidates"])
    assert all(c["validation_errors"] is None for c in result["candidates"])


async def test_batch_register_partial_failure():
    """批量注册：部分校验失败。"""
    svc, repo = _svc_with_repo()
    # 第二个重名
    repo.get_by_code = AsyncMock(side_effect=[None, make_metric()])
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    from app.services.semantic.schemas import MetricBatchRegisterRequest

    request = MetricBatchRegisterRequest(
        source_table="dwd.sales_detail",
        measure_columns=["gmv", "order_cnt"],
        dimension_mapping={"domain": "sales"},
        llm_prefill=True,
        domain="sales",
    )

    result = await svc.batch_register_metrics(request, actor_id=1)

    assert len(result["candidates"]) == 2
    # 失败信息在候选的 validation_errors 内（实现契约，无顶层 errors 键）
    assert result["candidates"][0]["status"] == "DRAFT"
    assert result["candidates"][1]["status"] == "VALIDATION_ERROR"
    assert result["candidates"][1]["validation_errors"] is not None


async def test_review_compliance_rejects_non_pii():
    """非 PII 指标无需合规复核 → PII_FLAG_REQUIRED。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=7, pii_flag=False))

    with pytest.raises(BusinessError) as exc:
        await svc.review_compliance("sales_gmv_daily", actor_id=99, role="domain_admin")
    assert exc.value.error_code == "PII_FLAG_REQUIRED"


# ---- 指标编码自动生成（FR-010）----


async def test_create_metric_auto_generates_code_from_measure():
    """metric_code 缺省时按 源表/度量列/周期 自动生成 4 段式编码。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric(metric_code="sales_sales_orderamount_day")
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    payload = make_create_payload(
        metric_code=None,
        source_table="dwd_sales_orders",
        measure_column="order_amount",
        period="day",
    )
    result = await svc.create_metric(MetricCreateRequest(**payload), owner_id=1)

    # 生成编码: sales(域) + sales(业务对象) + orderamount(度量) + day(周期)
    assert result.metric_code == "sales_sales_orderamount_day"


async def test_create_metric_auto_generates_fallback_code():
    """metric_code 缺省且无源表时回退 {domain}_entity_{measure}_day。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric(metric_code="sales_entity_value_day")
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    result = await svc.create_metric(
        MetricCreateRequest(**make_create_payload(metric_code=None)), owner_id=1,
    )

    assert result.metric_code == "sales_entity_value_day"


async def test_create_metric_auto_code_conflict_suffix():
    """自动生成的编码冲突时追加 _2 后缀。"""
    svc, repo = _svc_with_repo()
    # 生成时第 1 次查（候选已存在）→ _2；生成时第 2 次查（_2 可用）+ 创建前唯一性检查均不存在
    repo.get_by_code = AsyncMock(side_effect=[make_metric(), None, None])
    created = make_metric(metric_code="sales_entity_value_day_2")
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    result = await svc.create_metric(
        MetricCreateRequest(**make_create_payload(metric_code=None)), owner_id=1,
    )

    assert result.metric_code == "sales_entity_value_day_2"


async def test_create_metric_explicit_code_still_validated():
    """显式传入 metric_code 仍走 4 段式校验（ConflictError 之外，非法格式 422）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)

    with pytest.raises(ValueError):
        MetricCreateRequest(**make_create_payload(metric_code="bad_code"))


# ---- approve/reject 自审豁免（管理员可审核自己提交的指标，普通角色仍禁止）----


def _svc_approve_ready(svc, repo, *, submitted_by: int = 1, status: str = "REVIEW"):
    """准备 approve/reject 所需的 mock 环境，返回指标。"""
    metric = make_metric(status=status, submitted_by=submitted_by, owner_id=2)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=MagicMock(id=1, version=1))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock(return_value=None)
    svc._cache.invalidate = AsyncMock(return_value=None)
    svc._publish_event = AsyncMock(return_value=None)
    return metric


async def test_approve_metric_admin_can_self_review():
    """platform_admin 可审核自己提交的指标（提交人==审核人）。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1)

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="standard", target_version=1),
        actor_id=1,
        role="platform_admin",
    )

    assert result.status == "PUBLISHED"
    repo.update_with_optimistic_lock.assert_awaited_once()


async def test_approve_metric_domain_admin_can_self_review():
    """domain_admin 同样豁免自审。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1)

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="standard", target_version=1),
        actor_id=1,
        role="domain_admin",
    )

    assert result.status == "PUBLISHED"


async def test_approve_metric_non_admin_self_review_blocked():
    """普通角色（reviewer）自审仍被禁止。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1)

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=1,
            role="reviewer",
        )
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"
    repo.update_with_optimistic_lock.assert_not_awaited()


async def test_approve_metric_no_role_self_review_blocked():
    """role 缺省（None）按严格模式处理——向后兼容不传 role 的调用方。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1)

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=1,
        )
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"


async def test_approve_metric_different_actor_still_allowed():
    """提交人与审核人不同时，任何角色均可审核（不触发自审分支）。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1)

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="standard", target_version=1),
        actor_id=99,
        role="reviewer",
    )

    assert result.status == "PUBLISHED"


async def test_reject_metric_admin_can_self_review():
    """platform_admin 可驳回自己提交的指标。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1)
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="DRAFT"))

    result = await svc.reject_metric(
        "sales_gmv_daily",
        MetricRejectRequest(reason="口径需调整"),
        actor_id=1,
        role="platform_admin",
    )

    assert result.status == "DRAFT"


async def test_reject_metric_non_admin_self_review_blocked():
    """普通角色自驳回仍被禁止。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1)

    with pytest.raises(BusinessError) as exc:
        await svc.reject_metric(
            "sales_gmv_daily",
            MetricRejectRequest(reason="口径需调整"),
            actor_id=1,
            role="reviewer",
        )
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"


# ---- 补充覆盖率：辅助函数 / 状态流转 / 版本确认 / 灰度 / 健康 / 消费指南 ----


async def test_redact_definition_recurses():
    """redact_definition 保留键结构、叶子值全部脱敏为 ***。"""
    from app.services.semantic.service import redact_definition

    out = redact_definition(
        {"expr": "SUM(x)", "nested": {"a": 1}, "arr": ["x", "y"], "flag": True}
    )
    assert out == {"expr": "***", "nested": {"a": "***"}, "arr": ["***", "***"], "flag": "***"}


async def test_normalize_pii_syncs_dual_source():
    """_normalize_pii 双源一致：pii_flag 权威，回写/移除 definition.pii。"""
    from app.services.semantic.service import _normalize_pii

    # pii_flag=True 且 definition 无 pii → 回填
    d1, f1 = _normalize_pii({"expr": "x"}, True)
    assert d1["pii"] is True and f1 is True
    # definition 显式 pii=False 覆盖 flag
    d2, f2 = _normalize_pii({"pii": False}, True)
    assert "pii" not in d2 and f2 is False


async def test_submit_review_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    updated = make_metric(status="REVIEW")
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    result = await svc.submit_review("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert result.status == "REVIEW"
    assert repo.update_with_optimistic_lock.call_args.kwargs["submitted_by"] == 1


async def test_submit_review_invalid_transition():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", owner_id=1))
    with pytest.raises(ConflictError):
        await svc.submit_review("sales_gmv_daily", actor_id=1, role="metric_owner")


async def test_submit_metric_publishes_event():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))
    svc._publish_event = AsyncMock()

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="提交审核"),
        actor_id=1,
    )
    svc._publish_event.assert_awaited_once()
    assert svc._publish_event.call_args.args[0] == "metric.submitted"


async def test_review_metric_approved_publishes():
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1, pii_flag=False)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()

    result = await svc.review_metric(
        "sales_gmv_daily", approved=True, actor_id=2, role="domain_admin", change_reason="通过"
    )
    assert result.status == "PUBLISHED"


async def test_review_metric_self_review_blocked():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="REVIEW", owner_id=1))
    with pytest.raises(BusinessError) as exc:
        await svc.review_metric(
            "sales_gmv_daily", approved=True, actor_id=1, role="domain_admin", change_reason="x"
        )
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"


async def test_review_metric_reject_back_to_draft():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="REVIEW", owner_id=1))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="DRAFT"))

    result = await svc.review_metric(
        "sales_gmv_daily", approved=False, actor_id=2, role="domain_admin", change_reason="驳回"
    )
    assert result.status == "DRAFT"


async def test_approve_metric_standard():
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1, pii_flag=False)
    metric.submitted_by = 1
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()

    result = await svc.approve_metric(
        "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
    )
    assert result.status == "PUBLISHED"
    svc._publish_event.assert_awaited_once()


async def test_approve_metric_experimental_mode():
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1, pii_flag=False)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="EXPERIMENTAL"))
    repo.mark_version_published = AsyncMock()

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="experimental", gray_tenant_ids=[7]),
        actor_id=1,
        role="platform_admin",
    )
    assert result.status == "EXPERIMENTAL"
    assert repo.update_with_optimistic_lock.call_args.kwargs["gray_tenant_ids"] == [7]


async def test_approve_metric_self_review_blocked():
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1)
    metric.submitted_by = 1
    repo.get_by_code = AsyncMock(return_value=metric)
    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="metric_owner"
        )
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"


async def test_approve_metric_pii_blocked_without_compliance():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="REVIEW", owner_id=1, pii_flag=True, compliance_reviewed=False
        )
    )
    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
        )
    assert exc.value.error_code == "COMPLIANCE_BLOCKED"


async def test_reject_metric_success():
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1)
    metric.submitted_by = 1
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="DRAFT"))
    svc._publish_event = AsyncMock()

    result = await svc.reject_metric(
        "sales_gmv_daily", MetricRejectRequest(reason="口径不符"), actor_id=1, role="platform_admin"
    )
    assert result.status == "DRAFT"
    assert svc._publish_event.call_args.args[0] == "metric.rejected"


async def test_promote_metric_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="EXPERIMENTAL", owner_id=1))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()

    result = await svc.promote_metric("sales_gmv_daily", actor_id=1)
    assert result.status == "PUBLISHED"
    assert svc._publish_event.call_args.args[0] == "metric.promoted"


async def test_rollback_metric_success():
    svc, repo = _svc_with_repo()
    metric = make_metric(status="EXPERIMENTAL", owner_id=1, version=2, effective_version=2)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.list_versions = AsyncMock(return_value=[MagicMock(version=1, status="PUBLISHED")])
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_archived = AsyncMock()
    svc._db.execute = AsyncMock()
    svc._publish_event = AsyncMock()

    result = await svc.rollback_metric("sales_gmv_daily", actor_id=1)
    assert result.status == "PUBLISHED"
    assert svc._publish_event.call_args.args[0] == "metric.rolled_back"


async def test_delete_metric_success_and_reject():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    repo.soft_delete = AsyncMock()
    result = await svc.delete_metric("sales_gmv_daily", actor_id=1)
    assert result.status == "DRAFT"
    repo.soft_delete.assert_awaited_once()

    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", owner_id=1))
    with pytest.raises(BusinessError):
        await svc.delete_metric("sales_gmv_daily", actor_id=1)


async def test_get_versions():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.list_versions = AsyncMock(return_value=[MagicMock(version=1)])
    versions = await svc.get_versions("sales_gmv_daily")
    assert len(versions) == 1


async def test_confirm_version_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="PENDING")]
    )
    repo.update_confirmation_status = AsyncMock()
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric())
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()

    result = await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=9)
    assert result is not None


async def test_reject_version_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="PENDING")]
    )
    repo.update_confirmation_status = AsyncMock()
    svc._publish_event = AsyncMock()

    await svc.reject_version("sales_gmv_daily", version=1, consumer_id=9, reason="口径变更")
    assert repo.update_confirmation_status.await_count == 1


async def test_extend_version_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(
                id=1, consumer_id=9, status="PENDING", extension_count=0, deadline=None
            )
        ]
    )
    repo.extend_confirmation_deadline = AsyncMock(return_value=MagicMock(version=1))

    result = await svc.extend_version("sales_gmv_daily", version=1)
    assert result is not None


async def test_assert_owner_or_admin_rules():
    svc, _ = _svc_with_repo()
    metric = make_metric(owner_id=1, backup_owner_id=2)

    # admin 放行
    svc._assert_owner_or_admin(metric, actor_id=99, role="platform_admin")
    # owner 本人放行
    svc._assert_owner_or_admin(metric, actor_id=1, role="metric_owner")
    # backup owner 放行
    svc._assert_owner_or_admin(metric, actor_id=2, role="metric_owner")
    # 越权拒绝
    with pytest.raises(AuthError):
        svc._assert_owner_or_admin(metric, actor_id=3, role="metric_owner")
    # 无权限角色拒绝
    with pytest.raises(AuthError):
        svc._assert_owner_or_admin(metric, actor_id=1, role="viewer")


async def test_publish_pii_blocked():
    svc, repo = _svc_with_repo()
    metric = make_metric(pii_flag=True, compliance_reviewed=False)
    with pytest.raises(BusinessError) as exc:
        await svc._publish(metric, 1, actor_id=1)
    assert exc.value.error_code == "COMPLIANCE_BLOCKED"


async def test_publish_version_not_found():
    svc, repo = _svc_with_repo()
    repo.get_version = AsyncMock(return_value=None)
    metric = make_metric(pii_flag=False)
    with pytest.raises(NotFoundError):
        await svc._publish(metric, 99, actor_id=1)


async def test_compute_diff_detects_changes():
    svc, _ = _svc_with_repo()
    diff = svc._compute_diff({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4})
    assert "b" in diff
    assert diff["b"]["before"] == 2
    assert diff["b"]["after"] == 3


async def test_get_metric_health_critical_emits_event(monkeypatch):
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())

    class _FakeHealth:
        level = "CRITICAL"
        score = 30
        missing_dimensions = ["region"]

    class _FakeScorer:
        async def calculate(self, metric_id):
            return _FakeHealth()

    monkeypatch.setattr(
        "app.services.semantic.health_scorer.HealthScorer", lambda db: _FakeScorer()
    )
    svc._publish_event = AsyncMock()
    health = await svc.get_metric_health("sales_gmv_daily")
    assert health.level == "CRITICAL"
    assert svc._publish_event.call_args.args[0] == "metric.health_critical"


async def test_get_consumption_guide_generates_and_caches():
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get_guide = AsyncMock(return_value=None)
    svc._cache.set_guide = AsyncMock()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))

    guide = await svc.get_consumption_guide("sales_gmv_daily")
    assert guide["metric_code"] == "sales_gmv_daily"
    assert "recommended_usage" in guide
    svc._cache.set_guide.assert_awaited_once()


async def test_get_consumption_guide_cache_hit():
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get_guide = AsyncMock(return_value={"metric_code": "cached"})
    guide = await svc.get_consumption_guide("sales_gmv_daily")
    assert guide == {"metric_code": "cached"}
    repo.get_by_code.assert_not_called()


async def test_validate_domain_active_degraded(monkeypatch):
    """subject_domain 未配置（NotFoundError）→ 放行，不阻断创建。"""
    svc, _ = _svc_with_repo()

    class _Svc:
        async def validate_domain_active(self, code):
            raise NotFoundError("域未配置")

    monkeypatch.setattr(
        "app.services.subject_domain.service.SubjectDomainService", lambda db: _Svc()
    )
    # 不应抛异常
    await svc._validate_domain_active("sales")


async def test_get_domain_defaults_error_returns_empty(monkeypatch):
    svc, _ = _svc_with_repo()

    class _Boom:
        async def get_defaults(self, code):
            raise RuntimeError("DB down")

    monkeypatch.setattr(
        "app.services.subject_domain.service.SubjectDomainService", lambda db: _Boom()
    )
    assert await svc._get_domain_defaults("sales") == {}


async def test_validate_dict_fields_disabled_value_blocked(monkeypatch):
    """字典项已配置但停用 → BusinessError 拦截。"""
    svc, _ = _svc_with_repo()
    req = MetricCreateRequest(**make_create_payload())

    class _DictSvc:
        async def validate_dict_value(self, dict_type, code):
            raise BusinessError("字典项已停用", error_code="DICT_DISABLED")

    monkeypatch.setattr(
        "app.services.system_dict.service.SystemDictService", lambda db: _DictSvc()
    )
    with pytest.raises(BusinessError):
        await svc._validate_dict_fields(req)


async def test_validate_dict_fields_not_found_degrades(monkeypatch):
    """字典项未配置（NotFoundError）→ 放行。"""
    svc, _ = _svc_with_repo()
    req = MetricCreateRequest(**make_create_payload())

    class _DictSvc:
        async def validate_dict_value(self, dict_type, code):
            raise NotFoundError("未配置")

    monkeypatch.setattr(
        "app.services.system_dict.service.SystemDictService", lambda db: _DictSvc()
    )
    await svc._validate_dict_fields(req)


async def test_generate_metric_code_with_source_table(monkeypatch):
    """自动生成编码：源表+度量+周期齐全 → 4 段式。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)

    async def _gen(base, exists):
        return base

    monkeypatch.setattr("app.core.codegen.generate_unique_code", _gen)
    monkeypatch.setattr(
        "app.services.semantic.auto_fill.extract_biz_object", lambda t: "order"
    )
    monkeypatch.setattr(
        "app.services.semantic.auto_fill.extract_measure", lambda m: "amount"
    )
    req = MetricCreateRequest(
        **make_create_payload(metric_code=None, source_table="dwd_order", period="day")
    )
    code = await svc._generate_metric_code(req)
    assert code == "sales_order_amount_day"


# ---- 补充覆盖率：紧急发布 / 公共读路径 / 合规官可达性 ----


async def test_get_metric_public_from_db_and_cache():
    """get_metric_public：DB 回源 + 缓存命中双路径。"""
    from app.services.semantic.schemas import MetricResponse

    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()

    # DB 回源
    svc._cache.get = AsyncMock(return_value=None)
    svc._cache.set = AsyncMock()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    resp = await svc.get_metric_public("sales_gmv_daily")
    assert isinstance(resp, MetricResponse)
    svc._cache.set.assert_awaited_once()

    # 缓存命中（完整响应 dict）
    full = MetricResponse.model_validate(make_metric()).model_dump()
    svc._cache.get = AsyncMock(return_value=full)
    resp2 = await svc.get_metric_public("sales_gmv_daily")
    assert resp2.metric_code == "sales_gmv_daily"


async def test_get_metric_public_not_found():
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get = AsyncMock(return_value=None)
    repo.get_by_code = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.get_metric_public("missing")


async def test_get_metric_not_found():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.get_metric("missing")


async def test_emergency_publish_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", pii_flag=False))
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()
    svc._write_audit = AsyncMock()

    result = await svc.emergency_publish_metric(
        "sales_gmv_daily",
        MetricEmergencyPublishRequest(reason="生产系统故障需立即紧急发布处理", target_version=1),
        actor_id=1,
        role="domain_admin",
    )
    assert result.status == "PUBLISHED"
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["emergency_publish"] is True
    assert kwargs["emergency_reason"] == "生产系统故障需立即紧急发布处理"
    svc._write_audit.assert_awaited_once()


async def test_emergency_publish_forbidden_role():
    svc, repo = _svc_with_repo()
    with pytest.raises(Exception):
        await svc.emergency_publish_metric(
            "sales_gmv_daily",
            MetricEmergencyPublishRequest(reason="生产系统故障需立即紧急发布处理"),
            actor_id=1,
            role="metric_owner",
        )


async def test_emergency_publish_pii_blocked_with_officer(monkeypatch):
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", pii_flag=True, compliance_reviewed=False)
    )

    async def _officer_exists(domain):
        return True

    monkeypatch.setattr(svc, "_has_active_compliance_officer", _officer_exists)
    with pytest.raises(BusinessError) as exc:
        await svc.emergency_publish_metric(
            "sales_gmv_daily",
            MetricEmergencyPublishRequest(reason="生产系统故障需立即紧急发布处理"),
            actor_id=1,
            role="domain_admin",
        )
    assert exc.value.error_code == "COMPLIANCE_BLOCKED"


async def test_emergency_publish_pii_internal_downgrade(monkeypatch):
    """合规官不可达：仅 INTERNAL 分级可降级发布。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="DRAFT", pii_flag=True, compliance_reviewed=False, serving_mode="INTERNAL"
        )
    )
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()
    svc._write_audit = AsyncMock()

    async def _no_officer(domain):
        return False

    monkeypatch.setattr(svc, "_has_active_compliance_officer", _no_officer)
    result = await svc.emergency_publish_metric(
        "sales_gmv_daily",
        MetricEmergencyPublishRequest(reason="生产系统故障需立即紧急发布处理"),
        actor_id=1,
        role="domain_admin",
    )
    assert result.status == "PUBLISHED"


async def test_emergency_publish_pii_non_internal_blocked(monkeypatch):
    """合规官不可达且非 INTERNAL 分级 → 拦截。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="DRAFT", pii_flag=True, compliance_reviewed=False, serving_mode="BATCH_ONLY"
        )
    )

    async def _no_officer(domain):
        return False

    monkeypatch.setattr(svc, "_has_active_compliance_officer", _no_officer)
    with pytest.raises(BusinessError) as exc:
        await svc.emergency_publish_metric(
            "sales_gmv_daily",
            MetricEmergencyPublishRequest(reason="生产系统故障需立即紧急发布处理"),
            actor_id=1,
            role="domain_admin",
        )
    assert exc.value.error_code == "COMPLIANCE_UNREACHABLE_DOWNGRADE"


async def test_has_active_compliance_officer():
    """_has_active_compliance_officer：有活跃合规官返回 True，否则 False。"""
    svc, _ = _svc_with_repo()
    svc._db.execute = AsyncMock()

    # 无活跃合规官 → False
    svc._db.execute.return_value = MagicMock()
    svc._db.execute.return_value.scalar.return_value = 0
    assert await svc._has_active_compliance_officer("sales") is False

    # 有活跃合规官 → True
    svc._db.execute.return_value.scalar.return_value = 1
    assert await svc._has_active_compliance_officer("sales") is True
