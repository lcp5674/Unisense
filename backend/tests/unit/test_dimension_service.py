"""维度管理服务单元测试（TD §12.15 / FR-05 / FR-09）。"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ConflictError, NotFoundError
from app.models.dimension import (
    Dimension,
    Reconciliation,
)
from app.services.dimension.schemas import (
    DimensionCreate,
    DimensionMemberCreate,
)
from app.services.dimension.service import DimensionService


def _persist(t: Dimension) -> Dimension:
    t.id = 1
    return t


async def _svc() -> tuple[DimensionService, MagicMock]:
    db = MagicMock()
    svc = DimensionService(db)
    repo = MagicMock()
    repo.get_dimension = AsyncMock(return_value=None)
    repo.save_dimension = AsyncMock(side_effect=_persist)
    repo.save_member = AsyncMock(side_effect=lambda m: m)
    repo.list_members = AsyncMock(return_value=[])
    repo.get_reconciliation = AsyncMock(return_value=None)
    repo.save_reconciliation = AsyncMock(side_effect=lambda r: r)
    repo.commit = AsyncMock()
    svc._repo = repo  # noqa: SLF001
    return svc, repo


async def test_create_dimension_persists() -> None:
    svc, repo = await _svc()
    payload = DimensionCreate(dim_code="dim1", name="地区", domain="geo", type="SCD2", owner_id=1)
    resp = await svc.create_dimension(payload)
    assert resp.dim_code == "dim1"
    repo.save_dimension.assert_awaited()


async def test_create_dimension_conflict() -> None:
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="dim1"))
    with pytest.raises(ConflictError):
        await svc.create_dimension(
            DimensionCreate(dim_code="dim1", name="x", domain="y", owner_id=1)
        )


async def test_create_member_requires_dimension() -> None:
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.create_member(
            DimensionMemberCreate(dim_code="missing", member_code="m", member_name="n")
        )


async def test_review_reconciliation_sets_status() -> None:
    svc, repo = await _svc()
    rec = Reconciliation(id=5, metric_id=9, expected_expr="a", actual_expr="b")
    repo.get_reconciliation = AsyncMock(return_value=rec)
    out = await svc.review_reconciliation(5, MagicMock(decision="APPROVED", reviewer_id=2))
    assert out.status == "APPROVED"
    assert out.reviewed_by == 2
    assert isinstance(out.reviewed_at, datetime)
    repo.commit.assert_awaited()


async def test_create_dimension_auto_generates_code() -> None:
    """dim_code 缺省时由系统自动生成（domain_name slug）。"""
    svc, repo = await _svc()
    payload = DimensionCreate(dim_code=None, name="Customer Region", domain="geo", owner_id=1)
    out = await svc.create_dimension(payload, 1)
    assert out.dim_code == "geo_customer_region"


async def test_create_dimension_auto_code_conflict_suffix() -> None:
    """dim_code 自动生成冲突时追加 _2 后缀。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(side_effect=[
        Dimension(
            id=1, dim_code="geo_customer_region", name="x",
            domain="geo", type="SCD1", status="DRAFT", owner_id=1,
        ),
        None,
        None,
    ])
    payload = DimensionCreate(dim_code=None, name="Customer Region", domain="geo", owner_id=1)
    out = await svc.create_dimension(payload, 1)
    assert out.dim_code == "geo_customer_region_2"


async def test_create_member_auto_generates_code() -> None:
    """member_code 缺省时由系统自动生成（dim_code_name slug，维度内唯一）。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(
        id=1, dim_code="geo_region", name="地区", domain="geo",
        type="SCD1", status="DRAFT", owner_id=1,
    ))
    payload = DimensionMemberCreate(
        dim_code="geo_region", member_code=None,
        member_name="华东", status="PUBLISHED",
    )
    out = await svc.create_member(payload)
    # 纯中文名 → 回退 {dim_code}_member
    assert out.member_code == "geo_region_member"


async def test_create_member_auto_code_uses_name_slug() -> None:
    """member_code 缺省且 member_name 含 ASCII 时按 dim_code_name slug 生成。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(
        id=1, dim_code="geo_region", name="地区", domain="geo",
        type="SCD1", status="DRAFT", owner_id=1,
    ))
    payload = DimensionMemberCreate(
        dim_code="geo_region", member_code=None,
        member_name="East China", status="PUBLISHED",
    )
    out = await svc.create_member(payload)
    assert out.member_code == "geo_region_east_china"
