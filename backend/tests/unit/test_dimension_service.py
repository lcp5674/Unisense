"""维度管理服务单元测试（TD §12.15 / FR-05 / FR-09）。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import ConflictError, NotFoundError, UnisenseError, ValidationError
from app.models.dimension import (
    Dimension,
    DimensionMember,
    MetricDimension,
    Reconciliation,
)
from app.models.metric import Metric
from app.services.dimension.schemas import (
    DimensionCreate,
    DimensionMemberCreate,
    DimensionMemberUpdate,
    DimensionUpdate,
    MetricDimensionBind,
    ReconciliationSubmit,
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
    repo.list_metrics_by_dimension = AsyncMock(return_value=[])
    repo.commit = AsyncMock()
    svc._repo = repo  # noqa: SLF001
    return svc, repo


async def test_create_dimension_persists() -> None:
    svc, repo = await _svc()
    payload = DimensionCreate(dim_code="dim1", name="地区", domain="geo", type="SCD2", owner_id=1)
    resp = await svc.create_dimension(payload)
    assert resp.dim_code == "dim1"
    repo.save_dimension.assert_awaited()


async def test_create_dimension_rejects_blank_name() -> None:
    """维度名空/纯空白在 schema 层即拒绝（422），不落库空白名。"""
    from pydantic import ValidationError

    for bad in ("", "   ", "\t\n"):
        with pytest.raises(ValidationError):
            DimensionCreate(dim_code="dim1", name=bad, domain="geo")


async def test_create_member_rejects_blank_name() -> None:
    """维度成员名空/纯空白在 schema 层即拒绝（422），不落库空白成员。"""
    from pydantic import ValidationError

    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            DimensionMemberCreate(dim_code="dim1", member_name=bad)


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


async def test_submit_reconciliation_missing_metric_rejected() -> None:
    """对账引用的指标不存在 → 拒绝提交（防孤儿对账，跨服务一致性）。

    此前 metric_id 裸 BigInteger 无外键、不校验——对账引用不存在/已软删指标
    仍创建成功（维度对账记录显示悬空指标）。现校验未软删指标存在，缺失抛 404。
    """
    svc, repo = await _svc()
    repo.save_reconciliation = AsyncMock(side_effect=lambda r: r)
    # scalar_one_or_none 返回 None（指标不存在）
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    svc._session.execute = AsyncMock(return_value=result)
    try:
        await svc.submit_reconciliation(
            ReconciliationSubmit(metric_id=999, expected_expr="a", actual_expr="b")
        )
        raise AssertionError("应拒绝指标不存在的对账提交")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "NOT_FOUND"
    repo.save_reconciliation.assert_not_awaited()


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
        dim_code="geo_region", member_code="east", member_name="华东", status="PUBLISHED"
    )
    out = await svc.create_member(payload)
    assert out.path == "/east"


async def test_create_member_auto_resolves_child_path() -> None:
    """子成员（指定父级）path 自动推测为 父path/子member_code。"""
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
    parent = DimensionMember(
        id=1,
        dim_code="geo_region",
        member_code="east",
        member_name="华东",
        parent_code=None,
        path="/east",
        status="PUBLISHED",
    )
    repo.list_members = AsyncMock(return_value=[parent])
    payload = DimensionMemberCreate(
        dim_code="geo_region",
        member_code="east_nanjing",
        member_name="南京",
        parent_code="east",
        status="PUBLISHED",
    )
    out = await svc.create_member(payload)
    assert out.path == "/east/east_nanjing"


async def test_create_member_client_path_ignored_server_derives() -> None:
    """客户端直传 path 被忽略，服务端按父级独占推导（防层级错位）。"""
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
        member_code="east",
        member_name="华东",
        path="/自定义/路径",
        status="PUBLISHED",
    )
    out = await svc.create_member(payload)
    # path 为服务端派生字段：父级为唯一事实源，客户端直传 path 被忽略（防止层级错位）
    assert out.path == "/east"


# ---- 成员编辑 ----
async def test_update_member_reparent_resolves_path() -> None:
    """编辑成员改父级：自动重算 path = 新父 path + / + member_code。"""
    svc, repo = await _svc()
    member = DimensionMember(
        id=1,
        dim_code="geo_region",
        member_code="child",
        member_name="子级",
        parent_code=None,
        path="/child",
        status="DRAFT",
    )
    repo.get_member = AsyncMock(return_value=member)
    repo.list_members = AsyncMock(
        return_value=[
            member,
            DimensionMember(
                id=2,
                dim_code="geo_region",
                member_code="parent",
                member_name="父级",
                parent_code=None,
                path="/parent",
                status="PUBLISHED",
            ),
        ]
    )
    out = await svc.update_member(
        "geo_region",
        "child",
        DimensionMemberUpdate(parent_code="parent", member_name="子级（新）"),
    )
    assert out.member_name == "子级（新）"
    assert out.parent_code == "parent"
    assert out.path == "/parent/child"


async def test_update_member_reparent_cascades_descendant_path() -> None:
    """改父级后级联重算全部后代 path——移动 B 到根后，C 的 path 前缀须同步（防层级断裂）。"""
    svc, repo = await _svc()
    root = DimensionMember(
        id=1, dim_code="geo", member_code="root", member_name="根",
        parent_code=None, path="/root", status="DRAFT",
    )
    child = DimensionMember(
        id=2, dim_code="geo", member_code="child", member_name="子",
        parent_code="root", path="/root/child", status="DRAFT",
    )
    grand = DimensionMember(
        id=3, dim_code="geo", member_code="grand", member_name="孙",
        parent_code="child", path="/root/child/grand", status="DRAFT",
    )
    repo.get_member = AsyncMock(return_value=child)
    repo.list_members = AsyncMock(return_value=[root, child, grand])
    out = await svc.update_member("geo", "child", DimensionMemberUpdate(parent_code=""))
    assert out.parent_code is None
    assert out.path == "/child"
    # 级联核心：后代 grand 的 path 前缀须从 /root/child 同步为 /child（与父级 parent_code 链一致）
    assert grand.path == "/child/grand"


async def test_update_member_clear_parent_to_root() -> None:
    """编辑成员置为根：parent_code="" → parent 清空，path 重算为 /{member_code}。"""
    svc, repo = await _svc()
    member = DimensionMember(
        id=1,
        dim_code="geo_region",
        member_code="child",
        member_name="子级",
        parent_code="parent",
        path="/parent/child",
        status="DRAFT",
    )
    repo.get_member = AsyncMock(return_value=member)
    repo.list_members = AsyncMock(return_value=[member])
    out = await svc.update_member("geo_region", "child", DimensionMemberUpdate(parent_code=""))
    assert out.parent_code is None
    assert out.path == "/child"


async def test_update_member_rejects_cycle() -> None:
    """环防护：不能把成员移动到自身后代之下。"""
    svc, repo = await _svc()
    member = DimensionMember(
        id=1,
        dim_code="geo_region",
        member_code="root",
        member_name="根",
        parent_code=None,
        path="/root",
        status="DRAFT",
    )
    descendant = DimensionMember(
        id=2,
        dim_code="geo_region",
        member_code="sub",
        member_name="子",
        parent_code="root",
        path="/root/sub",
        status="PUBLISHED",
    )
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
    member = DimensionMember(
        id=1,
        dim_code="geo_region",
        member_code="m1",
        member_name="成员",
        parent_code=None,
        path="/m1",
        status="PUBLISHED",
    )
    repo.get_member = AsyncMock(return_value=member)
    with pytest.raises(ValidationError):
        await svc.update_member("geo_region", "m1", DimensionMemberUpdate(status="BOGUS"))


async def test_update_member_rename_draft_cascades_references() -> None:
    """DRAFT 成员改码成功：级联 rename_member_references + 自身编码更新。"""
    svc, repo = await _svc()
    member = DimensionMember(
        id=1,
        dim_code="geo_region",
        member_code="m1",
        member_name="成员",
        parent_code=None,
        path="/m1",
        status="DRAFT",
    )
    # 第一次 get_member(dim, m1) 返回该成员；第二次 get_member(dim, m2) 唯一性检查返回 None
    repo.get_member = AsyncMock(
        side_effect=lambda dim, code: member if code == "m1" else None
    )
    repo.rename_member_references = AsyncMock()
    updated = await svc.update_member(
        "geo_region", "m1", DimensionMemberUpdate(member_code="m2")
    )
    assert updated.member_code == "m2"
    repo.rename_member_references.assert_awaited_once_with("geo_region", "m1", "m2")


async def test_update_member_rename_duplicate_rejected() -> None:
    """DRAFT 改码撞已有成员编码 → 409（MEMBER_EXISTS）。"""
    svc, repo = await _svc()
    member = DimensionMember(
        id=1,
        dim_code="geo_region",
        member_code="m1",
        member_name="成员",
        parent_code=None,
        path="/m1",
        status="DRAFT",
    )
    existing = DimensionMember(
        id=2,
        dim_code="geo_region",
        member_code="m2",
        member_name="已存在",
        parent_code=None,
        path="/m2",
        status="PUBLISHED",
    )
    repo.get_member = AsyncMock(
        side_effect=lambda dim, code: member if code == "m1" else existing
    )
    with pytest.raises(ConflictError) as exc:
        await svc.update_member("geo_region", "m1", DimensionMemberUpdate(member_code="m2"))
    assert exc.value.error_code == "MEMBER_EXISTS"


async def test_update_member_rename_published_rejected() -> None:
    """PUBLISHED 成员改码 → 拒绝（仅 DRAFT 可改）。"""
    svc, repo = await _svc()
    member = DimensionMember(
        id=1,
        dim_code="geo_region",
        member_code="m1",
        member_name="成员",
        parent_code=None,
        path="/m1",
        status="PUBLISHED",
    )
    repo.get_member = AsyncMock(return_value=member)
    with pytest.raises(UnisenseError) as exc:
        await svc.update_member("geo_region", "m1", DimensionMemberUpdate(member_code="m2"))
    assert exc.value.error_code == "INVALID_STATE"


async def test_delete_member_cascades_subtree() -> None:
    """工业级语义：删除父级连带级联删除整个子树（BFS 收集后代一次删除）。"""
    svc, repo = await _svc()
    root = DimensionMember(
        id=1,
        dim_code="geo_region",
        member_code="root",
        member_name="根",
        parent_code=None,
        path="/root",
        status="PUBLISHED",
    )
    child = DimensionMember(
        id=2,
        dim_code="geo_region",
        member_code="sub",
        member_name="子",
        parent_code="root",
        path="/root/sub",
        status="PUBLISHED",
    )
    grand = DimensionMember(
        id=3,
        dim_code="geo_region",
        member_code="leaf",
        member_name="孙",
        parent_code="sub",
        path="/root/sub/leaf",
        status="PUBLISHED",
    )
    other = DimensionMember(
        id=4,
        dim_code="geo_region",
        member_code="sibling",
        member_name="旁支",
        parent_code=None,
        path="/sibling",
        status="PUBLISHED",
    )
    repo.get_member = AsyncMock(return_value=root)
    repo.list_members = AsyncMock(return_value=[root, child, grand, other])
    repo.delete_members = AsyncMock()
    repo.count_bindings_by_default_member = AsyncMock(return_value=0)
    deleted = await svc.delete_member("geo_region", "root")
    codes = {m.member_code for m in deleted}
    # 级联删除 root + 全部后代；旁支不受影响
    assert codes == {"root", "sub", "leaf"}
    repo.delete_members.assert_awaited_once()
    assert repo.delete_members.await_args[0][0] == deleted


async def test_delete_member_missing_raises() -> None:
    """删除不存在的成员 → 404。"""
    svc, repo = await _svc()
    repo.get_member = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.delete_member("geo_region", "nope")


async def test_update_mapping_edits_type_and_expression() -> None:
    """编辑映射：映射类型/表达式可更新，非法类型 422。"""
    svc, repo = await _svc()
    mapping = MagicMock()
    mapping.mapping_type = "EQUIVALENT"
    mapping.expression = "a=b"
    repo.get_mapping = AsyncMock(return_value=mapping)
    from app.services.dimension.schemas import DimensionMappingUpdate

    resp = await svc.update_mapping(
        1, DimensionMappingUpdate(mapping_type="PARTIAL", expression="c=d")
    )
    assert resp.mapping_type == "PARTIAL"
    assert resp.expression == "c=d"
    with pytest.raises(ValidationError):
        await svc.update_mapping(1, DimensionMappingUpdate(mapping_type="BOGUS"))


async def test_update_mapping_missing_raises() -> None:
    """编辑不存在的映射 → 404。"""
    svc, repo = await _svc()
    repo.get_mapping = AsyncMock(return_value=None)
    from app.services.dimension.schemas import DimensionMappingUpdate

    with pytest.raises(NotFoundError):
        await svc.update_mapping(99, DimensionMappingUpdate(expression="x"))


async def test_delete_mapping_removes() -> None:
    """删除映射：存在则删除，不存在 404。"""
    svc, repo = await _svc()
    mapping = MagicMock()
    repo.get_mapping = AsyncMock(return_value=mapping)
    repo.delete_mapping = AsyncMock()
    await svc.delete_mapping(1)
    repo.delete_mapping.assert_awaited_once_with(mapping)
    repo.get_mapping = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.delete_mapping(99)


async def test_list_dimension_metrics_joins_metric() -> None:
    """按维度查绑定指标：join Metric 补 metric_code/name/status（治理追溯）。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=MagicMock(deleted_at=None))
    binding = MagicMock()
    metric = MagicMock()
    repo.list_dimension_metrics = AsyncMock(return_value=[(binding, metric)])
    result = await svc.list_dimension_metrics("geo_region")
    assert result == [(binding, metric)]
    repo.list_dimension_metrics.assert_awaited_once_with("geo_region")


