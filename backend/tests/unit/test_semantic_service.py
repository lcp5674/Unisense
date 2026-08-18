"""语义服务单元测试（对齐 DEV_GUIDE §8b：纯逻辑、可独立运行）。

使用 mock 替换 MetricRepository，覆盖状态机、乐观锁、PII 合规闸门、
破坏性变更判定与分页计算。无数据库依赖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.conftest import make_create_payload, make_metric

from app.core.exceptions import (
    AuthError,
    BusinessError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.models.metric import Metric
from app.services.governance.policy import Decision
from app.services.semantic.schemas import (
    MetricApproveRequest,
    MetricCreateRequest,
    MetricDescriptionUpdateRequest,
    MetricEmergencyPublishRequest,
    MetricListParams,
    MetricRejectRequest,
    MetricSubmitRequest,
    MetricUpdateRequest,
)
from app.services.semantic.service import MetricService


def _svc_with_repo() -> tuple[MetricService, MagicMock]:
    """构造服务并替换其仓库为 mock，返回 (service, mock_repo_instance)。

    修复后：update_metric / submit_metric / approve_metric 调用
    GovernanceService.check_metric_permission，
    此处通过构造函数注入 mock 返回 allow=True，不阻断非 PDP 测试路径。
    """
    mock_gov_svc = MagicMock()
    mock_gov_svc.check_metric_permission = AsyncMock(
        return_value=Decision(allow=True, reason="mocked_allowed")
    )
    with patch("app.services.semantic.service.MetricRepository") as mock_repo_cls:
        svc = MetricService(db=MagicMock(), governance_svc=mock_gov_svc)
        # submit 路径会经 _notify_metric_stakeholders 开独立 DB 会话做定向通知，
        # 单元测试中会跨事件循环连库产生 RuntimeError——统一 mock 掉（通知行为另有集成测试）。
        svc._notify_metric_stakeholders = AsyncMock(return_value=None)
        # PENDING 确认期创建后定向通知消费方（同样跨事件循环，统一 mock）
        svc._notify_pending_consumers = AsyncMock(return_value=None)
        # PENDING 确认期检查（update_metric 破坏性变更前置）默认无待确认版本，
        # 个别测试覆盖为 True 验证防叠加。
        mock_repo_cls.return_value.has_pending_version = AsyncMock(return_value=False)
        # P2-14 owner 名称映射：默认空（对比/治理路径不依赖 owner 解析）；个别测试覆盖
        mock_repo_cls.return_value.get_user_display_names = AsyncMock(return_value={})
        return svc, mock_repo_cls.return_value


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


async def test_create_metric_merges_source_table_into_definition():
    """top-level source_table/measure_column 须合入 definition_json（血缘差异同步的消费键）。

    修复前：批量注册/后端构造路径只传 top-level source_table，definition_json 无
    source_table/measure_column → register_metric_from_definition 建不出「指标↔落地表」边
    （与前端单条 buildDefinitionJson 合入 ②源表/度量列的行为不一致）。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.create_metric(
        MetricCreateRequest(
            **make_create_payload(
                source_table="dwd.sales_detail",
                measure_column="order_amount",
                definition_json={"expression": "SUM(order_amount)", "dependencies": []},
            )
        ),
        owner_id=1,
    )

    captured = repo.create.call_args[0][0]
    defn = captured.definition_json
    assert defn["source_table"] == "dwd.sales_detail"
    assert defn["measure_column"] == "order_amount"
    # 显式声明的 source_tables 不被覆盖（保留调用方提供的源表集）
    assert "source_tables" not in defn


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


async def test_update_metric_published_non_breaking_syncs_effective_version():
    """P8 非破坏性 PUBLISHED 编辑：主表 version 与 effective_version 同步。

    修复前非破坏编辑只递增 version、不写 effective_version → 版本号与生效版本
    矛盾（出现永不转正的 DRAFT 版本）。非破坏性口径变更直接生效，生效版本=最新。
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED", row_version=1, version=2, effective_version=2
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", version=3, effective_version=3)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            # 非破坏性：改 source_fields（不在 BREAKING_DEF_FIELDS），expression 不变
            definition_json={**existing.definition_json, "source_fields": ["gmv", "channel"]},
            change_reason="补充来源字段（非破坏性）",
        ),
        actor_id=1,
        role="metric_owner",
    )

    lock_kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    # 非破坏性直接生效 → effective_version 同步到最新版本号
    assert lock_kwargs["effective_version"] == 3
    assert lock_kwargs["version"] == 3


async def test_update_metric_blocked_by_pdp_decision():
    """PDP 拒绝（check_metric_permission allow=False）→ 写操作被阻断，不落库。

    场景：actor 为 owner（通过 _assert_owner_or_admin），但 PDP 以跨域越权拒绝——
    验证 PDP 作为独立安全闸门在 owner 校验之上再拦截。
    """
    mock_gov_svc = MagicMock()
    mock_gov_svc.check_metric_permission = AsyncMock(
        return_value=Decision(allow=False, reason="跨域越权", error_code="FORBIDDEN_DOMAIN")
    )
    with patch("app.services.semantic.service.MetricRepository") as mock_repo_cls:
        svc = MetricService(db=MagicMock(), governance_svc=mock_gov_svc)
        repo = mock_repo_cls.return_value
        repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", row_version=1))
        repo.update_with_optimistic_lock = AsyncMock()

        with pytest.raises(BusinessError) as exc:
            await svc.update_metric(
                "sales_gmv_daily",
                MetricUpdateRequest(
                    definition_json={"expression": "SUM(order_amount)"},
                    change_reason="跨域尝试",
                ),
                actor_id=1,  # owner：通过 _assert_owner_or_admin
                role="metric_owner",
                user_domain="other_domain",
            )

        assert exc.value.error_code == "FORBIDDEN_DOMAIN"
        mock_gov_svc.check_metric_permission.assert_awaited_once()
        repo.update_with_optimistic_lock.assert_not_awaited()


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


async def test_approve_metric_clears_reject_reason():
    """指标一经发布清空历史驳回原因（生命周期闭环，approve_metric 统一路径）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="REVIEW",
            pii_flag=False,
            reject_reason="粒度与口径不符",
            reject_reviewer_id=3,
            rejected_at="2026-08-01 10:00:00",
        )
    )
    published = make_metric(status="PUBLISHED")
    repo.update_with_optimistic_lock = AsyncMock(return_value=published)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()
    svc._notify_metric_stakeholders = AsyncMock()

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(),
        actor_id=1,
        role="domain_admin",
    )

    assert result.status == "PUBLISHED"
    called = repo.update_with_optimistic_lock.call_args.kwargs
    assert called["reject_reason"] is None
    assert called["reject_reviewer_id"] is None
    assert called["rejected_at"] is None


async def test_approve_metric_invalid_status_rejected():
    """DRAFT 直接发布非法（须先 submit→REVIEW，再 approve→PUBLISHED）。"""
    svc, repo = _svc_with_repo()
    # DRAFT 直接发布非法（须先 submit→REVIEW，再 approve→PUBLISHED）
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT"))

    with pytest.raises(BusinessError):
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(),
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


async def test_deprecate_metric_self_successor_rejected():
    """自废弃防护：替代指标为指标自身时拒绝（废弃链不应指向自身，语义矛盾）。"""
    import pytest as _pytest

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))

    with _pytest.raises(BusinessError) as exc:
        await svc.deprecate_metric(
            "sales_gmv_daily", "sales_gmv_daily", actor_id=1, role="metric_owner"
        )
    assert exc.value.error_code == "VALIDATION_ERROR"
    # 自废弃在替代指标查询之前拦截：get_by_code 仅被调用一次（取指标自身），未查替代
    repo.get_by_code.assert_awaited_once()


async def test_deprecate_metric_blocked_by_pdp_cross_domain():
    """domain_admin 跨域废弃被 PDP 拒绝（deprecate 补 PDP 域校验，修复域隔离漏洞）。

    场景：domain_admin 已通过 _assert_owner_or_admin（admin 放行），但 PDP 以跨域越权
    拒绝——验证 deprecate 与 update/approve 一致的域权限闸门生效。
    """
    mock_gov_svc = MagicMock()
    mock_gov_svc.check_metric_permission = AsyncMock(
        return_value=Decision(
            allow=False, reason="跨域越权，无权废弃他域指标", error_code="FORBIDDEN_DOMAIN"
        )
    )
    with patch("app.services.semantic.service.MetricRepository") as mock_repo_cls:
        svc = MetricService(db=MagicMock(), governance_svc=mock_gov_svc)
        repo = mock_repo_cls.return_value
        repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
        repo.update_with_optimistic_lock = AsyncMock()

        with pytest.raises(BusinessError) as exc:
            await svc.deprecate_metric(
                "sales_gmv_daily",
                "sales_gmv_v2",
                actor_id=2,
                role="domain_admin",
                user_domain="finance",  # 跨域：指标在 sales 域
            )
        assert exc.value.error_code == "FORBIDDEN_DOMAIN"
        repo.update_with_optimistic_lock.assert_not_called()


async def test_deprecate_metric_empty_successor_direct_deprecate():
    """空替代指标（空串/None）直接废弃，不触发「替代指标不存在:（空）」误导错误。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    deprecated = make_metric(status="DEPRECATED", successor_code=None)
    repo.update_with_optimistic_lock = AsyncMock(return_value=deprecated)

    # 空串（前端未填替代指标提交）→ 应视为无替代直接废弃
    result = await svc.deprecate_metric("sales_gmv_daily", "", actor_id=1, role="metric_owner")

    assert result.status == "DEPRECATED"
    called = repo.update_with_optimistic_lock.call_args.kwargs
    assert called["successor_code"] is None  # 空串归一化为 None 落库
    # 未因空串触发替代指标不存在错误
    repo.get_by_code.assert_called_once()  # 仅查被废弃指标自身


async def test_deprecate_metric_none_successor_direct_deprecate():
    """None 替代指标直接废弃（无替代下线合法场景）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    deprecated = make_metric(status="DEPRECATED", successor_code=None)
    repo.update_with_optimistic_lock = AsyncMock(return_value=deprecated)

    result = await svc.deprecate_metric("sales_gmv_daily", None, actor_id=1, role="metric_owner")

    assert result.status == "DEPRECATED"
    called = repo.update_with_optimistic_lock.call_args.kwargs
    assert called["successor_code"] is None


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


async def test_list_metrics_passes_lifecycle_date_filters():
    """生命周期快筛日期参数透传（TD §13）：created_after/updated_before 传给 repository。"""
    from datetime import UTC, datetime

    svc, repo = _svc_with_repo()
    repo.list_metrics = AsyncMock(return_value=([make_metric()], 1))
    created_after = datetime(2026, 8, 1, tzinfo=UTC)
    updated_before = datetime(2026, 7, 1, tzinfo=UTC)

    await svc.list_metrics(
        MetricListParams(created_after=created_after, updated_before=updated_before)
    )

    called = repo.list_metrics.call_args.kwargs
    assert called["created_after"] == created_after
    assert called["updated_before"] == updated_before


async def test_is_breaking_change_detection():
    svc, _ = _svc_with_repo()
    old = {"expression": "SUM(a)", "dependencies": ["t1"]}
    same = {"expression": "SUM(a)", "dependencies": ["t1"]}
    diff = {"expression": "SUM(b)", "dependencies": ["t1"]}

    assert svc._is_breaking_change(old, same) is False
    assert svc._is_breaking_change(old, diff) is True


async def test_is_breaking_change_sql_mode():
    """SQL 模式口径变更与表达式同级触发破坏性判定（PENDING 确认期）。

    修复前 BREAKING_DEF_FIELDS 不含 sql/etl_sql：SQL 模式指标改口径被当
    非破坏性 UPDATE 直接生效，绕过 14 天消费方确认（治理漏洞）。
    """
    svc, _ = _svc_with_repo()
    old = {"sql": "SELECT SUM(amount) FROM sales", "source_tables": ["sales"]}
    same = {"sql": "SELECT SUM(amount) FROM sales", "source_tables": ["sales"]}
    diff = {
        "sql": "SELECT SUM(amount) FROM sales WHERE channel = 'APP'",
        "source_tables": ["sales"],
    }
    etl_old = {"etl_sql": "SELECT COUNT(*) FROM orders"}

    assert svc._is_breaking_change(old, same) is False
    assert svc._is_breaking_change(old, diff) is True
    assert (
        svc._is_breaking_change(
            etl_old, {"etl_sql": "SELECT COUNT(DISTINCT id) FROM orders"}
        )
        is True
    )


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


async def test_compare_metrics_archived_metric_raises_metric_archived():
    """跨服务一致性：对比中关联指标已因仲裁软删作废 → 抛 METRIC_ARCHIVED（携带 successor），
    而非对冲突仲裁弹窗直出裸「指标不存在」。"""
    svc, repo = _svc_with_repo()
    m2 = make_metric(metric_code="m2", definition_json={"expression": "SUM(y)"})
    # get_by_code 对 m1 返回 None（软删后不可见）；get_archived_by_code 命中软删 + successor
    repo.get_by_code = AsyncMock(side_effect=[None, m2])
    archived_m1 = make_metric(
        metric_code="m1",
        successor_code="m2",
    )
    archived_m1.arbitration_mark = {"status": "defeated"}
    repo.get_archived_by_code = AsyncMock(return_value=archived_m1)

    from app.core.error_codes import ErrorCode

    with pytest.raises(NotFoundError) as exc_info:
        await svc.compare_metrics("m1", "m2")
    assert exc_info.value.error_code == ErrorCode.METRIC_ARCHIVED
    assert exc_info.value.ctx["successor_code"] == "m2"
    assert exc_info.value.ctx["arbitration_mark"]["status"] == "defeated"


async def test_compare_matrix_all_identical():
    """多指标矩阵：三指标所有字段一致 → 每行 all_identical。"""
    svc, repo = _svc_with_repo()
    defn = {"expression": "SUM(x)", "dependencies": ["t1"]}
    m1 = make_metric(metric_code="m1", definition_json=defn, granularity="day")
    m2 = make_metric(metric_code="m2", definition_json=defn, granularity="day")
    m3 = make_metric(metric_code="m3", definition_json=defn, granularity="day")
    repo.get_by_code = AsyncMock(side_effect=[m1, m2, m3])

    result = await svc.compare_matrix(["m1", "m2", "m3"])
    assert result["metrics"] == ["m1", "m2", "m3"]
    for field in ("granularity", "unit", "definition", "dependencies"):
        assert result["fields"][field]["difference_level"] == "all_identical"
    # 依赖：全体交集为 t1，各指标无独有
    assert result["fields"]["dependencies"]["intersection"] == ["t1"]
    assert result["fields"]["dependencies"]["only"]["m1"] == []


