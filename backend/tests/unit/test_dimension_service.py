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
    repo.get_dimension = AsyncMock(return_value=MagicMock())
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
    payload = DimensionUpdate(dim_code="dim_new", name="渠道（新）")
    resp = await svc.update_dimension("dim_old", payload)
    assert resp.dim_code == "dim_new"
    assert resp.name == "渠道（新）"
    repo.rename_dimension_references.assert_awaited_once_with("dim_old", "dim_new")
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


# ---- 跨服务打通：绑定指标后回写指标声明维度（方案③ 单向打通）----
def _metric_with_dims(status: str, dims: list[str], metric_id: int = 42) -> Metric:
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


async def test_bind_metric_dimension_published_also_writes_back() -> None:
    """PUBLISHED 指标（已发布口径）绑定：仍回写声明维度（告警由日志承载，不过度设计）。"""
    svc, repo = await _svc()
    repo.get_dimension = AsyncMock(return_value=Dimension(dim_code="city"))
    repo.get_member = AsyncMock(return_value=None)
    repo.save_metric_dimension = AsyncMock(side_effect=lambda b: b)
    metric = _metric_with_dims("PUBLISHED", ["region"])
    svc._session.execute = AsyncMock(return_value=_bind_result(metric))

    await svc.bind_metric_dimension(
        MetricDimensionBind(metric_id=42, dim_code="city", role="SPLICE")
    )

    assert metric.definition_json["dimensions"] == ["region", "city"]
    repo.commit.assert_awaited()


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
    """解绑成功：删除绑定记录 + 从指标声明维度移除 dim_code（与 bind 对称反向）。"""
    svc, repo = await _svc()
    binding = MetricDimension(metric_id=42, dim_code="region", role="FILTER")
    repo.delete_metric_dimension = AsyncMock(return_value=binding)
    metric = _metric_with_dims("DRAFT", ["existing_dim", "region"])
    svc._session.execute = AsyncMock(return_value=_bind_result(metric))
    svc._session.flush = AsyncMock()

    await svc.unbind_metric_dimension(42, "region")

    repo.delete_metric_dimension.assert_awaited_with(42, "region")
    # 反向同步：声明维度移除 region，保留 existing_dim
    assert metric.definition_json["dimensions"] == ["existing_dim"]
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
    from app.services.dimension.schemas import DimensionUpdate

    await svc.update_dimension("dim_old", DimensionUpdate(dim_code="dim_new"))
    assert m1.definition_json["dimensions"] == ["dim_new", "dim_region"]
    assert repo.list_metrics_by_dimension.await_args.args[0] == "dim_old"


async def test_unbind_removes_lineage_dimension_edge() -> None:
    """解绑指标-维度联动删除血缘 USES_DIMENSION 边（register 追加语义下防陈旧边残留）。"""
    svc, repo = await _svc()
    m = SimpleNamespace(id=1, metric_code="gmv_day", status="DRAFT",
                        definition_json={"dimensions": ["dim_old"]})
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