async def test_list_dimensions_returns_count_tuples() -> None:
    """维度列表：repository 返回 (维度, 绑定指标数) 二元组。"""
    svc, repo = await _svc()
    dim = Dimension(
        id=1,
        dim_code="d",
        name="维度",
        domain="finance",
        type="SCD1",
        owner_id=1,
        status="PUBLISHED",
    )
    repo.list_dimensions = AsyncMock(return_value=[(dim, 3)])
    result = await svc.list_dimensions(None, None)
    assert result == [(dim, 3)]


async def test_update_dimension_renames_code_and_cascades() -> None:
    """编辑维度改编码：DRAFT 允许，且级联更新引用表。"""
    svc, repo = await _svc()
    dim = Dimension(
        id=1,
        dim_code="dim_old",
        name="渠道",
        domain="sales",
        type="SCD1",
        owner_id=1,
        status="DRAFT",
    )
    repo.get_dimension = AsyncMock(side_effect=lambda code: dim if code == "dim_old" else None)
    repo.rename_dimension_references = AsyncMock()
    repo.list_metrics_declaring_dimension = AsyncMock(return_value=[])
    payload = DimensionUpdate(dim_code="dim_new", name="渠道（新）")
    resp = await svc.update_dimension("dim_old", payload)
    assert resp.dim_code == "dim_new"
    assert resp.name == "渠道（新）"
    repo.rename_dimension_references.assert_awaited_once_with("dim_old", "dim_new")
    repo.commit.assert_awaited()


async def test_update_dimension_optimistic_lock_conflict() -> None:
    """P11 C-2：row_version 不匹配（他人已改）→ 409 乐观锁冲突，不落库。"""
    from app.core.exceptions import ConflictError

    svc, repo = await _svc()
    dim = Dimension(
        id=1,
        dim_code="dim_x",
        name="渠道",
        domain="sales",
        type="SCD1",
        owner_id=1,
        status="DRAFT",
        row_version=4,
    )
    repo.get_dimension = AsyncMock(return_value=dim)
    payload = DimensionUpdate(name="新名", row_version=3)
    with pytest.raises(ConflictError) as exc:
        await svc.update_dimension("dim_x", payload)
    assert exc.value.error_code == "OPTIMISTIC_LOCK_CONFLICT"
    assert dim.name == "渠道"  # 未被修改
    repo.commit.assert_not_awaited()


async def test_update_dimension_optimistic_lock_success_increments() -> None:
    """P11 C-2：row_version 匹配 → 成功更新并递增版本。"""
    svc, repo = await _svc()
    dim = Dimension(
        id=1,
        dim_code="dim_x",
        name="渠道",
        domain="sales",
        type="SCD1",
        owner_id=1,
        status="DRAFT",
        row_version=2,
    )
    repo.get_dimension = AsyncMock(return_value=dim)
    payload = DimensionUpdate(name="新名", row_version=2)
    resp = await svc.update_dimension("dim_x", payload)
    assert resp.name == "新名"
    assert dim.row_version == 3  # 2 -> 3
    repo.commit.assert_awaited()


async def test_update_dimension_rename_rejected_when_published() -> None:
    """已发布/已废弃维度禁止改编码（避免破坏线上引用）。"""
    from app.core.exceptions import UnisenseError

    svc, repo = await _svc()
    dim = Dimension(
        id=1,
        dim_code="dim_pub",
        name="渠道",
        domain="sales",
        type="SCD1",
        owner_id=1,
        status="PUBLISHED",
    )
    repo.get_dimension = AsyncMock(return_value=dim)
    payload = DimensionUpdate(dim_code="dim_new")
    try:
        await svc.update_dimension("dim_pub", payload)
        raise AssertionError("应拒绝已发布维度改编码")
    except UnisenseError as exc:
        assert "仅 DRAFT 状态可修改编码" in str(exc)


async def test_update_dimension_rename_conflict() -> None:
    """新编码与既有维度冲突时拒绝（DIM_EXISTS）。"""
    from app.core.exceptions import ConflictError

    svc, repo = await _svc()
    dim = Dimension(
        id=1,
        dim_code="dim_a",
        name="A",
        domain="sales",
        type="SCD1",
        owner_id=1,
        status="DRAFT",
    )
    repo.get_dimension = AsyncMock(
        side_effect=lambda code: dim if code == "dim_a" else object()
    )
    payload = DimensionUpdate(dim_code="dim_b")
    try:
        await svc.update_dimension("dim_a", payload)
        raise AssertionError("应拒绝冲突编码")
    except ConflictError as exc:
        assert "已存在" in str(exc)


async def test_preview_column_values_queries_source() -> None:
    """从数据源表列拉取去重枚举值：构建采集器 + SELECT DISTINCT。"""

    from unittest.mock import patch

    svc, repo = await _svc()
    src_mock = MagicMock(source_id="s1", source_type="mysql", connection_config="encrypted")
    # svc._db.execute 返回 AsyncMock 结果，其 scalar_one_or_none 是同步方法返回 DataSource
    db_exec = AsyncMock()
    db_exec_result = MagicMock()
    db_exec_result.scalar_one_or_none.return_value = src_mock
    db_exec.return_value = db_exec_result
    svc._db.execute = db_exec

    fake_collector = MagicMock()
    fake_collector.query = AsyncMock(
        return_value=[{"channel": "app"}, {"channel": "web"}, {"channel": "app"}]
    )
    fake_collector.dispose = AsyncMock()
    with patch(
        "app.services.collector.connectors.registry.build", return_value=fake_collector
    ) as mock_build:
        result = await svc.preview_column_values("s1", "dwd.sales", "channel", limit=100)

    assert mock_build.called
    fake_collector.query.assert_awaited()
    assert result["values"] == ["app", "web", "app"]
    assert result["total"] == 3
    assert result["truncated"] is False