async def test_compare_matrix_partial_and_different():
    """多指标矩阵：取值部分一致 → partial；取值互异 → all_different。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1", granularity="day")
    m2 = make_metric(metric_code="m2", granularity="day")
    m3 = make_metric(metric_code="m3", granularity="week")
    m4 = make_metric(metric_code="m4", granularity="month")
    repo.get_by_code = AsyncMock(side_effect=[m1, m2, m3, m4])

    result = await svc.compare_matrix(["m1", "m2", "m3", "m4"])
    # m1/m2 同为 day、m3=week、m4=month → 3 种取值、4 指标 → partial
    assert result["fields"]["granularity"]["difference_level"] == "partial"
    # unit 全部默认 yuan 一致
    assert result["fields"]["unit"]["difference_level"] == "all_identical"


async def test_compare_matrix_all_different_values():
    """多指标矩阵：每指标取值互异 → all_different。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1", granularity="day")
    m2 = make_metric(metric_code="m2", granularity="week")
    m3 = make_metric(metric_code="m3", granularity="month")
    repo.get_by_code = AsyncMock(side_effect=[m1, m2, m3])

    result = await svc.compare_matrix(["m1", "m2", "m3"])
    assert result["fields"]["granularity"]["difference_level"] == "all_different"


async def test_compare_matrix_dependencies():
    """多指标矩阵：依赖给出全体交集 + 各指标独有。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1", definition_json={"dependencies": ["t1", "t2"]})
    m2 = make_metric(metric_code="m2", definition_json={"dependencies": ["t1", "t2"]})
    m3 = make_metric(metric_code="m3", definition_json={"dependencies": ["t1"]})
    repo.get_by_code = AsyncMock(side_effect=[m1, m2, m3])

    result = await svc.compare_matrix(["m1", "m2", "m3"])
    deps = result["fields"]["dependencies"]
    assert deps["intersection"] == ["t1"]
    assert deps["only"]["m1"] == ["t2"]
    assert deps["only"]["m2"] == ["t2"]
    assert deps["only"]["m3"] == []
    # 依赖列表仅两种取值（t1/t2 与 t1）、3 指标 → partial
    assert deps["difference_level"] == "partial"


async def test_compare_matrix_dedup_preserves_order():
    """多指标矩阵：重复编码去重且保持首次出现顺序。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1")
    m2 = make_metric(metric_code="m2")

    def fake_get(code: str):
        return {"m1": m1, "m2": m2}[code]

    repo.get_by_code = AsyncMock(side_effect=fake_get)

    result = await svc.compare_matrix(["m2", "m1", "m2"])
    assert result["metrics"] == ["m2", "m1"]


async def test_compare_matrix_includes_governance_fields():
    """矩阵对比治理字段补全（P2-14）：PII/合规复核/状态/版本/描述入矩阵，owner 名称可读化。

    治理对比高价值信号（敏感分级不同、责任人不同）此前被挡在矩阵之外，本测试守护
    pii_flag/compliance_reviewed/status/version/description 五类字段行 + owner_names 映射。
    """
    svc, repo = _svc_with_repo()
    m1 = make_metric(
        metric_code="m1", pii_flag=True, compliance_reviewed=True,
        status="PUBLISHED", version=3, description="销售额", owner_id=1,
    )
    m2 = make_metric(
        metric_code="m2", pii_flag=False, compliance_reviewed=False,
        status="PUBLISHED", version=1, description="订单量", owner_id=1,
    )
    repo.get_by_code = AsyncMock(side_effect=[m1, m2])
    # owner 名称映射：owner_id=1 → 张三（走 repository 解析）
    repo.get_user_display_names = AsyncMock(return_value={1: "张三"})

    result = await svc.compare_matrix(["m1", "m2"])
    fields = result["fields"]
    # 五类治理字段全部入矩阵
    assert fields["pii_flag"]["values"] == {"m1": True, "m2": False}
    assert fields["pii_flag"]["difference_level"] == "all_different"
    assert fields["compliance_reviewed"]["values"] == {"m1": True, "m2": False}
    assert fields["status"]["values"] == {"m1": "PUBLISHED", "m2": "PUBLISHED"}
    assert fields["status"]["difference_level"] == "all_identical"
    assert fields["version"]["values"] == {"m1": 3, "m2": 1}
    assert fields["description"]["values"] == {"m1": "销售额", "m2": "订单量"}
    # owner 可读化：owner_names 映射携带责任人姓名
    assert result["owner_names"] == {1: "张三"}


async def test_compare_matrix_owner_names_skip_when_no_owner():
    """矩阵对比 owner 无责任人或无法解析：owner_names 为空映射，不阻断对比（P2-14）。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1", owner_id=None)
    m2 = make_metric(metric_code="m2", owner_id=None)
    repo.get_by_code = AsyncMock(side_effect=[m1, m2])

    result = await svc.compare_matrix(["m1", "m2"])
    assert result["owner_names"] == {}
    assert "granularity" in result["fields"]


async def test_compare_matrix_archived_metric_raises():
    """多指标矩阵：任一指标已因仲裁软删作废 → 抛 METRIC_ARCHIVED（携带 successor）。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="m1")
    repo.get_by_code = AsyncMock(side_effect=[None, m1])
    archived_m2 = make_metric(metric_code="m2", successor_code="m1")
    archived_m2.arbitration_mark = {"status": "defeated"}
    repo.get_archived_by_code = AsyncMock(return_value=archived_m2)

    from app.core.error_codes import ErrorCode

    with pytest.raises(NotFoundError) as exc_info:
        await svc.compare_matrix(["m2", "m1"])
    assert exc_info.value.error_code == ErrorCode.METRIC_ARCHIVED
    assert exc_info.value.ctx["successor_code"] == "m1"


async def test_compare_matrix_too_many_raises():
    """多指标矩阵：超过 6 个 → 抛中文 ValidationError（而非裸 Pydantic 422）。"""
    svc, repo = _svc_with_repo()
    with pytest.raises(ValidationError) as exc_info:
        await svc.compare_matrix([f"m{i}" for i in range(7)])
    assert "2~6 个" in exc_info.value.message
    assert "7" in exc_info.value.message
    # 校验发生在查询前：不应访问 repository
    repo.get_by_code.assert_not_called()


async def test_compare_matrix_too_few_raises():
    """多指标矩阵：少于 2 个 → 抛中文 ValidationError。"""
    svc, repo = _svc_with_repo()
    with pytest.raises(ValidationError) as exc_info:
        await svc.compare_matrix(["m1"])
    assert "2~6 个" in exc_info.value.message
    repo.get_by_code.assert_not_called()


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


async def test_batch_register_db_error_savepoint_continues():
    """P13 批量注册单列 DB 错误：savepoint 隔离，仅该列失败、后续列继续。

    修复前 SQLAlchemyError 走整会话 rollback + 中止剩余列，已 flush 的候选被回滚
    但 candidates 仍记为 DRAFT → 部分结果失真。修复后逐列 begin_nested 隔离。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=make_metric())
    repo.create_version = AsyncMock(return_value=MagicMock())

    from sqlalchemy.exc import IntegrityError

    from app.services.semantic.schemas import MetricBatchRegisterRequest

    request = MetricBatchRegisterRequest(
        source_table="dwd.sales_detail",
        measure_columns=["ok_col", "bad_col"],
        dimension_mapping={"domain": "sales"},
        llm_prefill=True,
        domain="sales",
    )

    real_create = svc.create_metric

    async def _flaky_create(req, **kw):
        # 第 2 列模拟唯一键冲突（DB 级 IntegrityError）
        if getattr(req, "measure_column", None) == "bad_col":
            raise IntegrityError("stmt", {}, Exception("duplicate key"))
        return await real_create(req, **kw)

    svc.create_metric = _flaky_create  # type: ignore[method-assign]

    # savepoint 语义：MagicMock 的 async with 行为不可靠（吞异常），
    # 用真实 asynccontextmanager 模拟 begin_nested——异常从 yield 抛出进外层 except
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_nested():
        yield

    svc._db.begin_nested = _fake_nested  # type: ignore[method-assign]

    result = await svc.batch_register_metrics(request, actor_id=1)

    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["status"] == "DRAFT"  # ok_col 成功
    assert result["candidates"][1]["status"] == "VALIDATION_ERROR"  # bad_col 被 savepoint 隔离捕获
    assert "已跳过该列" in result["candidates"][1]["validation_errors"]


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
        MetricCreateRequest(**make_create_payload(metric_code=None)),
        owner_id=1,
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
        MetricCreateRequest(**make_create_payload(metric_code=None)),
        owner_id=1,
    )

    assert result.metric_code == "sales_entity_value_day_2"


async def test_create_metric_explicit_code_still_validated():
    """显式传入 metric_code 仍走 4 段式校验（ConflictError 之外，非法格式 422）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)

    with pytest.raises(ValueError):
        MetricCreateRequest(**make_create_payload(metric_code="bad_code"))


# ---- approve/reject 自审豁免（管理员可审核自己提交的指标，普通角色仍禁止）----


def _svc_approve_ready(
    svc,
    repo,
    *,
    submitted_by: int = 1,
    status: str = "REVIEW",
    reviewer_id: int | None = None,
    reviewer_type: str | None = None,
    reviewer_domain: str | None = None,
):
    """准备 approve/reject 所需的 mock 环境，返回指标。"""
    metric = make_metric(
        status=status,
        submitted_by=submitted_by,
        owner_id=2,
        reviewer_id=reviewer_id,
        reviewer_type=reviewer_type,
        reviewer_domain=reviewer_domain,
    )
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
    """普通角色（reviewer）即便被指派为评审人，自审仍被禁止。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=1, reviewer_type="user")

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
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=1, reviewer_type="user")

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=1,
        )
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"


async def test_approve_metric_different_actor_still_allowed():
    """被指派评审人（非提交人）审核通过——评审人校验放行 + 不触发自审。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=99, reviewer_type="user")

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="standard", target_version=1),
        actor_id=99,
        role="reviewer",
    )

    assert result.status == "PUBLISHED"


async def test_approve_metric_gray_publishes_gray_event():
    """灰度发布（mode=experimental）→ 状态 EXPERIMENTAL + 发 metric.gray_published 事件（含租户）。

    灰度仅试点指定租户，通知语义须与标准发布区分，避免 stakeholders 收到
    「指标已通过」却实际仅灰度。R35 修复：灰度发独立事件与标题。
    """
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=99, reviewer_type="user")
    svc._publish_event = AsyncMock()

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="experimental", gray_tenant_ids=[101, 102], target_version=1),
        actor_id=99,
        role="reviewer",
    )

    assert result is not None  # mock 固定返回 PUBLISHED，此处仅验证事件语义
    event_type, payload = svc._publish_event.call_args.args[:2]
    assert event_type == "metric.gray_published"
    assert payload["gray_tenant_ids"] == [101, 102]


async def test_approve_metric_standard_publishes_approved_event():
    """标准发布（mode=standard）→ 发 metric.approved 事件（灰度事件不误发）。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=99, reviewer_type="user")
    svc._publish_event = AsyncMock()

    await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="standard", target_version=1),
        actor_id=99,
        role="reviewer",
    )

    event_type = svc._publish_event.call_args.args[0]
    assert event_type == "metric.approved"


# ---- 评审指派（TD §13）：提交保存指派 + 仅被指派评审人可通过/打回 ----


async def test_submit_metric_saves_user_reviewer():
    """提交评审指定用户评审人：reviewer_id/reviewer_type 落库。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))
    svc._publish_event = AsyncMock()

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="提交审核", reviewer_id=7, reviewer_type="user"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["reviewer_id"] == 7
    assert kwargs["reviewer_type"] == "user"
    assert kwargs["reviewer_domain"] is None


async def test_submit_metric_clears_reject_reason():
    """被驳回草稿重新提审时清空历史驳回原因（生命周期闭环）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="DRAFT",
            owner_id=1,
            reject_reason="粒度与口径不符",
            reject_reviewer_id=3,
            rejected_at="2026-08-01 10:00:00",
        )
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))
    svc._publish_event = AsyncMock()

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="已修正粒度，重新提审"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["reject_reason"] is None
    assert kwargs["reject_reviewer_id"] is None
    assert kwargs["rejected_at"] is None


async def test_submit_metric_domain_reviewer_defaults_to_metric_domain():
    """域评审组未指定域时缺省用指标自身域。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(status="DRAFT", owner_id=1, domain="sales")
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))
    svc._publish_event = AsyncMock()

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="提交审核", reviewer_type="domain"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["reviewer_type"] == "domain"
    assert kwargs["reviewer_domain"] == "sales"


async def test_submit_metric_user_reviewer_without_id_rejected():
    """指定 user 评审类型但未填评审人 → REVIEWER_ASSIGN_INVALID。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))

    with pytest.raises(BusinessError) as exc:
        await svc.submit_metric(
            "sales_gmv_daily",
            MetricSubmitRequest(change_reason="提交审核", reviewer_type="user"),
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "REVIEWER_ASSIGN_INVALID"


async def test_approve_metric_non_assigned_reviewer_rejected():
    """已指派评审用户，非被指派者（且非 platform_admin）通过 → FORBIDDEN_REVIEWER。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=7, reviewer_type="user")

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=99,
            role="reviewer",
        )
    assert exc.value.error_code == "FORBIDDEN_REVIEWER"
    repo.update_with_optimistic_lock.assert_not_awaited()


async def test_approve_metric_domain_team_same_domain_allowed():
    """域评审组：同域 reviewer 角色可通过。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_type="domain", reviewer_domain="sales")

    result = await svc.approve_metric(
        "sales_gmv_daily",
        MetricApproveRequest(mode="standard", target_version=1),
        actor_id=50,
        role="reviewer",
        user_domain="sales",
    )
    assert result.status == "PUBLISHED"


async def test_approve_metric_domain_team_wrong_domain_rejected():
    """域评审组：非该域用户（即便 reviewer 角色）→ FORBIDDEN_REVIEWER。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_type="domain", reviewer_domain="sales")

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=50,
            role="reviewer",
            user_domain="finance",
        )
    assert exc.value.error_code == "FORBIDDEN_REVIEWER"


async def test_approve_metric_domain_team_non_reviewer_role_rejected():
    """域评审组：非 domain_admin/reviewer 角色（如 metric_owner）→ FORBIDDEN_REVIEWER。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_type="domain", reviewer_domain="sales")

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=50,
            role="metric_owner",
            user_domain="sales",
        )
    assert exc.value.error_code == "FORBIDDEN_REVIEWER"


async def test_approve_metric_unassigned_non_domain_admin_rejected():
    """未指派评审人：非 domain_admin（reviewer 角色）→ FORBIDDEN_REVIEWER。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1)

    with pytest.raises(BusinessError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(mode="standard", target_version=1),
            actor_id=50,
            role="reviewer",
            user_domain="sales",
        )
    assert exc.value.error_code == "FORBIDDEN_REVIEWER"


