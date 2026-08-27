"""逻辑度量目录单测（app/services/measure_catalog/）。

覆盖：repository CRUD/分页/过滤/废弃保护统计 + service 创建（编码冲突/格式联动默认/
owner 覆盖）/更新（DRAFT 改码/格式联动）/状态机（publish/deprecate 保护）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.codegen import slugify_code
from app.core.exceptions import ConflictError, NotFoundError, UnisenseError
from app.models.measure_catalog import MeasureCatalog
from app.services.measure_catalog.repository import MeasureCatalogRepository
from app.services.measure_catalog.schemas import (
    MeasureApproveRequest,
    MeasureAutoSuggestRequest,
    MeasureCreate,
    MeasureRejectRequest,
    MeasureSubmitRequest,
    MeasureUpdate,
)
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
        # 审核流字段（透传便于指派/自审用例构造）
        submitted_by=kw.get("submitted_by"),
        approver_id=kw.get("approver_id"),
        reviewer_id=kw.get("reviewer_id"),
        reviewer_type=kw.get("reviewer_type"),
        reviewer_domain=kw.get("reviewer_domain"),
        reject_reason=kw.get("reject_reason"),
        reject_reviewer_id=kw.get("reject_reviewer_id"),
        rejected_at=kw.get("rejected_at"),
        reviewed_at=kw.get("reviewed_at"),
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

    async def test_list_reviewed_by_filters_approver_or_rejector(self, repo, session) -> None:
        """reviewed_by 过滤"我审过的"：approver_id 或 reject_reviewer_id 匹配（审批工作台）。"""
        session.execute = AsyncMock(side_effect=[_FakeResult(row=0), _FakeResult(rows=[])])
        await repo.list(None, "REVIEW", reviewed_by=9)
        list_sql = str(
            session.execute.call_args_list[1][0][0].compile(
                compile_kwargs={"literal_binds": True}
            )
        )
        assert "measure_catalog.approver_id = 9" in list_sql
        assert "measure_catalog.reject_reviewer_id = 9" in list_sql

    async def test_list_without_reviewed_by_no_extra_condition(self, repo, session) -> None:
        """reviewed_by 缺省时不追加审核人过滤（既有调用行为不变）。"""
        session.execute = AsyncMock(side_effect=[_FakeResult(row=0), _FakeResult(rows=[])])
        await repo.list(None, None)
        list_sql = str(session.execute.call_args_list[1][0][0].compile())
        # SELECT 全列查询天然含 approver_id 列名；断言 WHERE 无等号条件即可
        assert "approver_id =" not in list_sql
        assert "reject_reviewer_id =" not in list_sql

    async def test_count_metrics_by_measure(self, repo, session) -> None:
        session.execute = AsyncMock(return_value=_FakeResult(row=3))
        assert await repo.count_metrics_by_measure(1) == 3


# ---------- Service ----------


@pytest.fixture(autouse=True)
def _mock_dict_service(monkeypatch: pytest.MonkeyPatch) -> type:
    """mock SystemDictService（category/measure_format 字典化后 service 层校验的依赖）。

    默认「类型已配置」：list_by_type 非空 → 走 validate_dict_value；
    validate_dict_value 默认放行（模拟值已收录），可设 reject_values 模拟未收录拦截。
    测试可通过 ``_mock_dict_service.configured = False`` 模拟「字典未配置」回退枚举。
    ``extra_map``：code → 扩展属性（度量格式联动默认单位/小数位），命中才走字典联动，
    未命中抛 AttributeError → 服务层回退枚举常量（对齐真实 DB 字典未配 extra 场景）。
    """
    from app.core.exceptions import NotFoundError

    class _FakeItem:
        def __init__(self, extra: dict) -> None:
            self.extra = extra

    class _FakeDictService:
        configured = True  # False → list_by_type 返回空（回退枚举种子校验）
        reject_values: set[str] = set()  # 非空 → 这些值 validate 时抛 NotFoundError
        extra_map: dict[str, dict] = {}  # code → extra（度量格式联动默认）

        def __init__(self, db: object) -> None:
            self.db = db

        async def list_by_type(self, dict_type: str, status: str | None = "active") -> list:
            return [object()] if self.__class__.configured else []

        async def validate_dict_value(self, dict_type: str, code: str) -> object:
            if code in self.__class__.reject_values:
                raise NotFoundError(
                    f"字典值不存在: {dict_type}/{code}", error_code="DICT_VALUE_NOT_FOUND"
                )
            return object()

        async def get_item(self, dict_type: str, code: str) -> object:
            extra = self.__class__.extra_map.get(code)
            if extra is None:
                raise AttributeError("no extra")
            return _FakeItem(extra)

    monkeypatch.setattr(
        "app.services.system_dict.service.SystemDictService", _FakeDictService
    )
    return _FakeDictService


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

    async def test_create_rejects_unknown_category_via_dict(
        self, _mock_dict_service: type
    ) -> None:
        """分类字典化：字典已配置时未收录值在 service 层拦截（DICT_VALUE_NOT_FOUND）。"""
        _mock_dict_service.reject_values = {"BOGUS"}
        svc, repo = await _svc()
        with pytest.raises(NotFoundError):
            await svc.create_measure(
                MeasureCreate(measure_code="m", name="x", domain="y", category="BOGUS")
            )

    async def test_create_accepts_dict_custom_category(self) -> None:
        """分类字典化：字典已配置且收录的自定义值可通过（不再硬编码枚举）。"""
        svc, repo = await _svc()
        out = await svc.create_measure(
            MeasureCreate(measure_code="m", name="运营类", domain="y", category="OPERATION")
        )
        assert out.category == "OPERATION"

    async def test_create_fallback_to_enum_when_dict_unconfigured(
        self, _mock_dict_service: type
    ) -> None:
        """分类字典化：字典未配置（空表/未种子）→ 回退枚举种子值校验。"""
        from app.core.exceptions import ValidationError

        svc, repo = await _svc()
        _mock_dict_service.configured = False
        try:
            with pytest.raises(ValidationError):
                await svc._validate_category("BOGUS")  # noqa: SLF001 未配置时枚举兜底拦截
            await svc._validate_category("FLOW")  # noqa: SLF001 枚举种子值放行
        finally:
            _mock_dict_service.configured = True

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

    async def test_update_rejects_unknown_category_via_dict(
        self, _mock_dict_service: type
    ) -> None:
        """分类字典化：更新时字典已配置且未收录值在 service 层拦截。"""
        _mock_dict_service.reject_values = {"BOGUS"}
        svc, repo = await _svc()
        m = _m("amt", category="OTHER")
        repo.get = AsyncMock(return_value=m)
        with pytest.raises(NotFoundError):
            await svc.update_measure("amt", MeasureUpdate(category="BOGUS"))
        # 拦截发生在赋值前，原值保持
        assert m.category == "OTHER"

    async def test_create_rejects_unknown_format_via_dict(
        self, _mock_dict_service: type
    ) -> None:
        """格式字典化：字典已配置时未收录值在 service 层拦截（DICT_VALUE_NOT_FOUND）。"""
        _mock_dict_service.reject_values = {"BAD"}
        svc, repo = await _svc()
        with pytest.raises(NotFoundError):
            await svc.create_measure(
                MeasureCreate(measure_code="m", name="x", measure_format="BAD", domain="y")
            )

    async def test_create_accepts_dict_custom_format_with_extra(
        self, _mock_dict_service: type
    ) -> None:
        """格式字典化：字典已收录的自定义格式可通过，且默认单位/小数位按字典 extra 联动。"""
        _mock_dict_service.extra_map = {"PERCENT": {"unit": "%", "decimal": 2}}
        svc, repo = await _svc()
        out = await svc.create_measure(
            MeasureCreate(measure_code="m", name="占比", measure_format="PERCENT", domain="y")
        )
        assert out.measure_format == "PERCENT"
        assert out.default_unit == "%"
        assert out.default_decimal_places == 2

    async def test_create_fallback_to_enum_when_format_dict_unconfigured(
        self, _mock_dict_service: type
    ) -> None:
        """格式字典化：字典未配置（空表/未种子）→ 回退枚举种子值校验。"""
        from app.core.exceptions import ValidationError

        svc, repo = await _svc()
        _mock_dict_service.configured = False
        try:
            with pytest.raises(ValidationError):
                await svc._validate_format("BAD")  # noqa: SLF001 未配置时枚举兜底拦截
            await svc._validate_format("AMOUNT")  # noqa: SLF001 枚举种子值放行
        finally:
            _mock_dict_service.configured = True

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


# ---------- 生命周期（reactivate/delete/restore） ----------


class TestMeasureLifecycle:
    async def test_reactivate_requires_deprecated(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="DRAFT"))
        with pytest.raises(UnisenseError):
            await svc.reactivate_measure("amt")

    async def test_reactivate_sets_draft(self) -> None:
        svc, repo = await _svc()
        m = _m("amt", status="DEPRECATED")
        repo.get = AsyncMock(return_value=m)
        out = await svc.reactivate_measure("amt")
        assert out.status == "DRAFT"

    async def test_delete_rejects_review(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="REVIEW"))
        with pytest.raises(UnisenseError) as exc:
            await svc.delete_measure("amt", actor_id=1, role="platform_admin")
        assert exc.value.error_code == "INVALID_STATE"

    async def test_delete_rejects_published(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="PUBLISHED"))
        with pytest.raises(UnisenseError):
            await svc.delete_measure("amt", actor_id=1, role="platform_admin")

    async def test_delete_requires_admin_or_owner(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="DRAFT", owner_id=5))
        with pytest.raises(UnisenseError) as exc:
            await svc.delete_measure("amt", actor_id=1, role="metric_owner")
        assert exc.value.error_code == "FORBIDDEN"

    async def test_delete_protects_referenced_measure(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="DRAFT"))
        repo.count_metrics_by_measure = AsyncMock(return_value=1)
        with pytest.raises(ConflictError):
            await svc.delete_measure("amt", actor_id=1, role="platform_admin")

    async def test_delete_draft_soft_deletes(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="DRAFT"))
        repo.count_metrics_by_measure = AsyncMock(return_value=0)
        repo.soft_delete_measure = AsyncMock()
        out = await svc.delete_measure("amt", actor_id=1, role="platform_admin")
        repo.soft_delete_measure.assert_awaited_once_with(1)
        assert out.status == "DRAFT"

    async def test_delete_deprecated_by_owner(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="DEPRECATED", owner_id=1))
        repo.count_metrics_by_measure = AsyncMock(return_value=0)
        repo.soft_delete_measure = AsyncMock()
        await svc.delete_measure("amt", actor_id=1, role="metric_owner")
        repo.soft_delete_measure.assert_awaited_once_with(1)

    async def test_restore_requires_deleted(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="DRAFT"))
        with pytest.raises(UnisenseError):
            await svc.restore_measure("amt", actor_id=1, role="platform_admin")

    async def test_restore_requires_admin_or_owner(self) -> None:
        from datetime import UTC, datetime

        svc, repo = await _svc()
        m = _m("amt", status="DRAFT", owner_id=5)
        m.deleted_at = datetime.now(UTC)
        repo.get = AsyncMock(return_value=m)
        with pytest.raises(UnisenseError) as exc:
            await svc.restore_measure("amt", actor_id=1, role="metric_owner")
        assert exc.value.error_code == "FORBIDDEN"

    async def test_restore_clears_deleted_at(self) -> None:
        from datetime import UTC, datetime

        svc, repo = await _svc()
        m = _m("amt", status="DRAFT", owner_id=1)
        m.deleted_at = datetime.now(UTC)
        repo.get = AsyncMock(return_value=m)
        repo.restore_measure = AsyncMock()
        await svc.restore_measure("amt", actor_id=1, role="metric_owner")
        repo.restore_measure.assert_awaited_once_with(1)

    # ---- 已删记录不可变（_require 加固）----

    async def test_require_rejects_deleted_for_update(self) -> None:
        from datetime import UTC, datetime

        svc, repo = await _svc()
        m = _m("amt", status="DRAFT")
        m.deleted_at = datetime.now(UTC)
        repo.get = AsyncMock(return_value=m)
        with pytest.raises(UnisenseError) as exc:
            await svc.update_measure("amt", MeasureUpdate(name="改名"))
        assert exc.value.error_code == "INVALID_STATE"

    async def test_require_rejects_deleted_for_publish(self) -> None:
        from datetime import UTC, datetime

        svc, repo = await _svc()
        m = _m("amt", status="DRAFT")
        m.deleted_at = datetime.now(UTC)
        repo.get = AsyncMock(return_value=m)
        with pytest.raises(UnisenseError):
            await svc.publish_measure("amt")

    async def test_require_rejects_deleted_for_approve(self) -> None:
        from datetime import UTC, datetime

        svc, repo = await _svc()
        m = _m("amt", status="REVIEW")
        m.deleted_at = datetime.now(UTC)
        repo.get = AsyncMock(return_value=m)
        with pytest.raises(UnisenseError):
            await svc.approve_measure("amt", MeasureApproveRequest(), 1, "platform_admin")

    # ---- 彻底删除（purge，回收站硬删）----

    async def test_purge_requires_deleted(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="DRAFT"))
        with pytest.raises(UnisenseError) as exc:
            await svc.purge_measure("amt", actor_id=1, role="platform_admin")
        assert exc.value.error_code == "INVALID_STATE"

    async def test_purge_requires_admin(self) -> None:
        from datetime import UTC, datetime

        svc, repo = await _svc()
        m = _m("amt", status="DRAFT")
        m.deleted_at = datetime.now(UTC)
        repo.get = AsyncMock(return_value=m)
        with pytest.raises(UnisenseError) as exc:
            await svc.purge_measure("amt", actor_id=1, role="domain_admin")
        assert exc.value.error_code == "FORBIDDEN"

    async def test_purge_protects_referenced_measure(self) -> None:
        from datetime import UTC, datetime

        svc, repo = await _svc()
        m = _m("amt", status="DRAFT")
        m.deleted_at = datetime.now(UTC)
        repo.get = AsyncMock(return_value=m)
        repo.count_metrics_by_measure = AsyncMock(return_value=1)
        with pytest.raises(ConflictError):
            await svc.purge_measure("amt", actor_id=1, role="platform_admin")

    async def test_purge_deletes_row(self) -> None:
        from datetime import UTC, datetime

        svc, repo = await _svc()
        m = _m("amt", status="DRAFT")
        m.deleted_at = datetime.now(UTC)
        repo.get = AsyncMock(return_value=m)
        repo.count_metrics_by_measure = AsyncMock(return_value=0)
        repo.purge_measure = AsyncMock()
        await svc.purge_measure("amt", actor_id=1, role="platform_admin")
        repo.purge_measure.assert_awaited_once_with(1)


# ---------- 审核流（submit/approve/reject，对齐指标审核流 TD §13） ----------


async def _review_svc(measure: MeasureCatalog) -> tuple[MeasureCatalogService, MagicMock]:
    """构造审核服务：mock repo.get 返回指定度量，notify 静默。"""
    svc, repo = await _svc()
    repo.get = AsyncMock(return_value=measure)
    svc._notify_reviewers = AsyncMock()  # noqa: SLF001
    svc._notify_submitter = AsyncMock()  # noqa: SLF001
    return svc, repo


class TestMeasureReviewFlow:
    async def test_submit_requires_draft(self) -> None:
        svc, _ = await _review_svc(_m("amt", status="PUBLISHED"))
        with pytest.raises(UnisenseError):
            await svc.submit_measure(
                "amt", MeasureSubmitRequest(change_reason="发布新度量"), 1, "metric_owner"
            )

    async def test_submit_requires_stat_caliber(self) -> None:
        svc, _ = await _review_svc(_m("amt", status="DRAFT", stat_caliber=None))
        with pytest.raises(UnisenseError):
            await svc.submit_measure(
                "amt", MeasureSubmitRequest(change_reason="发布新度量"), 1, "metric_owner"
            )

    async def test_submit_sets_review_with_reviewer(self) -> None:
        m = _m("amt", status="DRAFT", stat_caliber="收费明细求和")
        svc, _ = await _review_svc(m)
        out = await svc.submit_measure(
            "amt",
            MeasureSubmitRequest(
                change_reason="发布新度量", reviewer_id=9, reviewer_type="user"
            ),
            1,
            "metric_owner",
        )
        assert out.status == "REVIEW"
        assert out.submitted_by == 1
        assert out.reviewer_id == 9
        assert out.reviewer_type == "user"

    async def test_submit_owner_only(self) -> None:
        svc, _ = await _review_svc(_m("amt", owner_id=5, stat_caliber="x"))
        with pytest.raises(UnisenseError):
            await svc.submit_measure(
                "amt", MeasureSubmitRequest(change_reason="越权提交"), 1, "metric_owner"
            )

    async def test_approve_requires_review(self) -> None:
        svc, _ = await _review_svc(_m("amt", status="DRAFT"))
        with pytest.raises(UnisenseError):
            await svc.approve_measure(
                "amt", MeasureApproveRequest(), 9, "domain_admin", user_domain="sales"
            )

    async def test_approve_self_review_blocked(self) -> None:
        # 指派给提交人本人（自审场景）：评审人身份校验通过，自审禁止拦截
        m = _m("amt", status="REVIEW", submitted_by=1, reviewer_type="user", reviewer_id=1)
        svc, _ = await _review_svc(m)
        with pytest.raises(UnisenseError) as exc:
            await svc.approve_measure("amt", MeasureApproveRequest(), 1, "reviewer")
        assert exc.value.error_code == "SELF_REVIEW_BLOCKED"

    async def test_approve_sets_published(self) -> None:
        m = _m("amt", status="REVIEW", submitted_by=1)
        svc, _ = await _review_svc(m)
        out = await svc.approve_measure(
            "amt", MeasureApproveRequest(comment="口径合理"), 9, "domain_admin", user_domain="sales"
        )
        assert out.status == "PUBLISHED"
        assert out.approver_id == 9
        assert out.reviewed_at is not None

    async def test_approve_assigned_reviewer_only(self) -> None:
        m = _m("amt", status="REVIEW", submitted_by=1, reviewer_type="user", reviewer_id=9)
        svc, _ = await _review_svc(m)
        # 非被指派评审人（domain_admin 兜底不覆盖 user 指派）被拒
        with pytest.raises(UnisenseError):
            await svc.approve_measure(
                "amt", MeasureApproveRequest(), 5, "domain_admin", user_domain="sales"
            )
        # 被指派者通过
        out = await svc.approve_measure("amt", MeasureApproveRequest(), 9, "reviewer")
        assert out.status == "PUBLISHED"

    async def test_approve_domain_reviewer_scope(self) -> None:
        m = _m(
            "amt",
            status="REVIEW",
            submitted_by=1,
            reviewer_type="domain",
            reviewer_domain="medical_fee",
        )
        svc, _ = await _review_svc(m)
        # 异域评审被拒
        with pytest.raises(UnisenseError):
            await svc.approve_measure(
                "amt", MeasureApproveRequest(), 5, "reviewer", user_domain="sales"
            )
        # 同域评审通过
        out = await svc.approve_measure(
            "amt", MeasureApproveRequest(), 5, "reviewer", user_domain="medical_fee"
        )
        assert out.status == "PUBLISHED"

    async def test_approve_unassigned_cross_domain_rejected(self) -> None:
        """X-3 越权加固：未指派评审的实体，异域 domain_admin 不可审批（此前只查角色）。"""
        m = _m("amt", status="REVIEW", submitted_by=1)
        svc, _ = await _review_svc(m)
        with pytest.raises(UnisenseError) as exc:
            await svc.approve_measure(
                "amt", MeasureApproveRequest(), 9, "domain_admin", user_domain="marketing"
            )
        assert exc.value.error_code == "FORBIDDEN_REVIEWER"

    async def test_reject_requires_review(self) -> None:
        svc, _ = await _review_svc(_m("amt", status="PUBLISHED"))
        with pytest.raises(UnisenseError):
            await svc.reject_measure(
                "amt",
                MeasureRejectRequest(reason="口径不清"),
                9,
                "domain_admin",
                user_domain="sales",
            )

    async def test_reject_sets_draft_with_reason(self) -> None:
        m = _m("amt", status="REVIEW", submitted_by=1)
        svc, _ = await _review_svc(m)
        out = await svc.reject_measure(
            "amt",
            MeasureRejectRequest(reason="统计口径与业务不符"),
            9,
            "domain_admin",
            user_domain="sales",
        )
        assert out.status == "DRAFT"
        assert out.reject_reason == "统计口径与业务不符"
        assert out.reject_reviewer_id == 9
        assert out.rejected_at is not None

    async def test_review_blocks_update(self) -> None:
        svc, repo = await _svc()
        repo.get = AsyncMock(return_value=_m("amt", status="REVIEW"))
        with pytest.raises(UnisenseError):
            await svc.update_measure("amt", MeasureUpdate(name="改名"))
        assert repo.commit.await_count == 0


# ---------- AI 推断（auto_suggest） ----------


async def _suggest_svc() -> tuple[MeasureCatalogService, MagicMock]:
    """构造推断服务（mock db；domain 查询走 execute，返回空即可）。"""
    db = MagicMock()
    db.execute = AsyncMock(return_value=_FakeResult(rows=[]))
    svc = MeasureCatalogService(db)
    svc._repo = MagicMock()  # noqa: SLF001
    return svc, db


def _patch_llm(enabled: bool = False, content: str = "{}"):
    """patch LlmConfigService：enabled 控制是否走 LLM，content 为 LLM 返回文本。"""
    client = MagicMock()
    client.enabled = enabled
    resp = MagicMock()
    resp.get.return_value = content
    client.chat = AsyncMock(return_value=resp)
    cls = MagicMock()
    cls.return_value.build_client = AsyncMock(return_value=client)
    return patch(
        "app.services.llm.config_service.LlmConfigService", cls
    ), cls.return_value.build_client


class TestMeasureAutoSuggest:
    async def test_rule_flow_register(self) -> None:
        """门诊挂号人次 → NUMERIC/人次/0/FLOW（规则确定性推断）。"""
        svc, _ = await _suggest_svc()
        with _patch_llm(enabled=False)[0]:
            resp = await svc.auto_suggest(
                MeasureAutoSuggestRequest(name="门诊挂号人次", domain="outpatient")
            )
        f = resp.fields
        assert f["measure_format"].value == "NUMERIC"
        assert f["default_unit"].value == "人次"
        assert f["default_decimal_places"].value == 0
        assert f["category"].value == "FLOW"
        assert f["measure_code"].value == f"outpatient_{slugify_code('门诊挂号人次')}"
        assert f["source_system"].value == ["HIS"]

    async def test_rule_fee_amount(self) -> None:
        """门诊收费金额 → AMOUNT/CNY/2/FEE。"""
        svc, _ = await _suggest_svc()
        with _patch_llm(enabled=False)[0]:
            resp = await svc.auto_suggest(
                MeasureAutoSuggestRequest(name="门诊收费金额", domain="medical_fee")
            )
        f = resp.fields
        assert f["measure_format"].value == "AMOUNT"
        assert f["default_unit"].value == "CNY"
        assert f["default_decimal_places"].value == 2
        assert f["category"].value == "FEE"

    async def test_rule_ratio_drug(self) -> None:
        """门诊药占比 → RATIO/小数/4/QUALITY。"""
        svc, _ = await _suggest_svc()
        with _patch_llm(enabled=False)[0]:
            resp = await svc.auto_suggest(
                MeasureAutoSuggestRequest(name="门诊药占比", domain="medication")
            )
        f = resp.fields
        assert f["measure_format"].value == "RATIO"
        assert f["default_unit"].value == "小数"
        assert f["default_decimal_places"].value == 4
        assert f["category"].value == "QUALITY"

    async def test_llm_unavailable_falls_back_to_rule(self) -> None:
        """LLM 不可用（disabled）→ 规则兜底，不抛错不阻断。"""
        svc, _ = await _suggest_svc()
        with _patch_llm(enabled=False)[0]:
            resp = await svc.auto_suggest(MeasureAutoSuggestRequest(name="门诊收费金额"))
        f = resp.fields
        assert f["measure_format"].value == "AMOUNT"
        assert f["synonyms"].value == []  # 规则兜底：同义词为空

    async def test_llm_enhances_synonyms_and_caliber(self) -> None:
        """LLM 可用且返回合法 JSON → 增强同义词/统计口径/业务域。"""
        llm_json = (
            '{"synonyms": ["门诊收入", "诊费"], "stat_caliber": "收费明细按结算日期去重后求和", '
            '"domain": "medical_fee", "source_system": ["HIS"], "description": "门诊收费总额"}'
        )
        svc, _ = await _suggest_svc()
        with _patch_llm(enabled=True, content=llm_json)[0]:
            resp = await svc.auto_suggest(
                MeasureAutoSuggestRequest(name="门诊收费金额", domain="medical_fee")
            )
        f = resp.fields
        assert f["synonyms"].value == ["门诊收入", "诊费"]
        assert f["stat_caliber"].value == "收费明细按结算日期去重后求和"
        assert f["domain"].value == "medical_fee"
        assert f["synonyms"].source == "llm"

    async def test_llm_domain_not_in_candidates_dropped(self) -> None:
        """LLM 推断域不在现有域集合 → 丢弃（防脏域），规则域保留。"""
        llm_json = '{"domain": "not_exist_domain", "synonyms": ["别名"]}'
        svc, _ = await _suggest_svc()
        with _patch_llm(enabled=True, content=llm_json)[0]:
            resp = await svc.auto_suggest(
                MeasureAutoSuggestRequest(name="门诊收费金额", domain="medical_fee")
            )
        f = resp.fields
        assert f["domain"].value == "medical_fee"  # 规则值保留（LLM 脏域被丢弃）
        assert f["synonyms"].value == ["别名"]  # 合法字段仍生效

    async def test_llm_bad_json_falls_back(self) -> None:
        """LLM 返回非法 JSON → 解析失败降级规则，不抛错。"""
        svc, _ = await _suggest_svc()
        with _patch_llm(enabled=True, content="不是JSON{{")[0]:
            resp = await svc.auto_suggest(MeasureAutoSuggestRequest(name="门诊收费金额"))
        f = resp.fields
        assert f["measure_format"].value == "AMOUNT"
        assert f["category"].value == "FEE"


class TestMeasureInferSynonyms:
    """编辑弹窗「AI 生成同义词」：基于名称/描述生成同义词候选（不落库）。"""

    async def test_synonyms_success(self) -> None:
        """LLM 返回 JSON 数组 → 解析为同义词列表。"""
        from app.services.measure_catalog.schemas import MeasureInferSynonymsRequest

        svc, _ = await _suggest_svc()
        with _patch_llm(enabled=True, content='["门诊收入","诊费","挂号费"]')[0]:
            result = await svc.infer_synonyms(
                MeasureInferSynonymsRequest(name="门诊收费金额", description="门诊收费")
            )
        assert result == ["门诊收入", "诊费", "挂号费"]

    async def test_synonyms_llm_disabled_raises(self) -> None:
        """LLM 不可用 → LLM_INFER_UNAVAILABLE（与 infer-dict-description 对齐）。"""
        from app.core.exceptions import BusinessError

        svc, _ = await _suggest_svc()
        with _patch_llm(enabled=False)[0], pytest.raises(BusinessError) as ei:
            await svc.infer_synonyms("门诊收费金额")
        assert ei.value.error_code == "LLM_INFER_UNAVAILABLE"

    async def test_synonyms_empty_content_raises(self) -> None:
        """LLM 返回空内容 → LLM_INFER_UNAVAILABLE。"""
        from app.core.exceptions import BusinessError

        svc, _ = await _suggest_svc()
        with _patch_llm(enabled=True, content="")[0], pytest.raises(BusinessError) as ei:
            await svc.infer_synonyms("门诊收费金额")
        assert ei.value.error_code == "LLM_INFER_UNAVAILABLE"

    async def test_synonyms_not_array_returns_empty(self) -> None:
        """LLM 返回有效但非数组 → 空列表（不报错，前端提示未生成）。"""
        svc, _ = await _suggest_svc()
        with _patch_llm(enabled=True, content='{"name":"x"}')[0]:
            result = await svc.infer_synonyms("门诊收费金额")
        assert result == []

    async def test_synonyms_abnormal_content_raises(self) -> None:
        """LLM 返回流式协议垃圾 → LLM_INFER_UNAVAILABLE（宽松校验拦截）。"""
        from app.core.exceptions import BusinessError

        svc, _ = await _suggest_svc()
        garbage = 'data: {"type":"message"}\\n' * 500
        with _patch_llm(enabled=True, content=garbage)[0], pytest.raises(BusinessError) as ei:
            await svc.infer_synonyms("门诊收费金额")
        assert ei.value.error_code == "LLM_INFER_UNAVAILABLE"