async def test_list_source_tables_groups_by_db() -> None:
    """源库表列举：按库分组展开为 库.表 完整名（维度值来源表选项框）。"""

    from unittest.mock import patch

    svc, _ = await _svc()
    src_mock = MagicMock(source_id="s1", source_type="mysql", connection_config="encrypted")
    db_exec = AsyncMock()
    db_exec_result = MagicMock()
    db_exec_result.scalar_one_or_none.return_value = src_mock
    db_exec.return_value = db_exec_result
    svc._db.execute = db_exec

    fake_collector = MagicMock()
    fake_collector.list_tables = AsyncMock(return_value={"dwd": ["a", "b"], "dws": ["c"]})
    fake_collector.dispose = AsyncMock()
    with patch(
        "app.services.collector.connectors.registry.build", return_value=fake_collector
    ):
        tables = await svc.list_source_tables("s1")

    assert tables == [
        {"database": "dwd", "table": "a", "name": "dwd.a"},
        {"database": "dwd", "table": "b", "name": "dwd.b"},
        {"database": "dws", "table": "c", "name": "dws.c"},
    ]
    fake_collector.dispose.assert_awaited()


async def test_list_source_tables_filters_by_databases() -> None:
    """级联选表：databases 透传给连接器，仅枚举所选库（避免全库枚举耗时）。"""

    from unittest.mock import patch

    svc, _ = await _svc()
    src_mock = MagicMock(source_id="s1", source_type="hive", connection_config="encrypted")
    db_exec = AsyncMock()
    db_exec_result = MagicMock()
    db_exec_result.scalar_one_or_none.return_value = src_mock
    db_exec.return_value = db_exec_result
    svc._db.execute = db_exec

    fake_collector = MagicMock()
    fake_collector.list_tables = AsyncMock(return_value={"dwd": ["a"]})
    fake_collector.dispose = AsyncMock()
    with patch(
        "app.services.collector.connectors.registry.build", return_value=fake_collector
    ):
        tables = await svc.list_source_tables("s1", databases=["dwd"])

    assert tables == [{"database": "dwd", "table": "a", "name": "dwd.a"}]
    fake_collector.list_tables.assert_awaited_once_with(["dwd"])
    fake_collector.dispose.assert_awaited()


async def test_list_source_databases() -> None:
    """级联选表：目标库列表走连接器轻量 list_databases。"""

    from unittest.mock import patch

    svc, _ = await _svc()
    src_mock = MagicMock(source_id="s1", source_type="hive", connection_config="encrypted")
    db_exec = AsyncMock()
    db_exec_result = MagicMock()
    db_exec_result.scalar_one_or_none.return_value = src_mock
    db_exec.return_value = db_exec_result
    svc._db.execute = db_exec

    fake_collector = MagicMock()
    fake_collector.list_databases = AsyncMock(return_value=["dwd", "dws", "wedw_dw"])
    fake_collector.dispose = AsyncMock()
    with patch(
        "app.services.collector.connectors.registry.build", return_value=fake_collector
    ):
        dbs = await svc.list_source_databases("s1")

    assert dbs == ["dwd", "dws", "wedw_dw"]
    fake_collector.list_databases.assert_awaited_once()
    fake_collector.dispose.assert_awaited()
    """MySQL 列列举：information_schema.columns 按 ordinal_position 排序返回列名+类型。"""

    from unittest.mock import patch

    svc, _ = await _svc()
    src_mock = MagicMock(source_id="s1", source_type="mysql", connection_config="encrypted")
    db_exec = AsyncMock()
    db_exec_result = MagicMock()
    db_exec_result.scalar_one_or_none.return_value = src_mock
    db_exec.return_value = db_exec_result
    svc._db.execute = db_exec

    fake_collector = MagicMock()
    fake_collector.query = AsyncMock(
        return_value=[
            {"column_name": "id", "data_type": "bigint"},
            {"column_name": "name", "data_type": "varchar"},
        ]
    )
    fake_collector.dispose = AsyncMock()
    with patch(
        "app.services.collector.connectors.registry.build", return_value=fake_collector
    ):
        columns = await svc.list_source_columns("s1", "dwd.dim_customer")

    assert columns == [
        {"name": "id", "data_type": "bigint", "comment": None},
        {"name": "name", "data_type": "varchar", "comment": None},
    ]
    fake_collector.query.assert_awaited()


async def test_list_source_columns_hive_via_describe() -> None:
    """Hive 列列举：DESCRIBE 输出解析，跳过分区头行。"""

    from unittest.mock import patch

    svc, _ = await _svc()
    src_mock = MagicMock(source_id="s1", source_type="hive", connection_config="encrypted")
    db_exec = AsyncMock()
    db_exec_result = MagicMock()
    db_exec_result.scalar_one_or_none.return_value = src_mock
    db_exec.return_value = db_exec_result
    svc._db.execute = db_exec

    fake_collector = MagicMock()
    fake_collector.list_columns = AsyncMock(
        return_value=[
            {"name": "id", "data_type": "bigint", "comment": None},
            {"name": "dt", "data_type": "string", "comment": None},
        ]
    )
    fake_collector.dispose = AsyncMock()
    with patch(
        "app.services.collector.connectors.registry.build", return_value=fake_collector
    ):
        columns = await svc.list_source_columns("s1", "ods.orders")

    assert [c["name"] for c in columns] == ["id", "dt"]
    assert columns[0]["data_type"] == "bigint"
    fake_collector.list_columns.assert_awaited_once_with("ods.orders")


async def test_list_source_columns_rejects_illegal_table() -> None:
    """非法表名（含分号）在连接源库前被拦截（防注入）。"""

    from unittest.mock import patch

    svc, _ = await _svc()
    src_mock = MagicMock(source_id="s1", source_type="mysql", connection_config="encrypted")
    db_exec = AsyncMock()
    db_exec_result = MagicMock()
    db_exec_result.scalar_one_or_none.return_value = src_mock
    db_exec.return_value = db_exec_result
    svc._db.execute = db_exec

    with patch("app.services.collector.connectors.registry.build") as mock_build:
        try:
            await svc.list_source_columns("s1", "dwd.t; drop table x")
            raise AssertionError("应拒绝非法表名")
        except ValidationError as exc:
            assert "不合法" in str(exc)
    mock_build.assert_not_called()


# ---- 跨服务打通：绑定指标后回写指标声明维度（方案③ 单向打通）----
def _metric_with_dims(
    status: str, dims: list[str], metric_id: int = 42, bound: list[str] | None = None
) -> Metric:
    m = Metric()
    m.id = metric_id
    m.metric_code = "sales_gmv_daily"
    m.status = status
    m.definition_json = {
        "expression": "SUM(x)",
        "dependencies": ["fct_order"],
        "dimensions": list(dims),
        "grain": "day",
    }
    if bound is not None:
        # _bound_dimensions：由 bind 追加的来源标记（P1-8 解绑来源保护）
        m.definition_json["_bound_dimensions"] = list(bound)
    return m


def _bind_result(metric: Metric) -> MagicMock:
    """构造 svc._session.execute 的返回：scalar_one_or_none → metric。"""
    result = MagicMock()
    result.scalar_one_or_none.return_value = metric
    return result


async def test_bind_metric_dimension_writes_back_draft_dimension() -> None:
    """绑定指标（DRAFT）成功后，dim_code 被追加进 metric.definition_json.dimensions。

    这是打通信息孤岛的核心：此前绑定只写 metric_dimension 表，消费链路读不到；
    现回写声明维度，消费校验（consume 服务）即可放行。
    """
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="region"))
    repo.get_member = AsyncMock(return_value=None)
    repo.save_metric_dimension = AsyncMock(side_effect=lambda b: b)
    metric = _metric_with_dims("DRAFT", ["existing_dim"])
    svc._session.execute = AsyncMock(return_value=_bind_result(metric))

    binding = await svc.bind_metric_dimension(
        MetricDimensionBind(metric_id=42, dim_code="region", role="FILTER")
    )

    assert isinstance(binding, MetricDimension)
    # 回写生效：声明维度新增 region
    assert metric.definition_json["dimensions"] == ["existing_dim", "region"]
    repo.save_metric_dimension.assert_awaited()
    # DRAFT 直接回写 → 已提交
    repo.commit.assert_awaited()