async def test_reject_metric_non_assigned_reviewer_rejected():
    """已指派评审用户，非被指派者打回 → FORBIDDEN_REVIEWER。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=7, reviewer_type="user")

    with pytest.raises(BusinessError) as exc:
        await svc.reject_metric(
            "sales_gmv_daily",
            MetricRejectRequest(reason="口径需调整"),
            actor_id=99,
            role="reviewer",
        )
    assert exc.value.error_code == "FORBIDDEN_REVIEWER"
    repo.update_with_optimistic_lock.assert_not_awaited()


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
    """普通角色即便被指派为评审人，自驳回仍被禁止。"""
    svc, repo = _svc_with_repo()
    _svc_approve_ready(svc, repo, submitted_by=1, reviewer_id=1, reviewer_type="user")

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

    out = redact_definition({"expr": "SUM(x)", "nested": {"a": 1}, "arr": ["x", "y"], "flag": True})
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


async def test_submit_metric_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    updated = make_metric(status="REVIEW")
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    result = await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="提交审核"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )
    assert result.status == "REVIEW"
    assert repo.update_with_optimistic_lock.call_args.kwargs["submitted_by"] == 1


async def test_submit_metric_blocked_when_definition_empty():
    """空心指标（无表达式/无SQL/无源表）提交评审被拦截——口径完整性校验（FR-012）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", owner_id=1, definition_json={})
    )

    with pytest.raises(BusinessError) as exc:
        await svc.submit_metric(
            "sales_gmv_daily",
            MetricSubmitRequest(change_reason="提交审核"),
            actor_id=1,
            role="metric_owner",
            user_domain="sales",
        )
    assert exc.value.error_code == "DEFINITION_INCOMPLETE"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_submit_metric_blocked_by_pdp_decision():
    """PDP 拒绝 → submit_metric 不提交审核。"""
    mock_gov_svc = MagicMock()
    mock_gov_svc.check_metric_permission = AsyncMock(
        return_value=Decision(allow=False, reason="无 write 权限", error_code="FORBIDDEN")
    )
    with patch("app.services.semantic.service.MetricRepository") as mock_repo_cls:
        svc = MetricService(db=MagicMock(), governance_svc=mock_gov_svc)
        repo = mock_repo_cls.return_value
        repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
        repo.update_with_optimistic_lock = AsyncMock()

        with pytest.raises(BusinessError) as exc:
            await svc.submit_metric(
                "sales_gmv_daily",
                MetricSubmitRequest(change_reason="提交审核"),
                actor_id=1,
                role="metric_owner",
                user_domain="other_domain",
            )

        assert exc.value.error_code == "FORBIDDEN"
        repo.update_with_optimistic_lock.assert_not_awaited()


async def test_submit_metric_publishes_event():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))
    svc._publish_event = AsyncMock()

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="提交审核"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )
    svc._publish_event.assert_awaited_once()
    assert svc._publish_event.call_args.args[0] == "metric.submitted"


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
    metric = make_metric(status="REVIEW", owner_id=1, reviewer_id=1, reviewer_type="user")
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

    result = await svc.promote_metric("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert result.status == "PUBLISHED"
    assert svc._publish_event.call_args.args[0] == "metric.promoted"


async def test_rollback_metric_success():
    svc, repo = _svc_with_repo()
    metric = make_metric(status="EXPERIMENTAL", owner_id=1, version=2, effective_version=2)
    repo.get_by_code = AsyncMock(return_value=metric)
    prev_pub = MagicMock(
        version=1, status="PUBLISHED", definition_json={"expression": "sum(amount)"}
    )
    repo.list_versions = AsyncMock(return_value=[prev_pub])
    # 当前灰度版本的 diff_json 记录 granularity 的 before（供回滚恢复 top-level 字段）
    gray_diff = MagicMock(
        version=2, status="EXPERIMENTAL", definition_json={"expression": "avg(amount)"},
        diff_json={"granularity": {"before": "day", "after": "hour", "change_type": "breaking"}},
    )
    repo.get_version = AsyncMock(return_value=gray_diff)
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_archived = AsyncMock()
    svc._db.execute = AsyncMock()
    svc._publish_event = AsyncMock()
    svc._register_metric_lineage_full = AsyncMock()

    result = await svc.rollback_metric("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert result.status == "PUBLISHED"
    assert svc._publish_event.call_args.args[0] == "metric.rolled_back"
    # 回滚恢复上一 PUBLISHED 口径 + 版本号回退 + top-level before 值
    _kw = repo.update_with_optimistic_lock.call_args.kwargs
    assert _kw["definition_json"] == {"expression": "sum(amount)"}
    assert _kw["version"] == 1
    assert _kw["effective_version"] == 1
    assert _kw["granularity"] == "day"
    # 回滚后血缘按恢复口径差异同步
    svc._register_metric_lineage_full.assert_awaited_once()


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


async def test_restore_metric_success_and_guards():
    # 平台管理员恢复已删草稿
    svc, repo = _svc_with_repo()
    repo.get_archived_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", owner_id=2, deleted_at="2026-08-01T00:00:00")
    )
    repo.restore_metric = AsyncMock()
    result = await svc.restore_metric("sales_gmv_daily", actor_id=1, role="platform_admin")
    assert result.status == "DRAFT"
    repo.restore_metric.assert_awaited_once()

    # 原 owner（非管理员）也可恢复
    repo.get_archived_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", owner_id=2, deleted_at="2026-08-01T00:00:00")
    )
    await svc.restore_metric("sales_gmv_daily", actor_id=2, role="metric_owner")

    # 非 owner 非管理员 → 拒绝
    repo.get_archived_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", owner_id=2, deleted_at="2026-08-01T00:00:00")
    )
    with pytest.raises(BusinessError) as e:
        await svc.restore_metric("sales_gmv_daily", actor_id=9, role="metric_owner")
    assert "仅平台管理员或指标原 Owner" in str(e.value)

    # 未删状态 → 拒绝
    repo.get_archived_by_code = AsyncMock(return_value=make_metric(status="DRAFT", owner_id=1))
    with pytest.raises(BusinessError):
        await svc.restore_metric("sales_gmv_daily", actor_id=1, role="platform_admin")

    # 非 DRAFT 已删 → 拒绝
    repo.get_archived_by_code = AsyncMock(
        return_value=make_metric(status="PUBLISHED", owner_id=1, deleted_at="2026-08-01T00:00:00")
    )
    with pytest.raises(BusinessError):
        await svc.restore_metric("sales_gmv_daily", actor_id=1, role="platform_admin")


async def test_get_versions():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.list_versions = AsyncMock(return_value=[MagicMock(version=1)])
    versions = await svc.get_versions("sales_gmv_daily")
    assert len(versions) == 1


async def test_get_version_responses_injects_confirm_progress():
    """版本历史响应注入多消费方确认进度（已确认 X/N）——修复前无进度字段，
    一方确认后另一方未确认、版本迟迟不转正时无法判断还差谁。"""
    from app.models.metric_version import MetricVersion

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    pending = MetricVersion(
        id=1,
        metric_id=5,
        version=2,
        status="PENDING_CONFIRMATION",
        change_type="breaking",
        definition_json={"expression": "SUM(x)"},
        change_reason="口径调整",
        created_by=9,
        created_at=datetime(2026, 8, 1, 10, 0, 0),
    )
    published = MetricVersion(
        id=2,
        metric_id=5,
        version=1,
        status="PUBLISHED",
        change_type="initial",
        definition_json={"expression": "SUM(x)"},
        change_reason="初版",
        created_by=9,
        created_at=datetime(2026, 7, 1, 10, 0, 0),
    )
    repo.list_versions = AsyncMock(return_value=[pending, published])
    repo.count_confirmations_by_versions = AsyncMock(
        return_value={2: (1, 2)}  # 版本 2：1/2 消费方已确认
    )
    responses = await svc.get_version_responses("sales_gmv_daily")
    assert len(responses) == 2
    # PENDING 版本带进度；已发布版本不带
    by_version = {r.version: r for r in responses}
    assert by_version[2].confirmed_count == 1
    assert by_version[2].consumer_count == 2
    assert by_version[1].confirmed_count is None
    assert by_version[1].consumer_count is None


async def test_get_version_responses_no_pending_skips_progress_query():
    """无 PENDING 版本时不做进度查询（避免无谓 DB 调用）。"""
    from app.models.metric_version import MetricVersion

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    published = MetricVersion(
        id=2,
        metric_id=5,
        version=1,
        status="PUBLISHED",
        change_type="initial",
        definition_json={"expression": "SUM(x)"},
        change_reason="初版",
        created_by=9,
        created_at=datetime(2026, 7, 1, 10, 0, 0),
    )
    repo.list_versions = AsyncMock(return_value=[published])
    repo.count_confirmations_by_versions = AsyncMock()
    responses = await svc.get_version_responses("sales_gmv_daily")
    assert len(responses) == 1
    repo.count_confirmations_by_versions.assert_not_called()


async def test_confirm_version_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="PENDING")]
    )
    repo.update_confirmation_status = AsyncMock()
    repo.get_version = AsyncMock(
        return_value=MagicMock(
            definition_json={"expression": "SUM(x)"},
            diff_json={"granularity": {"before": "daily", "after": "hourly"}},
        )
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric())
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()
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
    svc._db.execute = AsyncMock()
    svc._publish_event = AsyncMock()

    await svc.reject_version("sales_gmv_daily", version=1, consumer_id=9, reason="口径变更")
    assert repo.update_confirmation_status.await_count == 1
    # 被拒版本置 CANCELLED（DB 更新执行）
    svc._db.execute.assert_awaited_once()


async def test_reject_version_terminates_other_confirmations():
    """拒绝后终结该版本其他消费方的 PENDING 确认记录——修复前其他记录残留
    PENDING，pending_version 计算字段（status==PENDING）持续识别为待确认，
    前端警示最长残留 14 天；修复后全部确认记录终止，警示立即消失。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    # A 拒绝（mine）、B 仍 PENDING（应被终结）、C 已 CONFIRMED（保持）
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(id=1, consumer_id=9, status="PENDING"),
            MagicMock(id=2, consumer_id=10, status="PENDING"),
            MagicMock(id=3, consumer_id=11, status="CONFIRMED"),
        ]
    )
    repo.update_confirmation_status = AsyncMock()
    svc._db.execute = AsyncMock()
    svc._publish_event = AsyncMock()

    await svc.reject_version("sales_gmv_daily", version=1, consumer_id=9, reason="口径变更")

    # 当前拒绝者(id=1) + 其他 PENDING 记录(id=2) 被终结为 REJECTED；
    # 已 CONFIRMED 的(id=3)保持不动（不覆盖已确认事实）
    assert repo.update_confirmation_status.await_count == 2
    assert repo.update_confirmation_status.await_args.args[0] == 2  # id=2 的记录被终结
    assert repo.update_confirmation_status.await_args.args[1] == "REJECTED"



async def test_extend_version_success():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(id=1, consumer_id=9, status="PENDING", extension_count=0, deadline=None)
        ]
    )
    repo.extend_confirmation_deadline = AsyncMock(return_value=MagicMock(version=1))

    result = await svc.extend_version("sales_gmv_daily", version=1, actor_id=1, role="metric_owner")
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


async def test_get_metric_health_archived_raises_metric_archived():
    """health 直访已作废指标（软删 + successor）→ 结构化 METRIC_ARCHIVED（与详情读路径一致）。"""
    from app.core.error_codes import ErrorCode

    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    archived = make_metric(
        metric_code="sales_e2e_conflictb_day",
        successor_code="sales_e2e_conflicta_day",
    )
    archived.deleted_at = None  # 软删态由调用方保证；命中 archived 分支仅需 successor 存在
    archived.arbitration_mark = {"status": "defeated"}
    repo.get_archived_by_code = AsyncMock(return_value=archived)

    with pytest.raises(NotFoundError) as exc_info:
        await svc.get_metric_health("sales_e2e_conflictb_day")
    assert exc_info.value.error_code == ErrorCode.METRIC_ARCHIVED
    assert exc_info.value.ctx["successor_code"] == "sales_e2e_conflicta_day"
    assert exc_info.value.ctx["arbitration_mark"]["status"] == "defeated"


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
    """字典项已配置但停用 → BusinessError 拦截（P1-5 语义：类型有 active 项才校验）。"""
    svc, _ = _svc_with_repo()
    req = MetricCreateRequest(**make_create_payload())

    class _DictSvc:
        async def list_by_type(self, dict_type, status="active"):
            return [MagicMock()]  # 类型已配置（有 active 项）

        async def validate_dict_value(self, dict_type, code):
            raise BusinessError("字典项已停用", error_code="DICT_DISABLED")

    monkeypatch.setattr("app.services.system_dict.service.SystemDictService", lambda db: _DictSvc())
    with pytest.raises(BusinessError):
        await svc._validate_dict_fields(req)


async def test_validate_dict_fields_configured_but_value_missing_blocked(monkeypatch):
    """P1-5：类型已配置但值不存在 → NotFoundError 拦截（不再静默放行脏值）。"""
    svc, _ = _svc_with_repo()
    req = MetricCreateRequest(**make_create_payload())

    class _DictSvc:
        async def list_by_type(self, dict_type, status="active"):
            return [MagicMock()]  # 类型已配置

        async def validate_dict_value(self, dict_type, code):
            raise NotFoundError("字典值不存在", error_code="DICT_VALUE_NOT_FOUND")

    monkeypatch.setattr("app.services.system_dict.service.SystemDictService", lambda db: _DictSvc())
    with pytest.raises(NotFoundError):
        await svc._validate_dict_fields(req)


async def test_validate_dict_fields_unconfigured_type_passes(monkeypatch):
    """P1-5：类型完全未配置（空表/未种子，无 active 项）→ 放行，不阻断创建。"""
    svc, _ = _svc_with_repo()
    req = MetricCreateRequest(**make_create_payload())

    class _DictSvc:
        async def list_by_type(self, dict_type, status="active"):
            return []  # 类型未配置 → 该类型放行

        async def validate_dict_value(self, dict_type, code):
            raise AssertionError("类型未配置时不应走到值校验")

    monkeypatch.setattr("app.services.system_dict.service.SystemDictService", lambda db: _DictSvc())
    await svc._validate_dict_fields(req)  # 不应抛异常


async def test_generate_metric_code_with_source_table(monkeypatch):
    """自动生成编码：源表+度量+周期齐全 → 4 段式。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)

    async def _gen(base, exists):
        return base

    monkeypatch.setattr("app.core.codegen.generate_unique_code", _gen)
    monkeypatch.setattr("app.services.semantic.auto_fill.extract_biz_object", lambda t: "order")
    monkeypatch.setattr("app.services.semantic.auto_fill.extract_measure", lambda m: "amount")
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
    repo.get_archived_by_code = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.get_metric_public("missing")


