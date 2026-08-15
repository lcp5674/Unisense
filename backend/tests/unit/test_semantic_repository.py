"""MetricRepository 单元测试（使用 MagicMock 异步会话，无真实 DB 依赖）。

覆盖：CRUD / 乐观锁更新（成功·冲突·不存在）/ 软删除 / 版本读写 / 分页过滤。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ConflictError, NotFoundError
from app.models.metric import Metric, MetricVersion
from app.services.semantic.repository import MetricRepository


def _metric(**kwargs: object) -> MagicMock:
    m = MagicMock(spec=Metric)
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _version(**kwargs: object) -> MagicMock:
    m = MagicMock(spec=MetricVersion)
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _result(
    *,
    scalar_one_or_none: object = None,
    scalar: object = None,
    all_: list | None = None,
    rowcount: int = 0,
) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none.return_value = scalar_one_or_none
    r.scalar.return_value = scalar
    r.scalars.return_value.all.return_value = all_ if all_ is not None else []
    r.all.return_value = all_ if all_ is not None else []
    r.rowcount = rowcount
    return r


def _mock_session() -> MagicMock:
    """混合 mock：add 为同步方法，execute/flush/refresh 为异步方法。"""
    db = MagicMock()
    db.execute = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    return db


# ---------- create / read ----------


async def test_create_persists_and_returns_metric():
    db = _mock_session()
    repo = MetricRepository(db)
    metric = _metric(metric_code="m1")

    result = await repo.create(metric)

    db.add.assert_called_once_with(metric)
    db.flush.assert_awaited()
    db.refresh.assert_awaited_once_with(metric)
    assert result is metric


async def test_get_by_code_returns_metric_when_found():
    db = _mock_session()
    db.execute.return_value = _result(scalar_one_or_none=_metric(metric_code="m1"))
    repo = MetricRepository(db)

    metric = await repo.get_by_code("m1")

    assert metric is not None and metric.metric_code == "m1"
    db.execute.assert_awaited()


async def test_get_by_code_returns_none_when_missing():
    db = _mock_session()
    db.execute.return_value = _result(scalar_one_or_none=None)
    repo = MetricRepository(db)

    assert await repo.get_by_code("nope") is None


async def test_get_by_id_returns_metric_when_found():
    db = _mock_session()
    db.execute.return_value = _result(scalar_one_or_none=_metric(id=7))
    repo = MetricRepository(db)

    metric = await repo.get_by_id(7)

    assert metric is not None and metric.id == 7


# ---------- list ----------


async def test_list_metrics_applies_filters_and_returns_total():
    db = _mock_session()
    m1, m2 = _metric(metric_code="a"), _metric(metric_code="b")
    # 第一次 execute = count，第二次 = 列表
    db.execute.side_effect = [
        _result(scalar=2),
        _result(all_=[m1, m2]),
    ]
    repo = MetricRepository(db)

    items, total = await repo.list_metrics(domain="sales", keyword="a", offset=0, limit=10)

    assert total == 2
    assert items == [m1, m2]
    assert db.execute.await_count == 2


async def test_list_metrics_applies_owner_and_pii_filters():
    db = _mock_session()
    m1, m2 = _metric(metric_code="a"), _metric(metric_code="b")
    db.execute.side_effect = [
        _result(scalar=2),
        _result(all_=[m1, m2]),
    ]
    repo = MetricRepository(db)

    items, total = await repo.list_metrics(owner_id=7, pii_flag=True, offset=0, limit=10)

    assert total == 2
    assert items == [m1, m2]
    # 编译首条 count 语句，验证 owner_id 与 pii_flag 条件已加入
    stmt = db.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "owner_id" in compiled
    assert "pii_flag" in compiled


# ---------- update_with_optimistic_lock ----------


async def test_update_with_optimistic_lock_success():
    db = _mock_session()
    updated = _metric(id=1, row_version=2)
    # 命中 1 行 → 乐观锁通过；随后 get_by_id 回查返回更新对象
    db.execute.return_value = _result(scalar_one_or_none=updated, rowcount=1)
    repo = MetricRepository(db)

    result = await repo.update_with_optimistic_lock(1, expected_row_version=1, name="x")

    # 成功路径：返回更新对象并触发 refresh
    db.refresh.assert_awaited_once_with(updated)
    assert result is updated


async def test_create_duplicate_code_raises_conflict():
    from sqlalchemy.exc import IntegrityError

    from app.core.exceptions import ConflictError

    db = _mock_session()
    db.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
    db.rollback = AsyncMock()
    repo = MetricRepository(db)
    metric = _metric(metric_code="dup_code")

    with pytest.raises(ConflictError) as exc:
        await repo.create(metric)
    assert exc.value.error_code == "CONFLICT"
    db.rollback.assert_awaited_once()


async def test_get_version_returns_matching_version():
    db = _mock_session()
    version = _version(metric_id=1, version=2, status="DRAFT")
    db.execute.return_value = _result(scalar_one_or_none=version)
    repo = MetricRepository(db)

    result = await repo.get_version(1, 2)
    assert result is version


async def test_mark_version_published_executes_update():
    db = _mock_session()
    db.execute.return_value = _result(rowcount=1)
    repo = MetricRepository(db)

    await repo.mark_version_published(1, 2, "2026-08-07T00:00:00+00:00")
    db.execute.assert_awaited()


async def test_update_with_optimistic_lock_conflict_raises_conflict():
    db = _mock_session()
    existing = _metric(id=1, row_version=5)
    db.execute.side_effect = [
        _result(scalar_one_or_none=None),  # update 命中 0 行
        _result(scalar_one_or_none=existing),  # get_by_id 找到 -> 冲突
    ]
    repo = MetricRepository(db)

    with pytest.raises(ConflictError) as exc:
        await repo.update_with_optimistic_lock(1, expected_row_version=1, name="x")
    assert exc.value.error_code == "CONCURRENT_MODIFICATION"


async def test_update_with_optimistic_lock_not_found_raises_notfound():
    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar_one_or_none=None),
        _result(scalar_one_or_none=None),  # get_by_id 也找不到
    ]
    repo = MetricRepository(db)

    with pytest.raises(NotFoundError):
        await repo.update_with_optimistic_lock(1, expected_row_version=1, name="x")


# ---------- soft_delete ----------


async def test_soft_delete_success():
    db = _mock_session()
    db.execute.return_value = _result(rowcount=1)
    repo = MetricRepository(db)

    await repo.soft_delete(1)  # 不应抛异常
    db.execute.assert_awaited()


async def test_soft_delete_not_found_raises_notfound():
    db = _mock_session()
    db.execute.return_value = _result(rowcount=0)
    repo = MetricRepository(db)

    with pytest.raises(NotFoundError):
        await repo.soft_delete(1)


# ---------- versions ----------


async def test_create_version_persists_and_returns():
    db = _mock_session()
    repo = MetricRepository(db)
    v = _version(metric_id=1, version=1)

    result = await repo.create_version(v)

    db.add.assert_called_once_with(v)
    db.flush.assert_awaited()
    db.refresh.assert_awaited_once_with(v)
    assert result is v


async def test_list_versions_returns_desc_ordered():
    db = _mock_session()
    v1, v2 = _version(version=1), _version(version=2)
    db.execute.return_value = _result(all_=[v2, v1])
    repo = MetricRepository(db)

    versions = await repo.list_versions(1)

    assert versions == [v2, v1]


# ---------- 过滤分支（status / metric_tier） ----------


async def test_list_metrics_applies_status_and_tier_filters():
    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar=1),
        _result(all_=[_metric(metric_code="a")]),
    ]
    repo = MetricRepository(db)

    items, total = await repo.list_metrics(status="PUBLISHED", metric_tier="T1")

    assert total == 1
    assert len(items) == 1


async def test_list_metrics_escapes_like_wildcards():
    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar=0),
        _result(all_=[]),
    ]
    repo = MetricRepository(db)

    items, total = await repo.list_metrics(keyword="sales%rate_data")

    assert total == 0
    assert items == []


async def test_list_metrics_asc_sort_and_whitelist_fallback():
    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar=1),
        _result(all_=[_metric(metric_code="a")]),
    ]
    repo = MetricRepository(db)

    # 非法 sort_by 回落到 updated_at，asc 方向
    items, total = await repo.list_metrics(
        sort_by="not-a-column", sort_order="asc", offset=10, limit=5
    )
    assert total == 1
    assert items


# ---------- 乐观锁更新后数据一致性异常 ----------


async def test_update_with_optimistic_lock_updated_missing_raises_system_error():
    from app.core.exceptions import SystemError as AppSystemError

    db = _mock_session()
    # update 命中 1 行，但随后 get_by_id 回查返回 None（数据一致性异常）
    db.execute.return_value = _result(scalar_one_or_none=None, rowcount=1)
    repo = MetricRepository(db)

    with pytest.raises(AppSystemError) as exc:
        await repo.update_with_optimistic_lock(1, expected_row_version=1, name="x")
    assert exc.value.error_code == "INTERNAL_ERROR"


# ---------- create_version 冲突 ----------


async def test_create_version_duplicate_raises_conflict():
    from sqlalchemy.exc import IntegrityError

    db = _mock_session()
    db.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
    db.rollback = AsyncMock()
    repo = MetricRepository(db)
    v = _version(metric_id=1, version=2)

    with pytest.raises(ConflictError) as exc:
        await repo.create_version(v)
    assert exc.value.error_code == "CONFLICT"
    db.rollback.assert_awaited_once()


# ---------- PENDING_VERSION 确认相关 ----------


def _confirmation(**kwargs: object) -> MagicMock:
    c = MagicMock()
    defaults = {
        "id": 1,
        "metric_id": 1,
        "version": 2,
        "consumer_id": 10,
        "status": "PENDING",
        "deadline": None,
    }
    defaults.update(kwargs)
    for k, v in defaults.items():
        setattr(c, k, v)
    return c


async def test_save_pending_confirmation_persists():
    db = _mock_session()
    repo = MetricRepository(db)
    c = _confirmation()

    result = await repo.save_pending_confirmation(c)

    db.add.assert_called_once_with(c)
    db.flush.assert_awaited()
    db.refresh.assert_awaited_once_with(c)
    assert result is c


async def test_save_pending_confirmation_duplicate_raises_conflict():
    from sqlalchemy.exc import IntegrityError

    db = _mock_session()
    db.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
    db.rollback = AsyncMock()
    repo = MetricRepository(db)
    c = _confirmation()

    with pytest.raises(ConflictError):
        await repo.save_pending_confirmation(c)


async def test_get_pending_confirmations_returns_list():
    db = _mock_session()
    db.execute.return_value = _result(all_=[_confirmation(id=1), _confirmation(id=2)])
    repo = MetricRepository(db)

    rows = await repo.get_pending_confirmations(1, 2)

    assert len(rows) == 2


async def test_update_confirmation_status_with_and_without_reason():
    db = _mock_session()
    repo = MetricRepository(db)

    await repo.update_confirmation_status(1, "CONFIRMED", reason="looks good")
    await repo.update_confirmation_status(2, "REJECTED")

    assert db.execute.await_count == 2


async def test_get_pending_confirmation_returns_single():
    db = _mock_session()
    c = _confirmation(id=3)
    db.execute.return_value = _result(scalar_one_or_none=c)
    repo = MetricRepository(db)

    row = await repo.get_pending_confirmation(1, 2, 10)

    assert row is c


async def test_extend_confirmation_deadline():
    from datetime import UTC, datetime

    db = _mock_session()
    repo = MetricRepository(db)

    await repo.extend_confirmation_deadline(1, datetime.now(UTC))

    db.execute.assert_awaited_once()


async def test_get_timeout_pending_confirmations():
    db = _mock_session()
    db.execute.return_value = _result(all_=[_confirmation(id=1)])
    repo = MetricRepository(db)

    rows = await repo.get_timeout_pending_confirmations()

    assert len(rows) == 1


# ---------- 健康度评分 ----------


def _health_score(**kwargs: object) -> MagicMock:
    h = MagicMock()
    defaults = {
        "id": 1,
        "metric_id": 1,
        "score": 90,
        "level": "EXCELLENT",
        "completeness_score": 95,
        "activity_score": 90,
        "quality_score": 85,
        "owner_response_score": 92,
        "lineage_coverage_score": 88,
        "missing_dimensions": [],
        "calculated_at": None,
    }
    defaults.update(kwargs)
    for k, v in defaults.items():
        setattr(h, k, v)
    return h


async def test_save_health_score_updates_existing():
    db = _mock_session()
    existing = _health_score(id=1)
    db.execute.return_value = _result(scalar_one_or_none=existing)
    repo = MetricRepository(db)
    score = _health_score(metric_id=1)

    result = await repo.save_health_score(score)

    assert result is existing
    db.refresh.assert_awaited_once_with(existing)
    # 第一次 execute = 查询现有，第二次 = update
    assert db.execute.await_count == 2


async def test_save_health_score_creates_new():
    db = _mock_session()
    db.execute.return_value = _result(scalar_one_or_none=None)
    repo = MetricRepository(db)
    score = _health_score(metric_id=2)

    result = await repo.save_health_score(score)

    db.add.assert_called_once_with(score)
    db.flush.assert_awaited()
    db.refresh.assert_awaited_once_with(score)
    assert result is score


async def test_get_health_score_returns_score():
    db = _mock_session()
    h = _health_score(metric_id=1)
    db.execute.return_value = _result(scalar_one_or_none=h)
    repo = MetricRepository(db)

    row = await repo.get_health_score(1)

    assert row is h


async def test_list_critical_metrics():
    db = _mock_session()
    db.execute.return_value = _result(all_=[_metric(metric_code="m1")])
    repo = MetricRepository(db)

    rows = await repo.list_critical_metrics(level="CRITICAL")

    assert len(rows) == 1


# ---------- Dashboard 聚合 ----------


def _row_result(total: int, pii_count: int) -> MagicMock:
    row = MagicMock()
    row.total = total
    row.pii_count = pii_count
    r = MagicMock()
    r.one.return_value = row
    return r


async def test_aggregate_dashboard_with_filters():
    db = _mock_session()
    # 顺序：total+pii / by_status / by_tier / by_domain / owner×5+names /
    #       quality×2 / compliance / conflict / freshness / 资产总览×6
    db.execute.side_effect = [
        _row_result(total=5, pii_count=2),
        _result(all_=[("PUBLISHED", 3), ("DRAFT", 2)]),  # by_status
        _result(all_=[("T1", 4), ("T2", 1)]),  # by_tier
        _result(all_=[("sales", 5)]),  # by_domain
        # Owner 责任分布（跨资产）：指标 / 数据表 / 维度 / 术语 / 模板 / 数据源 / 显示名
        _result(all_=[(1, "PUBLISHED", 3), (1, "DRAFT", 2)]),  # owner_metric
        _result(all_=[(1, 5)]),  # owner_table
        _result(all_=[(1, 2)]),  # owner_dim
        _result(all_=[(1, 3)]),  # owner_term
        _result(all_=[(1, 1)]),  # owner_tpl
        _result(all_=[(1, 2)]),  # owner_source
        _result(all_=[(1, "Alice")]),  # owner_names
        # 治理指标体系（quality / compliance / conflict / freshness）
        _result(all_=[("P1", 1)]),  # quality by_severity
        _result(all_=[("OPEN", 1)]),  # quality by_status
        _result(all_=[(True, 4), (False, 1)]),  # compliance
        _result(all_=[("OPEN", 2)]),  # conflict
        _result(scalar=3),  # freshness updated_30d
        _result(all_=[("INTERNAL", 10), ("PII", 3)]),  # table: sensitivity_level
        _result(all_=[("healthy", 4), ("unknown", 1)]),  # source: health_status
        _result(all_=[("PUBLISHED", 2)]),  # dimension: status
        _result(all_=[("PUBLISHED", 3), ("DRAFT", 1)]),  # term: status
        _result(all_=[(True, 5), (False, 1)]),  # template: is_active（bool→active/inactive）
        _result(all_=[("active", 8), ("inactive", 2)]),  # system_dict: status
    ]
    repo = MetricRepository(db)

    result = await repo.aggregate_dashboard(domain="sales", owner_id=1)

    assert result["total"] == 5
    assert result["pii_count"] == 2
    assert result["by_status"] == {"PUBLISHED": 3, "DRAFT": 2}
    assert result["by_tier"] == {"T1": 4, "T2": 1}
    assert result["by_domain"] == {"sales": 5}
    assert result["pii_ratio"] == round(2 / 5, 4)
    assert db.execute.await_count == 22
    # Owner 责任分布（跨资产）：指标 5 + 数据表 5 + 维度 2 + 术语 3 + 模板 1 + 数据源 2 = 18
    assert result["by_owner"] == {
        1: {
            "name": "Alice",
            "total": 18,
            "metrics": {"total": 5, "by_status": {"PUBLISHED": 3, "DRAFT": 2}},
            "tables": 5,
            "sources": 2,
            "dimensions": 2,
            "terms": 3,
            "templates": 1,
        }
    }
    # 资产总览：指标复用顶层聚合；其余资产按各自状态列分组
    assert result["assets"]["metric"] == {
        "total": 5,
        "by_status": {"PUBLISHED": 3, "DRAFT": 2},
    }
    assert result["assets"]["table"] == {"total": 13, "by_status": {"INTERNAL": 10, "PII": 3}}
    assert result["assets"]["source"] == {"total": 5, "by_status": {"healthy": 4, "unknown": 1}}
    assert result["assets"]["dimension"] == {"total": 2, "by_status": {"PUBLISHED": 2}}
    assert result["assets"]["term"] == {"total": 4, "by_status": {"PUBLISHED": 3, "DRAFT": 1}}
    assert result["assets"]["template"] == {"total": 6, "by_status": {"active": 5, "inactive": 1}}
    assert result["assets"]["system_dict"] == {
        "total": 10,
        "by_status": {"active": 8, "inactive": 2},
    }


async def test_aggregate_dashboard_without_filters_and_zero_total():
    db = _mock_session()
    db.execute.side_effect = [
        _row_result(total=0, pii_count=None),  # pii_count None → or 0
        _result(all_=[]),  # by_status
        _result(all_=[]),  # by_tier
        _result(all_=[]),  # by_domain
        _result(all_=[]),  # owner_metric
        _result(all_=[]),  # owner_table
        _result(all_=[]),  # owner_dim
        _result(all_=[]),  # owner_term
        _result(all_=[]),  # owner_tpl
        _result(all_=[]),  # owner_source
        # owner_names 跳过（owner_ids 为空）
        _result(all_=[]),  # quality severity
        _result(all_=[]),  # quality status
        _result(all_=[]),  # compliance
        _result(all_=[]),  # conflict
        _result(scalar=0),  # freshness
        _result(all_=[]),  # table
        _result(all_=[]),  # source
        _result(all_=[]),  # dimension
        _result(all_=[]),  # term
        _result(all_=[]),  # template
        _result(all_=[]),  # system_dict
    ]
    repo = MetricRepository(db)

    result = await repo.aggregate_dashboard()

    assert result["total"] == 0
    assert result["pii_count"] == 0
    assert result["by_status"] == {}
    assert result["by_tier"] == {}
    assert result["by_domain"] == {}
    assert result["pii_ratio"] == 0.0
    assert result["by_owner"] == {}
    assert result["quality"] == {"total": 0, "by_severity": {}, "pending": 0}
    assert result["compliance"] == {"total": 0, "reviewed": 0, "pending": 0, "reviewed_ratio": 0.0}
    assert result["conflict"] == {"total": 0, "open": 0, "escalated": 0, "by_status": {}}
    assert result["freshness"] == {"total": 0, "updated_30d": 0, "updated_30d_ratio": 0.0}
    assert result["assets"]["metric"] == {"total": 0, "by_status": {}}
    assert result["assets"]["template"] == {"total": 0, "by_status": {"active": 0, "inactive": 0}}
    assert result["assets"]["system_dict"] == {
        "total": 0,
        "by_status": {"active": 0, "inactive": 0},
    }


async def test_aggregate_dashboard_governance_indicators():
    """总览仪表完整指标体系：by_owner 责任分布 + 质量/合规/冲突/新鲜度聚合。

    新增 6 次查询（插在 domain 之后、资产聚合之前）：
    by_owner / quality_severity / quality_status / compliance / conflict_status / freshness。
    """
    db = _mock_session()
    db.execute.side_effect = [
        _row_result(total=10, pii_count=3),  # total + pii
        _result(all_=[("PUBLISHED", 6), ("DRAFT", 3), ("REVIEW", 1)]),  # by_status
        _result(all_=[("T1", 4), ("T2", 4), ("T3", 2)]),  # by_tier
        _result(all_=[("sales", 6), ("risk", 4)]),  # by_domain
        # Owner 责任分布（跨资产）：指标 / 数据表 / 维度 / 术语 / 模板 / 数据源 / 显示名
        _result(all_=[
            (1, "PUBLISHED", 4),
            (1, "REVIEW", 1),
            (2, "PUBLISHED", 2),
            (2, "DRAFT", 3),
        ]),  # owner_metric (owner_id, status, count)
        _result(all_=[(1, 6), (2, 2)]),  # owner_table
        _result(all_=[(1, 3), (2, 1)]),  # owner_dim
        _result(all_=[(1, 4), (2, 2)]),  # owner_term
        _result(all_=[(1, 2)]),  # owner_tpl
        _result(all_=[(1, 1), (2, 1)]),  # owner_source
        _result(all_=[(1, "Alice"), (2, "Bob")]),  # owner_names
        _result(all_=[("P0", 1), ("P1", 2), ("P2", 3)]),  # quality by_severity
        _result(all_=[("OPEN", 4), ("ACK", 1), ("RESOLVED", 3)]),  # quality by_status
        _result(all_=[(True, 7), (False, 3)]),  # compliance reviewed
        _result(all_=[("OPEN", 2), ("NEGOTIATING", 1), ("ESCALATED", 1), ("RULED", 1)]),  # conflict
        _result(scalar=6),  # freshness updated_30d
        _result(all_=[("INTERNAL", 5), ("PII", 2)]),  # table
        _result(all_=[("healthy", 3)]),  # source
        _result(all_=[("PUBLISHED", 4)]),  # dimension
        _result(all_=[("PUBLISHED", 5), ("DRAFT", 1)]),  # term
        _result(all_=[(True, 5), (False, 2)]),  # template
        _result(all_=[("active", 6), ("inactive", 1)]),  # system_dict
    ]
    repo = MetricRepository(db)

    result = await repo.aggregate_dashboard()

    # Owner 责任分布（跨资产）：每 owner 汇总指标/数据表/维度/术语/模板/数据源计数
    # Alice：指标 5 + 数据表 6 + 维度 3 + 术语 4 + 模板 2 + 数据源 1 = 21
    # Bob：指标 5 + 数据表 2 + 维度 1 + 术语 2 + 模板 0 + 数据源 1 = 11
    assert result["by_owner"] == {
        1: {
            "name": "Alice",
            "total": 21,
            "metrics": {"total": 5, "by_status": {"PUBLISHED": 4, "REVIEW": 1}},
            "tables": 6,
            "sources": 1,
            "dimensions": 3,
            "terms": 4,
            "templates": 2,
        },
        2: {
            "name": "Bob",
            "total": 11,
            "metrics": {"total": 5, "by_status": {"PUBLISHED": 2, "DRAFT": 3}},
            "tables": 2,
            "sources": 1,
            "dimensions": 1,
            "terms": 2,
            "templates": 0,
        },
    }
    # 质量健康：按严重级分布 + 待处理（OPEN+ACK）
    assert result["quality"] == {
        "total": 6,
        "by_severity": {"P0": 1, "P1": 2, "P2": 3},
        "pending": 5,
    }
    # 合规：复核率
    assert result["compliance"] == {
        "total": 10,
        "reviewed": 7,
        "pending": 3,
        "reviewed_ratio": 0.7,
    }
    # 冲突风险：待仲裁 + 升级中
    assert result["conflict"] == {
        "total": 5,
        "open": 3,
        "escalated": 1,
        "by_status": {"OPEN": 2, "NEGOTIATING": 1, "ESCALATED": 1, "RULED": 1},
    }
    # 新鲜度：近 30 天更新
    assert result["freshness"] == {
        "total": 10,
        "updated_30d": 6,
        "updated_30d_ratio": 0.6,
    }
    assert db.execute.await_count == 22