async def test_bind_metric_dimension_idempotent_no_duplicate() -> None:
    """dim_code 已在声明维度中：幂等跳过，不重复追加，也不多余 commit。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="region"))
    repo.get_member = AsyncMock(return_value=None)
    repo.save_metric_dimension = AsyncMock(side_effect=lambda b: b)
    metric = _metric_with_dims("DRAFT", ["region"])
    svc._session.execute = AsyncMock(return_value=_bind_result(metric))

    await svc.bind_metric_dimension(
        MetricDimensionBind(metric_id=42, dim_code="region", role="PARTITION")
    )

    # 未追加、未重复
    assert metric.definition_json["dimensions"] == ["region"]
    # 幂等跳过早于 commit → 本次未产生 commit
    repo.commit.assert_not_awaited()


async def test_bind_metric_dimension_published_creates_pending_version() -> None:
    """PUBLISHED 指标绑定新维度（P1-9）：不再静默回写 live 口径，

    改为创建 PENDING_VERSION 确认期快照，live 口径保持原样直至转正；
    消费方在确认期内仍以旧口径为准（治理语义：绕过 14 天确认 = 数据错误）。
    """
    from unittest.mock import MagicMock, patch

    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="city"))
    repo.get_member = AsyncMock(return_value=None)
    repo.save_metric_dimension = AsyncMock(side_effect=lambda b: b)
    metric = _metric_with_dims("PUBLISHED", ["region"])
    metric.version = 5
    svc._session.execute = AsyncMock(return_value=_bind_result(metric))

    metric_repo = MagicMock()
    metric_repo.has_pending_version = AsyncMock(return_value=False)
    metric_repo.create_version = AsyncMock()
    with patch(
        "app.services.semantic.repository.MetricRepository",
        return_value=metric_repo,
    ), patch(
        "app.services.semantic.pending_version_manager.PendingVersionManager.create_pending",
        new=AsyncMock(),
    ):
        await svc.bind_metric_dimension(
            MetricDimensionBind(metric_id=42, dim_code="city", role="SPLICE"),
            actor_id=7,
        )

    # live 口径未被静默改写（旧行为会改成 ["region","city"]）
    assert metric.definition_json["dimensions"] == ["region"]
    # 已创建 PENDING 版本（确认期），而非直接回写主表
    metric_repo.create_version.assert_awaited_once()
    created = metric_repo.create_version.call_args.args[0]
    assert created.status == "PENDING_CONFIRMATION"
    assert created.definition_json["dimensions"] == ["region", "city"]
    assert created.created_by == 7


async def test_bind_metric_dimension_registers_lineage_edge() -> None:
    """绑定成功后即时注册血缘 USES_DIMENSION 边（对称于 unbind 的即时移除）。

    血缘注册是追加语义（指标创建/编辑/发布时全量重注册），bind 若不同步建边，
    新绑定维度的指标血缘图要等下次编辑/发布才出现「指标↔维度」边——不对称。
    """
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="region"))
    repo.get_member = AsyncMock(return_value=None)
    repo.save_metric_dimension = AsyncMock(side_effect=lambda b: b)
    metric = _metric_with_dims("DRAFT", ["existing_dim"])
    svc._session.execute = AsyncMock(return_value=_bind_result(metric))

    with patch(
        "app.services.lineage.repository.LineageRepository.upsert_metric_dimension_edge",
        new=AsyncMock(return_value=None),
    ) as mock_edge:
        await svc.bind_metric_dimension(
            MetricDimensionBind(metric_id=42, dim_code="region", role="FILTER")
        )
        mock_edge.assert_awaited_once_with(
            metric_code="sales_gmv_daily",
            dim_node="dimension:region",
            change_reason="metric_dimension_binding",
        )


async def test_bind_metric_dimension_lineage_failure_is_best_effort() -> None:
    """血缘注册失败不阻断绑定主流程（best-effort，与 unbind 清理的容错一致）。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="region"))
    repo.get_member = AsyncMock(return_value=None)
    repo.save_metric_dimension = AsyncMock(side_effect=lambda b: b)
    metric = _metric_with_dims("DRAFT", [])
    svc._session.execute = AsyncMock(return_value=_bind_result(metric))

    with patch(
        "app.services.lineage.repository.LineageRepository.upsert_metric_dimension_edge",
        new=AsyncMock(side_effect=RuntimeError("lineage store down")),
    ):
        # 不应抛异常——血缘注册失败仅告警
        binding = await svc.bind_metric_dimension(
            MetricDimensionBind(metric_id=42, dim_code="region", role="FILTER")
        )

    assert isinstance(binding, MetricDimension)
    # 声明维度已回写（绑定主流程不受血缘故障影响）
    assert metric.definition_json["dimensions"] == ["region"]



async def test_bind_metric_dimension_missing_metric_rejected() -> None:
    """绑定引用的指标不存在 → 拒绝绑定（防孤儿绑定，跨服务一致性）。

    此前裸 BigInteger 无外键、不校验指标存在性——metric_id 指向不存在/已软删
    指标时绑定仍成功，维度详情显示悬空绑定。现校验未软删指标存在，缺失抛 404。
    """
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="region"))
    repo.get_member = AsyncMock(return_value=None)
    repo.save_metric_dimension = AsyncMock(side_effect=lambda b: b)
    # scalar_one_or_none 返回 None（指标不存在）
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    svc._session.execute = AsyncMock(return_value=result)

    try:
        await svc.bind_metric_dimension(
            MetricDimensionBind(metric_id=999, dim_code="region", role="FILTER")
        )
        raise AssertionError("应拒绝绑定不存在的指标")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "NOT_FOUND"
    # 指标缺失 → 不落绑定、不 commit
    repo.save_metric_dimension.assert_not_awaited()
    repo.commit.assert_not_awaited()



async def test_unbind_metric_dimension_removes_binding_and_dim() -> None:
    """解绑成功：删除绑定记录 + 从指标声明维度移除由 bind 追加的 dim_code（P1-8 来源保护）。"""
    svc, repo = await _svc()
    binding = MetricDimension(metric_id=42, dim_code="region", role="FILTER")
    repo.delete_metric_dimension = AsyncMock(return_value=binding)
    # region 是 bind 追加（标记在 _bound_dimensions），解绑应移除；existing_dim 是手工声明保留
    metric = _metric_with_dims("DRAFT", ["existing_dim", "region"], bound=["region"])
    svc._session.execute = AsyncMock(return_value=_bind_result(metric))
    svc._session.flush = AsyncMock()

    await svc.unbind_metric_dimension(42, "region")

    repo.delete_metric_dimension.assert_awaited_with(42, "region")
    # 反向同步：移除来源标记的 region，保留手工声明 existing_dim
    assert metric.definition_json["dimensions"] == ["existing_dim"]
    assert metric.definition_json["_bound_dimensions"] == []
    svc._session.flush.assert_awaited()


async def test_unbind_metric_dimension_keeps_manual_declaration() -> None:
    """解绑只删绑定来源维度，不抹用户手工声明（P1-8）：口径声明保留，避免误删用户口径。

    此前 unbind 无条件删除 definition_json.dimensions 中的 dim_code，会静默抹掉
    用户手工声明的维度（bind 幂等跳过时未追加来源标记）；现仅移除有来源标记的维度。
    """
    svc, repo = await _svc()
    binding = MetricDimension(metric_id=42, dim_code="region", role="FILTER")
    repo.delete_metric_dimension = AsyncMock(return_value=binding)
    # region 无 _bound_dimensions 标记 → 视为手工声明，解绑应保留
    metric = _metric_with_dims("DRAFT", ["existing_dim", "region"])
    svc._session.execute = AsyncMock(return_value=_bind_result(metric))
    svc._session.flush = AsyncMock()

    await svc.unbind_metric_dimension(42, "region")

    repo.delete_metric_dimension.assert_awaited_with(42, "region")
    # 手工声明未受解绑影响：声明维度与口径完整保留
    assert metric.definition_json["dimensions"] == ["existing_dim", "region"]
    svc._session.flush.assert_awaited()


async def test_unbind_metric_dimension_missing_binding_raises() -> None:
    """绑定关系不存在：抛 NotFoundError（防止静默误判解绑成功）。"""
    svc, repo = await _svc()
    repo.delete_metric_dimension = AsyncMock(return_value=None)

    from app.core.exceptions import NotFoundError

    try:
        await svc.unbind_metric_dimension(42, "ghost")
    except NotFoundError as exc:
        assert "ghost" in str(exc)
    else:
        raise AssertionError("should raise NotFoundError")


async def test_deprecate_dimension_blocked_when_bound_by_metrics() -> None:
    """维度被指标绑定时禁止废弃（跨服务一致性：防指标维度声明悬空）。"""
    from app.core.exceptions import BusinessError

    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=SimpleNamespace(dim_code="region", status="PUBLISHED")
    )
    repo.count_metric_dimensions = AsyncMock(return_value=3)

    try:
        await svc.deprecate_dimension("region")
    except BusinessError as exc:
        assert "3 个指标绑定" in str(exc)
        assert exc.error_code == "DIMENSION_BOUND_BY_METRICS"
    else:
        raise AssertionError("should raise BusinessError")
    # 不应触碰状态/提交
    repo.commit.assert_not_awaited()


async def test_deprecate_dimension_allowed_when_unbound() -> None:
    """维度未被绑定（或已全部解绑）时可正常废弃。"""
    svc, repo = await _svc()
    dim = SimpleNamespace(dim_code="region", status="PUBLISHED")
    repo.get_dimension = AsyncMock(return_value=dim)
    repo.count_metric_dimensions = AsyncMock(return_value=0)

    with patch(
        "app.services.lineage.service.LineageService.delete_by_node",
        new=AsyncMock(return_value=0),
    ) as mock_del:
        result = await svc.deprecate_dimension("region")
        mock_del.assert_awaited_once_with("dimension:region")

    assert result.status == "DEPRECATED"
    repo.commit.assert_awaited()


async def test_deprecate_dimension_lineage_cleanup_failure_is_best_effort() -> None:
    """血缘清理失败不阻断维度废弃（best-effort，与指标废弃边清理容错一致）。"""
    svc, repo = await _svc()
    dim = SimpleNamespace(dim_code="region", status="PUBLISHED")
    repo.get_dimension = AsyncMock(return_value=dim)
    repo.count_metric_dimensions = AsyncMock(return_value=0)

    with patch(
        "app.services.lineage.service.LineageService.delete_by_node",
        new=AsyncMock(side_effect=RuntimeError("lineage store down")),
    ):
        # 不应抛异常——血缘清理失败仅告警，维度仍正常废弃
        result = await svc.deprecate_dimension("region")

    assert result.status == "DEPRECATED"
    repo.commit.assert_awaited()


# ---------- 生命周期（reactivate/delete/restore，草稿/废弃可删、审核/启用禁删） ----------


async def test_reactivate_dimension_requires_deprecated() -> None:
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=SimpleNamespace(dim_code="region", status="DRAFT"))
    try:
        await svc.reactivate_dimension("region")
    except UnisenseError as exc:
        assert exc.error_code == "INVALID_STATE"
    else:
        raise AssertionError("should raise UnisenseError")


async def test_reactivate_dimension_sets_draft() -> None:
    svc, repo = await _svc()
    dim = SimpleNamespace(dim_code="region", status="DEPRECATED")
    repo.get_dimension = AsyncMock(return_value=dim)
    result = await svc.reactivate_dimension("region")
    assert result.status == "DRAFT"
    repo.commit.assert_awaited()