async def test_get_metric_public_archived_raises_metric_archived():
    """详情直访已作废指标（软删 + successor）→ 结构化 METRIC_ARCHIVED，携带胜方指针。"""
    from app.core.error_codes import ErrorCode

    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get = AsyncMock(return_value=None)
    repo.get_by_code = AsyncMock(return_value=None)
    archived = make_metric(
        metric_code="sales_e2e_conflictb_day",
        successor_code="sales_e2e_conflicta_day",
    )
    archived.deleted_at = None  # 触发 archived 分支仅需 successor 存在；软删态由调用方保证
    archived.arbitration_mark = {
        "status": "defeated",
        "conflict_id": "CF-ABC",
        "opposite_code": "sales_e2e_conflicta_day",
    }
    repo.get_archived_by_code = AsyncMock(return_value=archived)

    with pytest.raises(NotFoundError) as exc_info:
        await svc.get_metric_public("sales_e2e_conflictb_day")

    assert exc_info.value.error_code == ErrorCode.METRIC_ARCHIVED
    assert exc_info.value.ctx["successor_code"] == "sales_e2e_conflicta_day"
    assert exc_info.value.ctx["metric_code"] == "sales_e2e_conflictb_day"
    assert exc_info.value.ctx["arbitration_mark"]["status"] == "defeated"


async def test_get_archived_metric_public_returns_detail_with_successor():
    """作废详情端点：返回完整历史口径 + successor 指针 + 裁决标记（供作废引导页展示）。"""
    svc, repo = _svc_with_repo()
    archived = make_metric(
        metric_code="sales_e2e_conflictb_day",
        name="E2E 冲突指标 B",
        successor_code="sales_e2e_conflicta_day",
        definition_json={"expression": "sum(order_amount)", "sql": "SELECT 1"},
    )
    archived.arbitration_mark = {
        "status": "defeated",
        "decision": "merge",
        "conflict_id": "CF-ABC",
        "opposite_code": "sales_e2e_conflicta_day",
    }
    repo.get_archived_by_code = AsyncMock(return_value=archived)

    data = await svc.get_archived_metric_public("sales_e2e_conflictb_day")

    assert data["successor_code"] == "sales_e2e_conflicta_day"
    assert data["arbitration_mark"]["decision"] == "merge"
    assert data["metric"].metric_code == "sales_e2e_conflictb_day"
    assert data["metric"].name == "E2E 冲突指标 B"
    assert data["metric"].definition_json["expression"] == "sum(order_amount)"
    repo.get_archived_by_code.assert_awaited_once_with("sales_e2e_conflictb_day")


async def test_get_archived_metric_public_missing_raises_not_found():
    """作废详情端点：指标不存在或未作废 → 仍抛 NOT_FOUND（非 METRIC_ARCHIVED）。"""
    svc, repo = _svc_with_repo()
    repo.get_archived_by_code = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError) as exc_info:
        await svc.get_archived_metric_public("missing")

    assert exc_info.value.error_code == "NOT_FOUND"


async def test_get_metric_public_deleted_without_successor_still_not_found():
    """软删但无 successor（手动删除/其他作废路径）→ 仍按普通 NOT_FOUND 处理。"""
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get = AsyncMock(return_value=None)
    repo.get_by_code = AsyncMock(return_value=None)
    archived = make_metric(metric_code="gone")
    archived.deleted_at = None  # 软删但无 successor_code（make_metric 默认 None）
    repo.get_archived_by_code = AsyncMock(return_value=archived)

    with pytest.raises(NotFoundError) as exc_info:
        await svc.get_metric_public("gone")
    assert exc_info.value.error_code == "NOT_FOUND"


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
    with pytest.raises(BusinessError):
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


async def test_complete_emergency_review_success():
    """P1-6: 紧急发布补审——写 emergency_reviewed_at，发布事件。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="PUBLISHED", emergency_publish=True, emergency_reviewed_at=None
        )
    )
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", emergency_publish=True)
    )
    svc._publish_event = AsyncMock()

    result = await svc.complete_emergency_review("sales_gmv_daily", actor_id=1, role="domain_admin")

    assert result.status == "PUBLISHED"
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["emergency_reviewed_at"] is not None
    svc._publish_event.assert_awaited_once()
    event_type = svc._publish_event.await_args.args[0]
    assert event_type == "metric.emergency_reviewed"


async def test_complete_emergency_review_forbidden_role():
    """非管理角色补审 → 拒绝（与紧急发布同角色门禁）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="PUBLISHED", emergency_publish=True)
    )
    with pytest.raises(AuthError) as exc:
        await svc.complete_emergency_review("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert exc.value.error_code == "FORBIDDEN"


async def test_complete_emergency_review_not_emergency():
    """非紧急发布指标 → 拒绝补审。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="PUBLISHED", emergency_publish=False)
    )
    with pytest.raises(ConflictError) as exc:
        await svc.complete_emergency_review("sales_gmv_daily", actor_id=1, role="domain_admin")
    assert exc.value.error_code == "NOT_EMERGENCY_PUBLISHED"


async def test_complete_emergency_review_already_done():
    """已完成补审 → 拒绝重复补审。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="PUBLISHED", emergency_publish=True, emergency_reviewed_at=datetime.now(UTC)
        )
    )
    with pytest.raises(ConflictError) as exc:
        await svc.complete_emergency_review("sales_gmv_daily", actor_id=1, role="domain_admin")
    assert exc.value.error_code == "EMERGENCY_ALREADY_REVIEWED"


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


# =====================================================================
# 覆盖率补齐：semantic/service.py 84% → 90%+（未覆盖分支）
# =====================================================================

# ---- update_metric 破坏性变更分支（PUBLISHED → PENDING_CONFIRMATION）----


async def test_update_metric_published_breaking_def_creates_pending():
    """PUBLISHED + 口径破坏性变更 → PENDING_CONFIRMATION 版本
    + create_pending（含备份 Owner 消费方）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=1,
        backup_owner_id=2,
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    fake_pvm = MagicMock()
    fake_pvm.create_pending = AsyncMock()

    with patch(
        "app.services.semantic.pending_version_manager.PendingVersionManager",
        return_value=fake_pvm,
    ):
        result = await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                definition_json={"expression": "SUM(refund_amount)"},
                change_reason="破坏性口径变更",
            ),
            actor_id=1,
            role="metric_owner",
        )

    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.status == "PENDING_CONFIRMATION"
    assert version_arg.change_type == "BREAKING"
    # create_pending(metric, version, consumer_ids) 位置参数
    assert fake_pvm.create_pending.call_args.args[2] == [1, 2]
    assert result.row_version == 2


async def test_update_metric_published_second_breaking_blocked_during_pending():
    """PENDING 确认期内再次发起破坏性变更 → 拒绝（METRIC_PENDING_VERSION_EXISTS）。

    修复前：多个 PENDING 版本并存，转正低版本号会把主表 version 回退并覆盖
    高版本口径（版本历史倒挂、已确认的高版本变更丢失）。
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=2,  # 主表 version 已在第一次 PENDING 时递增预留
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    # 已有未转正的 PENDING_CONFIRMATION 版本
    repo.has_pending_version = AsyncMock(return_value=True)

    with pytest.raises(ConflictError) as exc:
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                definition_json={"expression": "SUM(refund_amount)"},
                change_reason="PENDING 期内二次破坏性变更",
            ),
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "METRIC_PENDING_VERSION_EXISTS"
    # 未创建版本记录、未创建新 PENDING（拒绝在创建前）
    repo.create_version.assert_not_called()
    repo.update_with_optimistic_lock.assert_not_called()


async def test_update_metric_pending_notifies_consumers():
    """PUBLISHED 破坏性变更创建 PENDING 后，定向通知消费方（Owner/备份 Owner）。

    修复前：create_pending 只建确认记录不通知（pending_version_manager TODO），
    消费方在 14 天确认期内不知情只能被动等超时——确认期闭环断裂。
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=1,
        owner_id=2,
        backup_owner_id=3,
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    fake_pvm = MagicMock()
    fake_pvm.create_pending = AsyncMock()

    with patch(
        "app.services.semantic.pending_version_manager.PendingVersionManager",
        return_value=fake_pvm,
    ):
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                definition_json={"expression": "SUM(refund_amount)"},
                change_reason="破坏性口径变更",
            ),
            actor_id=2,  # owner 本人发起变更
            role="metric_owner",
        )

    # 通知消费方（owner=2 + backup=3），跳过发起变更的 actor（2 已发起已知晓）
    svc._notify_pending_consumers.assert_awaited_once_with(
        metric_code="sales_gmv_daily",
        version=2,
        consumer_ids=[2, 3],
        skip_actor=2,
    )


async def test_update_metric_published_breaking_defers_lineage_registration():
    """PUBLISHED + 口径破坏性变更 → PENDING 期不立即注册血缘（延迟到转正后）。

    修复前：update_metric 无条件按新口径注册血缘 → PENDING 期血缘图显示"未来口径"
    （误导影响分析），且消费方拒绝后被拒口径的边已注册/旧边已删（错误残留）。
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=1,
        backup_owner_id=2,
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    fake_pvm = MagicMock()
    fake_pvm.create_pending = AsyncMock()

    with (
        patch(
            "app.services.semantic.pending_version_manager.PendingVersionManager",
            return_value=fake_pvm,
        ),
        patch.object(
            MetricService, "_register_metric_lineage_full", AsyncMock()
        ) as mock_lineage,
    ):
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                definition_json={"expression": "SUM(refund_amount)"},
                change_reason="破坏性口径变更",
            ),
            actor_id=1,
            role="metric_owner",
        )
    # PENDING 期新口径未生效 → 不应注册血缘（由 _promote_pending_version 转正后注册）
    mock_lineage.assert_not_awaited()


async def test_update_metric_draft_breaking_registers_lineage_immediately():
    """DRAFT + 破坏性口径变更（不触发 PENDING，立即生效）→ 立即注册血缘。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="DRAFT",
        row_version=1,
        version=1,
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    with patch.object(
        MetricService, "_register_metric_lineage_full", AsyncMock()
    ) as mock_lineage:
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                definition_json={"expression": "SUM(refund_amount)"},
                change_reason="草稿口径修正",
            ),
            actor_id=1,
            role="metric_owner",
        )
    # DRAFT 变更立即生效 → 立即注册血缘
    mock_lineage.assert_awaited_once()


async def test_update_metric_published_top_level_breaking_creates_pending():
    """PUBLISHED + top-level 破坏性字段（granularity）变更且无
    definition_json → PENDING + 结构化 diff。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=1,
        granularity="daily",
        backup_owner_id=2,
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    fake_pvm = MagicMock()
    fake_pvm.create_pending = AsyncMock()

    with patch(
        "app.services.semantic.pending_version_manager.PendingVersionManager",
        return_value=fake_pvm,
    ):
        result = await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(granularity="hourly", change_reason="粒度变更"),
            actor_id=1,
            role="metric_owner",
        )

    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.status == "PENDING_CONFIRMATION"
    assert version_arg.change_type == "BREAKING"
    assert version_arg.diff_json["granularity"]["before"] == "daily"
    assert version_arg.diff_json["granularity"]["after"] == "hourly"
    assert fake_pvm.create_pending.call_args.args[2] == [1, 2]
    assert result.row_version == 2


async def test_update_metric_published_aggregation_breaking_creates_pending():
    """PUBLISHED + 直接改 aggregation（聚合方式）→ 触发 PENDING_CONFIRMATION。

    修复前：aggregation 被误归治理属性（BREAKING_TOP_LEVEL_FIELDS 不含它），
    直接修改静默更新主表、不触发版本确认——而 definition_json 路径的
    BREAKING_DEF_FIELDS 判 aggregation 为 BREAKING，两条路径判定矛盾
    （SUM→AVG 本质是口径变更）。R40 修复后与 granularity/unit 同级触发 PENDING。
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=1,
        aggregation="SUM",
        backup_owner_id=2,
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    fake_pvm = MagicMock()
    fake_pvm.create_pending = AsyncMock()

    with patch(
        "app.services.semantic.pending_version_manager.PendingVersionManager",
        return_value=fake_pvm,
    ):
        result = await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(aggregation="AVG", change_reason="聚合方式调整"),
            actor_id=1,
            role="metric_owner",
        )

    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.status == "PENDING_CONFIRMATION"
    assert version_arg.change_type == "BREAKING"
    assert version_arg.diff_json["aggregation"]["before"] == "SUM"
    assert version_arg.diff_json["aggregation"]["after"] == "AVG"
    # 主表 aggregation 不应被直写（进入 PENDING，等消费方确认后转正）
    assert repo.update_with_optimistic_lock.await_args.kwargs.get("aggregation") is None
    assert result.row_version == 2



async def test_update_metric_syncs_pii_flag_from_definition():
    """definition_json 显式声明 pii=True 时，pii_flag 以权威源同步到 updates。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="DRAFT",
        row_version=1,
        version=1,
        pii_flag=False,
        definition_json={"expression": "SUM(order_amount)"},
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            definition_json={"expression": "SUM(order_amount)", "pii": True},
            change_reason="补充 PII 标记",
        ),
        actor_id=1,
        role="metric_owner",
    )
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["pii_flag"] is True


async def test_update_metric_collects_optional_fields():
    """非 None 的可选字段（name/sla/backup_owner_id）进入 updates。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=1, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            name="新名称",
            sla="08:00",
            backup_owner_id=5,
            change_reason="调整元数据",
        ),
        actor_id=1,
        role="metric_owner",
    )
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["name"] == "新名称"
    assert kwargs["sla"] == "08:00"
    assert kwargs["backup_owner_id"] == 5


async def test_update_metric_applies_governance_fields_without_version():
    """治理字段（dw_layer/freshness/metric_tier/currency 等）更新主表治理列，
    不触发版本递增（非破坏性变更）——修复创建后治理字段不可改的缺口。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=1, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            dw_layer="DWS",
            freshness="T0",
            metric_tier="T1",
            currency="CNY",
            change_reason="治理字段调整：分层纠正+时效调整+分级晋升+币种修正",
        ),
        actor_id=1,
        role="metric_owner",
    )
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["dw_layer"] == "DWS"
    assert kwargs["freshness"] == "T0"
    assert kwargs["metric_tier"] == "T1"
    assert kwargs["currency"] == "CNY"
    # 非破坏性治理变更不触发版本递增（不在 BREAKING_TOP_LEVEL_FIELDS）
    assert "version" not in kwargs
    repo.create_version.assert_not_called()


