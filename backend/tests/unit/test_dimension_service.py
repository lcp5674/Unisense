"""维度管理服务单元测试（TD §12.15 / FR-05 / FR-09）。"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.dimension import (
    Dimension,
    DimensionMember,
    Reconciliation,
)
from app.services.dimension.schemas import (
    DimensionCreate,
    DimensionMemberCreate,
    DimensionMemberUpdate,
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
    repo.get_member = AsyncMock(return_value=None)
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
    repo.get_dimension = AsyncMock(
        side_effect=[
            Dimension(
                id=1,
                dim_code="geo_customer_region",
                name="x",
                domain="geo",
                type="SCD1",
                status="DRAFT",
                owner_id=1,
            ),
            None,
            None,
        ]
    )
    payload = DimensionCreate(dim_code=None, name="Customer Region", domain="geo", owner_id=1)
    out = await svc.create_dimension(payload, 1)
    assert out.dim_code == "geo_customer_region_2"


async def test_create_member_auto_generates_code() -> None:
    """member_code 缺省时由系统自动生成（dim_code_name slug，维度内唯一）。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=Dimension(
            id=1,
            dim_code="geo_region",
            name="地区",
            domain="geo",
            type="SCD1",
            status="DRAFT",
            owner_id=1,
        )
    )
    payload = DimensionMemberCreate(
        dim_code="geo_region",
        member_code=None,
        member_name="华东",
        status="PUBLISHED",
    )
    out = await svc.create_member(payload)
    # 纯中文名 → 英文 slug（华东 → east_china），带维度前缀
    assert out.member_code == "geo_region_east_china"


async def test_create_member_auto_code_uses_name_slug() -> None:
    """member_code 缺省且 member_name 含 ASCII 时按 dim_code_name slug 生成。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=Dimension(
            id=1,
            dim_code="geo_region",
            name="地区",
            domain="geo",
            type="SCD1",
            status="DRAFT",
            owner_id=1,
        )
    )
    payload = DimensionMemberCreate(
        dim_code="geo_region",
        member_code=None,
        member_name="East China",
        status="PUBLISHED",
    )
    out = await svc.create_member(payload)
    assert out.member_code == "geo_region_east_china"


# ---- 编码自动生成：slug 缺失降级分支与中文转英文 ----
async def test_generate_dim_code_name_only() -> None:
    """domain 无可提取字符（纯标点）时降级为 dim_{name_slug}。"""
    svc, repo = await _svc()
    payload = DimensionCreate(dim_code=None, name="Customer Region", domain="!@#", owner_id=1)
    out = await svc.create_dimension(payload, 1)
    assert out.dim_code == "dim_customer_region"


async def test_generate_dim_code_domain_only() -> None:
    """name 无可提取字符（纯标点）时降级为 dim_{domain_slug}。"""
    svc, repo = await _svc()
    payload = DimensionCreate(dim_code=None, name="!!!", domain="geo", owner_id=1)
    out = await svc.create_dimension(payload, 1)
    assert out.dim_code == "dim_geo"


async def test_generate_dim_code_both_empty() -> None:
    """domain/name 均为纯标点无可提取字符时回退为 dim。"""
    svc, repo = await _svc()
    payload = DimensionCreate(dim_code=None, name="!!!", domain="@@@", owner_id=1)
    out = await svc.create_dimension(payload, 1)
    assert out.dim_code == "dim"


async def test_generate_dim_code_chinese_to_english() -> None:
    """中文 domain/name → 英文 slug（地理 + 地区 → geo_area）。"""
    svc, repo = await _svc()
    payload = DimensionCreate(dim_code=None, name="地区", domain="地理", owner_id=1)
    out = await svc.create_dimension(payload, 1)
    assert out.dim_code == "geo_area"


# ---- 缓慢变化维类型枚举校验 ----
async def test_create_dimension_invalid_type_rejected() -> None:
    """非法 SCD 类型（如 SCD9）应在服务层 4xx，而非 DB Enum 500。"""
    svc, repo = await _svc()
    payload = DimensionCreate(dim_code="dim1", name="地区", domain="geo", type="SCD9", owner_id=1)
    with pytest.raises(ValidationError):
        await svc.create_dimension(payload, 1)


async def test_create_dimension_scd3_accepted() -> None:
    """SCD3（有限历史）属生产全集，应被接受并落库。"""
    svc, repo = await _svc()
    payload = DimensionCreate(dim_code="dim3", name="地区", domain="geo", type="SCD3", owner_id=1)
    out = await svc.create_dimension(payload, 1)
    assert out.type == "SCD3"


# ---- 成员层级路径自动推测 ----
async def test_create_member_auto_resolves_root_path() -> None:
    """根成员（无父级）path 自动推测为 /{member_code}。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=Dimension(id=1, dim_code="geo_region", name="地区", domain="geo", type="SCD1", status="DRAFT", owner_id=1)
    )
    payload = DimensionMemberCreate(dim_code="geo_region", member_code="east", member_name="华东", status="PUBLISHED")
    out = await svc.create_member(payload)
    assert out.path == "/east"


