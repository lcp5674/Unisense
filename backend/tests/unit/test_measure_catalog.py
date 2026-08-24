"""逻辑度量目录单测（app/services/measure_catalog/）。

覆盖：repository CRUD/分页/过滤/废弃保护统计 + service 创建（编码冲突/格式联动默认/
owner 覆盖）/更新（DRAFT 改码/格式联动）/状态机（publish/deprecate 保护）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ConflictError, NotFoundError, UnisenseError, ValidationError
from app.models.measure_catalog import MeasureCatalog
from app.services.measure_catalog.repository import MeasureCatalogRepository
from app.services.measure_catalog.schemas import MeasureCreate, MeasureUpdate
from app.services.measure_catalog.service import MeasureCatalogService


class _FakeResult:
    """模拟 SQLAlchemy Result：支持 scalar_one_or_none / scalar / scalars().all()。"""

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
def repo(session: MagicMock) -> MeasureCatalogRepository:
    return MeasureCatalogRepository(session)


def _m(measure_code: str = "pay_amt", **kw) -> MeasureCatalog:
    return MeasureCatalog(
        id=kw.get("id", 1),
        measure_code=measure_code,
        name=kw.get("name", "支付金额"),
        measure_format=kw.get("measure_format", "AMOUNT"),
        default_unit=kw.get("default_unit", "元"),
        default_decimal_places=kw.get("default_decimal_places", 2),
        category=kw.get("category", "OTHER"),
        stat_caliber=kw.get("stat_caliber"),
        domain=kw.get("domain", "sales"),
        owner_id=kw.get("owner_id", 1),
        status=kw.get("status", "DRAFT"),
    )


# ---------- Repository ----------


class TestMeasureRepository:
    async def test_save_adds_and_flushes(self, repo, session) -> None:
        obj = _m()
        out = await repo.save(obj)
        assert out is obj
        session.add.assert_called_once_with(obj)
        session.flush.assert_awaited_once()

    async def test_get_queries_by_code(self, repo, session) -> None:
        obj = _m()
        session.execute = AsyncMock(return_value=_FakeResult(row=obj))
        out = await repo.get("pay_amt")
        assert out is obj

    async def test_get_by_id(self, repo, session) -> None:
        obj = _m()
        session.execute = AsyncMock(return_value=_FakeResult(row=obj))
        out = await repo.get_by_id(1)
        assert out is obj

    async def test_list_filters_and_paginates(self, repo, session) -> None:
        items = [_m("a"), _m("b")]
        session.execute = AsyncMock(
            side_effect=[_FakeResult(row=2), _FakeResult(rows=items)]
        )
        out, total = await repo.list("sales", "DRAFT", keyword="pay", limit=10, offset=0)
        assert out == items
        assert total == 2
        # 第 1 次 execute 为 count（独立查询，无 JOIN）
        count_sql = str(session.execute.call_args_list[0][0][0].compile())
        list_sql = str(session.execute.call_args_list[1][0][0].compile())
        assert "count(" in count_sql
        assert "LIMIT" in list_sql

    async def test_count_metrics_by_measure(self, repo, session) -> None:
        session.execute = AsyncMock(return_value=_FakeResult(row=3))
        assert await repo.count_metrics_by_measure(1) == 3


# ---------- Service ----------


async def _svc() -> tuple[MeasureCatalogService, MagicMock]:
    db = MagicMock()
    svc = MeasureCatalogService(db)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    repo.save = AsyncMock(side_effect=lambda m: _persist(m))
    repo.commit = AsyncMock()
    svc._repo = repo  # noqa: SLF001
    return svc, repo


def _persist(m: MeasureCatalog) -> MeasureCatalog:
    if m.id is None:
        m.id = 1
    return m


class TestMeasureService:
    async def test_create_persists_with_explicit_code(self) -> None:
        svc, repo = await _svc()
        payload = MeasureCreate(
            measure_code="pay_amt", name="支付金额", measure_format="AMOUNT", domain="sales"
        )
        out = await svc.create_measure(payload, actor_id=9)
        assert out.measure_code == "pay_amt"
        assert out.owner_id == 9  # PLAT-2：认证身份优先
        repo.save.assert_awaited()

    async def test_create_fills_format_defaults(self) -> None:
        """度量格式联动默认（PRD FR-02-08）：金额=元/2 位，比率=小数/4 位。"""
        svc, repo = await _svc()
        amount = await svc.create_measure(
            MeasureCreate(measure_code="amt", name="金额", measure_format="AMOUNT", domain="s")
        )
        assert amount.default_unit == "元"
        assert amount.default_decimal_places == 2
        ratio = await svc.create_measure(
            MeasureCreate(measure_code="rate", name="比率", measure_format="RATIO", domain="s")
        )
        assert ratio.default_unit == "小数"
        assert ratio.default_decimal_places == 4
        numeric = await svc.create_measure(
            MeasureCreate(measure_code="num", name="数值", measure_format="NUMERIC", domain="s")
        )
        assert numeric.default_unit == ""

    async def test_create_conflict(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("pay_amt"))
        with pytest.raises(ConflictError):
            await svc.create_measure(
                MeasureCreate(measure_code="pay_amt", name="x", domain="y")
            )

    async def test_create_defaults_category_other(self) -> None:
        """未指定分类时默认 OTHER（度量分类新增字段，存量语义不破坏）。"""
        svc, repo = await _svc()
        out = await svc.create_measure(
            MeasureCreate(measure_code="m", name="度量", domain="outpatient")
        )
        assert out.category == "OTHER"
        assert out.stat_caliber is None

    async def test_create_persists_category_and_caliber(self) -> None:
        """创建时透传度量分类与统计口径。"""
        svc, repo = await _svc()
        out = await svc.create_measure(
            MeasureCreate(
                measure_code="m",
                name="门诊挂号人次",
                domain="outpatient",
                category="FLOW",
                stat_caliber="挂号记录数去重后计数",
            )
        )
        assert out.category == "FLOW"
        assert out.stat_caliber == "挂号记录数去重后计数"

    async def test_create_rejects_invalid_category(self) -> None:
        from pydantic import ValidationError

        svc, repo = await _svc()
        with pytest.raises(ValidationError):
            MeasureCreate(measure_code="m", name="x", domain="y", category="BOGUS")

    async def test_update_category_and_caliber(self) -> None:
        svc, repo = await _svc()
        m = _m("amt", category="OTHER")
        repo.get = AsyncMock(return_value=m)
        await svc.update_measure(
            "amt",
            MeasureUpdate(category="DRUG", stat_caliber="处方明细按开方日期汇总"),
        )
        assert m.category == "DRUG"
        assert m.stat_caliber == "处方明细按开方日期汇总"

    async def test_update_rejects_invalid_category(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MeasureUpdate(category="BOGUS")

    async def test_create_rejects_invalid_format(self) -> None:
        from pydantic import ValidationError

        svc, repo = await _svc()
        with pytest.raises(ValidationError):
            MeasureCreate(measure_code="m", name="x", measure_format="BAD", domain="y")

    async def test_get_not_found(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError):
            await svc.get_measure("nope")

    async def test_list_passes_pagination(self) -> None:
        svc, repo = await _svc()
        repo.list = AsyncMock(return_value=([_m()], 1))
        out, total = await svc.list_measures(None, None, page=2, page_size=50)
        assert total == 1
        # service 层换算 limit/offset 透传
        assert repo.list.await_args.kwargs["limit"] == 50
        assert repo.list.await_args.kwargs["offset"] == 50

    async def test_update_measure_code_only_in_draft(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("old", status="PUBLISHED"))
        with pytest.raises(UnisenseError):
            await svc.update_measure("old", MeasureUpdate(measure_code="new"))

    async def test_update_format_redis_defaults(self) -> None:
        svc, repo = await _svc()
        m = _m("amt", measure_format="AMOUNT", default_unit="元", default_decimal_places=2)
        repo.get = AsyncMock(return_value=m)
        await svc.update_measure("amt", MeasureUpdate(measure_format="RATIO"))
        assert m.default_unit == "小数"
        assert m.default_decimal_places == 4

    async def test_deprecated_blocks_update(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="DEPRECATED"))
        with pytest.raises(UnisenseError):
            await svc.update_measure("amt", MeasureUpdate(name="x"))

    async def test_publish_requires_draft(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="PUBLISHED"))
        with pytest.raises(UnisenseError):
            await svc.publish_measure("amt")

    async def test_publish_sets_status(self) -> None:
        svc, repo = await _svc()
        m = _m("amt", status="DRAFT")
        repo.get = AsyncMock(return_value=m)
        out = await svc.publish_measure("amt")
        assert out.status == "PUBLISHED"

    async def test_deprecate_protects_referenced_measure(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="DRAFT"))
        repo.count_metrics_by_measure = AsyncMock(return_value=2)
        with pytest.raises(ConflictError):
            await svc.deprecate_measure("amt")

    async def test_deprecate_sets_status(self) -> None:
        svc, repo = await _svc()
        m = _m("amt", status="PUBLISHED")
        repo.get = AsyncMock(return_value=m)
        repo.count_metrics_by_measure = AsyncMock(return_value=0)
        out = await svc.deprecate_measure("amt")
        assert out.status == "DEPRECATED"