async def test_update_metric_rename_clears_rename_required_mark():
    """仲裁「保留差异+指定改名」的指标，Owner 改名后清除 rename_required 标记（TD §12.4）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="DRAFT",
        row_version=1,
        version=1,
        name="旧名称",
        arbitration_mark={
            "status": "coexist",
            "conflict_id": "CF-RENAME",
            "rename_required": True,
            "rename_opposite_code": "gmv_total",
        },
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=2, name="新名称")
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(name="新名称", change_reason="响应仲裁改名要求"),
        actor_id=1,
        role="metric_owner",
    )
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["name"] == "新名称"
    # rename_required 被清除并记录 resolved_at（幂等闭环）
    mark = kwargs["arbitration_mark"]
    assert mark["rename_required"] is False
    assert mark["resolved_at"]


async def test_update_metric_rename_keeps_mark_when_name_unchanged():
    """未改名（name 不变）时不误清 rename_required 标记——仅真正改名才触发清除。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="DRAFT",
        row_version=1,
        version=1,
        name="原名称",
        arbitration_mark={
            "status": "coexist",
            "conflict_id": "CF-RENAME",
            "rename_required": True,
            "rename_opposite_code": "gmv_total",
        },
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(name="原名称", change_reason="仅同步其他字段"),
        actor_id=1,
        role="metric_owner",
    )
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert "arbitration_mark" not in kwargs  # 未改名 → 不触碰仲裁标记


# ---- 状态机非法跃迁（submit/review/reject/promote/rollback/deprecate）----


async def test_submit_metric_invalid_transition():
    """非 DRAFT 状态提交（如 PUBLISHED）→ ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", owner_id=1))
    with pytest.raises(ConflictError) as exc:
        await svc.submit_metric(
            "sales_gmv_daily",
            MetricSubmitRequest(change_reason="提交评审"),
            actor_id=1,
            role="metric_owner",
            user_domain="sales",
        )
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_reject_metric_invalid_transition():
    """非 REVIEW 状态驳回（如 EXPERIMENTAL）→ ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="EXPERIMENTAL"))
    with pytest.raises(ConflictError) as exc:
        await svc.reject_metric(
            "sales_gmv_daily",
            MetricRejectRequest(reason="口径不符"),
            actor_id=1,
            role="platform_admin",
        )
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_promote_metric_invalid_transition():
    """非 EXPERIMENTAL 状态 promote → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT"))
    with pytest.raises(ConflictError) as exc:
        await svc.promote_metric("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_rollback_metric_invalid_transition():
    """非 EXPERIMENTAL 状态 rollback → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT"))
    with pytest.raises(ConflictError) as exc:
        await svc.rollback_metric("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_rollback_metric_no_previous_published():
    """EXPERIMENTAL 回退但无上一 PUBLISHED 版本 → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="EXPERIMENTAL", version=2))
    repo.list_versions = AsyncMock(return_value=[MagicMock(status="EXPERIMENTAL", version=2)])
    with pytest.raises(ConflictError) as exc:
        await svc.rollback_metric("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert exc.value.error_code == "NO_PREVIOUS_PUBLISHED_VERSION"


async def test_recycle_expired_gray_success():
    """P1-7: 灰度超期回收——EXPERIMENTAL → DRAFT，清灰度白名单，发布事件。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="EXPERIMENTAL", gray_tenant_ids=[1, 2])
    )
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", gray_tenant_ids=None)
    )
    svc._publish_event = AsyncMock()

    result = await svc.recycle_expired_gray("sales_gmv_daily", actor_id=0)

    assert result.status == "DRAFT"
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["status"] == "DRAFT"
    assert kwargs["gray_tenant_ids"] is None
    svc._publish_event.assert_awaited_once()
    assert svc._publish_event.await_args.args[0] == "metric.gray_recycled"


async def test_recycle_expired_gray_non_experimental_rejected():
    """非 EXPERIMENTAL 状态回收 → ConflictError（INVALID_TRANSITION）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    with pytest.raises(ConflictError) as exc:
        await svc.recycle_expired_gray("sales_gmv_daily", actor_id=0)
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_reject_metric_experimental_still_blocked():
    """P1-7: EXPERIMENTAL→DRAFT 虽入状态机，reject 通道仍仅限 REVIEW（回收走系统路径）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="EXPERIMENTAL"))
    with pytest.raises(ConflictError) as exc:
        await svc.reject_metric(
            "sales_gmv_daily",
            MetricRejectRequest(reason="口径不符"),
            actor_id=1,
            role="platform_admin",
        )
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_deprecate_metric_invalid_transition():
    """非 PUBLISHED 状态废弃（如 DRAFT）→ ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT"))
    with pytest.raises(ConflictError) as exc:
        await svc.deprecate_metric(
            "sales_gmv_daily",
            successor_code="sales_gmv_amount_daily",
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_deprecate_metric_successor_not_found():
    """替代指标不存在 → NotFoundError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.get_by_code.side_effect = [
        make_metric(status="PUBLISHED"),  # 原指标
        None,  # successor 查询
    ]
    with pytest.raises(NotFoundError):
        await svc.deprecate_metric(
            "sales_gmv_daily", successor_code="not_exist_code", actor_id=1, role="metric_owner"
        )


# ---- approve_metric 派生/复合指标依赖校验（921-938 / 944 / 975）----


async def test_approve_derived_metric_unpublished_dependency():
    """派生指标发布时依赖未发布 → DEPENDENCY_NOT_PUBLISHED。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="REVIEW",
        owner_id=1,
        pii_flag=False,
        type="derived",
        definition_json={"dependencies": ["sales_gmv_amount_daily"]},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    fake_checker = MagicMock()
    fake_checker.check_dependencies_published = AsyncMock(return_value=["sales_gmv_amount_daily"])
    with (
        patch(
            "app.services.semantic.dependency_checker.DependencyChecker",
            return_value=fake_checker,
        ),
        pytest.raises(BusinessError) as exc,
    ):
        await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
        )
    assert exc.value.error_code == "DEPENDENCY_NOT_PUBLISHED"


async def test_approve_derived_metric_cycle():
    """派生指标发布时检测到循环依赖 → CYCLIC_DEPENDENCY。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="REVIEW",
        owner_id=1,
        pii_flag=False,
        type="composite",
        definition_json={"dependencies": ["sales_gmv_amount_daily"]},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    fake_checker = MagicMock()
    fake_checker.check_dependencies_published = AsyncMock(return_value=[])
    fake_checker.detect_cycle = AsyncMock(return_value=["a", "b", "a"])
    with (
        patch(
            "app.services.semantic.dependency_checker.DependencyChecker",
            return_value=fake_checker,
        ),
        pytest.raises(BusinessError) as exc,
    ):
        await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
        )
    assert exc.value.error_code == "CYCLIC_DEPENDENCY"


async def test_approve_derived_metric_emits_dependencies():
    """派生指标正常发布 → 事件 payload 携带 dependencies（975）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        status="REVIEW",
        owner_id=1,
        pii_flag=False,
        type="derived",
        definition_json={"dependencies": ["sales_gmv_amount_daily"]},
    )
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=MagicMock())
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.mark_version_published = AsyncMock()
    svc._publish_event = AsyncMock()
    fake_checker = MagicMock()
    fake_checker.check_dependencies_published = AsyncMock(return_value=[])
    fake_checker.detect_cycle = AsyncMock(return_value=None)
    with patch(
        "app.services.semantic.dependency_checker.DependencyChecker",
        return_value=fake_checker,
    ):
        result = await svc.approve_metric(
            "sales_gmv_daily", MetricApproveRequest(), actor_id=1, role="platform_admin"
        )
    assert result.status == "PUBLISHED"
    payload = svc._publish_event.call_args.args[1]
    assert payload["dependencies"] == ["sales_gmv_amount_daily"]


async def test_approve_metric_version_not_found():
    """approve 时目标版本（当前待审核版本）记录缺失 → NotFoundError。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1, pii_flag=False)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(target_version=1),
            actor_id=1,
            role="platform_admin",
        )


# ---- emergency_publish 边界 ----


async def test_emergency_publish_invalid_status():
    """紧急发布非 DRAFT/REVIEW 状态（如 PUBLISHED）→ ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", pii_flag=False))
    with pytest.raises(ConflictError) as exc:
        await svc.emergency_publish_metric(
            "sales_gmv_daily",
            MetricEmergencyPublishRequest(reason="生产系统故障需立即紧急发布处理"),
            actor_id=1,
            role="domain_admin",
        )
    assert exc.value.error_code == "INVALID_TRANSITION"


async def test_emergency_publish_version_not_found():
    """紧急发布时目标版本（当前版本）记录缺失 → NotFoundError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DRAFT", pii_flag=False))
    repo.get_version = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.emergency_publish_metric(
            "sales_gmv_daily",
            MetricEmergencyPublishRequest(
                reason="生产系统故障需立即紧急发布处理", target_version=1
            ),
            actor_id=1,
            role="domain_admin",
        )


async def test_approve_metric_rejects_historical_target_version():
    """审批经 API 直调传历史版本号 → ConflictError INVALID_TARGET_VERSION（防版本历史篡改）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(status="REVIEW", owner_id=1, pii_flag=False, version=2)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_version = AsyncMock(return_value=None)
    with pytest.raises(ConflictError) as exc:
        await svc.approve_metric(
            "sales_gmv_daily",
            MetricApproveRequest(target_version=1),
            actor_id=1,
            role="platform_admin",
        )
    assert exc.value.error_code == "INVALID_TARGET_VERSION"


async def test_emergency_publish_rejects_historical_target_version():
    """紧急发布传历史版本号 → ConflictError INVALID_TARGET_VERSION（防口径/版本矛盾）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", pii_flag=False, version=2)
    )
    repo.get_version = AsyncMock(return_value=None)
    with pytest.raises(ConflictError) as exc:
        await svc.emergency_publish_metric(
            "sales_gmv_daily",
            MetricEmergencyPublishRequest(
                reason="生产系统故障需立即紧急发布处理", target_version=1
            ),
            actor_id=1,
            role="domain_admin",
        )
    assert exc.value.error_code == "INVALID_TARGET_VERSION"


# ---- confirm_version 边界 ----


async def test_confirm_version_no_pending():
    """版本无待确认记录 → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(return_value=[])
    with pytest.raises(ConflictError) as exc:
        await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=9)
    assert exc.value.error_code == "NO_PENDING_CONFIRMATION"


async def test_confirm_version_no_mine():
    """当前用户无待确认记录 → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="PENDING")]
    )
    with pytest.raises(ConflictError) as exc:
        await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=888)
    assert exc.value.error_code == "NO_PENDING_CONFIRMATION"


async def test_confirm_version_rejected_cannot_reconfirm():
    """已拒绝的消费方不可再次确认——拒绝决定不可静默撤销（防 CANCELLED 版本误转正）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="REJECTED")]
    )
    repo.update_confirmation_status = AsyncMock()
    with pytest.raises(ConflictError) as exc:
        await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=9)
    assert exc.value.error_code == "NO_PENDING_CONFIRMATION"
    repo.update_confirmation_status.assert_not_called()


async def test_confirm_version_already_confirmed_idempotent():
    """已确认的确认记录幂等返回（不重复转正）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric()
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="CONFIRMED")]
    )
    repo.update_confirmation_status = AsyncMock()
    result = await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=9)
    assert result is metric
    repo.update_confirmation_status.assert_not_called()


async def test_confirm_version_partial_returns_without_promote():
    """部分消费方确认（未全部）→ 返回 metric，不触发版本转正。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(id=1, consumer_id=9, status="PENDING"),
            MagicMock(id=2, consumer_id=3, status="PENDING"),
        ]
    )
    repo.update_confirmation_status = AsyncMock()
    result = await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=9)
    assert result is not None
    repo.update_with_optimistic_lock.assert_not_called()


async def test_confirm_version_lock_conflict_rolls_back():
    """全部确认后转正遇乐观锁冲突 → 回滚确认状态为 PENDING 并重新抛出。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="PENDING")]
    )
    repo.update_confirmation_status = AsyncMock()
    repo.get_version = AsyncMock(
        return_value=MagicMock(definition_json={"expression": "SUM(x)"}, diff_json={})
    )
    repo.update_with_optimistic_lock = AsyncMock(side_effect=ConflictError("乐观锁冲突"))
    with pytest.raises(ConflictError):
        await svc.confirm_version("sales_gmv_daily", version=1, consumer_id=9)
    # 回滚确认状态为 PENDING（位置参数 mine.id, status）
    assert repo.update_confirmation_status.call_args.args[1] == "PENDING"


# ---- auto_accept_timeout / _promote_pending_version（并行进程新增强）----


async def test_auto_accept_timeout_promotes_when_all_accepted():
    """超时自动接受：全部 PENDING 置 TIMEOUT_ACCEPTED 后转正。"""
    svc, repo = _svc_with_repo()
    repo.get_by_id = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.get_pending_confirmations = AsyncMock(
        side_effect=[
            [MagicMock(id=1, status="PENDING")],  # 首次读取：有 PENDING
            [MagicMock(id=1, status="TIMEOUT_ACCEPTED")],  # 重读：全部已接受
        ]
    )
    repo.update_confirmation_status = AsyncMock()
    repo.get_version = AsyncMock(
        return_value=MagicMock(definition_json={"expression": "SUM(x)"}, diff_json={})
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric())
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()

    result = await svc.auto_accept_timeout(metric_id=1, version=1)
    assert result is not None
    assert repo.update_confirmation_status.call_args.args[1] == "TIMEOUT_ACCEPTED"


async def test_auto_accept_timeout_all_confirmed_promotes():
    """无 PENDING 可标记但全部已确认 → 直接转正（返回更新后指标）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_id = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.get_pending_confirmations = AsyncMock(return_value=[MagicMock(id=1, status="CONFIRMED")])
    repo.update_confirmation_status = AsyncMock()
    repo.get_version = AsyncMock(
        return_value=MagicMock(definition_json={"expression": "SUM(x)"}, diff_json={})
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric())
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()

    result = await svc.auto_accept_timeout(metric_id=1, version=1)
    assert result is not None
    # 无 PENDING → 不写 TIMEOUT_ACCEPTED
    assert repo.update_confirmation_status.await_count == 0


async def test_auto_accept_timeout_no_confirmations_returns_none():
    """版本无待确认记录 → 返回 None（不抛错）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_id = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.get_pending_confirmations = AsyncMock(return_value=[])
    result = await svc.auto_accept_timeout(metric_id=1, version=1)
    assert result is None


