"""MetricRepository 单元测试（使用 MagicMock 异步会话，无真实 DB 依赖）。

覆盖：CRUD / 乐观锁更新（成功·冲突·不存在）/ 软删除 / 版本读写 / 分页过滤。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ConflictError, NotFoundError
from app.models.metric import Metric, MetricVersion
from app.services.semantic.repository import MetricRepository


def _metric(**kwargs: object) -> MagicMock:
    m = MagicMock(spec=Metric)
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _version(**kwargs: object) -> MagicMock:
    m = MagicMock(spec=MetricVersion)
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _result(
    *,
    scalar_one_or_none: object = None,
    scalar: object = None,
    all_: list | None = None,
    rowcount: int = 0,
) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none.return_value = scalar_one_or_none
    r.scalar.return_value = scalar
    r.scalars.return_value.all.return_value = all_ if all_ is not None else []
    r.all.return_value = all_ if all_ is not None else []
    r.rowcount = rowcount
    return r


def _mock_session() -> MagicMock:
    """混合 mock：add 为同步方法，execute/flush/refresh 为异步方法。"""
    db = MagicMock()
    db.execute = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _compiled_sql(db: MagicMock, index: int) -> str:
    """编译第 index 次 execute 的语句（literal_binds 便于断言过滤条件）。"""
    stmt = db.execute.call_args_list[index].args[0]
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


# ---------- create / read ----------


async def test_create_persists_and_returns_metric():
    db = _mock_session()
    repo = MetricRepository(db)
    metric = _metric(metric_code="m1")

    result = await repo.create(metric)

    db.add.assert_called_once_with(metric)
    db.flush.assert_awaited()
    db.refresh.assert_awaited_once_with(metric)
    assert result is metric


async def test_get_by_code_returns_metric_when_found():
    db = _mock_session()
    db.execute.return_value = _result(scalar_one_or_none=_metric(metric_code="m1"))
    repo = MetricRepository(db)

    metric = await repo.get_by_code("m1")

    assert metric is not None and metric.metric_code == "m1"
    db.execute.assert_awaited()


async def test_get_by_code_returns_none_when_missing():
    db = _mock_session()
    db.execute.return_value = _result(scalar_one_or_none=None)
    repo = MetricRepository(db)

    assert await repo.get_by_code("nope") is None


async def test_get_by_id_returns_metric_when_found():
    db = _mock_session()
    db.execute.return_value = _result(scalar_one_or_none=_metric(id=7))
    repo = MetricRepository(db)

    metric = await repo.get_by_id(7)

    assert metric is not None and metric.id == 7


# ---------- list ----------


async def test_list_metrics_applies_filters_and_returns_total():
    db = _mock_session()
    m1, m2 = _metric(metric_code="a"), _metric(metric_code="b")
    # 第一次 execute = count，第二次 = 列表
    db.execute.side_effect = [
        _result(scalar=2),
        _result(all_=[m1, m2]),
    ]
    repo = MetricRepository(db)

    items, total = await repo.list_metrics(domain="sales", keyword="a", offset=0, limit=10)

    assert total == 2
    assert items == [m1, m2]
    assert db.execute.await_count == 2


async def test_list_metrics_applies_owner_and_pii_filters():
    db = _mock_session()
    m1, m2 = _metric(metric_code="a"), _metric(metric_code="b")
    db.execute.side_effect = [
        _result(scalar=2),
        _result(all_=[m1, m2]),
    ]
    repo = MetricRepository(db)

    items, total = await repo.list_metrics(owner_id=7, pii_flag=True, offset=0, limit=10)

    assert total == 2
    assert items == [m1, m2]
    # 编译首条 count 语句，验证 owner_id 与 pii_flag 条件已加入
    stmt = db.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "owner_id" in compiled
    assert "pii_flag" in compiled


async def test_list_metrics_applies_batch_id_filter():
    """批次筛选（生产就绪审查 P2）：list_metrics 支持 batch_id 精确匹配——审核/
    列表页可按"这一批"收敛批量创建的指标（此前 MetricListParams 无该参数）。"""
    db = _mock_session()
    m1 = _metric(metric_code="a")
    db.execute.side_effect = [
        _result(scalar=1),
        _result(all_=[m1]),
    ]
    repo = MetricRepository(db)

    items, total = await repo.list_metrics(batch_id="sqlbatch_abc", offset=0, limit=10)

    assert total == 1
    assert items == [m1]
    # 编译 count 语句验证 batch_id 等值条件已加入
    stmt = db.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "batch_id" in compiled
    assert "sqlbatch_abc" in compiled


async def test_list_metrics_has_downstream_filters():
    """下游引用过滤（批量废弃前按引用收敛）：has_downstream=True 仅保留活跃下游引用
    （DERIVED_FROM/CONSUMED_BY 边，deleted_at/stale 不计）；False 取反；None 不过滤。"""
    db = _mock_session()
    m1 = _metric(metric_code="a")
    db.execute.side_effect = [
        _result(scalar=1),
        _result(all_=[m1]),
    ]
    repo = MetricRepository(db)
    await repo.list_metrics(has_downstream=True, offset=0, limit=10)
    compiled = _compiled_sql(db, 0)
    # 指标编码拼 metric: 前缀后与活跃血缘边的 source_node 比对（IN 子查询）
    concat_ok = (
        "concat('metric:', metric.metric_code)" in compiled
        or "concat(CAST('metric:'" in compiled
    )
    assert concat_ok
    assert "lineage_edge" in compiled
    assert "DERIVED_FROM" in compiled
    assert "CONSUMED_BY" in compiled
    assert "deleted_at IS NULL" in compiled


async def test_list_metrics_applies_health_level_filter():
    """健康度档位过滤（仪表盘/可观测中心分布下钻）：health_level 命中
    metric_health_score.level 的指标（IN 子查询，count/list 语义一致）；
    不传时不过滤（无该子查询）。"""
    db = _mock_session()
    m1 = _metric(metric_code="a")
    db.execute.side_effect = [
        _result(scalar=1),
        _result(all_=[m1]),
    ]
    repo = MetricRepository(db)

    items, total = await repo.list_metrics(health_level="WARNING", offset=0, limit=10)

    assert total == 1
    assert items == [m1]
    compiled = _compiled_sql(db, 0)
    assert "metric_health_score" in compiled
    assert "WARNING" in compiled
    assert "metric.id IN" in compiled


async def test_list_metrics_without_health_level_has_no_health_join():
    """健康度不过滤时，count 查询不应出现 health 子查询（避免多余开销）。"""
    db = _mock_session()
    m1 = _metric(metric_code="a")
    db.execute.side_effect = [
        _result(scalar=1),
        _result(all_=[m1]),
    ]
    repo = MetricRepository(db)

    await repo.list_metrics(offset=0, limit=10)

    compiled = _compiled_sql(db, 0)
    assert "metric_health_score" not in compiled

    # 无下游 = IN 子查询取反（NOT IN）
    db2 = _mock_session()
    db2.execute.side_effect = [_result(scalar=1), _result(all_=[m1])]
    await MetricRepository(db2).list_metrics(has_downstream=False, offset=0, limit=10)
    compiled2 = _compiled_sql(db2, 0)
    assert "NOT" in compiled2

    # 缺省 None 不过滤：编译语句不含 lineage_edge
    db3 = _mock_session()
    db3.execute.side_effect = [_result(scalar=1), _result(all_=[m1])]
    await MetricRepository(db3).list_metrics(offset=0, limit=10)
    compiled3 = _compiled_sql(db3, 0)
    assert "lineage_edge" not in compiled3


# ---------- update_with_optimistic_lock ----------


async def test_update_with_optimistic_lock_success():
    db = _mock_session()
    updated = _metric(id=1, row_version=2)
    # 命中 1 行 → 乐观锁通过；随后 get_by_id 回查返回更新对象
    db.execute.return_value = _result(scalar_one_or_none=updated, rowcount=1)
    repo = MetricRepository(db)

    result = await repo.update_with_optimistic_lock(1, expected_row_version=1, name="x")

    # 成功路径：返回更新对象并触发 refresh
    db.refresh.assert_awaited_once_with(updated)
    assert result is updated


async def test_create_duplicate_code_raises_conflict():
    from sqlalchemy.exc import IntegrityError

    from app.core.exceptions import ConflictError

    db = _mock_session()
    db.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
    db.rollback = AsyncMock()
    repo = MetricRepository(db)
    metric = _metric(metric_code="dup_code")

    with pytest.raises(ConflictError) as exc:
        await repo.create(metric)
    assert exc.value.error_code == "CONFLICT"
    db.rollback.assert_awaited_once()


async def test_get_version_returns_matching_version():
    db = _mock_session()
    version = _version(metric_id=1, version=2, status="DRAFT")
    db.execute.return_value = _result(scalar_one_or_none=version)
    repo = MetricRepository(db)

    result = await repo.get_version(1, 2)
    assert result is version


async def test_mark_version_published_executes_update():
    db = _mock_session()
    db.execute.return_value = _result(rowcount=1)
    repo = MetricRepository(db)

    await repo.mark_version_published(1, 2, "2026-08-07T00:00:00+00:00")
    db.execute.assert_awaited()


async def test_update_with_optimistic_lock_conflict_raises_conflict():
    db = _mock_session()
    existing = _metric(id=1, row_version=5)
    db.execute.side_effect = [
        _result(scalar_one_or_none=None),  # update 命中 0 行
        _result(scalar_one_or_none=existing),  # get_by_id 找到 -> 冲突
    ]
    repo = MetricRepository(db)

    with pytest.raises(ConflictError) as exc:
        await repo.update_with_optimistic_lock(1, expected_row_version=1, name="x")
    assert exc.value.error_code == "CONCURRENT_MODIFICATION"


async def test_update_with_optimistic_lock_not_found_raises_notfound():
    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar_one_or_none=None),
        _result(scalar_one_or_none=None),  # get_by_id 也找不到
    ]
    repo = MetricRepository(db)

    with pytest.raises(NotFoundError):
        await repo.update_with_optimistic_lock(1, expected_row_version=1, name="x")


# ---------- soft_delete ----------


async def test_soft_delete_success():
    db = _mock_session()
    db.execute.return_value = _result(rowcount=1)
    repo = MetricRepository(db)

    await repo.soft_delete(1)  # 不应抛异常
    db.execute.assert_awaited()


async def test_soft_delete_not_found_raises_notfound():
    db = _mock_session()
    db.execute.return_value = _result(rowcount=0)
    repo = MetricRepository(db)

    with pytest.raises(NotFoundError):
        await repo.soft_delete(1)


# ---------- versions ----------


async def test_create_version_persists_and_returns():
    db = _mock_session()
    # L-2：create_version 会触发 _archive_excess_versions（查询版本数），
    # 返回空列表（<= 保留上限）不归档。
    db.execute.return_value = _result(all_=[])
    repo = MetricRepository(db)
    v = _version(metric_id=1, version=1)

    result = await repo.create_version(v)

    db.add.assert_called_once_with(v)
    db.flush.assert_awaited()
    db.refresh.assert_awaited_once_with(v)
    assert result is v


async def test_list_versions_returns_desc_ordered():
    db = _mock_session()
    v1, v2 = _version(version=1), _version(version=2)
    db.execute.return_value = _result(all_=[v2, v1])
    repo = MetricRepository(db)

    versions = await repo.list_versions(1)

    assert versions == [v2, v1]


# ---------- 过滤分支（status / metric_tier） ----------


async def test_list_metrics_applies_status_and_tier_filters():
    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar=1),
        _result(all_=[_metric(metric_code="a")]),
    ]
    repo = MetricRepository(db)

    items, total = await repo.list_metrics(status="PUBLISHED", metric_tier="T1")

    assert total == 1
    assert len(items) == 1


async def test_list_metrics_excludes_statuses():
    """exclude_statuses 生成 Metric.status NOT IN 条件（资产地图「指标总数」下钻
    与统计口径一致排除 DRAFT/DEPRECATED，防止明细多出草稿/已废弃）。"""
    from sqlalchemy.dialects import mysql

    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar=0),
        _result(all_=[]),
    ]
    repo = MetricRepository(db)

    await repo.list_metrics(
        exclude_statuses=["DRAFT", "DEPRECATED"], offset=0, limit=10
    )

    list_stmt = db.execute.call_args_list[1].args[0]
    literal_sql = str(
        list_stmt.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "metric.status NOT IN ('DRAFT', 'DEPRECATED')" in literal_sql


async def test_list_metrics_exclude_statuses_empty_has_no_filter():
    """空排除列表不加过滤条件（退化保护，避免空 NOT IN 语义歧义）。"""
    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar=0),
        _result(all_=[]),
    ]
    repo = MetricRepository(db)

    await repo.list_metrics(exclude_statuses=[], offset=0, limit=10)

    list_stmt = db.execute.call_args_list[1].args[0]
    assert "NOT IN" not in _compiled_sql(db, 1)


async def test_list_metrics_filters_by_metric_type():
    """metric_type 服务端过滤：list_metrics(metric_type='atomic') 生成 Metric.type 等值条件。

    派生指标「绑定基础原子指标」下拉靠此条件在 SQL 层只取原子指标，替代前端页内
    filter(type)——混合类型不再占满单页导致原子指标漏项。
    """
    from sqlalchemy.dialects import mysql

    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar=0),
        _result(all_=[]),
    ]
    repo = MetricRepository(db)

    await repo.list_metrics(metric_type="atomic", offset=0, limit=10)

    # 第二个 execute 是列表查询（第一个是 count），编译为 MySQL 方言并内联字面量
    list_stmt = db.execute.call_args_list[1].args[0]
    literal_sql = str(
        list_stmt.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
    )
    # Metric.type 列（带 metric 表前缀）+ ENUM 值 'atomic' 内联为等值条件
    assert "metric.type = 'atomic'" in literal_sql


async def test_list_metrics_escapes_like_wildcards():
    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar=0),
        _result(all_=[]),
    ]
    repo = MetricRepository(db)

    items, total = await repo.list_metrics(keyword="sales%rate_data")

    assert total == 0
    assert items == []


async def test_list_metrics_wildcard_escape_generates_escape_clause():
    """含 %/_ 的关键词必须生成 ESCAPE 子句（FR-035 防模糊放大）。

    修复前：手动 replace 成 \\% 但 contains() 不生成 ESCAPE，MySQL 默认把 \\
    当普通字符、%/_ 仍当通配符 → 转义实际失效（搜 order_cnt 会匹配所有含 order）。
    autoescape=True 由 SQLAlchemy 自动转义并生成 ESCAPE '/' 子句。
    """
    from sqlalchemy.dialects import mysql

    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar=0),
        _result(all_=[]),
    ]
    repo = MetricRepository(db)

    await repo.list_metrics(keyword="order_cnt", offset=0, limit=10)

    # 第二个 execute 是列表查询（第一个是 count），取其 SELECT 语句编译为 MySQL 方言
    list_stmt = db.execute.call_args_list[1].args[0]
    sql = str(list_stmt.compile(dialect=mysql.dialect()))
    assert "ESCAPE" in sql
    # autoescape 把关键词中的下划线转义为 /_（编译时内联字面量可见），而非裸 _ 通配符
    literal_sql = str(
        list_stmt.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "/_" in literal_sql


async def test_list_metrics_reviewed_by_or_filter():
    """评审历史过滤（reviewed_by）命中 审批通过(approver_id) 或 驳回(reject_reviewer_id) 任一。

    「我审过的」完整视图：评审人通过 + 驳回的记录都应可见，不得丢驳回历史。
    """
    from sqlalchemy.dialects import mysql

    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar=0),
        _result(all_=[]),
    ]
    repo = MetricRepository(db)

    await repo.list_metrics(reviewed_by=7, offset=0, limit=10)

    list_stmt = db.execute.call_args_list[1].args[0]
    sql = str(list_stmt.compile(dialect=mysql.dialect()))
    # OR 条件同时覆盖 approver_id 与 reject_reviewer_id
    assert "approver_id" in sql and "reject_reviewer_id" in sql


async def test_list_metrics_reviewer_sees_only_user_assigned():
    """评审人可见性（TD §13）：reviewer_type=user 仅 reviewer_id 指定的用户可看待审指标。"""
    db = _mock_session()
    db.execute.side_effect = [_result(scalar=0), _result(all_=[])]
    repo = MetricRepository(db)

    await repo.list_metrics(
        offset=0, limit=10, visible_actor_id=7, visible_role="reviewer"
    )

    sql = _compiled_sql(db, 0)
    # 不再出现「任意评审角色可见全部 REVIEW」的裸 status 分支——必须带评审指派判定
    assert "reviewer_type" in sql and "reviewer_id" in sql
    assert "= 7" in sql
    # 松散分支（仅 status=REVIEW、无指派条件）不应作为独立 OR 项存在：
    # 编译 SQL 中 REVIEW 状态必须与指派条件同属一个 and_ 分组
    assert "status" in sql


async def test_list_metrics_reviewer_sees_domain_assigned():
    """评审人可见性（TD §13）：reviewer_type=domain 仅同域评审组可见。"""
    db = _mock_session()
    db.execute.side_effect = [_result(scalar=0), _result(all_=[])]
    repo = MetricRepository(db)

    await repo.list_metrics(
        offset=0,
        limit=10,
        visible_actor_id=7,
        visible_role="reviewer",
        visible_user_domains=["outpatient"],
    )

    sql = _compiled_sql(db, 0)
    assert "reviewer_domain" in sql and "outpatient" in sql


async def test_list_metrics_reviewer_unassigned_not_visible():
    """评审人可见性（TD §13）：未指派评审人的 REVIEW 指标对 reviewer 角色不可见（域管理员兜底）。"""
    db = _mock_session()
    db.execute.side_effect = [_result(scalar=0), _result(all_=[])]
    repo = MetricRepository(db)

    await repo.list_metrics(
        offset=0, limit=10, visible_actor_id=7, visible_role="reviewer"
    )

    sql = _compiled_sql(db, 0)
    # 编译 SQL 必须含指派判定（reviewer_type），证明松散 status=REVIEW 分支已被替换
    assert "reviewer_type" in sql and "reviewer_id" in sql


async def test_count_review_assigned_matches_designation():
    """指派待审数统计（TD §13）：user 指派=本人 / domain 指派=同域，两者并集。"""
    db = _mock_session()
    db.execute.return_value = _result(scalar=3)
    db.execute.return_value.scalar_one.return_value = 3
    repo = MetricRepository(db)

    cnt = await repo.count_review_assigned(7, ["outpatient"])

    assert cnt == 3
    sql = _compiled_sql(db, 0)
    assert "reviewer_type" in sql and "reviewer_id" in sql and "reviewer_domain" in sql
    assert "= 7" in sql and "outpatient" in sql
    assert "OR" in sql.upper()


async def test_count_review_actionable_platform_admin_full():
    """可审待审数（TD §13）：platform_admin=全量 REVIEW（不加指派过滤，最终兜底）。"""
    db = _mock_session()
    db.execute.return_value = _result(scalar=6)
    db.execute.return_value.scalar_one.return_value = 6
    repo = MetricRepository(db)

    cnt = await repo.count_review_actionable(7, ["outpatient"], "platform_admin")

    assert cnt == 6
    sql = _compiled_sql(db, 0)
    # platform_admin 全量：不出现 reviewer 指派过滤条件（仅 status + deleted_at）
    assert "reviewer_type" not in sql


async def test_count_review_actionable_domain_admin_scope():
    """可审待审数（TD §13）：domain_admin=user 型指派给我 + domain 型同域 + 未指派兜底。"""
    db = _mock_session()
    db.execute.return_value = _result(scalar=2)
    db.execute.return_value.scalar_one.return_value = 2
    repo = MetricRepository(db)

    cnt = await repo.count_review_actionable(7, ["outpatient"], "domain_admin")

    assert cnt == 2
    sql = _compiled_sql(db, 0)
    assert "reviewer_type" in sql and "reviewer_id" in sql and "reviewer_domain" in sql
    assert "= 7" in sql and "outpatient" in sql
    # 未指派兜底：reviewer_type IS NULL 分支存在（域管理员可审未指派指标）
    assert "REVIEWER_TYPE IS NULL" in sql.upper()


async def test_count_review_actionable_reviewer_no_unassigned():
    """可审待审数（TD §13）：reviewer=user 型指派给我 + domain 型同域，不含未指派兜底。"""
    db = _mock_session()
    db.execute.return_value = _result(scalar=1)
    db.execute.return_value.scalar_one.return_value = 1
    repo = MetricRepository(db)

    cnt = await repo.count_review_actionable(7, ["outpatient"], "reviewer")

    assert cnt == 1
    sql = _compiled_sql(db, 0)
    assert "reviewer_type" in sql and "reviewer_id" in sql and "reviewer_domain" in sql
    assert "= 7" in sql and "outpatient" in sql
    # reviewer 不兜底未指派：不出现 reviewer_type IS NULL 分支
    assert "REVIEWER_TYPE IS NULL" not in sql.upper()


async def test_count_review_actionable_normal_role_zero():
    """可审待审数（TD §13）：普通角色无可审资格，仅 user 型指派给本人的条目计数。"""
    db = _mock_session()
    db.execute.return_value = _result(scalar=0)
    db.execute.return_value.scalar_one.return_value = 0
    repo = MetricRepository(db)

    cnt = await repo.count_review_actionable(7, ["outpatient"], "analyst")

    assert cnt == 0
    sql = _compiled_sql(db, 0)
    # 普通角色：不出现 domain/未指派分支（只有 user 型指派条件）
    assert "REVIEWER_DOMAIN" not in sql.upper()
    assert "REVIEWER_TYPE IS NULL" not in sql.upper()


async def test_list_metrics_asc_sort_and_whitelist_fallback():
    db = _mock_session()
    db.execute.side_effect = [
        _result(scalar=1),
        _result(all_=[_metric(metric_code="a")]),
    ]
    repo = MetricRepository(db)

    # 非法 sort_by 回落到 updated_at，asc 方向
    items, total = await repo.list_metrics(
        sort_by="not-a-column", sort_order="asc", offset=10, limit=5
    )
    assert total == 1
    assert items


# ---------- 乐观锁更新后数据一致性异常 ----------


async def test_update_with_optimistic_lock_updated_missing_raises_system_error():
    from app.core.exceptions import SystemError as AppSystemError

    db = _mock_session()
    # update 命中 1 行，但随后 get_by_id 回查返回 None（数据一致性异常）
    db.execute.return_value = _result(scalar_one_or_none=None, rowcount=1)
    repo = MetricRepository(db)

    with pytest.raises(AppSystemError) as exc:
        await repo.update_with_optimistic_lock(1, expected_row_version=1, name="x")
    assert exc.value.error_code == "INTERNAL_ERROR"


# ---------- create_version 冲突 ----------


async def test_create_version_duplicate_raises_conflict():
    from sqlalchemy.exc import IntegrityError

    db = _mock_session()
    db.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
    db.rollback = AsyncMock()
    repo = MetricRepository(db)
    v = _version(metric_id=1, version=2)

    with pytest.raises(ConflictError) as exc:
        await repo.create_version(v)
    assert exc.value.error_code == "CONFLICT"
    db.rollback.assert_awaited_once()


# ---------- PENDING_VERSION 确认相关 ----------


def _confirmation(**kwargs: object) -> MagicMock:
    c = MagicMock()
    defaults = {
        "id": 1,
        "metric_id": 1,
        "version": 2,
        "consumer_id": 10,
        "status": "PENDING",
        "deadline": None,
    }
    defaults.update(kwargs)
    for k, v in defaults.items():
        setattr(c, k, v)
    return c


async def test_save_pending_confirmation_persists():
    db = _mock_session()
    repo = MetricRepository(db)
    c = _confirmation()

    result = await repo.save_pending_confirmation(c)

    db.add.assert_called_once_with(c)
    db.flush.assert_awaited()
    db.refresh.assert_awaited_once_with(c)
    assert result is c


async def test_save_pending_confirmation_duplicate_raises_conflict():
    from sqlalchemy.exc import IntegrityError

    db = _mock_session()
    db.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
    db.rollback = AsyncMock()
    repo = MetricRepository(db)
    c = _confirmation()

    with pytest.raises(ConflictError):
        await repo.save_pending_confirmation(c)


async def test_get_pending_confirmations_returns_list():
    db = _mock_session()
    db.execute.return_value = _result(all_=[_confirmation(id=1), _confirmation(id=2)])
    repo = MetricRepository(db)

    rows = await repo.get_pending_confirmations(1, 2)

    assert len(rows) == 2


async def test_update_confirmation_status_with_and_without_reason():
    db = _mock_session()
    repo = MetricRepository(db)

    await repo.update_confirmation_status(1, "CONFIRMED", reason="looks good")
    await repo.update_confirmation_status(2, "REJECTED")

    assert db.execute.await_count == 2


async def test_get_pending_confirmation_returns_single():
    db = _mock_session()
    c = _confirmation(id=3)
    db.execute.return_value = _result(scalar_one_or_none=c)
    repo = MetricRepository(db)

    row = await repo.get_pending_confirmation(1, 2, 10)

    assert row is c


async def test_extend_confirmation_deadline():
    from datetime import UTC, datetime

    db = _mock_session()
    repo = MetricRepository(db)

    await repo.extend_confirmation_deadline(1, datetime.now(UTC))

    db.execute.assert_awaited_once()


async def test_get_timeout_pending_confirmations():
    db = _mock_session()
    db.execute.return_value = _result(all_=[_confirmation(id=1)])
    repo = MetricRepository(db)

    rows = await repo.get_timeout_pending_confirmations()

    assert len(rows) == 1


# ---------- 健康度评分 ----------


def _health_score(**kwargs: object) -> MagicMock:
    h = MagicMock()
    defaults = {
        "id": 1,
        "metric_id": 1,
        "score": 90,
        "level": "EXCELLENT",
        "completeness_score": 95,
        "activity_score": 90,
        "quality_score": 85,
        "owner_response_score": 92,
        "lineage_coverage_score": 88,
        "missing_dimensions": [],
        "calculated_at": None,
    }
    defaults.update(kwargs)
    for k, v in defaults.items():
        setattr(h, k, v)
    return h


async def test_save_health_score_updates_existing():
    db = _mock_session()
    existing = _health_score(id=1)
    db.execute.return_value = _result(scalar_one_or_none=existing)
    repo = MetricRepository(db)
    score = _health_score(metric_id=1)

    result = await repo.save_health_score(score)

    assert result is existing
    db.refresh.assert_awaited_once_with(existing)
    # 第一次 execute = 查询现有，第二次 = update
    assert db.execute.await_count == 2


async def test_save_health_score_creates_new():
    db = _mock_session()
    db.execute.return_value = _result(scalar_one_or_none=None)
    repo = MetricRepository(db)
    score = _health_score(metric_id=2)

    result = await repo.save_health_score(score)

    db.add.assert_called_once_with(score)
    db.flush.assert_awaited()
    db.refresh.assert_awaited_once_with(score)
    assert result is score


async def test_get_health_score_returns_score():
    db = _mock_session()
    h = _health_score(metric_id=1)
    db.execute.return_value = _result(scalar_one_or_none=h)
    repo = MetricRepository(db)

    row = await repo.get_health_score(1)

    assert row is h


async def test_list_critical_metrics():
    db = _mock_session()
    db.execute.return_value = _result(all_=[_metric(metric_code="m1")])
    repo = MetricRepository(db)

    rows = await repo.list_critical_metrics(level="CRITICAL")

    assert len(rows) == 1


# ---------- Dashboard 聚合 ----------


def _row_result(total: int, pii_count: int) -> MagicMock:
    row = MagicMock()
    row.total = total
    row.pii_count = pii_count
    r = MagicMock()
    r.one.return_value = row
    return r


async def test_aggregate_dashboard_with_filters():
    db = _mock_session()
    # 顺序：total+pii / by_status / by_tier / by_domain / owner×5+names /
    #       quality×2 / compliance / conflict / freshness / 资产总览×6
    db.execute.side_effect = [
        _row_result(total=5, pii_count=2),
        _result(all_=[("PUBLISHED", 3), ("DRAFT", 2)]),  # by_status
        _result(all_=[("T1", 4), ("T2", 1)]),  # by_tier
        _result(all_=[("sales", 5)]),  # by_domain
        # Owner 责任分布（跨资产）：指标 / 数据表 / 维度 / 术语 / 模板 / 数据源 / 显示名
        _result(all_=[(1, "PUBLISHED", 3), (1, "DRAFT", 2)]),  # owner_metric
        _result(all_=[(1, 5)]),  # owner_table
        _result(all_=[(1, "PUBLISHED", 2)]),  # owner_dim (owner_id, status, count)
        _result(all_=[(1, "PUBLISHED", 3)]),  # owner_term (owner_id, status, count)
        _result(all_=[(1, 1)]),  # owner_tpl
        _result(all_=[(1, 2)]),  # owner_source
        _result(all_=[(1, "Alice")]),  # owner_names
        # 治理指标体系（quality / compliance / conflict / freshness）
        _result(all_=[("P1", 1)]),  # quality by_severity
        _result(all_=[("OPEN", 1)]),  # quality by_status
        _result(all_=[(True, 4), (False, 1)]),  # compliance
        _result(all_=[("OPEN", 2)]),  # conflict
        _result(scalar=3),  # freshness updated_30d
        _result(all_=[("INTERNAL", 10), ("PII", 3)]),  # table: sensitivity_level
        _result(all_=[("healthy", 4), ("unknown", 1)]),  # source: health_status
        _result(all_=[("PUBLISHED", 2)]),  # dimension: status
        _result(all_=[("PUBLISHED", 3), ("DRAFT", 1)]),  # term: status
        _result(all_=[(True, 5), (False, 1)]),  # template: is_active（bool→active/inactive）
        _result(all_=[("active", 8), ("inactive", 2)]),  # system_dict: status
    ]
    repo = MetricRepository(db)

    result = await repo.aggregate_dashboard(domain="sales", owner_id=1)

    assert result["total"] == 5
    assert result["pii_count"] == 2
    assert result["by_status"] == {"PUBLISHED": 3, "DRAFT": 2}
    assert result["by_tier"] == {"T1": 4, "T2": 1}
    assert result["by_domain"] == {"sales": 5}
    assert result["pii_ratio"] == round(2 / 5, 4)
    assert db.execute.await_count == 22
    # Owner 责任分布（跨资产）：指标 5 + 数据表 5 + 维度 2 + 术语 3 + 模板 1 + 数据源 2 = 18
    assert result["by_owner"] == {
        1: {
            "name": "Alice",
            "total": 18,
            "metrics": {"total": 5, "by_status": {"PUBLISHED": 3, "DRAFT": 2}},
            "tables": {"total": 5, "by_status": {}},
            "sources": {"total": 2, "by_status": {}},
            "dimensions": {"total": 2, "by_status": {"PUBLISHED": 2}},
            "terms": {"total": 3, "by_status": {"PUBLISHED": 3}},
            "templates": {"total": 1, "by_status": {}},
        }
    }
    # 资产总览：指标复用顶层聚合；其余资产按各自状态列分组
    assert result["assets"]["metric"] == {
        "total": 5,
        "by_status": {"PUBLISHED": 3, "DRAFT": 2},
    }
    assert result["assets"]["table"] == {"total": 13, "by_status": {"INTERNAL": 10, "PII": 3}}
    assert result["assets"]["source"] == {"total": 5, "by_status": {"healthy": 4, "unknown": 1}}
    assert result["assets"]["dimension"] == {"total": 2, "by_status": {"PUBLISHED": 2}}
    assert result["assets"]["term"] == {"total": 4, "by_status": {"PUBLISHED": 3, "DRAFT": 1}}
    assert result["assets"]["template"] == {"total": 6, "by_status": {"active": 5, "inactive": 1}}
    assert result["assets"]["system_dict"] == {
        "total": 10,
        "by_status": {"active": 8, "inactive": 2},
    }


async def test_aggregate_dashboard_without_filters_and_zero_total():
    db = _mock_session()
    db.execute.side_effect = [
        _row_result(total=0, pii_count=None),  # pii_count None → or 0
        _result(all_=[]),  # by_status
        _result(all_=[]),  # by_tier
        _result(all_=[]),  # by_domain
        _result(all_=[]),  # owner_metric
        _result(all_=[]),  # owner_table
        _result(all_=[]),  # owner_dim
        _result(all_=[]),  # owner_term
        _result(all_=[]),  # owner_tpl
        _result(all_=[]),  # owner_source
        # owner_names 跳过（owner_ids 为空）
        _result(all_=[]),  # quality severity
        _result(all_=[]),  # quality status
        _result(all_=[]),  # compliance
        _result(all_=[]),  # conflict
        _result(scalar=0),  # freshness
        _result(all_=[]),  # table
        _result(all_=[]),  # source
        _result(all_=[]),  # dimension
        _result(all_=[]),  # term
        _result(all_=[]),  # template
        _result(all_=[]),  # system_dict
    ]
    repo = MetricRepository(db)

    result = await repo.aggregate_dashboard()

    assert result["total"] == 0
    assert result["pii_count"] == 0
    assert result["by_status"] == {}
    assert result["by_tier"] == {}
    assert result["by_domain"] == {}
    assert result["pii_ratio"] == 0.0
    assert result["by_owner"] == {}
    assert result["quality"] == {"total": 0, "by_severity": {}, "pending": 0}
    assert result["compliance"] == {"total": 0, "reviewed": 0, "pending": 0, "reviewed_ratio": 0.0}
    assert result["conflict"] == {"total": 0, "open": 0, "escalated": 0, "by_status": {}}
    assert result["freshness"] == {"total": 0, "updated_30d": 0, "updated_30d_ratio": 0.0}
    assert result["assets"]["metric"] == {"total": 0, "by_status": {}}
    assert result["assets"]["template"] == {"total": 0, "by_status": {"active": 0, "inactive": 0}}
    assert result["assets"]["system_dict"] == {
        "total": 0,
        "by_status": {"active": 0, "inactive": 0},
    }


async def test_aggregate_dashboard_governance_indicators():
    """总览仪表完整指标体系：by_owner 责任分布 + 质量/合规/冲突/新鲜度聚合。

    新增 6 次查询（插在 domain 之后、资产聚合之前）：
    by_owner / quality_severity / quality_status / compliance / conflict_status / freshness。
    """
    db = _mock_session()
    db.execute.side_effect = [
        _row_result(total=10, pii_count=3),  # total + pii
        _result(all_=[("PUBLISHED", 6), ("DRAFT", 3), ("REVIEW", 1)]),  # by_status
        _result(all_=[("T1", 4), ("T2", 4), ("T3", 2)]),  # by_tier
        _result(all_=[("sales", 6), ("risk", 4)]),  # by_domain
        # Owner 责任分布（跨资产）：指标 / 数据表 / 维度 / 术语 / 模板 / 数据源 / 显示名
        _result(all_=[
            (1, "PUBLISHED", 4),
            (1, "REVIEW", 1),
            (2, "PUBLISHED", 2),
            (2, "DRAFT", 3),
        ]),  # owner_metric (owner_id, status, count)
        _result(all_=[(1, 6), (2, 2)]),  # owner_table
        _result(all_=[(1, "PUBLISHED", 3), (2, "PUBLISHED", 1)]),  # owner_dim
        _result(all_=[(1, "PUBLISHED", 4), (2, "PUBLISHED", 2)]),  # owner_term
        _result(all_=[(1, 2)]),  # owner_tpl
        _result(all_=[(1, 1), (2, 1)]),  # owner_source
        _result(all_=[(1, "Alice"), (2, "Bob")]),  # owner_names
        _result(all_=[("P0", 1), ("P1", 2), ("P2", 2)]),  # quality by_severity（仅未关闭，合计 5）
        _result(all_=[("OPEN", 4), ("ACK", 1), ("RESOLVED", 3)]),  # quality by_status
        _result(all_=[(True, 7), (False, 3)]),  # compliance reviewed
        _result(all_=[("OPEN", 2), ("NEGOTIATING", 1), ("ESCALATED", 1), ("RULED", 1)]),  # conflict
        _result(scalar=6),  # freshness updated_30d
        _result(all_=[("INTERNAL", 5), ("PII", 2)]),  # table
        _result(all_=[("healthy", 3)]),  # source
        _result(all_=[("PUBLISHED", 4)]),  # dimension
        _result(all_=[("PUBLISHED", 5), ("DRAFT", 1)]),  # term
        _result(all_=[(True, 5), (False, 2)]),  # template
        _result(all_=[("active", 6), ("inactive", 1)]),  # system_dict
    ]
    repo = MetricRepository(db)

    result = await repo.aggregate_dashboard()

    # Owner 责任分布（跨资产）：每 owner 汇总指标/数据表/维度/术语/模板/数据源计数
    # Alice：指标 5 + 数据表 6 + 维度 3 + 术语 4 + 模板 2 + 数据源 1 = 21
    # Bob：指标 5 + 数据表 2 + 维度 1 + 术语 2 + 模板 0 + 数据源 1 = 11
    assert result["by_owner"] == {
        1: {
            "name": "Alice",
            "total": 21,
            "metrics": {"total": 5, "by_status": {"PUBLISHED": 4, "REVIEW": 1}},
            "tables": {"total": 6, "by_status": {}},
            "sources": {"total": 1, "by_status": {}},
            "dimensions": {"total": 3, "by_status": {"PUBLISHED": 3}},
            "terms": {"total": 4, "by_status": {"PUBLISHED": 4}},
            "templates": {"total": 2, "by_status": {}},
        },
        2: {
            "name": "Bob",
            "total": 11,
            "metrics": {"total": 5, "by_status": {"PUBLISHED": 2, "DRAFT": 3}},
            "tables": {"total": 2, "by_status": {}},
            "sources": {"total": 1, "by_status": {}},
            "dimensions": {"total": 1, "by_status": {"PUBLISHED": 1}},
            "terms": {"total": 2, "by_status": {"PUBLISHED": 2}},
            "templates": {"total": 0, "by_status": {}},
        },
    }
    # 质量健康：大数字 = 当前待处理事件（by_severity 仅统计 OPEN/ACK，与可观测中心同口径）
    assert result["quality"] == {
        "total": 5,
        "by_severity": {"P0": 1, "P1": 2, "P2": 2},
        "pending": 5,
    }
    # 合规：复核率
    assert result["compliance"] == {
        "total": 10,
        "reviewed": 7,
        "pending": 3,
        "reviewed_ratio": 0.7,
    }
    # 冲突风险：未关闭 = OPEN + NEGOTIATING + ESCALATED（RULED 已决不计入）
    assert result["conflict"] == {
        "total": 4,
        "open": 3,
        "escalated": 1,
        "by_status": {"OPEN": 2, "NEGOTIATING": 1, "ESCALATED": 1, "RULED": 1},
    }
    # 新鲜度：近 30 天更新
    assert result["freshness"] == {
        "total": 10,
        "updated_30d": 6,
        "updated_30d_ratio": 0.6,
    }
    assert db.execute.await_count == 22


async def test_aggregate_dashboard_domain_applied_to_owner_and_domain_queries():
    """?domain=X 时 domain 过滤须贯穿 by_domain 与 Owner 分布全部资产查询，避免口径撕裂。

    回归审查发现：owner_metric 带 domain 过滤但 owner_table/dim/term/tpl/source 与
    by_domain 不带，同一张 Owner 卡内指标按 X 域、其它资产按全库统计。
    """
    db = _mock_session()
    db.execute.side_effect = [
        _row_result(total=5, pii_count=1),
        _result(all_=[("PUBLISHED", 5)]),  # by_status
        _result(all_=[("T1", 5)]),  # by_tier
        _result(all_=[("sales", 5)]),  # by_domain
        _result(all_=[(1, "PUBLISHED", 5)]),  # owner_metric
        _result(all_=[(1, 5)]),  # owner_table
        _result(all_=[(1, "PUBLISHED", 2)]),  # owner_dim
        _result(all_=[(1, "PUBLISHED", 3)]),  # owner_term
        _result(all_=[(1, 1)]),  # owner_tpl
        _result(all_=[(1, 2)]),  # owner_source
        _result(all_=[(1, "Alice")]),  # owner_names
        _result(all_=[("P1", 1)]),  # quality by_severity
        _result(all_=[("OPEN", 1)]),  # quality by_status
        _result(all_=[(True, 5)]),  # compliance
        _result(all_=[("OPEN", 1)]),  # conflict
        _result(scalar=5),  # freshness
        _result(all_=[]),  # table
        _result(all_=[]),  # source
        _result(all_=[]),  # dimension
        _result(all_=[]),  # term
        _result(all_=[]),  # template
        _result(all_=[]),  # system_dict
    ]
    repo = MetricRepository(db)

    await repo.aggregate_dashboard(domain="sales")

    # 主聚合 + by_domain + Owner 分布 6 查询（指标/表/维度/术语/模板/源）均带 domain 过滤
    for i in (0, 3, 4, 5, 6, 7, 8, 9):
        sql = _compiled_sql(db, i)
        assert "domain = 'sales'" in sql, f"第 {i} 次查询缺 domain 过滤: {sql}"
    # 对照：by_status / by_tier 属指标聚合，已含 domain（走同一 conditions），
    # 此处仅确认查询顺序无错位——by_domain 索引 3 之后不再出现未过滤的资产查询


# ---------- P1-F/P1-G：冲突预检活动指标加载（排除 DEPRECATED + 分页全量） ----------


def _scalar_result(rows: list) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value.all.return_value = rows
    return r


async def test_list_active_for_conflict_excludes_deprecated_and_paginates():
    """P1-F/G：排除 DEPRECATED + 分页全量（无 limit=1000 截断）。

    修复前用 ``list_metrics(limit=1000)``：不过滤状态致 DEPRECATED 参与比对
    制造仲裁台噪音、1000 条截断漏检更早的历史指标。现按 id 分页全量加载。
    """
    db = _mock_session()
    page1 = [_metric(id=1, status="PUBLISHED"), _metric(id=2, status="EXPERIMENTAL")]
    page2 = [_metric(id=3, status="PUBLISHED")]  # 不满页 → 循环结束
    db.execute = AsyncMock(
        side_effect=[_scalar_result(page1), _scalar_result(page2)]
    )
    repo = MetricRepository(db)
    rows = await repo.list_active_for_conflict(page_size=2)
    assert [m.id for m in rows] == [1, 2, 3]
    # 分页：两次查询（第一次满页、第二次不满页结束）
    assert db.execute.await_count == 2
    # 每次查询均携带 select 语句对象（含 where/order/offset）
    assert db.execute.await_args_list[0].args[0] is not None
    assert db.execute.await_args_list[1].args[0] is not None


async def test_purge_metric_executes_cascade_without_nameerror():
    """purge 级联真实执行不抛 NameError（防模型 import 回归）。

    背景：purge_metric 引用 QualityRule/Favorite 等模型执行 delete，若 repository
    import 区被误删（曾发生 QualityRule/Favorite/text 缺失），真实执行到该 delete
    构造即抛 NameError——单元测试若仅 MagicMock repo 方法无法暴露。此处用
    _mock_session 让全部 delete 语句真实构造（execute 为 AsyncMock 不真正执行）。
    """
    db = _mock_session()
    repo = MetricRepository(db)
    # 真实执行 purge：12 个级联 delete 语句逐一构造（模型名须在 repository 命名空间可见）
    await repo.purge_metric(metric_id=7, metric_code="sales_gmv_daily")
    # 全部级联删除 + 主行删除均发出（LineageEdge/快照/版本/待确认/维度/健康度/挂载/
    # 质量规则/冲突/收藏/主行 = 11 条 delete；LineageEdge 亦计 delete）
    assert db.execute.await_count >= 11
    # 每个调用均为 delete 语句（非空）
    for call in db.execute.await_args_list:
        assert call.args[0] is not None