async def test_delete_dimension_rejects_review() -> None:
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=SimpleNamespace(dim_code="region", status="REVIEW"))
    try:
        await svc.delete_dimension("region", actor_id=1, role="platform_admin")
    except UnisenseError as exc:
        assert exc.error_code == "INVALID_STATE"
    else:
        raise AssertionError("should raise UnisenseError")


async def test_delete_dimension_rejects_published() -> None:
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=SimpleNamespace(dim_code="region", status="PUBLISHED")
    )
    try:
        await svc.delete_dimension("region", actor_id=1, role="platform_admin")
    except UnisenseError:
        pass
    else:
        raise AssertionError("should raise UnisenseError")


async def test_delete_dimension_requires_admin_or_owner() -> None:
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=SimpleNamespace(dim_code="region", status="DRAFT", owner_id=5)
    )
    try:
        await svc.delete_dimension("region", actor_id=1, role="metric_owner")
    except UnisenseError as exc:
        assert exc.error_code == "FORBIDDEN"
    else:
        raise AssertionError("should raise UnisenseError")


async def test_delete_dimension_protects_bound() -> None:
    from app.core.exceptions import BusinessError

    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=SimpleNamespace(dim_code="region", status="DRAFT", owner_id=1)
    )
    repo.count_metric_dimensions = AsyncMock(return_value=2)
    try:
        await svc.delete_dimension("region", actor_id=1, role="platform_admin")
    except BusinessError as exc:
        assert exc.error_code == "DIMENSION_BOUND_BY_METRICS"
    else:
        raise AssertionError("should raise BusinessError")


async def test_delete_dimension_soft_deletes() -> None:
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=SimpleNamespace(id=1, dim_code="region", status="DRAFT", owner_id=1)
    )
    repo.count_metric_dimensions = AsyncMock(return_value=0)
    repo.soft_delete_dimension = AsyncMock()
    await svc.delete_dimension("region", actor_id=1, role="metric_owner")
    repo.soft_delete_dimension.assert_awaited_once_with(1)


async def test_restore_dimension_requires_deleted() -> None:
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=SimpleNamespace(dim_code="region", status="DRAFT", deleted_at=None)
    )
    try:
        await svc.restore_dimension("region", actor_id=1, role="platform_admin")
    except UnisenseError as exc:
        assert exc.error_code == "INVALID_STATE"
    else:
        raise AssertionError("should raise UnisenseError")


async def test_restore_dimension_requires_admin_or_owner() -> None:
    from datetime import UTC, datetime

    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=SimpleNamespace(
            dim_code="region", status="DRAFT", owner_id=5, deleted_at=datetime.now(UTC)
        )
    )
    try:
        await svc.restore_dimension("region", actor_id=1, role="metric_owner")
    except UnisenseError as exc:
        assert exc.error_code == "FORBIDDEN"
    else:
        raise AssertionError("should raise UnisenseError")


async def test_restore_dimension_clears_deleted_at() -> None:
    from datetime import UTC, datetime

    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(
        return_value=SimpleNamespace(
            id=1, dim_code="region", status="DRAFT", owner_id=1, deleted_at=datetime.now(UTC)
        )
    )
    repo.restore_dimension = AsyncMock()
    await svc.restore_dimension("region", actor_id=1, role="metric_owner")
    repo.restore_dimension.assert_awaited_once_with(1)


async def test_update_member_deprecated_rejected() -> None:
    """状态机保护：已废弃成员拒绝任何更新（防止静默复活/篡改）。"""
    svc, repo = await _svc()
    member = DimensionMember(
        id=1,
        dim_code="geo_region",
        member_code="child",
        member_name="子级",
        parent_code=None,
        path="/child",
        status="DEPRECATED",
    )
    repo.get_member = AsyncMock(return_value=member)
    from app.services.dimension.schemas import DimensionMemberUpdate

    with pytest.raises(UnisenseError) as ei:
        await svc.update_member(
            "geo_region", "child", DimensionMemberUpdate(member_name="改名")
        )
    assert ei.value.error_code == "INVALID_STATE"


async def test_update_member_published_reparent_rejected() -> None:
    """状态机保护：已发布成员禁止变更父级（层级是下游权威来源，须废弃重建）。"""
    svc, repo = await _svc()
    member = DimensionMember(
        id=1,
        dim_code="geo_region",
        member_code="child",
        member_name="子级",
        parent_code=None,
        path="/child",
        status="PUBLISHED",
    )
    repo.get_member = AsyncMock(return_value=member)
    from app.services.dimension.schemas import DimensionMemberUpdate

    with pytest.raises(UnisenseError) as ei:
        await svc.update_member(
            "geo_region", "child", DimensionMemberUpdate(parent_code="newparent")
        )
    assert ei.value.error_code == "INVALID_STATE"


async def test_update_member_published_rename_allowed() -> None:
    """状态机保护：已发布成员可改非破坏性字段（名称），对齐维度主体语义。"""
    svc, repo = await _svc()
    member = DimensionMember(
        id=1,
        dim_code="geo_region",
        member_code="child",
        member_name="子级",
        parent_code=None,
        path="/child",
        status="PUBLISHED",
    )
    repo.get_member = AsyncMock(return_value=member)
    from app.services.dimension.schemas import DimensionMemberUpdate

    result = await svc.update_member(
        "geo_region", "child", DimensionMemberUpdate(member_name="改名")
    )
    assert result.member_name == "改名"


async def test_create_member_default_draft() -> None:
    """新成员默认 DRAFT（对齐维度主体/指标/术语状态机起点），须显式发布后才被下游消费。"""
    from app.services.dimension.schemas import DimensionMemberCreate

    req = DimensionMemberCreate(
        dim_code="geo_region",
        member_name="华东",
        parent_code=None,
    )
    assert req.status == "DRAFT"


async def test_update_dimension_rename_rewrites_metric_definitions() -> None:
    """维度改编码联动回写指标口径声明（definition_json.dimensions 旧→新）——防消费/血缘悬空。"""
    svc, repo = await _svc()
    dim = Dimension(
        id=1, dim_code="dim_old", name="渠道", domain="sales",
        type="SCD1", owner_id=1, status="DRAFT",
    )
    repo.get_dimension = AsyncMock(side_effect=lambda code: dim if code == "dim_old" else None)
    repo.rename_dimension_references = AsyncMock()
    m1 = SimpleNamespace(id=1, definition_json={"dimensions": ["dim_old", "dim_region"]})
    repo.list_metrics_by_dimension = AsyncMock(return_value=[m1])
    repo.list_metrics_declaring_dimension = AsyncMock(return_value=[])
    from app.services.dimension.schemas import DimensionUpdate

    await svc.update_dimension("dim_old", DimensionUpdate(dim_code="dim_new"))
    assert m1.definition_json["dimensions"] == ["dim_new", "dim_region"]
    assert repo.list_metrics_by_dimension.await_args.args[0] == "dim_old"


async def test_update_dimension_rename_rewrites_manual_declared_dimension() -> None:
    """维度改编码联动回写仅在 definition_json.dimensions 手工声明、未绑定的指标（P1-7 加固）。

    此前仅扫绑定表，手工声明维度被遗漏 → 改码后消费 FORBIDDEN_DIMENSION、血缘边悬挂。
    """
    svc, repo = await _svc()
    dim = Dimension(
        id=1, dim_code="dim_old", name="渠道", domain="sales",
        type="SCD1", owner_id=1, status="DRAFT",
    )
    repo.get_dimension = AsyncMock(side_effect=lambda code: dim if code == "dim_old" else None)
    repo.rename_dimension_references = AsyncMock()
    # 绑定表为空，但指标 A 手工声明了 dim_old（未建绑定）
    m1 = SimpleNamespace(id=1, definition_json={"dimensions": ["dim_old", "dim_region"]})
    repo.list_metrics_by_dimension = AsyncMock(return_value=[])
    repo.list_metrics_declaring_dimension = AsyncMock(return_value=[m1])
    await svc.update_dimension("dim_old", DimensionUpdate(dim_code="dim_new"))
    # 未绑定但手工声明的维度也被级联改名（防悬挂）
    assert m1.definition_json["dimensions"] == ["dim_new", "dim_region"]


async def test_unbind_removes_lineage_dimension_edge() -> None:
    """解绑指标-维度联动删除血缘 USES_DIMENSION 边（register 追加语义下防陈旧边残留）。"""
    svc, repo = await _svc()
    # dim_old 由 bind 追加（标记在 _bound_dimensions），解绑移除声明 + 血缘边
    m = SimpleNamespace(id=1, metric_code="gmv_day", status="DRAFT",
                        definition_json={"dimensions": ["dim_old"],
                                         "_bound_dimensions": ["dim_old"]})
    repo.delete_metric_dimension = AsyncMock(return_value=SimpleNamespace())
    stmt_result = MagicMock()
    stmt_result.scalar_one_or_none.return_value = m
    svc._session.execute = AsyncMock(return_value=stmt_result)
    svc._session.flush = AsyncMock()
    from app.services.lineage.repository import LineageRepository
    with __import__("unittest").mock.patch.object(
        LineageRepository, "soft_delete_edge_by_key", new=AsyncMock(return_value=None)
    ):
        await svc.unbind_metric_dimension(1, "dim_old")
    assert m.definition_json["dimensions"] == []
    assert m.definition_json["_bound_dimensions"] == []


async def test_publish_member_draft_to_published() -> None:
    """发布成员：DRAFT → PUBLISHED（对齐维度主体状态机）。"""
    svc, repo = await _svc()
    m = SimpleNamespace(dim_code="dim_c", member_code="c1", member_name="线上",
                        status="DRAFT", parent_code=None)
    repo.get_dimension = AsyncMock(return_value=SimpleNamespace(status="active"))
    repo.get_member = AsyncMock(return_value=m)
    await svc.publish_member("dim_c", "c1")
    assert m.status == "PUBLISHED"
    repo.commit.assert_awaited()