async def test_auto_accept_timeout_metric_not_found():
    """指标不存在 → NotFoundError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_id = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.auto_accept_timeout(metric_id=999, version=1)


async def test_promote_pending_version_applies_top_level_after():
    """_promote_pending_version：应用版本口径 + top-level 破坏性字段 after 值回写主表。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(row_version=1, version=1)
    repo.get_version = AsyncMock(
        return_value=MagicMock(
            definition_json={"expression": "SUM(refund_amount)"},
            diff_json={"granularity": {"before": "daily", "after": "hourly"}},
        )
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(version=2))
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()

    result = await svc._promote_pending_version(metric, version=2)
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["effective_version"] == 2
    assert kwargs["version"] == 2
    assert kwargs["definition_json"]["expression"] == "SUM(refund_amount)"
    assert kwargs["granularity"] == "hourly"  # top-level diff after 回写
    assert result.version == 2


async def test_promote_pending_version_notifies_all_consumers():
    """转正（确认/超时默认接受）后全量通知消费方新口径已生效（skip_actor=None）。

    修复前：auto_accept_timeout 超时默认接受后新口径悄然生效，消费方无通知
    ——与"创建 PENDING 通知"不对称（超时场景消费方未主动确认、最需要被告知）。
    """
    svc, repo = _svc_with_repo()
    metric = make_metric(
        row_version=1, version=1, owner_id=2, backup_owner_id=3
    )
    repo.get_version = AsyncMock(
        return_value=MagicMock(
            definition_json={"expression": "SUM(refund_amount)"},
            diff_json={},
        )
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(version=2))
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()

    await svc._promote_pending_version(metric, version=2)

    svc._notify_pending_consumers.assert_awaited_once_with(
        metric_code=metric.metric_code,
        version=2,
        consumer_ids=[2, 3],  # owner + backup，全量通知（含未主动确认者）
        skip_actor=None,
        event_type="metric.breaking_change_promoted",
        title="指标口径变更已生效",
        extra_payload={"effective_version": 2},
    )


async def test_promote_pending_version_writes_promote_audit():
    """转正（新口径正式生效）写审计——修复前转正动作零审计，
    超时自动转正（定时任务、无用户操作）14 天后口径悄然生效不可追溯。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(row_version=1, version=1, owner_id=2)
    repo.get_version = AsyncMock(
        return_value=MagicMock(
            definition_json={"expression": "SUM(refund_amount)"},
            diff_json={},
        )
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(version=2))
    repo.mark_version_published = AsyncMock()
    svc._cache.invalidate = AsyncMock()
    svc._write_audit = AsyncMock()

    # 超时触发场景：actor 为指标 owner，trigger=timeout
    await svc._promote_pending_version(
        metric, version=2, trigger="timeout", actor_id=metric.owner_id
    )
    _, kwargs = svc._write_audit.call_args
    assert kwargs["action"] == "metric_definition.promote_version"
    assert kwargs["entity_type"] == "metric_definition"
    assert kwargs["entity_id"] == metric.metric_code
    assert kwargs["actor_id"] == 2
    assert kwargs["detail"]["version"] == 2
    assert kwargs["detail"]["trigger"] == "timeout"


async def test_confirm_version_promote_passes_consumer_trigger():
    """消费方全部确认触发转正：_promote_pending_version 以 consumer_confirm +
    最后确认的消费方为 actor 调用（审计可追溯谁触发了生效）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(row_version=1, version=2)
    svc.get_metric = AsyncMock(return_value=metric)
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(id=1, consumer_id=2, status="CONFIRMED"),
            MagicMock(id=2, consumer_id=3, status="PENDING"),
        ]
    )
    repo.update_confirmation_status = AsyncMock()
    svc._promote_pending_version = AsyncMock(return_value=metric)

    await svc.confirm_version(metric.metric_code, 2, consumer_id=3)

    svc._promote_pending_version.assert_awaited_once_with(
        metric, 2, trigger="consumer_confirm", actor_id=3
    )


async def test_auto_accept_promote_passes_timeout_trigger():
    """超时自动转正：_promote_pending_version 以 timeout 触发 + owner 为 actor
    调用（审计可追溯超时自动生效，非用户操作）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(
        row_version=1, version=2, owner_id=2, backup_owner_id=3, status="PUBLISHED"
    )
    repo.get_by_id = AsyncMock(return_value=metric)
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(id=1, consumer_id=2, status="TIMEOUT_ACCEPTED"),
            MagicMock(id=2, consumer_id=3, status="TIMEOUT_ACCEPTED"),
        ]
    )
    repo.update_confirmation_status = AsyncMock()
    svc._promote_pending_version = AsyncMock(return_value=metric)

    await svc.auto_accept_timeout(metric.id, 2)

    svc._promote_pending_version.assert_awaited_once_with(
        metric, 2, trigger="timeout", actor_id=2
    )


async def test_promote_pending_version_not_found():
    """_promote_pending_version 版本不存在 → NotFoundError。"""
    svc, repo = _svc_with_repo()
    repo.get_version = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc._promote_pending_version(make_metric(), version=99)


async def test_reject_version_no_pending():
    """reject 无待确认记录 → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(return_value=[])
    with pytest.raises(ConflictError):
        await svc.reject_version("sales_gmv_daily", version=1, consumer_id=9, reason="不认可")


async def test_reject_version_no_mine():
    """reject 当前用户无记录 → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, consumer_id=9, status="PENDING")]
    )
    with pytest.raises(ConflictError):
        await svc.reject_version("sales_gmv_daily", version=1, consumer_id=888, reason="不认可")


async def test_extend_version_no_pending():
    """extend 无待确认记录 → ConflictError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(return_value=[])
    with pytest.raises(ConflictError):
        await svc.extend_version("sales_gmv_daily", version=1, actor_id=1, role="metric_owner")


async def test_extend_version_limit_reached():
    """已延期满 1 次 → EXTEND_LIMIT_REACHED。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    repo.get_pending_confirmations = AsyncMock(
        return_value=[
            MagicMock(id=1, consumer_id=9, status="PENDING", extension_count=1, deadline=None)
        ]
    )
    with pytest.raises(ConflictError) as exc:
        await svc.extend_version("sales_gmv_daily", version=1, actor_id=1, role="metric_owner")
    assert exc.value.error_code == "EXTEND_LIMIT_REACHED"


# ---- _is_breaking_change 依赖集合比较 / compare_metrics similar ----


async def test_is_breaking_change_dependencies_set_diff():
    """依赖项按集合比较：顺序不同不算破坏，集合不同算破坏。"""
    svc, _ = _svc_with_repo()
    # 顺序不同但集合相同 → 非破坏
    assert (
        svc._is_breaking_change({"dependencies": ["a", "b"]}, {"dependencies": ["b", "a"]}) is False
    )
    # 集合不同 → 破坏
    assert (
        svc._is_breaking_change({"dependencies": ["a", "b"]}, {"dependencies": ["a", "c"]}) is True
    )


async def test_compare_metrics_similar():
    """字段值存在字符串包含关系 → 标记 similar。"""
    svc, repo = _svc_with_repo()
    a = make_metric(granularity="daily")
    b = make_metric(granularity="daily_hourly")
    repo.get_by_code = AsyncMock(side_effect=[a, b])
    result = await svc.compare_metrics("sales_gmv_daily", "sales_gmv_daily")
    assert result["metrics"] == ["sales_gmv_daily", "sales_gmv_daily"]
    assert result["fields"]["granularity"]["difference_level"] == "similar"


# ---- get_consumption_guide 分支 ----


async def test_get_consumption_guide_uses_existing():
    """已有 consumption_guide 直接返回（不生成默认）。"""
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get_guide = AsyncMock(return_value=None)
    svc._cache.set_guide = AsyncMock()
    existing_guide = {"metric_code": "sales_gmv_daily", "recommended_usage": ["自定义"]}
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="PUBLISHED", consumption_guide=existing_guide)
    )
    guide = await svc.get_consumption_guide("sales_gmv_daily")
    assert guide is existing_guide


async def test_get_consumption_guide_pii_caution():
    """PII 指标默认 guide 追加合规 caution。"""
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get_guide = AsyncMock(return_value=None)
    svc._cache.set_guide = AsyncMock()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", pii_flag=True))
    guide = await svc.get_consumption_guide("sales_gmv_daily")
    assert any("PII" in c for c in guide["cautions"])


async def test_get_consumption_guide_semi_additive_caution():
    """SEMI_ADDITIVE 指标默认 guide 追加不可加维度 caution。"""
    svc, repo = _svc_with_repo()
    svc._cache = MagicMock()
    svc._cache.get_guide = AsyncMock(return_value=None)
    svc._cache.set_guide = AsyncMock()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="PUBLISHED",
            additivity="SEMI_ADDITIVE",
            non_additive_dimensions=["store_id"],
        )
    )
    guide = await svc.get_consumption_guide("sales_gmv_daily")
    assert any("store_id" in c for c in guide["cautions"])


# ---- create_metric 自动补全 / PII 传播 / 编码耗尽 ----


async def test_create_metric_auto_fill_sets_default_field(monkeypatch):
    """自动推断命中且当前值=默认值（metric_tier=T3）时覆盖为建议值（275 行 setattr）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())

    async def _get_defaults(domain):
        return {"metric_tier": "T3"}

    monkeypatch.setattr(svc, "_get_domain_defaults", _get_defaults)
    monkeypatch.setattr(
        "app.services.semantic.auto_fill.auto_fill",
        lambda **kw: {"defaults": {"metric_tier": "T2"}, "measure": "gmv"},
    )
    payload = make_create_payload(
        metric_code=None,
        source_table="ods_order",
        measure_column="amount",
        period="day",
        metric_tier="T3",  # 默认值 → 将被覆盖为 T2
    )
    result = await svc.create_metric(MetricCreateRequest(**payload), owner_id=1)
    assert result.status == "DRAFT"


async def test_create_metric_propagates_pii(monkeypatch):
    """definition 含 source_fields 时触发 PII 血缘传播（best-effort，不阻断创建）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=None)
    created = make_metric()
    repo.create = AsyncMock(return_value=created)
    repo.create_version = AsyncMock(return_value=MagicMock())
    propagated = AsyncMock()
    monkeypatch.setattr(
        "app.services.governance.service.GovernanceService",
        lambda db: MagicMock(propagate_pii_to_metric=propagated),
    )
    payload = make_create_payload(
        definition_json={
            "expression": "SUM(order_amount)",
            "source_fields": [{"name": "order_amount", "pii": True}],
        }
    )
    result = await svc.create_metric(MetricCreateRequest(**payload), owner_id=1)
    assert result is created
    propagated.assert_awaited_once()


async def test_generate_metric_code_exhausted_raises_conflict(monkeypatch):
    """自动编码耗尽（generate_unique_code 抛 RuntimeError）→ ConflictError。"""
    svc, repo = _svc_with_repo()

    def _boom(base, exists):
        raise RuntimeError("exhausted")

    monkeypatch.setattr("app.core.codegen.generate_unique_code", _boom)
    with pytest.raises(ConflictError) as exc:
        await svc._generate_metric_code(
            MetricCreateRequest(**make_create_payload(metric_code=None))
        )
    assert exc.value.ctx["code"] == "CODE_EXHAUSTED"


async def test_redis_available_true(monkeypatch):
    """Redis 已初始化时 _redis_available 返回 True（49 行）。"""
    import app.services.semantic.service as svc_mod

    monkeypatch.setattr(svc_mod, "get_redis", lambda: MagicMock())
    assert svc_mod._redis_available() is True


async def test_update_metric_published_def_plus_top_level_breaking_merges_diff():
    """口径 + 顶层破坏性字段同时变更（走 definition_json 分支）→ top_diff 合并进 diff_json。

    覆盖 update_metric 的 ``if request.definition_json is not None`` 分支：
    - top_level_breaking 检测（BREAKING_TOP_LEVEL_FIELDS 变化）
    - top_diff 构建（556）与 merged_diff.update(top_diff)（585）
    - PUBLISHED + breaking → PENDING_CONFIRMATION + PendingVersionManager.create_pending
    """
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        row_version=1,
        version=1,
        granularity="daily",
        backup_owner_id=2,
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", row_version=2, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())
    fake_pvm = MagicMock()
    fake_pvm.create_pending = AsyncMock()

    with patch(
        "app.services.semantic.pending_version_manager.PendingVersionManager",
        return_value=fake_pvm,
    ):
        result = await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                definition_json={"expression": "SUM(new_col)"},
                granularity="hourly",
                change_reason="口径+粒度双重变更",
            ),
            actor_id=1,
            role="metric_owner",
        )

    version_arg = repo.create_version.call_args.args[0]
    assert version_arg.status == "PENDING_CONFIRMATION"
    assert version_arg.change_type == "BREAKING"
    # top-level 破坏性字段 diff 已合并（556/585 覆盖）
    assert version_arg.diff_json["granularity"]["before"] == "daily"
    assert version_arg.diff_json["granularity"]["after"] == "hourly"
    # 定义 diff 已合并（_compute_diff 产物）
    assert "expression" in version_arg.diff_json
    assert fake_pvm.create_pending.call_args.args[2] == [1, 2]
    assert result.row_version == 2


async def test_auto_accept_timeout_partial_rejected_returns_none():
    """超时自动接受后有 REJECTED 残留（非全部 CONFIRMED/ACCEPTED）→ 返回 None。

    覆盖 auto_accept_timeout 的 ``return None`` 分支（1598）：PENDING 被置
    TIMEOUT_ACCEPTED 后重读，仍存在 REJECTED 记录时不转正。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_id = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    repo.get_pending_confirmations = AsyncMock(
        side_effect=[
            [
                MagicMock(id=1, status="PENDING"),
                MagicMock(id=2, status="REJECTED"),
            ],
            [
                MagicMock(id=1, status="TIMEOUT_ACCEPTED"),
                MagicMock(id=2, status="REJECTED"),
            ],
        ]
    )
    repo.update_confirmation_status = AsyncMock()

    result = await svc.auto_accept_timeout(metric_id=1, version=1)
    assert result is None
    # PENDING 记录被置 TIMEOUT_ACCEPTED
    assert repo.update_confirmation_status.call_args.args[1] == "TIMEOUT_ACCEPTED"
    # 未触发转正
    repo.update_with_optimistic_lock.assert_not_called()