async def test_create_member_auto_resolves_child_path() -> None:
    """子成员（指定父级）path 自动推测为 父path/子member_code。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=Dimension(id=1, dim_code="geo_region", name="地区", domain="geo", type="SCD1", status="DRAFT", owner_id=1)
    )
    parent = DimensionMember(id=1, dim_code="geo_region", member_code="east", member_name="华东", parent_code=None, path="/east", status="PUBLISHED")
    repo.list_members = AsyncMock(return_value=[parent])
    payload = DimensionMemberCreate(
        dim_code="geo_region", member_code="east_nanjing", member_name="南京", parent_code="east", status="PUBLISHED"
    )
    out = await svc.create_member(payload)
    assert out.path == "/east/east_nanjing"


async def test_create_member_explicit_path_kept() -> None:
    """客户端显式提供 path 时服务端不覆盖（保留手工路径）。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=Dimension(id=1, dim_code="geo_region", name="地区", domain="geo", type="SCD1", status="DRAFT", owner_id=1)
    )
    payload = DimensionMemberCreate(
        dim_code="geo_region", member_code="east", member_name="华东", path="/自定义/路径", status="PUBLISHED"
    )
    out = await svc.create_member(payload)
    assert out.path == "/自定义/路径"


# ---- 成员编辑 ----
async def test_update_member_reparent_resolves_path() -> None:
    """编辑成员改父级：自动重算 path = 新父 path + / + member_code。"""
    svc, repo = await _svc()
    member = DimensionMember(id=1, dim_code="geo_region", member_code="child", member_name="子级", parent_code=None, path="/child", status="PUBLISHED")
    repo.get_member = AsyncMock(return_value=member)
    repo.list_members = AsyncMock(
        return_value=[
            member,
            DimensionMember(id=2, dim_code="geo_region", member_code="parent", member_name="父级", parent_code=None, path="/parent", status="PUBLISHED"),
        ]
    )
    out = await svc.update_member(
        "geo_region", "child",
        DimensionMemberUpdate(parent_code="parent", member_name="子级（新）"),
    )
    assert out.member_name == "子级（新）"
    assert out.parent_code == "parent"
    assert out.path == "/parent/child"


async def test_update_member_clear_parent_to_root() -> None:
    """编辑成员置为根：parent_code="" → parent 清空，path 重算为 /{member_code}。"""
    svc, repo = await _svc()
    member = DimensionMember(id=1, dim_code="geo_region", member_code="child", member_name="子级", parent_code="parent", path="/parent/child", status="PUBLISHED")
    repo.get_member = AsyncMock(return_value=member)
    repo.list_members = AsyncMock(return_value=[member])
    out = await svc.update_member("geo_region", "child", DimensionMemberUpdate(parent_code=""))
    assert out.parent_code is None
    assert out.path == "/child"


async def test_update_member_rejects_cycle() -> None:
    """环防护：不能把成员移动到自身后代之下。"""
    svc, repo = await _svc()
    member = DimensionMember(id=1, dim_code="geo_region", member_code="root", member_name="根", parent_code=None, path="/root", status="PUBLISHED")
    descendant = DimensionMember(id=2, dim_code="geo_region", member_code="sub", member_name="子", parent_code="root", path="/root/sub", status="PUBLISHED")
    repo.get_member = AsyncMock(return_value=member)
    # 尝试把 root 挂到 sub（root 的后代）下 → 环
    repo.list_members = AsyncMock(return_value=[member, descendant])
    with pytest.raises(ConflictError):
        await svc.update_member("geo_region", "root", DimensionMemberUpdate(parent_code="sub"))


async def test_update_member_missing_raises() -> None:
    """编辑不存在的成员 → 404。"""
    svc, repo = await _svc()
    repo.get_member = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.update_member("geo_region", "nope", DimensionMemberUpdate(member_name="x"))


async def test_update_member_invalid_status_rejected() -> None:
    """非法成员状态在服务层 4xx，而非 DB Enum 500。"""
    svc, repo = await _svc()
    member = DimensionMember(id=1, dim_code="geo_region", member_code="m1", member_name="成员", parent_code=None, path="/m1", status="PUBLISHED")
    repo.get_member = AsyncMock(return_value=member)
    with pytest.raises(ValidationError):
        await svc.update_member("geo_region", "m1", DimensionMemberUpdate(status="BOGUS"))
