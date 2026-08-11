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
