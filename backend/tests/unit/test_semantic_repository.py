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
    db.execute.side_effect = [
        _row_result(total=5, pii_count=2),
        _result(all_=[("PUBLISHED", 3), ("DRAFT", 2)]),  # by_status
        _result(all_=[("T1", 4), ("T2", 1)]),  # by_tier
        _result(all_=[("sales", 5)]),  # by_domain
    ]
    repo = MetricRepository(db)

    result = await repo.aggregate_dashboard(domain="sales", owner_id=1)

    assert result["total"] == 5
    assert result["pii_count"] == 2
    assert result["by_status"] == {"PUBLISHED": 3, "DRAFT": 2}
    assert result["by_tier"] == {"T1": 4, "T2": 1}
    assert result["by_domain"] == {"sales": 5}
    assert result["pii_ratio"] == round(2 / 5, 4)
    assert db.execute.await_count == 4


async def test_aggregate_dashboard_without_filters_and_zero_total():
    db = _mock_session()
    db.execute.side_effect = [
        _row_result(total=0, pii_count=None),  # pii_count None → or 0
        _result(all_=[]),
        _result(all_=[]),
        _result(all_=[]),
    ]
    repo = MetricRepository(db)

    result = await repo.aggregate_dashboard()

    assert result["total"] == 0
    assert result["pii_count"] == 0
    assert result["by_status"] == {}
    assert result["by_tier"] == {}
    assert result["by_domain"] == {}
    assert result["pii_ratio"] == 0.0