async def test_publish_member_already_published_idempotent() -> None:
    """已发布成员发布幂等放行（不重复提交）。"""
    svc, repo = await _svc()
    m = SimpleNamespace(dim_code="dim_c", member_code="c1", status="PUBLISHED",
                        parent_code=None)
    repo.get_dimension = AsyncMock(return_value=SimpleNamespace(status="active"))
    repo.get_member = AsyncMock(return_value=m)
    await svc.publish_member("dim_c", "c1")
    assert m.status == "PUBLISHED"


async def test_publish_member_deprecated_rejected() -> None:
    """已废弃成员（终态）禁止发布。"""
    svc, repo = await _svc()
    m = SimpleNamespace(dim_code="dim_c", member_code="c1", status="DEPRECATED",
                        parent_code=None)
    repo.get_dimension = AsyncMock(return_value=SimpleNamespace(status="active"))
    repo.get_member = AsyncMock(return_value=m)
    try:
        await svc.publish_member("dim_c", "c1")
        raise AssertionError("应拒绝发布已废弃成员")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "INVALID_STATE"


async def test_publish_member_rejected_when_parent_deprecated() -> None:
    """父级已废弃时禁止发布子级（层级一致性，对称于子成员保护）。"""
    svc, repo = await _svc()
    child = SimpleNamespace(dim_code="dim_c", member_code="c2", member_name="线下",
                            status="DRAFT", parent_code="c1")
    parent = SimpleNamespace(dim_code="dim_c", member_code="c1", member_name="父级",
                             status="DEPRECATED", parent_code=None)
    repo.get_dimension = AsyncMock(return_value=SimpleNamespace(status="active"))
    repo.get_member = AsyncMock(return_value=child)
    repo.list_members = AsyncMock(return_value=[parent, child])
    try:
        await svc.publish_member("dim_c", "c2")
        raise AssertionError("应拒绝发布废弃父级下的子级")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "INVALID_STATE"
        assert child.status == "DRAFT"  # 未误改状态


async def test_publish_member_allowed_when_parent_published() -> None:
    """父级已发布（非废弃）时允许发布子级（父子可各自发布）。"""
    svc, repo = await _svc()
    child = SimpleNamespace(dim_code="dim_c", member_code="c2", member_name="线下",
                            status="DRAFT", parent_code="c1")
    parent = SimpleNamespace(dim_code="dim_c", member_code="c1", member_name="父级",
                             status="PUBLISHED", parent_code=None)
    repo.get_dimension = AsyncMock(return_value=SimpleNamespace(status="active"))
    repo.get_member = AsyncMock(return_value=child)
    repo.list_members = AsyncMock(return_value=[parent, child])
    await svc.publish_member("dim_c", "c2")
    assert child.status == "PUBLISHED"


async def test_deprecate_member_with_children_rejected() -> None:
    """存在子成员时禁止废弃（层级权威保护）。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=SimpleNamespace(status="active"))
    repo.get_member = AsyncMock(return_value=SimpleNamespace(
        dim_code="dim_c", member_code="c1", status="PUBLISHED", parent_code=None))
    repo.list_members = AsyncMock(return_value=[
        SimpleNamespace(parent_code="c1"),
        SimpleNamespace(parent_code=None),
    ])
    try:
        await svc.deprecate_member("dim_c", "c1")
        raise AssertionError("应拒绝废弃含子成员的成员")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "MEMBER_HAS_CHILDREN"


async def test_deprecate_member_leaf_ok() -> None:
    """叶子成员可废弃；DEPRECATED 幂等。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=SimpleNamespace(status="active"))
    repo.get_member = AsyncMock(return_value=SimpleNamespace(
        dim_code="dim_c", member_code="c1", status="PUBLISHED", parent_code=None))
    repo.list_members = AsyncMock(return_value=[
        SimpleNamespace(parent_code=None),  # 无子成员
    ])
    repo.count_bindings_by_default_member = AsyncMock(return_value=0)
    await svc.deprecate_member("dim_c", "c1")


async def test_bind_rejects_draft_default_member() -> None:
    """绑定指标时默认成员须已发布——DRAFT 成员作默认值被拒（跨服务一致性）。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="region"))
    repo.get_member = AsyncMock(side_effect=lambda d, c: SimpleNamespace(
        status="DRAFT" if c == "c_draft" else None))
    # 指标存在（通过存在性校验），命中默认成员状态校验
    svc._session.execute = AsyncMock(
        return_value=_bind_result(_metric_with_dims("PUBLISHED", []))
    )
    try:
        await svc.bind_metric_dimension(MetricDimensionBind(
            metric_id=42, dim_code="region", role="FILTER", default_member="c_draft"))
        raise AssertionError("应拒绝 DRAFT 成员作默认值")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "DEFAULT_MEMBER_NOT_PUBLISHED"


async def test_bind_rejects_deprecated_default_member() -> None:
    """绑定指标时默认成员须已发布——DEPRECATED 成员作默认值被拒。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="region"))
    repo.get_member = AsyncMock(side_effect=lambda d, c: SimpleNamespace(
        status="DEPRECATED" if c == "c_old" else None))
    # 指标存在（通过存在性校验），命中默认成员状态校验
    svc._session.execute = AsyncMock(
        return_value=_bind_result(_metric_with_dims("PUBLISHED", []))
    )
    try:
        await svc.bind_metric_dimension(MetricDimensionBind(
            metric_id=42, dim_code="region", role="FILTER", default_member="c_old"))
        raise AssertionError("应拒绝 DEPRECATED 成员作默认值")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "DEFAULT_MEMBER_NOT_PUBLISHED"


async def test_bind_allows_published_default_member() -> None:
    """绑定指标时已发布成员可作默认值（合法场景放行）。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="region"))
    repo.get_member = AsyncMock(side_effect=lambda d, c: SimpleNamespace(
        status="PUBLISHED" if c == "c_pub" else None))
    repo.save_metric_dimension = AsyncMock(side_effect=lambda b: b)
    metric = _metric_with_dims("DRAFT", ["existing_dim"])
    svc._session.execute = AsyncMock(return_value=_bind_result(metric))
    binding = await svc.bind_metric_dimension(MetricDimensionBind(
        metric_id=42, dim_code="region", role="FILTER", default_member="c_pub"))
    assert binding.default_member == "c_pub"
    assert metric.definition_json["dimensions"] == ["existing_dim", "region"]


async def test_deprecate_member_bound_as_default_rejected() -> None:
    """成员被指标绑定为默认值时禁止废弃（对称于 deprecate_dimension 绑定保护）。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=SimpleNamespace(status="active"))
    repo.get_member = AsyncMock(return_value=SimpleNamespace(
        dim_code="dim_c", member_code="c1", status="PUBLISHED", parent_code=None))
    repo.list_members = AsyncMock(return_value=[
        SimpleNamespace(parent_code=None),
    ])
    repo.count_bindings_by_default_member = AsyncMock(return_value=2)
    try:
        await svc.deprecate_member("dim_c", "c1")
        raise AssertionError("应拒绝废弃被绑定为默认值的成员")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "MEMBER_BOUND_BY_METRICS"


async def test_publish_all_members_bulk() -> None:
    """批量发布：DRAFT 成员全部置 PUBLISHED，非 DRAFT 跳过。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="dim_c"))
    repo.list_members = AsyncMock(return_value=[
        SimpleNamespace(status="DRAFT"),
        SimpleNamespace(status="DRAFT"),
        SimpleNamespace(status="PUBLISHED"),
        SimpleNamespace(status="DEPRECATED"),
    ])
    result = await svc.publish_all_members("dim_c")
    assert result == {"published": 2, "skipped": 2}
    repo.commit.assert_awaited()


async def test_publish_all_members_no_draft_no_commit() -> None:
    """无 DRAFT 成员时不提交（幂等无副作用）。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="dim_c"))
    repo.list_members = AsyncMock(return_value=[
        SimpleNamespace(status="PUBLISHED"),
    ])
    result = await svc.publish_all_members("dim_c")
    assert result == {"published": 0, "skipped": 1}
    repo.commit.assert_not_awaited()


async def test_delete_member_rejected_when_bound_by_metric() -> None:
    """删除被指标绑定为默认值的成员被拒（跨服务一致性，对称于 deprecate_member 保护）。"""
    svc, repo = await _svc()
    root = DimensionMember(
        id=1, dim_code="geo", member_code="root", member_name="根",
        parent_code=None, path="/root", status="PUBLISHED",
    )
    child = DimensionMember(
        id=2, dim_code="geo", member_code="sub", member_name="子",
        parent_code="root", path="/root/sub", status="PUBLISHED",
    )
    repo.get_member = AsyncMock(return_value=root)
    repo.list_members = AsyncMock(return_value=[root, child])
    repo.delete_members = AsyncMock()
    # 子树内任一成员被绑定（此处子成员 sub 被绑定）→ 拒绝删除
    async def _count(dim_code, member_code):
        return 1 if member_code == "sub" else 0
    repo.count_bindings_by_default_member = AsyncMock(side_effect=_count)
    try:
        await svc.delete_member("geo", "root")
        raise AssertionError("应拒绝删除含被绑定成员的子树")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "MEMBER_BOUND_BY_METRICS"
    repo.delete_members.assert_not_awaited()


