"""指标挂载实体单测（app/services/metric_mount/）。

覆盖：repository CRUD/分页/过滤/软删除 + service 创建（仅派生可挂载/唯一挂载点）/
更新/删除。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ConflictError, NotFoundError, UnisenseError
from app.models.metric import Metric
from app.models.metric_mount import MetricMount
from app.services.metric_mount.repository import MetricMountRepository
from app.services.metric_mount.schemas import MetricMountCreate, MetricMountUpdate
from app.services.metric_mount.service import MetricMountService


class _FakeResult:
    def __init__(self, row: object | None = None, rows: list | None = None) -> None:
        self._row = row
        self._rows = rows or []

    def scalar_one_or_none(self) -> object | None:
        return self._row

    def scalar(self) -> object | None:
        return self._row

    def scalar_one(self) -> object:
        if self._row is None:
            raise ValueError("scalar_one on empty result")
        return self._row

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list:
        return self._rows


@pytest.fixture
def session() -> MagicMock:
    s = MagicMock()
    s.execute = AsyncMock()
    s.flush = AsyncMock()
    s.commit = AsyncMock()
    return s


@pytest.fixture
def repo(session: MagicMock) -> MetricMountRepository:
    return MetricMountRepository(session)


def _mount(metric_id: int = 1, mount_id: int = 1) -> MetricMount:
    return MetricMount(
        id=mount_id,
        metric_id=metric_id,
        source_table="dwd.sales_detail",
        source_column="gmv",
        granularity="日",
        default_period="day",
        domain="sales",
    )


def _metric(metric_id: int = 1, mtype: str = "derived") -> Metric:
    return Metric(
        id=metric_id,
        metric_code="sales_gmv_day",
        name="销售GMV",
        domain="sales",
        type=mtype,
        granularity="日",
        unit="元",
        aggregation="SUM",
        time_semantics="PERIOD",
        freshness="T1",
        dw_layer="DWS",
        serving_mode="BATCH_ONLY",
        additivity="ADDITIVE",
        definition_json={},
        owner_id=1,
        status="DRAFT",
    )


# ---------- Repository ----------


class TestMountRepository:
    async def test_save_adds_and_flushes(self, repo, session) -> None:
        obj = _mount()
        out = await repo.save(obj)
        assert out is obj
        session.add.assert_called_once_with(obj)
        session.flush.assert_awaited_once()

    async def test_get_queries_by_id(self, repo, session) -> None:
        obj = _mount()
        session.execute = AsyncMock(return_value=_FakeResult(row=obj))
        assert await repo.get(1) is obj

    async def test_get_by_metric(self, repo, session) -> None:
        obj = _mount()
        session.execute = AsyncMock(return_value=_FakeResult(row=obj))
        assert await repo.get_by_metric(1) is obj

    async def test_list_filters_and_paginates(self, repo, session) -> None:
        rows = [(_mount(), _metric())]
        session.execute = AsyncMock(
            side_effect=[_FakeResult(row=1), _FakeResult(rows=rows)]
        )
        out, total = await repo.list(metric_id=1, domain="sales", limit=10, offset=0)
        assert out == rows
        assert total == 1

    async def test_soft_delete_updates_deleted_at(self, repo, session) -> None:
        await repo.soft_delete(1)
        stmt = session.execute.call_args[0][0]
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "metric_mount" in sql
        assert "deleted_at" in sql


# ---------- Service ----------


async def _svc() -> tuple[MetricMountService, MagicMock]:
    db = MagicMock()
    svc = MetricMountService(db)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.get_by_metric = AsyncMock(return_value=None)
    repo.save = AsyncMock(side_effect=lambda m: _persist(m))
    repo.soft_delete = AsyncMock()
    repo.commit = AsyncMock()
    # C2（第七轮）：挂载粒度与主表冗余列同步——service 新增回填/清空调用须 mock
    repo.update_metric_granularity = AsyncMock()
    repo.clear_metric_granularity = AsyncMock()
    svc._repo = repo  # noqa: SLF001
    return svc, repo


def _persist(m: MetricMount) -> MetricMount:
    if m.id is None:
        m.id = 1
    return m


async def _svc_with_metric(mtype: str = "derived") -> tuple[MetricMountService, MagicMock]:
    svc, repo = await _svc()
    svc._require_metric = AsyncMock(return_value=_metric(mtype=mtype))  # noqa: SLF001
    return svc, repo


class TestMountService:
    async def test_create_mount_for_derived(self) -> None:
        svc, repo = await _svc_with_metric("derived")
        out = await svc.create_mount(
            MetricMountCreate(
                metric_id=1,
                source_table="dwd.sales_detail",
                source_column="gmv",
                granularity="日",
                domain="sales",
            )
        )
        assert out.source_table == "dwd.sales_detail"
        repo.save.assert_awaited()

    async def test_create_mount_rejects_atomic(self) -> None:
        """原子指标不挂物理表（OneData 界限文档 §2.1 / §2.3）。"""
        svc, _ = await _svc_with_metric("atomic")
        with pytest.raises(UnisenseError):
            await svc.create_mount(
                MetricMountCreate(
                    metric_id=1,
                    source_table="dwd.sales_detail",
                    source_column="gmv",
                    granularity="日",
                    domain="sales",
                )
            )

    async def test_create_mount_rejects_composite(self) -> None:
        svc, _ = await _svc_with_metric("composite")
        with pytest.raises(UnisenseError):
            await svc.create_mount(
                MetricMountCreate(
                    metric_id=1,
                    source_table="t",
                    source_column="c",
                    granularity="日",
                    domain="s",
                )
            )

    async def test_create_mount_unique_per_metric(self) -> None:
        svc, repo = await _svc_with_metric("derived")
        repo.get_by_metric = AsyncMock(return_value=_mount())
        with pytest.raises(ConflictError):
            await svc.create_mount(
                MetricMountCreate(
                    metric_id=1,
                    source_table="t",
                    source_column="c",
                    granularity="日",
                    domain="s",
                )
            )

    async def test_create_mount_requires_existing_metric(self) -> None:
        svc, _ = await _svc()
        svc._require_metric = AsyncMock(  # noqa: SLF001
            side_effect=NotFoundError("指标不存在: 999")
        )
        with pytest.raises(NotFoundError):
            await svc.create_mount(
                MetricMountCreate(
                    metric_id=999,
                    source_table="t",
                    source_column="c",
                    granularity="日",
                    domain="s",
                )
            )

    async def test_get_mount_not_found(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await svc.get_mount(1)

    async def test_update_mount(self) -> None:
        svc, repo = await _svc()
        m = _mount()
        repo.get = AsyncMock(return_value=m)
        out = await svc.update_mount(1, MetricMountUpdate(source_table="dwd.v2", granularity="月"))
        assert out.source_table == "dwd.v2"
        assert out.granularity == "月"

    async def test_delete_mount_soft_deletes(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_mount())
        await svc.delete_mount(1)
        repo.soft_delete.assert_awaited_with(1)
        repo.commit.assert_awaited()
