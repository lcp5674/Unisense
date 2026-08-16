"""DimensionRepository 单测（app/services/dimension/repository.py）。

使用轻量 AsyncSession 替身覆盖全部 CRUD 方法，并断言传入 execute 的查询条件。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.dimension.repository import DimensionRepository


class _FakeResult:
    """模拟 SQLAlchemy Result：同时支持 scalar_one_or_none 与 scalars().all()。"""

    def __init__(self, row: object | None = None, rows: list | None = None) -> None:
        self._row = row
        self._rows = rows or []

    def scalar_one_or_none(self) -> object | None:
        return self._row

    def scalar(self) -> object | None:
        return self._row

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list:
        return self._rows


@pytest.fixture
def session() -> MagicMock:
    """AsyncSession 替身：add 为同步方法，execute/flush/commit 为异步。"""
    s = MagicMock()
    s.execute = AsyncMock()
    s.flush = AsyncMock()
    s.commit = AsyncMock()
    return s


@pytest.fixture
def repo(session: AsyncMock) -> DimensionRepository:
    return DimensionRepository(session)


def _first_stmt(session: AsyncMock):
    """取最近一次 execute 的语句，并编译为 SQL 用于条件断言。"""
    stmt = session.execute.call_args[0][0]
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def _rows_stmt(session: AsyncMock):
    """取第 2 次 execute（rows 查询）的语句——count 查询在第 1 次。"""
    stmt = session.execute.call_args_list[1][0][0]
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


class TestDimensionCRUD:
    async def test_save_dimension_adds_and_flushes(self, repo, session) -> None:
        dim = SimpleNamespace(dim_code="region")
        out = await repo.save_dimension(dim)
        assert out is dim
        session.add.assert_called_once_with(dim)
        session.flush.assert_awaited_once()

    async def test_get_dimension_queries_by_code(self, repo, session) -> None:
        dim = SimpleNamespace(dim_code="region")
        session.execute = AsyncMock(return_value=_FakeResult(row=dim))

        out = await repo.get_dimension("region")
        assert out is dim
        assert "'region'" in _first_stmt(session)

    async def test_get_dimension_returns_none(self, repo, session) -> None:
        session.execute = AsyncMock(return_value=_FakeResult(row=None))
        assert await repo.get_dimension("missing") is None

    async def test_list_dimensions_no_filter(self, repo, session) -> None:
        dims = [SimpleNamespace(dim_code="a"), SimpleNamespace(dim_code="b")]
        # list_dimensions 返回 (维度, 绑定指标数) 二元组（LEFT JOIN 聚合）
        session.execute = AsyncMock(
            side_effect=[
                _FakeResult(row=len(dims)),
                _FakeResult(rows=[(d, 0) for d in dims]),
            ]
        )

        items, total = await repo.list_dimensions(None, None)
        assert [d for d, _ in items] == dims
        assert total == len(dims)
        stmt = _rows_stmt(session)
        assert "dimension" in stmt

    async def test_list_dimensions_filters_deleted(self, repo, session) -> None:
        """软删维度不出现在列表（deleted_at IS NULL 过滤）。"""
        session.execute = AsyncMock(side_effect=[_FakeResult(row=0), _FakeResult(rows=[])])
        await repo.list_dimensions(None, None)
        stmt = _rows_stmt(session)
        assert "deleted_at IS NULL" in stmt

    async def test_list_dimensions_with_domain_and_status(self, repo, session) -> None:
        session.execute = AsyncMock(side_effect=[_FakeResult(row=0), _FakeResult(rows=[])])
        await repo.list_dimensions("sales", "PUBLISHED")
        stmt = _rows_stmt(session)
        assert "'sales'" in stmt
        assert "'PUBLISHED'" in stmt

    async def test_list_dimensions_with_keyword(self, repo, session) -> None:
        """keyword 命中编码/名称/描述 LIKE 条件。"""
        session.execute = AsyncMock(side_effect=[_FakeResult(row=0), _FakeResult(rows=[])])
        await repo.list_dimensions(None, None, "region")
        stmt = _rows_stmt(session)
        assert "%region%" in stmt

    async def test_list_dimensions_keyword_escapes_wildcards(self, repo, session) -> None:
        """LIKE 通配符（% / _）须转义，防模糊放大。"""
        session.execute = AsyncMock(side_effect=[_FakeResult(row=0), _FakeResult(rows=[])])
        await repo.list_dimensions(None, None, "100%_x")
        stmt = _rows_stmt(session)
        # 转义符为 /：% → /% 、_ → /_，并生成 ESCAPE '/' 子句（防模糊放大）
        assert "100/%/_x" in stmt
        assert "ESCAPE '/'" in stmt


class TestMemberCRUD:
    async def test_save_member_adds_and_flushes(self, repo, session) -> None:
        member = SimpleNamespace(member_code="east")
        out = await repo.save_member(member)
        assert out is member
        session.add.assert_called_once_with(member)

    async def test_list_members_filters_by_dim_code(self, repo, session) -> None:
        members = [SimpleNamespace(member_code="east")]
        session.execute = AsyncMock(return_value=_FakeResult(rows=members))

        out = await repo.list_members("region")
        assert out == members
        assert "'region'" in _first_stmt(session)

    async def test_get_member_filters_by_dim_and_code(self, repo, session) -> None:
        member = SimpleNamespace(member_code="east")
        session.execute = AsyncMock(return_value=_FakeResult(row=member))

        out = await repo.get_member("region", "east")
        assert out is member
        stmt = _first_stmt(session)
        assert "'region'" in stmt
        assert "'east'" in stmt

    async def test_get_member_returns_none(self, repo, session) -> None:
        session.execute = AsyncMock(return_value=_FakeResult(row=None))
        assert await repo.get_member("region", "missing") is None


class TestMappingCRUD:
    async def test_save_mapping_adds_and_flushes(self, repo, session) -> None:
        mapping = SimpleNamespace(source_dim_code="a", target_dim_code="b")
        out = await repo.save_mapping(mapping)
        assert out is mapping
        session.add.assert_called_once_with(mapping)

    async def test_list_mappings_no_filter(self, repo, session) -> None:
        session.execute = AsyncMock(return_value=_FakeResult(rows=[]))
        await repo.list_mappings(None)
        stmt = _first_stmt(session)
        assert "dimension_mapping" in stmt

    async def test_list_mappings_filter_by_source(self, repo, session) -> None:
        session.execute = AsyncMock(return_value=_FakeResult(rows=[]))
        await repo.list_mappings("src_dim")
        assert "'src_dim'" in _first_stmt(session)


class TestMetricDimensionCRUD:
    async def test_save_metric_dimension(self, repo, session) -> None:
        md = SimpleNamespace(metric_id=1, dim_code="region")
        out = await repo.save_metric_dimension(md)
        assert out is md
        session.add.assert_called_once_with(md)

    async def test_list_metric_dimensions_filters_by_metric_id(self, repo, session) -> None:
        rows = [SimpleNamespace(dim_code="region")]
        session.execute = AsyncMock(return_value=_FakeResult(rows=rows))
        out = await repo.list_metric_dimensions(42)
        assert out == rows
        assert "42" in _first_stmt(session)


class TestReconciliationCRUD:
    async def test_save_reconciliation(self, repo, session) -> None:
        rec = SimpleNamespace(metric_id=1)
        out = await repo.save_reconciliation(rec)
        assert out is rec
        session.add.assert_called_once_with(rec)

    async def test_list_reconciliations_no_filter(self, repo, session) -> None:
        session.execute = AsyncMock(return_value=_FakeResult(rows=[]))
        await repo.list_reconciliations(None)
        assert "reconciliation" in _first_stmt(session)

    async def test_list_reconciliations_filter_by_status(self, repo, session) -> None:
        session.execute = AsyncMock(return_value=_FakeResult(rows=[]))
        await repo.list_reconciliations("APPROVED")
        assert "'APPROVED'" in _first_stmt(session)

    async def test_get_reconciliation_by_id(self, repo, session) -> None:
        rec = SimpleNamespace(id=7)
        session.execute = AsyncMock(return_value=_FakeResult(row=rec))
        out = await repo.get_reconciliation(7)
        assert out is rec
        assert "7" in _first_stmt(session)

    async def test_get_reconciliation_returns_none(self, repo, session) -> None:
        session.execute = AsyncMock(return_value=_FakeResult(row=None))
        assert await repo.get_reconciliation(999) is None


class TestCommit:
    async def test_commit(self, repo, session) -> None:
        await repo.commit()
        session.commit.assert_awaited_once()
