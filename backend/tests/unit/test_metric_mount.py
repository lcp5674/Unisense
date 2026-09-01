"""指标挂载实体单测（app/services/metric_mount/）。

覆盖：repository CRUD/分页/过滤/软删除 + service 创建（仅派生可挂载/唯一挂载点）/
更新/删除。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import NotFoundError, UnisenseError
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

    async def test_get_default_mount_returns_default_period_row(self, repo, session) -> None:
        """多变体默认变体解析：default_period 非空行优先。"""
        m1 = _mount(mount_id=1)
        m1.default_period = None
        m2 = _mount(mount_id=2)
        session.execute = AsyncMock(return_value=_FakeResult(rows=[m1, m2]))
        assert await repo.get_default_mount(1) is m2

    async def test_get_default_mount_empty_returns_none(self, repo, session) -> None:
        session.execute = AsyncMock(return_value=_FakeResult(rows=[]))
        assert await repo.get_default_mount(1) is None

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
    repo.save = AsyncMock(side_effect=lambda m: _persist(m))
    repo.soft_delete = AsyncMock()
    repo.commit = AsyncMock()
    # C2（第七轮）：挂载粒度与主表冗余列同步——service 新增回填/清空调用须 mock
    repo.update_metric_granularity = AsyncMock()
    repo.clear_metric_granularity = AsyncMock()
    # 2026-08-27 多变体：全量对齐/默认变体解析走 list_by_metric / get_default_mount
    repo.list_by_metric = AsyncMock(return_value=[])
    repo.get_default_mount = AsyncMock(return_value=None)
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

    async def test_create_mount_allows_multi_mount(self) -> None:
        """2026-08-27 放开一指标一挂载：同一指标可新增第二个挂载（多变体），
        不再抛 MOUNT_EXISTS（唯一约束已改普通索引）。"""
        svc, repo = await _svc_with_metric("derived")
        repo.list_by_metric = AsyncMock(return_value=[_mount()])
        out = await svc.create_mount(
            MetricMountCreate(
                metric_id=1,
                source_table="dwd.hospital_fee",
                source_column="fee",
                granularity="医院",
                default_period="day",
                domain="sales",
            )
        )
        assert out.source_table == "dwd.hospital_fee"
        repo.save.assert_awaited()
        # 默认变体粒度回填：已有行 default_period=day 优先（id 最小），冗余列回填其粒度
        repo.update_metric_granularity.assert_awaited_with(1, "日")

    async def test_create_mount_persists_business_filter(self) -> None:
        """变体级业务限定透传落库（OneData 派生 = 基础原子 + 业务限定 + 周期）。"""
        svc, repo = await _svc_with_metric("derived")
        out = await svc.create_mount(
            MetricMountCreate(
                metric_id=1,
                source_table="dwd.mt_cancer",
                source_column="occur_amt",
                granularity="日",
                default_period="day",
                domain="medical",
                business_filter="病种=门特",
            )
        )
        assert out.business_filter == "病种=门特"
        repo.save.assert_awaited()

    async def test_create_mount_persists_variant_owner(self) -> None:
        """变体级口径三方责任透传落库（方案 B：多变体可归属不同需求方/开发角色）。"""
        svc, repo = await _svc_with_metric("derived")
        out = await svc.create_mount(
            MetricMountCreate(
                metric_id=1,
                source_table="dwd.hospital_fee",
                source_column="fee",
                granularity="医院",
                default_period="day",
                domain="medical",
                product_owner_id=11,
                tech_owner_id=12,
                dw_developer_id=13,
                product_owner_name="张三",
                tech_owner_name="李四",
                dw_developer_name="王五",
            )
        )
        assert out.product_owner_id == 11
        assert out.tech_owner_id == 12
        assert out.dw_developer_id == 13
        assert out.product_owner_name == "张三"
        assert out.tech_owner_name == "李四"
        assert out.dw_developer_name == "王五"
        repo.save.assert_awaited()

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
        """DRAFT/REVIEW 指标修改粒度/源表直接生效（非破坏性，无消费方确认期）。"""
        svc, repo = await _svc_with_metric("derived")
        m = _mount()
        repo.get = AsyncMock(return_value=m)
        out = await svc.update_mount(1, MetricMountUpdate(source_table="dwd.v2", granularity="月"))
        assert out.source_table == "dwd.v2"
        assert out.granularity == "月"
        repo.commit.assert_awaited()

    @pytest.mark.parametrize(
        "update",
        [
            MetricMountUpdate(granularity="月"),
            MetricMountUpdate(source_table="dwd.v2"),
            MetricMountUpdate(granularity_dims=["hospital"]),
        ],
    )
    async def test_update_mount_rejects_published_breaking(self, update: MetricMountUpdate) -> None:
        """已发布指标修改挂载粒度/源表/粒度维度 = 破坏性口径变更，禁止绕过确认流直接生效。

        与 semantic._sync_mounts 判定一致（granularity/source_table/granularity_dims
        变化触发 PENDING_VERSION 消费方确认）；须经指标更新接口提交（mounts 带 id + 变更原因）。
        """
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "PUBLISHED"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        repo.get = AsyncMock(return_value=_mount())
        with pytest.raises(UnisenseError) as exc:
            await svc.update_mount(1, update)
        assert exc.value.error_code == "MOUNT_UPDATE_REQUIRES_CONFIRMATION"
        repo.commit.assert_not_awaited()

    async def test_update_mount_allows_non_breaking_on_published(self) -> None:
        """已发布指标仅改非破坏字段（业务限定/默认周期/域/度量列）不拦截。"""
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "PUBLISHED"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        m = _mount()
        repo.get = AsyncMock(return_value=m)
        out = await svc.update_mount(
            1,
            MetricMountUpdate(
                business_filter="病种=门特",
                default_period="month",
                domain="medical",
                source_column="occur_amt",
            ),
        )
        assert out.business_filter == "病种=门特"
        assert out.default_period == "month"
        assert out.domain == "medical"
        assert out.source_column == "occur_amt"
        repo.commit.assert_awaited()

    async def test_update_mount_allows_same_value_on_published(self) -> None:
        """已发布指标传与原值相同的粒度/源表不算变更，放行（幂等更新）。"""
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "PUBLISHED"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        m = _mount()
        repo.get = AsyncMock(return_value=m)
        out = await svc.update_mount(
            1, MetricMountUpdate(granularity="日", source_table="dwd.sales_detail")
        )
        assert out.granularity == "日"
        assert out.source_table == "dwd.sales_detail"
        repo.commit.assert_awaited()

    async def test_update_mount_rejects_published_clearing_granularity_dims(self) -> None:
        """已发布指标清除粒度维度（["hospital"] → []，组合粒度唯一性构成变化）→ 拦截。"""
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "PUBLISHED"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        m = _mount()
        m.granularity_dims = ["hospital"]
        repo.get = AsyncMock(return_value=m)
        with pytest.raises(UnisenseError) as exc:
            await svc.update_mount(1, MetricMountUpdate(granularity_dims=[]))
        assert exc.value.error_code == "MOUNT_UPDATE_REQUIRES_CONFIRMATION"
        repo.commit.assert_not_awaited()

    async def test_update_mount_allows_same_granularity_dims_on_published(self) -> None:
        """已发布指标粒度维度与原值相同不算变更，放行（幂等更新）。"""
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "PUBLISHED"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        m = _mount()
        m.granularity_dims = ["hospital"]
        repo.get = AsyncMock(return_value=m)
        out = await svc.update_mount(1, MetricMountUpdate(granularity_dims=["hospital"]))
        assert out.granularity_dims == ["hospital"]
        repo.commit.assert_awaited()

    async def test_create_mount_persists_granularity_dims(self) -> None:
        """组合粒度（方案 B）：create 透传粒度维度落库（如 主粒度月 + 粒度维度医院）。"""
        svc, repo = await _svc_with_metric("derived")
        out = await svc.create_mount(
            MetricMountCreate(
                metric_id=1,
                source_table="dwd.hospital_fee",
                source_column="fee",
                granularity="月",
                granularity_dims=["hospital"],
                default_period="month",
                domain="medical",
            )
        )
        assert out.granularity == "月"
        assert out.granularity_dims == ["hospital"]
        repo.save.assert_awaited()

    async def test_update_mount_updates_variant_owner(self) -> None:
        """变体级责任方（治理属性，非破坏性）：已发布指标也可直接更新，不触发拦截。"""
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "PUBLISHED"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        m = _mount()
        repo.get = AsyncMock(return_value=m)
        out = await svc.update_mount(
            1,
            MetricMountUpdate(
                product_owner_id=21,
                tech_owner_name="外部技术协作方",
                dw_developer_id=23,
            ),
        )
        assert out.product_owner_id == 21
        assert out.tech_owner_name == "外部技术协作方"
        assert out.dw_developer_id == 23
        repo.commit.assert_awaited()

    async def test_delete_mount_soft_deletes(self) -> None:
        """DRAFT/REVIEW 指标直接软删（非破坏性，无消费方确认期）。"""
        svc, repo = await _svc_with_metric("derived")
        repo.get = AsyncMock(return_value=_mount())
        await svc.delete_mount(1)
        repo.soft_delete.assert_awaited_with(1)
        repo.commit.assert_awaited()

    async def test_delete_mount_rejects_published(self) -> None:
        """已发布指标解除挂载 = 破坏性口径变更，禁止绕过确认流直接软删。

        须经指标更新接口提交（mounts 去行 + 变更原因），由 semantic._sync_mounts
        判定 removed 行破坏性走 PENDING_VERSION 消费方确认（14 天）。
        """
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "PUBLISHED"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        repo.get = AsyncMock(return_value=_mount())
        with pytest.raises(UnisenseError) as exc:
            await svc.delete_mount(1)
        assert exc.value.error_code == "MOUNT_DELETE_REQUIRES_CONFIRMATION"
        repo.soft_delete.assert_not_awaited()
        repo.commit.assert_not_awaited()

    # ---- B4（审查修复）：废弃/数据源下线状态禁止改删挂载；REVIEW 破坏性须撤回 ----

    async def test_update_mount_rejects_deprecated_status(self) -> None:
        """DEPRECATED 指标禁止修改挂载（此前仅拦 PUBLISHED，废弃指标挂载可被悄悄改动）。"""
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "DEPRECATED"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        repo.get = AsyncMock(return_value=_mount())
        with pytest.raises(UnisenseError) as exc:
            await svc.update_mount(1, MetricMountUpdate(source_column="gmv2"))
        assert exc.value.error_code == "MOUNT_EDIT_FORBIDDEN"
        repo.commit.assert_not_awaited()

    async def test_update_mount_rejects_data_source_dropped(self) -> None:
        """DATA_SOURCE_DROPPED 指标禁止修改挂载（退役语义不可绕过）。"""
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "DATA_SOURCE_DROPPED"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        repo.get = AsyncMock(return_value=_mount())
        with pytest.raises(UnisenseError) as exc:
            await svc.update_mount(1, MetricMountUpdate(business_filter="x"))
        assert exc.value.error_code == "MOUNT_EDIT_FORBIDDEN"

    async def test_update_mount_rejects_review_breaking(self) -> None:
        """REVIEW 指标改挂载粒度/源表须撤回评审——不得经挂载端点绕过「编辑即撤回」。"""
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "REVIEW"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        repo.get = AsyncMock(return_value=_mount())
        with pytest.raises(UnisenseError) as exc:
            await svc.update_mount(1, MetricMountUpdate(granularity="月"))
        assert exc.value.error_code == "MOUNT_UPDATE_REQUIRES_CONFIRMATION"
        repo.commit.assert_not_awaited()

    async def test_update_mount_allows_non_breaking_on_review(self) -> None:
        """REVIEW 指标改非破坏字段（业务限定/度量列）不拦截。"""
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "REVIEW"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        m = _mount()
        repo.get = AsyncMock(return_value=m)
        out = await svc.update_mount(1, MetricMountUpdate(source_column="gmv2"))
        assert out.source_column == "gmv2"
        repo.commit.assert_awaited()

    async def test_delete_mount_rejects_deprecated(self) -> None:
        """DEPRECATED 指标禁止删除挂载（退役语义由指标生命周期管理）。"""
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "DEPRECATED"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        repo.get = AsyncMock(return_value=_mount())
        with pytest.raises(UnisenseError) as exc:
            await svc.delete_mount(1)
        assert exc.value.error_code == "MOUNT_EDIT_FORBIDDEN"
        repo.soft_delete.assert_not_awaited()

    async def test_delete_mount_rejects_review(self) -> None:
        """REVIEW 指标解除挂载须撤回评审（与 PUBLISHED 同级禁止绕过确认流）。"""
        svc, repo = await _svc()
        metric = _metric()
        metric.status = "REVIEW"
        svc._require_metric = AsyncMock(return_value=metric)  # noqa: SLF001
        repo.get = AsyncMock(return_value=_mount())
        with pytest.raises(UnisenseError) as exc:
            await svc.delete_mount(1)
        assert exc.value.error_code == "MOUNT_DELETE_REQUIRES_CONFIRMATION"