async def test_member_ops_rejected_when_dimension_deprecated() -> None:
    """废弃维度下禁止成员写操作（维度终态，成员字典不应再活跃——状态一致）。"""
    from app.services.dimension.schemas import DimensionMemberCreate

    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=SimpleNamespace(status="DEPRECATED"))
    repo.get_member = AsyncMock(return_value=SimpleNamespace(
        dim_code="dim_c", member_code="c1", member_name="线上", status="DRAFT",
        parent_code=None, path="/c1"))
    # 1) 新建成员被拒
    try:
        await svc.create_member(
            DimensionMemberCreate(dim_code="dim_c", member_code="c2", member_name="线下")
        )
        raise AssertionError("废弃维度下应拒绝新建成员")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "INVALID_STATE"
    # 2) 发布成员被拒
    try:
        await svc.publish_member("dim_c", "c1")
        raise AssertionError("废弃维度下应拒绝发布成员")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "INVALID_STATE"
    # 3) 废弃成员被拒（维度已废弃，成员状态操作无意义）
    try:
        await svc.deprecate_member("dim_c", "c1")
        raise AssertionError("废弃维度下应拒绝成员废弃操作")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "INVALID_STATE"
    # 4) 批量发布被拒
    try:
        await svc.publish_all_members("dim_c")
        raise AssertionError("废弃维度下应拒绝批量发布")
    except Exception as exc:
        assert getattr(exc, "error_code", None) == "INVALID_STATE"


async def test_submit_dimension_goes_to_review() -> None:
    """审核流：维度 submit_dimension 提交审核（DRAFT → REVIEW），写入提交人/评审指派。"""
    from app.services.master_data_review.schemas import ReviewSubmitRequest

    svc, repo = await _svc()
    dim = Dimension(
        dim_code="dim_r", name="地区", domain="outpatient", type="SCD1",
        status="DRAFT", owner_id=1,
    )
    repo.get_dimension = AsyncMock(return_value=dim)
    svc._notify_reviewers = AsyncMock()

    resp = await svc.submit_dimension(
        "dim_r",
        ReviewSubmitRequest(change_reason="完善地区维度定义后提审", reviewer_type="domain"),
        1, "metric_owner", "outpatient",
    )
    assert resp.status == "REVIEW"
    assert dim.submitted_by == 1
    assert dim.reviewer_type == "domain"
    assert dim.reviewer_domain == "outpatient"


async def test_approve_dimension_sets_published() -> None:
    """审核流：维度 approve_dimension 审核通过（REVIEW → PUBLISHED），写入通过人。"""
    from app.services.master_data_review.schemas import ReviewApproveRequest

    svc, repo = await _svc()
    dim = Dimension(
        dim_code="dim_r", name="地区", domain="outpatient", type="SCD1",
        status="REVIEW", owner_id=1, submitted_by=2,
    )
    repo.get_dimension = AsyncMock(return_value=dim)
    svc._notify_submitter = AsyncMock()

    resp = await svc.approve_dimension(
        "dim_r", ReviewApproveRequest(), 3, "domain_admin", "outpatient"
    )
    assert resp.status == "PUBLISHED"
    assert dim.approver_id == 3
    assert dim.reviewed_at is not None


async def test_reject_dimension_sets_draft_with_reason() -> None:
    """审核流：维度 reject_dimension 驳回（REVIEW → DRAFT），驳回原因落库可追溯。"""
    from app.services.master_data_review.schemas import ReviewRejectRequest

    svc, repo = await _svc()
    dim = Dimension(
        dim_code="dim_r", name="地区", domain="outpatient", type="SCD1",
        status="REVIEW", owner_id=1, submitted_by=2,
    )
    repo.get_dimension = AsyncMock(return_value=dim)
    svc._notify_submitter = AsyncMock()

    resp = await svc.reject_dimension(
        "dim_r", ReviewRejectRequest(reason="缺少层级说明"), 3, "domain_admin", "outpatient"
    )
    assert resp.status == "DRAFT"
    assert dim.reject_reason == "缺少层级说明"
    assert dim.reject_reviewer_id == 3
    assert dim.rejected_at is not None


async def test_dimension_review_blocks_update() -> None:
    """审核中锁定：REVIEW 状态维度禁止编辑（评审失真防护，驳回后修改重提）。"""
    svc, repo = await _svc()
    dim = Dimension(
        dim_code="dim_r", name="地区", domain="outpatient", type="SCD1",
        status="REVIEW", owner_id=1,
    )
    repo.get_dimension = AsyncMock(return_value=dim)
    from app.services.dimension.schemas import DimensionUpdate

    try:
        await svc.update_dimension("dim_r", DimensionUpdate(name="新名称"))
        raise AssertionError("REVIEW 状态应禁止编辑")
    except UnisenseError as exc:
        assert exc.error_code == "INVALID_STATE"
        assert "审核中" in str(exc)


# ---- 引用型维度（SNAPSHOT 快照 + 值级映射） ----


def _snapshot_dim(**kw) -> Dimension:
    """构造 sync_mode=snapshot 的维度对象。"""
    defaults = {
        "dim_code": "dim_customer",
        "name": "客户",
        "domain": "sales",
        "type": "SCD2",
        "status": "PUBLISHED",
        "owner_id": 1,
        "sync_mode": "snapshot",
        "source_id": "s1",
        "source_table": "dwd.dim_customer",
        "source_column": "customer_id",
        "refresh_interval_hours": 24,
    }
    defaults.update(kw)
    return Dimension(**defaults)


async def _snapshot_svc(dim: Dimension | None = None) -> tuple[DimensionService, MagicMock]:
    """构造引用型测试用 svc：db 为 AsyncMock 化的 MagicMock，支持 execute/flush/commit。"""
    db = MagicMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    db.add_all = MagicMock()
    db.delete = MagicMock()
    svc = DimensionService(db)
    repo = MagicMock()
    repo.get_dimension = AsyncMock(return_value=dim)
    repo.list_members = AsyncMock(return_value=[])
    repo.get_member = AsyncMock(return_value=None)
    repo.count_bindings_by_default_member = AsyncMock(return_value=0)
    svc._repo = repo
    return svc, repo


def _exec_result(scalars: list | None = None, scalar: object = None) -> MagicMock:
    """构造 execute 结果：同步 scalars()/scalar_one()/scalar_one_or_none() 链。

    execute 本身是 AsyncMock（await 返回本对象），其后 .scalars().all() 是
    同步调用链——必须用 MagicMock（AsyncMock 的 .scalars() 会返回 coroutine）。
    """
    r = MagicMock()
    r.scalars.return_value.all.return_value = scalars or []
    r.scalars.return_value.first.return_value = scalar
    r.scalar_one.return_value = 0
    r.scalar_one_or_none.return_value = scalar
    return r


async def test_refresh_dimension_snapshot_full_diff_and_run() -> None:
    """刷新快照：拉全量 → 与上批 diff → 写新批 ACTIVE + 上批消失置 REMOVED + run 统计落库。"""
    from datetime import UTC, datetime

    svc, repo = await _snapshot_svc(_snapshot_dim())
    now = datetime.now(UTC).replace(microsecond=0)
    # 上批已有值 a/b，本批拉取 a/c → added=[c], removed=[b]
    prev_row_b = MagicMock(value="b", status="ACTIVE")
    prev_row_a = MagicMock(value="a", status="ACTIVE")
    # execute 依次返回：prev_rows 查询 → prune 的 distinct 批次查询（返回 [now, prev]）
    results = [
        _exec_result(scalars=[prev_row_a, prev_row_b]),
        _exec_result(scalars=[now]),
    ]
    svc._db.execute.side_effect = results
    svc._fetch_column_values = AsyncMock(return_value=["a", "c"])
    svc._count_null_stats = AsyncMock(return_value={"total": 10, "null_count": 2})

    result = await svc.refresh_dimension_snapshot("dim_customer")

    assert result["total"] == 2
    assert result["added"] == ["c"]
    assert result["removed"] == ["b"]
    assert result["null_count"] == 2
    assert result["null_rate"] == 0.2
    # 新批次 ACTIVE 行写入
    added_rows = list(svc._db.add_all.call_args.args[0])
    assert {r.value for r in added_rows} == {"a", "c"}
    assert all(r.status == "ACTIVE" for r in added_rows)
    assert prev_row_b.status == "REMOVED"
    # run 记录更新为 SUCCESS
    run = svc._db.add.call_args.args[0]
    assert run.status == "SUCCESS"
    assert run.total_count == 2
    assert run.added_count == 1
    assert run.removed_count == 1
    assert run.null_count == 2


async def test_refresh_dimension_snapshot_rejects_non_snapshot() -> None:
    """非引用型维度（sync_mode=none）拒绝刷新快照。"""
    from app.core.exceptions import ValidationError

    svc, repo = await _snapshot_svc(
        Dimension(
            dim_code="dim_x", name="X", domain="s", type="SCD1",
            status="PUBLISHED", owner_id=1,
        )
    )
    try:
        await svc.refresh_dimension_snapshot("dim_x")
        raise AssertionError("应拒绝非引用型刷新")
    except ValidationError as exc:
        assert exc.error_code == "NOT_SNAPSHOT_MODE"


async def test_refresh_dimension_snapshot_failure_records_run_failed() -> None:
    """刷新失败：run 记录置 FAILED 并携带 error_msg（不丢失统计口径）。"""
    from app.core.exceptions import ValidationError

    svc, repo = await _snapshot_svc(_snapshot_dim())
    svc._fetch_column_values = AsyncMock(
        side_effect=ValidationError("连接失败", error_code="CONN_ERR")
    )
    with pytest.raises(ValidationError):
        await svc.refresh_dimension_snapshot("dim_customer")
    # rollback 后重新 add run 置 FAILED
    run = svc._db.add.call_args.args[0]
    assert run.status == "FAILED"
    assert "连接失败" in (run.error_msg or "")


async def test_bind_dimension_reference_sets_snapshot_mode() -> None:
    """绑定引用型值来源：校验数据源 + 标识符，置 sync_mode=snapshot。"""
    from app.services.dimension.schemas import DimensionReferenceBind

    svc, repo = await _snapshot_svc(
        Dimension(
            dim_code="dim_c", name="客户", domain="s", type="SCD1",
            status="DRAFT", owner_id=1,
        )
    )
    src_row = MagicMock(source_id="s1")
    svc._db.execute = AsyncMock(return_value=_exec_result(scalar=src_row))
    dim = await svc.bind_dimension_reference(
        "dim_c",
        DimensionReferenceBind(
            source_id="s1", table="dwd.dim_c", column="id", refresh_interval_hours=48
        ),
    )
    assert dim.sync_mode == "snapshot"
    assert dim.source_table == "dwd.dim_c"
    assert dim.refresh_interval_hours == 48