async def test_auto_accept_timeout_skips_deprecated_metric():
    """PENDING 确认期指标被废弃（DEPRECATED）后超时任务不得转正其版本。

    修复前：auto_accept_timeout 不检查指标状态——废弃指标 14 天后仍被转正
    PENDING 版本（口径悄然变更 + 通知"新口径已生效"，语义矛盾：废弃指标
    不应再发生口径变更）；get_by_id 只过滤软删（deleted_at），DEPRECATED
    状态此前未被识别。
    """
    svc, repo = _svc_with_repo()
    repo.get_by_id = AsyncMock(return_value=make_metric(status="DEPRECATED"))
    repo.get_pending_confirmations = AsyncMock(
        return_value=[MagicMock(id=1, status="PENDING")]
    )
    repo.update_confirmation_status = AsyncMock()

    result = await svc.auto_accept_timeout(metric_id=1, version=1)

    assert result is None
    # 未触发确认记录终结、未触发转正——废弃指标版本保持不变
    repo.update_confirmation_status.assert_not_called()
    repo.update_with_optimistic_lock.assert_not_called()


# ---------------------------------------------------------------------------
# update_metric_description（TD §12.1 指标业务描述，不触发版本/不参与口径变更）
# ---------------------------------------------------------------------------


async def test_update_metric_description_sets_manual_source():
    svc, repo = _svc_with_repo()
    existing = make_metric(status="PUBLISHED", row_version=3, version=2)
    repo.get_by_code = AsyncMock(return_value=existing)
    updated = make_metric(status="PUBLISHED", row_version=4, version=2)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    result = await svc.update_metric_description(
        "sales_gmv_daily",
        MetricDescriptionUpdateRequest(description="  每日成交总额（含退款前）  "),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["description"] == "每日成交总额（含退款前）"  # 去除首尾空白
    assert kwargs["description_source"] == "manual"
    assert kwargs["description_updated_by"] == 1
    assert kwargs["description_updated_at"] is not None
    # 描述更新不触发版本号递增
    assert "version" not in kwargs
    assert repo.create_version.call_count == 0
    assert result is updated


async def test_update_metric_description_clears_on_blank():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(description="旧描述"))
    updated = make_metric(description=None, description_source=None)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    await svc.update_metric_description(
        "sales_gmv_daily",
        MetricDescriptionUpdateRequest(description="   "),
        actor_id=1,
        role="metric_owner",
    )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["description"] is None
    assert kwargs["description_source"] is None
    assert kwargs["description_updated_by"] == 1


async def test_update_metric_description_blocked_by_pdp():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    svc._governance_svc.check_metric_permission = AsyncMock(
        return_value=Decision(allow=False, reason="no write", error_code="FORBIDDEN")
    )

    with pytest.raises(BusinessError) as ei:
        await svc.update_metric_description(
            "sales_gmv_daily",
            MetricDescriptionUpdateRequest(description="越权描述"),
            actor_id=9,
            role="viewer",
        )
    assert ei.value.error_code == "FORBIDDEN"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_update_metric_description_not_owner_raises_auth():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=1))

    with pytest.raises(AuthError):
        await svc.update_metric_description(
            "sales_gmv_daily",
            MetricDescriptionUpdateRequest(description="他人指标"),
            actor_id=99,
            role="analyst",
            user_domain="sales",
        )
    repo.update_with_optimistic_lock.assert_not_called()


# bind_metric_term（P2-11：指标↔术语绑定写路径）
# ---------------------------------------------------------------------------


async def test_bind_metric_term_success():
    """P2-11: 绑定术语 → 写 metric.term_id，不触发版本。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="PUBLISHED", row_version=3, version=2, term_id=None)
    repo.get_by_code = AsyncMock(return_value=existing)
    # 术语存在性校验通过：Term.id 查询返回 1
    svc._db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: 1)
    )
    updated = make_metric(status="PUBLISHED", row_version=4, version=2, term_id=55)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    result = await svc.bind_metric_term(
        "sales_gmv_daily", 55, actor_id=1, role="metric_owner", user_domain="sales"
    )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["term_id"] == 55
    assert "version" not in kwargs  # 绑定不触发版本
    assert result is updated


async def test_bind_metric_term_unbind():
    """P2-11: term_id=None 解绑，不校验术语存在性。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", term_id=55))
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="PUBLISHED", term_id=None)
    )
    # 解绑不应发起术语查询
    svc._db.execute = AsyncMock(side_effect=AssertionError("不应查询术语"))

    await svc.bind_metric_term(
        "sales_gmv_daily", None, actor_id=1, role="metric_owner", user_domain="sales"
    )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["term_id"] is None


async def test_bind_metric_term_term_not_found():
    """P2-11: 术语不存在 → NotFoundError，不更新指标。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED"))
    svc._db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )

    with pytest.raises(NotFoundError) as exc:
        await svc.bind_metric_term(
            "sales_gmv_daily", 999, actor_id=1, role="metric_owner", user_domain="sales"
        )
    assert exc.value.error_code == "NOT_FOUND"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_bind_metric_term_not_owner_raises_auth():
    """P2-11: 非 Owner 操作他人指标 → AuthError。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=1))

    with pytest.raises(AuthError):
        await svc.bind_metric_term(
            "sales_gmv_daily", 55, actor_id=99, role="analyst", user_domain="sales"
        )
    repo.update_with_optimistic_lock.assert_not_called()


# infer_metric_description（TD §12.1 LLM 推断指标业务描述，source=llm）
# ---------------------------------------------------------------------------


def _llm_client(content: str) -> MagicMock:
    """构造 enabled=True 的假 LLM 客户端（chat 返回指定 content）。"""
    client = MagicMock()
    client.enabled = True
    client.chat = AsyncMock(return_value={"content": content})
    client.close = AsyncMock()
    return client


async def test_infer_metric_description_llm_success_sets_llm_source():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", row_version=3))
    updated = make_metric(description="每日成交总额（含退款前）", description_source="llm")
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    fake = _llm_client(
        '{"description": "每日成交总额（含退款前），按渠道汇总", "confidence": 0.92}'
    )
    with patch.object(svc, "_build_llm_client", AsyncMock(return_value=fake)):
        result = await svc.infer_metric_description(
            "sales_gmv_daily", actor_id=1, role="metric_owner", user_domain="sales"
        )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["description"] == "每日成交总额（含退款前），按渠道汇总"
    assert kwargs["description_source"] == "llm"
    assert kwargs["description_updated_by"] == 1
    # 推断不触发版本号递增
    assert "version" not in kwargs
    assert result is updated


async def test_infer_metric_description_llm_unavailable_raises_business():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    fake = MagicMock()
    fake.enabled = False
    with (
        patch.object(svc, "_build_llm_client", AsyncMock(return_value=fake)),
        pytest.raises(BusinessError) as ei,
    ):
        await svc.infer_metric_description("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert ei.value.error_code == "LLM_INFER_UNAVAILABLE"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_infer_metric_description_parse_fail_raises_business():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    # chat 返回非 JSON → 解析失败 → 视为 LLM 不可用
    fake = _llm_client("抱歉，我无法生成描述。")
    with (
        patch.object(svc, "_build_llm_client", AsyncMock(return_value=fake)),
        pytest.raises(BusinessError) as ei,
    ):
        await svc.infer_metric_description("sales_gmv_daily", actor_id=1, role="metric_owner")
    assert ei.value.error_code == "LLM_INFER_UNAVAILABLE"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_infer_metric_description_blocked_by_pdp():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric())
    svc._governance_svc.check_metric_permission = AsyncMock(
        return_value=Decision(allow=False, reason="no write", error_code="FORBIDDEN")
    )

    with pytest.raises(BusinessError) as ei:
        await svc.infer_metric_description(
            "sales_gmv_daily", actor_id=1, role="metric_owner", user_domain="sales"
        )
    assert ei.value.error_code == "FORBIDDEN"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_infer_metric_description_not_owner_raises_auth():
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(owner_id=1))

    with pytest.raises(AuthError):
        await svc.infer_metric_description(
            "sales_gmv_daily", actor_id=99, role="analyst", user_domain="sales"
        )
    repo.update_with_optimistic_lock.assert_not_called()


async def test_infer_metric_description_skips_existing_llm_without_force():
    """已有 LLM 描述且未 force → 短路返回，不重复调 LLM（去重/省时核心防线）。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="PUBLISHED",
        description="每日成交总额（含退款前）",
        description_source="llm",
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    with patch.object(
        svc, "_llm_infer_metric_description", AsyncMock(return_value=None)
    ) as mock_infer:
        result = await svc.infer_metric_description(
            "sales_gmv_daily", actor_id=1, role="metric_owner", user_domain="sales"
        )

    assert result is existing
    mock_infer.assert_not_called()
    repo.update_with_optimistic_lock.assert_not_called()


async def test_infer_metric_description_force_regenerates_existing_llm():
    """force=True → 忽略已有 LLM 描述，重新调 LLM 生成并覆盖。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="PUBLISHED",
            description="旧描述",
            description_source="llm",
            row_version=5,
        )
    )
    updated = make_metric(description="新描述", description_source="llm")
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    fake = _llm_client('{"description": "新描述", "confidence": 0.9}')
    with patch.object(svc, "_build_llm_client", AsyncMock(return_value=fake)):
        result = await svc.infer_metric_description(
            "sales_gmv_daily", actor_id=1, role="metric_owner", user_domain="sales", force=True
        )

    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["description"] == "新描述"
    assert kwargs["description_source"] == "llm"
    assert result is updated


# ---- 废弃指标重新发起评审（DEPRECATED → REVIEW 闭环）----


async def test_deprecated_metric_can_resubmit_for_review():
    """废弃指标可重新提交评审（DEPRECATED → REVIEW），并清除废弃标记。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(
            status="DEPRECATED",
            owner_id=1,
            successor_code="sales_gmv_v2",
            deprecated_at="2026-08-01T00:00:00+00:00",
        )
    )
    updated = make_metric(status="REVIEW")
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)

    result = await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="废弃后重新评审"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )

    assert result.status == "REVIEW"
    kwargs = repo.update_with_optimistic_lock.call_args.kwargs
    assert kwargs["status"] == "REVIEW"
    assert kwargs["successor_code"] is None  # 重评审清除废弃标记
    assert kwargs["deprecated_at"] is None
    assert kwargs["sunset_until"] is None


async def test_published_metric_resubmit_still_blocked():
    """PUBLISHED 状态提交评审仍被拒（状态机约束不放松）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="PUBLISHED", owner_id=1))
    with pytest.raises(ConflictError) as exc:
        await svc.submit_metric(
            "sales_gmv_daily",
            MetricSubmitRequest(change_reason="不应允许"),
            actor_id=1,
            role="metric_owner",
            user_domain="sales",
        )
    assert exc.value.error_code == "INVALID_TRANSITION"


# ---- 发起评审通知指定评审人/团队 ----


