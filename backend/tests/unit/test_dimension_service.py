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