async def test_bind_dimension_reference_rejects_bad_identifier() -> None:
    """绑定非法表名/列名（注入防护）拒绝。"""
    from app.core.exceptions import ValidationError
    from app.services.dimension.schemas import DimensionReferenceBind

    svc, repo = await _snapshot_svc(
        Dimension(
            dim_code="dim_c", name="客户", domain="s", type="SCD1",
            status="DRAFT", owner_id=1,
        )
    )
    svc._db.execute = AsyncMock(return_value=_exec_result(scalar=MagicMock(source_id="s1")))
    with pytest.raises(ValidationError):
        await svc.bind_dimension_reference(
            "dim_c",
        DimensionReferenceBind(
            source_id="s1", table="dwd.dim_c; DROP TABLE x", column="id"
        ),
        )


async def test_batch_delete_members_union_dedup() -> None:
    """批量删除：勾选父+子并集去重，一次性删除整棵子树。"""
    parent = DimensionMember(
        id=1, dim_code="d", member_code="p", member_name="父", status="PUBLISHED"
    )
    child = DimensionMember(
        id=2, dim_code="d", member_code="c", member_name="子",
        parent_code="p", path="p", status="PUBLISHED",
    )
    svc, repo = await _snapshot_svc(
        Dimension(dim_code="d", name="D", domain="s", type="SCD1", status="PUBLISHED", owner_id=1)
    )
    repo.list_members = AsyncMock(return_value=[parent, child])
    repo.delete_members = AsyncMock()
    result = await svc.batch_delete_members("d", ["p", "c"])
    assert result["deleted"] == 2
    deleted = repo.delete_members.call_args.args[0]
    assert {m.member_code for m in deleted} == {"p", "c"}


async def test_batch_delete_members_blocks_bound_default() -> None:
    """批量删除：子树内成员被指标绑定为默认值时整体拒绝。"""
    from app.core.exceptions import BusinessError

    parent = DimensionMember(
        id=1, dim_code="d", member_code="p", member_name="父", status="PUBLISHED"
    )
    svc, repo = await _snapshot_svc(
        Dimension(dim_code="d", name="D", domain="s", type="SCD1", status="PUBLISHED", owner_id=1)
    )
    repo.list_members = AsyncMock(return_value=[parent])
    repo.count_bindings_by_default_member = AsyncMock(return_value=2)
    with pytest.raises(BusinessError) as ei:
        await svc.batch_delete_members("d", ["p"])
    assert ei.value.error_code == "MEMBER_BOUND_BY_METRICS"


async def test_batch_publish_members_skips_published() -> None:
    """批量发布：PUBLISHED 记 skipped，DRAFT 发布。"""
    draft = DimensionMember(
        id=1, dim_code="d", member_code="m1", member_name="一", status="DRAFT"
    )
    published = DimensionMember(
        id=2, dim_code="d", member_code="m2", member_name="二", status="PUBLISHED"
    )
    svc, repo = await _snapshot_svc(
        Dimension(dim_code="d", name="D", domain="s", type="SCD1", status="PUBLISHED", owner_id=1)
    )
    repo.get_member = AsyncMock(
        side_effect=lambda code, mc: next(
            (m for m in [draft, published] if m.member_code == mc), None
        )
    )
    repo.list_members = AsyncMock(return_value=[draft, published])
    repo.commit = AsyncMock()
    result = await svc.batch_publish_members("d", ["m1", "m2"])
    assert result["published"] == 1
    assert result["skipped"] == 1


async def test_translate_value_hit_miss_and_expression_fallback() -> None:
    """翻译：值级映射命中 covered；未配置映射时 expression 仅原样返回。"""
    from app.models.dimension import DimensionMapping

    svc, repo = await _snapshot_svc()
    mapping = DimensionMapping(
        id=1, source_dim_code="dim_a", target_dim_code="dim_b",
        mapping_type="EQUIVALENT", expression="dept_01=neike", created_by=1,
    )
    # 第一个 execute 查 mapping（返回 mapping），第二个查值级映射（返回 None）
    svc._db.execute.side_effect = [
        _exec_result(scalar=mapping),
        _exec_result(scalar=None),
    ]
    r = await svc.translate_value("dim_a", "dim_b", "dept_01")
    assert r.source_value == "dept_01"
    assert r.target_value == "dept_01"  # expression 兜底：原样返回
    assert r.covered is False

    # 命中值级映射
    from app.models.dimension import DimensionMappingValue

    mv = DimensionMappingValue(
        id=1, mapping_id=1, source_value="dept_01",
        target_value="neike", created_by=1,
    )
    svc._db.execute.side_effect = [
        _exec_result(scalar=mapping),
        _exec_result(scalar=mv),
    ]
    r2 = await svc.translate_value("dim_a", "dim_b", "dept_01")
    assert r2.target_value == "neike"
    assert r2.covered is True


async def test_translate_value_no_mapping_returns_none() -> None:
    """无任何映射时翻译结果 target_value=None。"""
    svc, repo = await _snapshot_svc()
    svc._db.execute = AsyncMock(return_value=_exec_result(scalar=None))
    r = await svc.translate_value("dim_a", "dim_b", "x")
    assert r.target_value is None
    assert r.covered is False


async def test_mapping_coverage_enum_source() -> None:
    """覆盖率：枚举型维度以成员编码为源值集合，未映射清单返回。"""
    from app.models.dimension import DimensionMapping

    svc, repo = await _snapshot_svc()
    mapping = DimensionMapping(
        id=1, source_dim_code="dim_a", target_dim_code="dim_b",
        mapping_type="EQUIVALENT", created_by=1,
    )
    repo.get_dimension = AsyncMock(
        return_value=Dimension(
            dim_code="dim_a", name="A", domain="s", type="SCD1",
            status="PUBLISHED", owner_id=1, sync_mode="none",
        )
    )
    repo.get_mapping = AsyncMock(return_value=mapping)
    repo.list_members = AsyncMock(
        return_value=[
            DimensionMember(id=1, dim_code="dim_a", member_code="x", member_name="x",
                     status="PUBLISHED"),
            DimensionMember(id=2, dim_code="dim_a", member_code="y", member_name="y",
                     status="PUBLISHED"),
        ]
    )
    svc._db.execute = AsyncMock(return_value=_exec_result(scalars=["x"]))
    cov = await svc.mapping_coverage(1)
    assert cov.total == 2
    assert cov.covered == 1
    assert cov.uncovered == ["y"]


async def test_get_dimension_visible_public_for_anyone() -> None:
    """已发布维度对任何登录用户可见（消费场景）。"""

    svc, repo = await _svc()
    dim = _persist(Dimension(dim_code="dim_region", name="地区", domain="geo", type="SCD2",
                             owner_id=3, status="PUBLISHED"))
    repo.get_dimension = AsyncMock(return_value=dim)
    resp = await svc.get_dimension_visible("dim_region", actor_id=7, role="analyst")
    assert resp.dim_code == "dim_region"
    # 管理角色/owner/reviewer 均可见
    await svc.get_dimension_visible("dim_region", actor_id=7, role="platform_admin")
    await svc.get_dimension_visible("dim_region", actor_id=3, role="analyst")
    await svc.get_dimension_visible("dim_region", actor_id=9, role="reviewer")


async def test_get_dimension_visible_draft_only_owner_or_admin() -> None:
    """草稿维度仅本人/管理角色可见；他人读取按不存在处理（不泄露存在性）。"""
    from app.core.exceptions import NotFoundError

    svc, repo = await _svc()
    dim = _persist(Dimension(dim_code="dim_region", name="地区", domain="geo", type="SCD2",
                             owner_id=3, status="DRAFT"))
    repo.get_dimension = AsyncMock(return_value=dim)
    # 本人可见
    await svc.get_dimension_visible("dim_region", actor_id=3, role="analyst")
    # 管理角色可见
    await svc.get_dimension_visible("dim_region", actor_id=1, role="platform_admin")
    # 他人不可见（NotFound，非 403——不泄露存在性）
    with pytest.raises(NotFoundError):
        await svc.get_dimension_visible("dim_region", actor_id=7, role="analyst")
    # reviewer 对 DRAFT 不可见（仅 REVIEW 待审放行）
    with pytest.raises(NotFoundError):
        await svc.get_dimension_visible("dim_region", actor_id=9, role="reviewer")


async def test_get_dimension_visible_review_reviewer_sees() -> None:
    """待审（REVIEW）维度：评审人可见（审批工作台）；普通他人不可见。"""
    from app.core.exceptions import NotFoundError

    svc, repo = await _svc()
    dim = _persist(Dimension(dim_code="dim_region", name="地区", domain="geo", type="SCD2",
                             owner_id=3, status="REVIEW"))
    repo.get_dimension = AsyncMock(return_value=dim)
    await svc.get_dimension_visible("dim_region", actor_id=9, role="reviewer")
    with pytest.raises(NotFoundError):
        await svc.get_dimension_visible("dim_region", actor_id=7, role="analyst")


async def test_get_dimension_visible_internal_no_context_passthrough() -> None:
    """内部调用（actor/role 均为 None）不过滤——端点层必传鉴权上下文。"""
    svc, repo = await _svc()
    dim = _persist(Dimension(dim_code="dim_region", name="地区", domain="geo", type="SCD2",
                             owner_id=3, status="DRAFT"))
    repo.get_dimension = AsyncMock(return_value=dim)
    resp = await svc.get_dimension_visible("dim_region")
    assert resp.dim_code == "dim_region"