async def test_submit_notifies_specific_reviewer_when_assigned():
    """指定评审用户（reviewer_type=user）时，仅通知该评审人（非整个域）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(
        return_value=make_metric(status="DRAFT", owner_id=1, domain="sales")
    )
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(
            change_reason="提交审核，指定评审人", reviewer_type="user", reviewer_id=99
        ),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )

    call = svc._notify_metric_stakeholders.call_args
    assert call.args[0] == "metric.submitted"
    # 指定评审人时 payload 携带 reviewer_id + reviewer_type，通知目标按指定人
    assert call.kwargs["payload"]["reviewer_id"] == 99
    assert call.kwargs["payload"]["reviewer_type"] == "user"


async def test_deprecated_resubmit_publishes_resubmitted_event():
    """废弃指标重评审时发布 metric.resubmitted 事件（区别于首次提交）。"""
    svc, repo = _svc_with_repo()
    repo.get_by_code = AsyncMock(return_value=make_metric(status="DEPRECATED", owner_id=1))
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="REVIEW"))
    svc._publish_event = AsyncMock()

    await svc.submit_metric(
        "sales_gmv_daily",
        MetricSubmitRequest(change_reason="重新评审"),
        actor_id=1,
        role="metric_owner",
        user_domain="sales",
    )

    published = svc._publish_event.call_args.args[0]
    assert published == "metric.resubmitted"


# ---- 血缘接入：_register_metric_lineage_full（Task A）----


async def test_register_metric_lineage_full_derived_registers_dependency_edges():
    """derived 指标：注册表级血缘 + 每条 metric:{dep}→metric:{code} 依赖边（DERIVED_FROM/L3）。"""
    svc, _repo = _svc_with_repo()
    metric = make_metric(
        type="derived",
        definition_json={
            "expression": "SUM(a)/SUM(b)",
            "dependencies": ["fct_order", "dim_user"],
            "source_tables": ["dwd_order"],
        },
    )
    with (
        patch("app.services.lineage.service.LineageService") as mock_ls,
        patch("app.services.lineage.repository.LineageRepository") as mock_lr,
    ):
        lineage_svc = mock_ls.return_value
        lineage_svc.register_metric_from_definition = AsyncMock(return_value=[])
        lineage_svc.sync_metric_dimension_edges = AsyncMock(return_value=(0, 0))
        repo_inst = mock_lr.return_value
        repo_inst.upsert_edge = AsyncMock()

        await svc._register_metric_lineage_full(metric)

        # 表级血缘始终注册（commit=False 交由外层指标事务统一提交）
        lineage_svc.register_metric_from_definition.assert_awaited_once_with(metric, commit=False)
        # 两个依赖各一条 DERIVED_FROM / L3 边（composite/derived 统一 DERIVED_FROM）
        assert repo_inst.upsert_edge.await_count == 2
        calls = {c.kwargs["source_node"]: c.kwargs for c in repo_inst.upsert_edge.call_args_list}
        assert calls["metric:fct_order"]["target_node"] == "metric:sales_gmv_daily"
        assert calls["metric:fct_order"]["edge_type"] == "DERIVED_FROM"
        assert calls["metric:fct_order"]["granularity"] == "L3"
        assert calls["metric:dim_user"]["target_node"] == "metric:sales_gmv_daily"


async def test_register_metric_lineage_full_atomic_skips_dependency_edges():
    """atomic 指标即使 definition 含 dependencies 也跳过指标间依赖边（仅注册表级血缘）。"""
    svc, _repo = _svc_with_repo()
    metric = make_metric(
        type="atomic",
        definition_json={
            "expression": "SUM(x)",
            "dependencies": ["fct_order"],
            "source_tables": ["dwd_x"],
        },
    )
    with (
        patch("app.services.lineage.service.LineageService") as mock_ls,
        patch("app.services.lineage.repository.LineageRepository") as mock_lr,
    ):
        lineage_svc = mock_ls.return_value
        lineage_svc.register_metric_from_definition = AsyncMock(return_value=[])
        lineage_svc.sync_metric_dimension_edges = AsyncMock(return_value=(0, 0))
        repo_inst = mock_lr.return_value
        repo_inst.upsert_edge = AsyncMock()

        await svc._register_metric_lineage_full(metric)

        lineage_svc.register_metric_from_definition.assert_awaited_once()
        repo_inst.upsert_edge.assert_not_awaited()  # atomic 无指标间依赖边


async def test_register_metric_lineage_full_registers_dimension_edges():
    """atomic 指标含 dimensions：维度边差异同步由 register_metric_from_definition 内完成。"""
    svc, _repo = _svc_with_repo()
    metric = make_metric(
        type="atomic",
        definition_json={
            "expression": "SUM(gmv)",
            "dimensions": ["dim_store", "dim_channel"],
            "source_tables": ["dwd_order"],
        },
    )
    with patch("app.services.lineage.service.LineageService") as mock_ls:
        lineage_svc = mock_ls.return_value
        lineage_svc.register_metric_from_definition = AsyncMock(return_value=[])
        lineage_svc.sync_metric_dimension_edges = AsyncMock(return_value=(0, 0))

        await svc._register_metric_lineage_full(metric)

        # 表/维度/字段血缘统一由 register_metric_from_definition 差异同步
        lineage_svc.register_metric_from_definition.assert_awaited_once_with(metric, commit=False)
        # atomic 无依赖边，不调用依赖注册（register_metric_from_definition 在依赖边前执行）
        lineage_svc.sync_metric_dimension_edges.assert_not_awaited()


async def test_register_metric_lineage_full_clears_dimension_edges_when_absent():
    """atomic 无维度：仍调用 register_metric_from_definition（其内部差异同步清残留）。"""
    svc, _repo = _svc_with_repo()
    metric = make_metric(
        type="atomic",
        definition_json={"expression": "SUM(gmv)", "source_tables": ["dwd_order"]},
    )
    with patch("app.services.lineage.service.LineageService") as mock_ls:
        lineage_svc = mock_ls.return_value
        lineage_svc.register_metric_from_definition = AsyncMock(return_value=[])
        lineage_svc.sync_metric_dimension_edges = AsyncMock(return_value=(0, 0))

        await svc._register_metric_lineage_full(metric)

        lineage_svc.register_metric_from_definition.assert_awaited_once_with(metric, commit=False)
        lineage_svc.sync_metric_dimension_edges.assert_not_awaited()


async def test_register_metric_lineage_full_failure_is_swallowed():
    """血缘注册抛错不向上传播（best-effort，绝不阻断指标创建/发布/更新主流程）。"""
    svc, _repo = _svc_with_repo()
    metric = make_metric(type="composite", definition_json={"dependencies": ["fct_order"]})
    with patch("app.services.lineage.service.LineageService") as mock_ls:
        mock_ls.return_value.register_metric_from_definition = AsyncMock(
            side_effect=RuntimeError("lineage store down")
        )
        # 不应抛异常
        await svc._register_metric_lineage_full(metric)


# ---- 血缘变更影响通知：notify_lineage_impacted_owners（Task C）----


async def test_notify_lineage_impacted_owners_notifies_owners():
    """下游存在受影响指标 → 按 owner 定向通知，event_type=lineage.change_impacted / title=血缘变更影响。"""# noqa: E501
    svc, repo = _svc_with_repo()
    edges = [
        MagicMock(target_node="metric:gmv_derived"),
        MagicMock(target_node="metric:gmv_composite"),
        MagicMock(target_node="table:db.orders"),  # 非指标节点，应忽略
    ]

    async def _lookup(code: str) -> Metric:
        return make_metric(metric_code=code, owner_id=7 if code == "gmv_derived" else 9)

    with (
        patch("app.db.mysql.async_session_factory") as mock_factory,
        patch("app.services.lineage.service.LineageService") as mock_ls,
        patch("app.services.notify.service.NotifyService") as mock_ns,
    ):
        mock_factory.return_value = AsyncMock()  # 独立会话异步上下文管理器
        lineage_svc = mock_ls.return_value
        lineage_svc.query_impact = AsyncMock(return_value=edges)
        repo.get_by_code = _lookup  # 按 code 返回对应 owner 的指标
        notify_inst = mock_ns.return_value
        notify_inst.notify_user = AsyncMock(return_value=None)

        count = await svc.notify_lineage_impacted_owners("table:db.orders")

    assert count == 2
    assert notify_inst.notify_user.await_count == 2
    event_types = {c.kwargs["event_type"] for c in notify_inst.notify_user.call_args_list}
    assert event_types == {"lineage.change_impacted"}
    titles = {c.kwargs["title"] for c in notify_inst.notify_user.call_args_list}
    assert titles == {"血缘变更影响"}


async def test_notify_lineage_impacted_owners_query_failure_returns_zero():
    """影响分析查询抛错 → 返回 0，且不发任何通知（best-effort，不阻断上游变更）。"""
    svc, _repo = _svc_with_repo()
    with (
        patch("app.services.lineage.service.LineageService") as mock_ls,
        patch("app.services.notify.service.NotifyService") as mock_ns,
    ):
        mock_ls.return_value.query_impact = AsyncMock(side_effect=RuntimeError("graph down"))
        notify_inst = mock_ns.return_value
        notify_inst.notify_user = AsyncMock(return_value=None)

        count = await svc.notify_lineage_impacted_owners("table:db.orders")

    assert count == 0
    notify_inst.notify_user.assert_not_awaited()


# ============================================================
# DATA_SOURCE_DROPPED 状态闭环（TD §12.3 / PRD R5-01）
#   mark_source_dropped     : 数据源 DROP → 下游 PUBLISHED 指标置 DSD
#   recover_source_dropped  : DSD → PUBLISHED（源恢复/误报）
#   confirm_deprecate_dropped : DSD → DEPRECATED（确认退役，须填替代指标）
# ============================================================


async def test_mark_source_dropped_marks_published_metrics():
    """数据源 DROP → 血缘下游 PUBLISHED 指标批量置 DATA_SOURCE_DROPPED。"""
    svc, repo = _svc_with_repo()
    m1 = make_metric(metric_code="gmv", status="PUBLISHED", row_version=1)
    m2 = make_metric(metric_code="aov", status="PUBLISHED", row_version=1)
    # 1) 数据源 → 该源下 2 张表（DBCatalog 查询）
    result = MagicMock()
    result.scalars.return_value.all.return_value = ["dwd_orders", "dwd_pay"]
    svc._db.execute = AsyncMock(return_value=result)
    # 2) 血缘下游：每张表返回对应 metric 节点
    with patch("app.services.lineage.service.LineageService") as mock_ls:
        mock_ls.return_value.query_impact = AsyncMock(
            side_effect=[
                [MagicMock(target_node="metric:gmv")],
                [MagicMock(target_node="metric:aov")],
            ]
        )
        repo.get_by_code = AsyncMock(side_effect=[m1, m2])
        repo.update_with_optimistic_lock = AsyncMock(return_value=m1)
        svc._publish_event = AsyncMock(return_value=None)
        svc._cache.invalidate = AsyncMock(return_value=None)

        count = await svc.mark_source_dropped(["mysql_orders"], actor_id=1, role="platform_admin")

    assert count == 2
    assert repo.update_with_optimistic_lock.await_count == 2
    # 目标状态是 DATA_SOURCE_DROPPED
    calls = repo.update_with_optimistic_lock.call_args_list
    for c in calls:
        assert c.kwargs["status"] == "DATA_SOURCE_DROPPED"


async def test_mark_source_dropped_rejects_non_admin():
    """越权防护：非管理角色调 mark_source_dropped → AuthError，不得批量变更他人指标状态。"""
    from app.core.exceptions import AuthError

    svc, repo = _svc_with_repo()
    # 不 mock 任何血缘/DB 查询：角色校验须在副作用发生前拦截
    with pytest.raises(AuthError) as exc:
        await svc.mark_source_dropped(["mysql_orders"], actor_id=1, role="metric_owner")
    assert exc.value.error_code == "FORBIDDEN"
    # 未触碰任何指标状态更新
    repo.update_with_optimistic_lock.assert_not_called()


async def test_recover_source_dropped_returns_to_published():
    """DSD → PUBLISHED（源恢复/确认误报），状态机合法。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(metric_code="gmv", status="DATA_SOURCE_DROPPED", row_version=1)
    updated = make_metric(metric_code="gmv", status="PUBLISHED", row_version=2)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.update_with_optimistic_lock = AsyncMock(return_value=updated)
    svc._cache.invalidate = AsyncMock(return_value=None)
    svc._publish_event = AsyncMock(return_value=None)

    result = await svc.recover_source_dropped("gmv", actor_id=1, role="platform_admin")

    assert result.status == "PUBLISHED"
    assert repo.update_with_optimistic_lock.await_count == 1
    assert repo.update_with_optimistic_lock.call_args.kwargs["status"] == "PUBLISHED"


async def test_recover_source_dropped_rejects_non_dsd():
    """非 DSD 状态调 recover → 409 非法跃迁。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(metric_code="gmv", status="PUBLISHED", row_version=1)
    repo.get_by_code = AsyncMock(return_value=metric)

    with pytest.raises(ConflictError):
        await svc.recover_source_dropped("gmv", actor_id=1, role="platform_admin")


async def test_confirm_deprecate_dropped_invalid_successor_raises():
    """DSD → DEPRECATED 填了不存在的替代指标 → 404。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(metric_code="gmv", status="DATA_SOURCE_DROPPED", row_version=1)
    repo.get_by_code = AsyncMock(side_effect=[metric, None])

    with pytest.raises(NotFoundError):
        await svc.confirm_deprecate_dropped(
            "gmv", successor_code="no_such_metric", actor_id=1, role="platform_admin"
        )


async def test_confirm_deprecate_dropped_allows_no_successor():
    """DSD → DEPRECATED 无替代指标也可退役（可选替代）。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(metric_code="gmv", status="DATA_SOURCE_DROPPED", row_version=1)
    dep = make_metric(metric_code="gmv", status="DEPRECATED", row_version=2)
    repo.get_by_code = AsyncMock(return_value=metric)
    repo.update_with_optimistic_lock = AsyncMock(return_value=dep)
    svc._publish_event = AsyncMock(return_value=None)
    svc._cleanup_metric_lineage = AsyncMock(return_value=None)
    svc._cache.invalidate = AsyncMock(return_value=None)

    result = await svc.confirm_deprecate_dropped(
        "gmv", successor_code=None, actor_id=1, role="platform_admin"
    )

    assert result.status == "DEPRECATED"
    assert repo.update_with_optimistic_lock.call_args.kwargs["successor_code"] is None


async def test_confirm_deprecate_dropped_success():
    """DSD → DEPRECATED（确认退役），带替代指标 + 事件。"""
    svc, repo = _svc_with_repo()
    metric = make_metric(metric_code="gmv", status="DATA_SOURCE_DROPPED", row_version=1)
    successor = make_metric(metric_code="gmv_v2", status="PUBLISHED", row_version=1)
    dep = make_metric(metric_code="gmv", status="DEPRECATED", row_version=2)
    repo.get_by_code = AsyncMock(side_effect=[metric, successor])
    repo.update_with_optimistic_lock = AsyncMock(return_value=dep)
    svc._publish_event = AsyncMock(return_value=None)
    svc._cleanup_metric_lineage = AsyncMock(return_value=None)
    svc._cache.invalidate = AsyncMock(return_value=None)

    result = await svc.confirm_deprecate_dropped(
        "gmv", successor_code="gmv_v2", actor_id=1, role="platform_admin"
    )

    assert result.status == "DEPRECATED"
    svc._publish_event.assert_awaited_once()
    assert svc._publish_event.call_args.args[0] == "metric.deprecated"


async def test_update_metric_row_version_conflict_raises_409():
    """跨请求乐观锁：前端回传 row_version 与当前不一致 → ConflictError（防静默覆盖）。"""
    from app.core.exceptions import ConflictError

    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=5, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)

    with pytest.raises(ConflictError) as exc:
        await svc.update_metric(
            "sales_gmv_daily",
            MetricUpdateRequest(
                name="新名称",
                change_reason="调整元数据",
                row_version=3,  # 客户端持有的旧版本号
            ),
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "OPTIMISTIC_LOCK_CONFLICT"
    assert exc.value.ctx["current_row_version"] == 5
    # 冲突时不应触达落库
    repo.update_with_optimistic_lock.assert_not_called()


async def test_update_metric_row_version_match_passes():
    """跨请求乐观锁：row_version 一致时正常更新。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(status="DRAFT", row_version=5, version=1)
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(
        return_value=make_metric(status="DRAFT", row_version=6, version=2)
    )
    repo.create_version = AsyncMock(return_value=MagicMock())

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(
            name="新名称",
            change_reason="调整元数据",
            row_version=5,  # 与当前一致
        ),
        actor_id=1,
        role="metric_owner",
    )
    _, kwargs = repo.update_with_optimistic_lock.call_args
    assert kwargs["name"] == "新名称"


async def test_update_metric_description_row_version_conflict_raises_409():
    """描述编辑跨请求乐观锁：row_version 不一致 → ConflictError（防静默覆盖）。"""
    from app.core.exceptions import ConflictError

    svc, repo = _svc_with_repo()
    existing = make_metric(status="PUBLISHED", row_version=4, version=2)
    repo.get_by_code = AsyncMock(return_value=existing)

    with pytest.raises(ConflictError) as exc:
        await svc.update_metric_description(
            "sales_gmv_daily",
            MetricDescriptionUpdateRequest(description="新描述", row_version=2),
            actor_id=1,
            role="metric_owner",
        )
    assert exc.value.error_code == "OPTIMISTIC_LOCK_CONFLICT"
    repo.update_with_optimistic_lock.assert_not_called()


async def test_update_metric_review_edit_resets_to_draft():
    """REVIEW 状态编辑即撤回重提（FR-005 闭环）：重置 DRAFT 并清空评审指派。"""
    svc, repo = _svc_with_repo()
    existing = make_metric(
        status="REVIEW", row_version=1, version=1, reviewer_id=3, reviewer_type="user"
    )
    repo.get_by_code = AsyncMock(return_value=existing)
    repo.update_with_optimistic_lock = AsyncMock(return_value=make_metric(status="DRAFT"))

    await svc.update_metric(
        "sales_gmv_daily",
        MetricUpdateRequest(name="销售 GMV 改", change_reason="修正名称"),
        actor_id=1,
        role="metric_owner",
    )

    call_args = repo.update_with_optimistic_lock.call_args
    updates = call_args.kwargs
    assert updates["status"] == "DRAFT"
    assert updates["reviewer_id"] is None
    assert updates["reviewer_type"] is None
    assert updates["reviewer_domain"] is None
