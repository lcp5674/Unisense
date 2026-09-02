"""语义 API 层测试：指标模板责任人指派（PATCH /semantics/templates/{id}/owner）。

背景（总览仪表 Owner 责任分布跨资产）：
- MetricTemplate 新增 owner_id 字段后，模板可纳入 Owner 责任统计。
- PATCH 端点用于前端「模板列表指派责任人」，本测试固化端点行为：
  指派/解除、404（模板/用户不存在）、422（非法 owner_id）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.semantic import update_template_owner
from app.core.exceptions import NotFoundError


def _make_template(template_id: int = 1, code: str = "tpl_fin_gmv") -> MagicMock:
    t = MagicMock()
    t.id = template_id
    t.code = code
    t.owner_id = None
    return t


async def test_template_owner_assign_and_errors() -> None:
    """指派/解除责任人 + 404/422 分支（直接调用 API 函数）。"""

    template = _make_template()
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    # 1) 指派 owner_id=2：查模板 + 查用户存在 → 写入 + commit
    r1 = MagicMock()
    r1.scalar_one_or_none.return_value = template
    u = MagicMock()
    u.scalar_one_or_none.return_value = MagicMock(id=2)
    db.execute = AsyncMock(side_effect=[r1, u])
    user = MagicMock(id=1)
    req = MagicMock()
    await update_template_owner(
        user=user, template_id=1, request=req, body={"owner_id": 2}, db=db
    )
    assert template.owner_id == 2
    db.commit.assert_awaited_once()

    # 2) 解除 owner（owner_id=None）：只查模板，owner 置空
    template2 = _make_template()
    template2.owner_id = 2
    r2 = MagicMock()
    r2.scalar_one_or_none.return_value = template2
    db.execute = AsyncMock(side_effect=[r2])
    await update_template_owner(
        user=user, template_id=1, request=req, body={"owner_id": None}, db=db
    )
    assert template2.owner_id is None

    # 3) 模板不存在 → NotFoundError
    r3 = MagicMock()
    r3.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[r3])
    with pytest.raises(NotFoundError):
        await update_template_owner(
            user=user, template_id=999, request=req, body={"owner_id": 2}, db=db
        )

    # 4) 目标用户不存在 → NotFoundError
    template4 = _make_template()
    r4 = MagicMock()
    r4.scalar_one_or_none.return_value = template4
    u4 = MagicMock()
    u4.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[r4, u4])
    with pytest.raises(NotFoundError):
        await update_template_owner(
            user=user, template_id=1, request=req, body={"owner_id": 999}, db=db
        )

    # 5) owner_id 非法（0）→ ValidationError 422（错误信封收敛后不再抛 HTTPException）
    from app.core.exceptions import ValidationError

    r5 = MagicMock()
    r5.scalar_one_or_none.return_value = _make_template()
    db.execute = AsyncMock(side_effect=[r5])
    with pytest.raises(ValidationError) as exc:
        await update_template_owner(
            user=user, template_id=1, request=req, body={"owner_id": 0}, db=db
        )
    assert exc.value.error_code == "VALIDATION_ERROR"


async def test_instantiate_template_commit_integrity_error() -> None:
    """模板实例化并发竞态：commit 唯一键冲突 → 转 ConflictError(409)，不 500。

    覆盖审查遗留项：instantiate_template 端点 commit 无 IntegrityError 兜底——
    "先预检再插"的 TOCTOU 场景下血缘/冲突等延迟 flush 对象在 commit 才暴露唯一键
    冲突，捕获转 ConflictError（对齐上方模板创建端点先例）。
    """
    from unittest.mock import patch

    from sqlalchemy.exc import IntegrityError

    from app.api.semantic import instantiate_template
    from app.core.exceptions import ConflictError

    template = MagicMock()
    template.defaults_json = {}
    for _f in (
        "type",
        "granularity",
        "unit",
        "aggregation",
        "time_semantics",
        "freshness",
        "dw_layer",
        "serving_mode",
        "additivity",
        "metric_tier",
        "measure_id",
        "mount",
        "product_owner_id",
        "tech_owner_id",
        "dw_developer_id",
        "product_owner_name",
        "tech_owner_name",
        "dw_developer_name",
    ):
        setattr(template, _f, None)
    template.required_fields = []

    r = MagicMock()
    r.scalar_one_or_none.return_value = template
    db = MagicMock()
    db.execute = AsyncMock(return_value=r)
    db.commit = AsyncMock(
        side_effect=IntegrityError(
            "Duplicate entry 'tpl_sales_gmv_day' for key 'metric.metric_code'", None, None
        )
    )
    db.rollback = AsyncMock()

    body = {
        "metric_code": "tpl_sales_gmv_day",
        "name": "销售 GMV 日",
        "domain": "sales",
        "type": "atomic",
        "aggregation": "SUM",
    }
    user = MagicMock(id=1)
    with (
        patch("app.api.semantic.MetricService") as mock_svc,
        patch("app.api.semantic._drop_invalid_literal_presets", return_value=None),
        patch("app.services.semantic.schemas.MetricCreateRequest", return_value=MagicMock()),
    ):
        mock_svc.return_value.create_metric = AsyncMock(
            return_value=MagicMock(metric_code="tpl_sales_gmv_day")
        )
        with pytest.raises(ConflictError) as exc_info:
            await instantiate_template(
                user=user, template_id=1, request=MagicMock(), body=body, db=db
            )
    assert exc_info.value.error_code == "METRIC_CODE_EXISTS"
    db.rollback.assert_awaited_once()


async def test_list_templates_escapes_wildcards_and_sorts_stably() -> None:
    """模板列表：LIKE 通配符转义（FR-035）+ 排序确定性（domain,name,id 次级，防翻页重漏）。"""
    from sqlalchemy.dialects import mysql

    from app.api.semantic import list_templates

    db = MagicMock()
    r1 = MagicMock()
    r1.scalar_one_or_none.return_value = 0
    r2 = MagicMock()
    r2.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[r1, r2])

    await list_templates(
        request=MagicMock(),
        _user=MagicMock(),
        domain=None,
        is_active=None,
        keyword="100%_x",
        owner_id=None,
        page=1,
        page_size=20,
        db=db,
    )

    # 第二个 execute 是列表查询：编译为 MySQL 方言验证 ESCAPE 与排序
    list_stmt = db.execute.call_args_list[1].args[0]
    literal_sql = str(
        list_stmt.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "ESCAPE '/'" in literal_sql
    assert "100%_x" not in literal_sql  # 原始关键词（含裸 %/_）不得出现
    assert "metric_template.id" in literal_sql  # 主键次级排序（排序确定性）


async def test_instantiate_required_fields_satisfied_by_template_default() -> None:
    """required_fields 校验应对齐 merged：模板默认值亦满足必填（仅查 body 会误拒）。"""
    from unittest.mock import patch

    from app.api.semantic import instantiate_template
    from app.core.exceptions import ValidationError

    template = MagicMock()
    template.id = 1
    template.is_active = True
    template.defaults_json = {
        "granularity": "day",
        "definition_json": {"expression": "sum(x)"},
        # OneData 原子层：模板实例化 atomic 须引用逻辑度量
        "measure_id": 1,
    }
    template.required_fields = ["granularity"]
    for f, v in (
        ("type", "atomic"), ("unit", "元"), ("aggregation", "SUM"),
        ("time_semantics", "PERIOD"), ("freshness", "T1"), ("dw_layer", "DWS"),
        ("serving_mode", "BATCH_ONLY"), ("additivity", "ADDITIVE"), ("metric_tier", "T3"),
        # OneData 预设列：mock 须显式置 None（否则 MagicMock 泄漏进 merged → 422）
        ("measure_id", None), ("mount", None),
        ("product_owner_id", None), ("tech_owner_id", None),
        # 数仓开发责任方必填（注册门禁）：预设平台用户 id 满足校验
        ("dw_developer_id", 2),
        ("product_owner_name", None), ("tech_owner_name", None), ("dw_developer_name", None),
    ):
        setattr(template, f, v)

    def _db_with_query(return_template):
        db = MagicMock()
        r = MagicMock()
        r.scalar_one_or_none.return_value = return_template
        db.execute = AsyncMock(return_value=r)
        db.commit = AsyncMock()
        return db

    fake_metric = MagicMock()
    fake_metric.metric_code = "sales_gmv_day"
    fake_metric.to_dict = MagicMock(return_value={"metric_code": "sales_gmv_day"})
    svc_instance = MagicMock()
    svc_instance.create_metric = AsyncMock(return_value=fake_metric)

    # 场景 1：required 的 granularity 由模板默认提供（body 不传）→ 通过并创建
    db = _db_with_query(template)
    user = MagicMock(id=1)
    req = MagicMock()
    with patch("app.api.semantic.MetricService", return_value=svc_instance):
        resp = await instantiate_template(
            user=user, template_id=1, request=req, body={"name": "测试", "domain": "sales"}, db=db
        )
    assert resp.data["metric_code"] == "sales_gmv_day"
    svc_instance.create_metric.assert_awaited_once()

    # 场景 2：required 的 source_table 在 body 与模板默认均缺失 → 拒绝
    template2 = MagicMock()
    template2.id = 2
    template2.is_active = True
    template2.defaults_json = {}
    template2.required_fields = ["source_table"]
    for f, v in (
        ("type", "atomic"), ("unit", "元"), ("aggregation", "SUM"),
        ("time_semantics", "PERIOD"), ("freshness", "T1"), ("dw_layer", "DWS"),
        ("serving_mode", "BATCH_ONLY"), ("additivity", "ADDITIVE"), ("metric_tier", "T3"),
    ):
        setattr(template2, f, v)
    db2 = _db_with_query(template2)
    with pytest.raises(ValidationError):
        await instantiate_template(
            user=user, template_id=2, request=req, body={"name": "x", "domain": "s"}, db=db2
        )


async def test_template_active_toggle_and_errors() -> None:
    """启用/停用模板 + 404/422（直接调用 API 函数，对齐 owner 端点测试模式）。"""

    from app.api.semantic import update_template_active

    user = MagicMock(id=1)
    req = MagicMock()

    # 1) 停用模板（is_active=False）
    template = _make_template()
    template.is_active = True
    r1 = MagicMock()
    r1.scalar_one_or_none.return_value = template
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(side_effect=[r1])
    await update_template_active(
        user=user, template_id=1, request=req, body={"is_active": False}, db=db
    )
    assert template.is_active is False
    db.commit.assert_awaited_once()

    # 2) 重新启用
    template2 = _make_template()
    template2.is_active = False
    r2 = MagicMock()
    r2.scalar_one_or_none.return_value = template2
    db.execute = AsyncMock(side_effect=[r2])
    await update_template_active(
        user=user, template_id=1, request=req, body={"is_active": True}, db=db
    )
    assert template2.is_active is True

    # 3) 模板不存在 → NotFoundError
    r3 = MagicMock()
    r3.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[r3])
    with pytest.raises(NotFoundError):
        await update_template_active(
            user=user, template_id=999, request=req, body={"is_active": True}, db=db
        )

    # 4) is_active 非布尔 → ValidationError 422（ed84c0ef 错误信封收敛 HTTPException→UnisenseError）
    from app.core.exceptions import ValidationError

    r4 = MagicMock()
    r4.scalar_one_or_none.return_value = _make_template()
    db.execute = AsyncMock(side_effect=[r4])
    with pytest.raises(ValidationError) as exc:
        await update_template_active(
            user=user, template_id=1, request=req, body={"is_active": "yes"}, db=db
        )
    assert exc.value.error_code == "VALIDATION_ERROR"


async def test_instantiate_empty_definition_falls_back_to_template_default() -> None:
    """body 传空 definition_json（前端未填口径）时不覆盖模板默认口径（防空心指标）。"""
    from unittest.mock import patch

    from app.api.semantic import instantiate_template

    template = MagicMock()
    template.id = 1
    template.is_active = True
    template.defaults_json = {
        "granularity": "day",
        "definition_json": {"expression": "sum(x)", "source_tables": ["dwd.orders"]},
        # OneData 原子层：模板实例化 atomic 须引用逻辑度量
        "measure_id": 1,
    }
    template.required_fields = []
    for f, v in (
        ("type", "atomic"), ("unit", "元"), ("aggregation", "SUM"),
        ("time_semantics", "PERIOD"), ("freshness", "T1"), ("dw_layer", "DWS"),
        ("serving_mode", "BATCH_ONLY"), ("additivity", "ADDITIVE"), ("metric_tier", "T3"),
        # OneData 预设列：mock 须显式置 None（否则 MagicMock 泄漏进 merged → 422）
        ("measure_id", None), ("mount", None),
        ("product_owner_id", None), ("tech_owner_id", None),
        # 数仓开发责任方必填（注册门禁）：预设平台用户 id 满足校验
        ("dw_developer_id", 2),
        ("product_owner_name", None), ("tech_owner_name", None), ("dw_developer_name", None),
    ):
        setattr(template, f, v)

    db = MagicMock()
    r = MagicMock()
    r.scalar_one_or_none.return_value = template
    db.execute = AsyncMock(return_value=r)
    db.commit = AsyncMock()

    fake_metric = MagicMock()
    fake_metric.metric_code = "sales_gmv_day"
    fake_metric.to_dict = MagicMock(return_value={"metric_code": "sales_gmv_day"})
    svc_instance = MagicMock()
    svc_instance.create_metric = AsyncMock(return_value=fake_metric)

    user = MagicMock(id=1)
    req = MagicMock()
    with patch("app.api.semantic.MetricService", return_value=svc_instance):
        await instantiate_template(
            user=user,
            template_id=1,
            request=req,
            body={"name": "测试", "domain": "sales", "definition_json": {}},
            db=db,
        )
    # create_metric 收到的口径应回退为模板默认（而非空对象覆盖）
    call_kwargs = svc_instance.create_metric.call_args[0][0]
    assert call_kwargs.definition_json == {
        "expression": "sum(x)",
        "source_tables": ["dwd.orders"],
    }


def _instantiate_template_mock(
    *,
    type_: str = "atomic",
    defaults: dict | None = None,
    **presets: object,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """构造 instantiate 测试的公共 mock（模板 + db + MetricService）。

    模板列预设经 ``presets`` 覆盖（measure_id/mount/三方责任/serving_mode 等），
    defaults_json 从 ``defaults`` 合并（含 definition_json 保证原子口径合法）。
    """
    from unittest.mock import patch

    template = MagicMock()
    template.id = 1
    template.is_active = True
    template.defaults_json = {
        "granularity": "day",
        "measure_id": 1,
        "definition_json": {"expression": "sum(x)"},
        **(defaults or {}),
    }
    template.required_fields = []
    base = {
        "type": type_, "unit": "元", "aggregation": "SUM",
        "time_semantics": "PERIOD", "freshness": "T1", "dw_layer": "DWS",
        "serving_mode": "BATCH_ONLY", "additivity": "ADDITIVE", "metric_tier": "T3",
        "measure_id": None, "mount": None,
        "product_owner_id": None, "tech_owner_id": None,
        # 数仓开发责任方必填（注册门禁 PRD 4.5）：mock 预设平台用户 id，
        # 否则 MetricCreateRequest 的 validate_dw_developer_required 会 422 全部实例化测试
        "dw_developer_id": 2,
        "product_owner_name": None, "tech_owner_name": None, "dw_developer_name": None,
    }
    base.update(presets)
    for f, v in base.items():
        setattr(template, f, v)

    db = MagicMock()
    r = MagicMock()
    r.scalar_one_or_none.return_value = template
    db.execute = AsyncMock(return_value=r)
    db.commit = AsyncMock()

    fake_metric = MagicMock()
    fake_metric.metric_code = "sales_gmv_day"
    fake_metric.to_dict = MagicMock(return_value={"metric_code": "sales_gmv_day"})
    svc_instance = MagicMock()
    svc_instance.create_metric = AsyncMock(return_value=fake_metric)
    patch_obj = patch("app.api.semantic.MetricService", return_value=svc_instance)
    return template, db, patch_obj, svc_instance


async def test_instantiate_atomic_presets_measure_id_and_responsible() -> None:
    """方案A：原子模板实例化——模板列预设的 measure_id/三方责任并入 create_metric 请求。

    修复前模板无 measure_id 字段，原子实例化因缺逻辑度量 422；
    修复后模板 measure_id 预设透传，且可预设默认三方责任。
    """
    from app.api.semantic import instantiate_template

    _tpl, db, patch_obj, svc_instance = _instantiate_template_mock(
        type_="atomic", measure_id=7, product_owner_id=3, product_owner_name="外部产品"
    )
    user = MagicMock(id=1)
    req = MagicMock()
    with patch_obj:
        await instantiate_template(
            user=user, template_id=1, request=req,
            body={"name": "测试", "domain": "sales"}, db=db,
        )
    create_req = svc_instance.create_metric.call_args[0][0]
    assert create_req.measure_id == 7
    assert create_req.product_owner_id == 3
    assert create_req.product_owner_name == "外部产品"


async def test_instantiate_derived_presets_mount() -> None:
    """方案A：派生模板实例化——模板 mount 预设并入 create_metric 请求（落 metric_mount）。

    修复前模板无 mount 字段，派生实例化出"无家"指标（无粒度/无落地血缘）；
    修复后 mount 透传，service 自动落 metric_mount 并回填粒度。
    """
    from app.api.semantic import instantiate_template

    mount_preset = {
        "source_table": "dwd.sales_detail", "source_column": "amount",
        "granularity": "日", "default_period": "day", "domain": "sales",
    }
    _tpl, db, patch_obj, svc_instance = _instantiate_template_mock(
        type_="derived",
        defaults={"definition_json": {"expression": "x", "dependencies": ["sales_gmv"]}},
        mount=mount_preset,
    )
    user = MagicMock(id=1)
    req = MagicMock()
    with patch_obj:
        await instantiate_template(
            user=user, template_id=1, request=req,
            body={"name": "派生GMV", "domain": "sales"}, db=db,
        )
    create_req = svc_instance.create_metric.call_args[0][0]
    assert create_req.mount.source_table == "dwd.sales_detail"
    assert create_req.mount.granularity == "日"


async def test_instantiate_body_overrides_template_preset() -> None:
    """用户 body 显式传值优先于模板预设（measure_id 覆盖）。"""
    from app.api.semantic import instantiate_template

    _tpl, db, patch_obj, svc_instance = _instantiate_template_mock(
        type_="atomic", measure_id=7
    )
    user = MagicMock(id=1)
    req = MagicMock()
    with patch_obj:
        await instantiate_template(
            user=user, template_id=1, request=req,
            body={"name": "测试", "domain": "sales", "measure_id": 9}, db=db,
        )
    create_req = svc_instance.create_metric.call_args[0][0]
    assert create_req.measure_id == 9


async def test_instantiate_drops_invalid_literal_preset() -> None:
    """强韧性：模板预设非法枚举值（如历史脏数据 serving_mode='REALTIME'）不 422 卡死。

    实例化前剔除非法枚举，让其落到 MetricCreateRequest 默认值（BATCH_ONLY）。
    """
    from app.api.semantic import instantiate_template

    _tpl, db, patch_obj, svc_instance = _instantiate_template_mock(
        type_="atomic", serving_mode="REALTIME"
    )
    user = MagicMock(id=1)
    req = MagicMock()
    with patch_obj:
        await instantiate_template(
            user=user, template_id=1, request=req,
            body={"name": "测试", "domain": "sales"}, db=db,
        )
    create_req = svc_instance.create_metric.call_args[0][0]
    assert create_req.serving_mode == "BATCH_ONLY"


async def test_instantiate_mount_only_applies_for_derived() -> None:
    """挂载预设仅对派生生效：atomic 模板带 mount 预设时，不并入请求（原子不挂载）。"""
    from app.api.semantic import instantiate_template

    mount_preset = {
        "source_table": "dwd.sales_detail", "source_column": "amount",
        "granularity": "日", "default_period": "day", "domain": "sales",
    }
    _tpl, db, patch_obj, svc_instance = _instantiate_template_mock(
        type_="atomic", mount=mount_preset
    )
    user = MagicMock(id=1)
    req = MagicMock()
    with patch_obj:
        await instantiate_template(
            user=user, template_id=1, request=req,
            body={"name": "测试", "domain": "sales"}, db=db,
        )
    create_req = svc_instance.create_metric.call_args[0][0]
    assert create_req.mount is None


async def test_instantiate_skips_inapplicable_required_fields() -> None:
    """强韧性：模板必填含「类型不适用」字段时豁免，而非 422 卡死实例化。

    原子模板 required_fields 误设 mount/currency/granularity（原子恒日、无挂载、
    币种由逻辑度量体系承载）——这些字段对原子类型永远无法由表单满足，
    豁免后其余必填满足即成功（对应生产 tpl_verify_owner 场景）。
    """

    from app.api.semantic import instantiate_template

    _tpl, db, patch_obj, svc_instance = _instantiate_template_mock(type_="atomic")
    _tpl.required_fields = [
        "name", "domain", "metric_code", "granularity", "mount", "currency",
    ]
    user = MagicMock(id=1)
    req = MagicMock()
    with patch_obj:
        await instantiate_template(
            user=user, template_id=1, request=req,
            body={"name": "测试", "domain": "sales", "metric_code": "sales_gmv_inst_day"},
            db=db,
        )
    # 豁免 mount/currency/granularity，剩余必填（name/domain/metric_code）满足 → 成功
    assert svc_instance.create_metric.await_count == 1


async def test_instantiate_keeps_applicable_required_fields() -> None:
    """豁免只针对「类型不适用 + 系统可自动生成」字段：name 适用且缺失仍报必填缺失。"""
    from app.api.semantic import instantiate_template
    from app.core.exceptions import ValidationError

    _tpl, db, patch_obj, svc_instance = _instantiate_template_mock(type_="atomic")
    _tpl.required_fields = ["name", "metric_code", "currency"]
    user = MagicMock(id=1)
    req = MagicMock()
    with patch_obj, pytest.raises(ValidationError) as ei:
        await instantiate_template(
            user=user, template_id=1, request=req,
            body={"domain": "sales"}, db=db,
        )
    # currency 对原子豁免、metric_code 系统可自动生成豁免；name 适用且缺失 → 仍报必填缺失
    assert "指标名称" in ei.value.message
    assert "metric_code" not in ei.value.message
    assert "币种" not in ei.value.message
    svc_instance.create_metric.assert_not_called()


async def test_instantiate_metric_code_missing_auto_generates() -> None:
    """强韧性：metric_code 是系统可自动生成字段（schema 缺省由 Service 层补全），
    模板 required_fields 含它但用户留空时不得 422——留空交由 create_metric 自动生成。
    """
    from app.api.semantic import instantiate_template

    _tpl, db, patch_obj, svc_instance = _instantiate_template_mock(type_="atomic")
    _tpl.required_fields = ["name", "domain", "metric_code"]
    user = MagicMock(id=1)
    req = MagicMock()
    with patch_obj:
        await instantiate_template(
            user=user, template_id=1, request=req,
            body={"name": "测试", "domain": "sales"}, db=db,
        )
    # metric_code 未提供 → 必填校验通过（豁免），交由 Service 层自动生成
    assert svc_instance.create_metric.await_count == 1
    create_req = svc_instance.create_metric.call_args[0][0]
    assert create_req.metric_code is None


async def test_instantiate_bool_false_is_not_missing() -> None:
    """必填判定：布尔 False 是显式取值（pii_flag=false 合法），不得判缺失。"""
    from app.api.semantic import instantiate_template

    _tpl, db, patch_obj, svc_instance = _instantiate_template_mock(type_="atomic")
    _tpl.required_fields = ["metric_code", "pii_flag"]
    user = MagicMock(id=1)
    req = MagicMock()
    with patch_obj:
        await instantiate_template(
            user=user, template_id=1, request=req,
            body={
                "name": "测试", "domain": "sales",
                "metric_code": "sales_gmv_inst_day", "pii_flag": False,
            },
            db=db,
        )
    # pii_flag=false 显式关闭 PII 是合法取值 → 通过必填校验并创建
    assert svc_instance.create_metric.await_count == 1


async def test_create_template_commit_integrity_error_maps_conflict() -> None:
    """create_template commit 撞唯一键 → 回滚 + ConflictError(TPL_EXISTS) 而非 500。

    背景：select 预检在并发下可能同时通过，唯一键冲突到 commit 才暴露；
    捕获 IntegrityError 转友好 ConflictError，避免裸 500（与预检分支同码）。
    """
    from sqlalchemy.exc import IntegrityError

    from app.api.semantic import create_template
    from app.core.exceptions import ConflictError

    body = {"code": "tpl_fin_gmv", "name": "GMV", "domain": "finance"}
    # 预检通过（无重复）
    r = MagicMock()
    r.scalar_one_or_none.return_value = None
    db = MagicMock()
    db.execute = AsyncMock(return_value=r)
    # commit 抛 IntegrityError（并发唯一键冲突）
    db.commit = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dup key")))
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    req = MagicMock()
    req.headers.get.return_value = ""
    user = MagicMock(id=1)

    with pytest.raises(ConflictError) as ei:
        await create_template(user=user, request=req, body=body, db=db)
    assert ei.value.error_code == "TPL_EXISTS"
    assert "已存在" in ei.value.message
    db.rollback.assert_awaited_once()


async def test_dashboard_collection_task_unavailable_on_collector_failure() -> None:
    """采集服务异常时 collection_task 显式标记 unavailable（不再伪装全零）。

    2026-08-28：此前 except 吞掉异常返回 ``{total: 0, by_status: {}}``，前端无法区分
    「真无任务」与「采集链路故障」——故障被误读为 0 个采集任务。现置 unavailable 标记。
    """
    from unittest.mock import patch

    from app.api.semantic import dashboard

    data = {
        "metrics": {"total": 1, "by_status": {}},
        "assets": {"metric": {"total": 1, "by_status": {}}},
    }
    db = MagicMock()
    req = MagicMock()
    user = MagicMock()
    user.id = 7
    user.roles_all.return_value = ["analyst"]
    with patch(
        "app.services.semantic.repository.MetricRepository.aggregate_dashboard",
        new=AsyncMock(return_value=data),
    ), patch(
        "app.services.collector.service.CollectorService.count_jobs_by_status",
        new=AsyncMock(side_effect=RuntimeError("collector down")),
    ):
        resp = await dashboard(request=req, user=user, db=db)

    ct = resp.data["assets"]["collection_task"]
    assert ct["total"] == 0
    assert ct["by_status"] == {}
    assert ct["unavailable"] is True
    assert "不可用" in ct["message"]


async def test_dashboard_collection_task_normal_stats() -> None:
    """采集服务正常时 collection_task 返回真实任务分布（无 unavailable 标记）。"""
    from unittest.mock import patch

    from app.api.semantic import dashboard

    data = {
        "metrics": {"total": 1, "by_status": {}},
        "assets": {"metric": {"total": 1, "by_status": {}}},
    }
    db = MagicMock()
    req = MagicMock()
    user = MagicMock()
    user.id = 7
    user.roles_all.return_value = ["analyst"]
    with patch(
        "app.services.semantic.repository.MetricRepository.aggregate_dashboard",
        new=AsyncMock(return_value=data),
    ), patch(
        "app.services.collector.service.CollectorService.count_jobs_by_status",
        new=AsyncMock(return_value={"RUNNING": 1, "COMPLETED": 2}),
    ):
        resp = await dashboard(request=req, user=user, db=db)

    ct = resp.data["assets"]["collection_task"]
    assert ct["total"] == 3
    assert ct["by_status"] == {"RUNNING": 1, "COMPLETED": 2}
    assert ct.get("unavailable") is None


async def test_dashboard_shows_global_overview_regardless_of_role() -> None:
    """总览页展示系统总体情况（产品语义）：任意登录用户均查看全平台聚合统计。

    - 外部传入 owner_id=99 / domain=sales 透传给聚合（作为用户主动筛选），不按角色强制改写
    - 覆盖普通用户 / platform_admin / domain_admin（绑定域）/ domain_admin（未绑定域）四类角色
    - 模块级越权保护在点击跳转时由路由守卫 + 读端点行级收敛完成，总览不承担个人收敛
    """
    from unittest.mock import patch

    from app.api.semantic import dashboard

    data = {
        "metrics": {"total": 0, "by_status": {}},
        "assets": {"metric": {"total": 0, "by_status": {}}},
    }
    db = MagicMock()
    req = MagicMock()

    captured: list[tuple] = []

    async def _fake_aggregate(self, domain=None, owner_id=None):
        captured.append((domain, owner_id))
        return data

    users = [
        ("analyst", MagicMock(id=7, role="analyst", domain=None)),
        ("platform_admin", MagicMock(id=1, role="platform_admin", domain=None)),
        ("domain_admin_bind", MagicMock(id=2, role="domain_admin", domain="outpatient")),
        ("domain_admin_none", MagicMock(id=3, role="domain_admin", domain=None)),
    ]
    for name, u in users:
        u.roles_all.return_value = [u.role]
        captured.clear()
        with patch(
            "app.services.semantic.repository.MetricRepository.aggregate_dashboard",
            new=_fake_aggregate,
        ), patch(
            "app.services.semantic.repository.MetricRepository.count_review_actionable",
            new=AsyncMock(return_value=0),
        ), patch(
            "app.services.collector.service.CollectorService.count_jobs_by_status",
            new=AsyncMock(return_value={}),
        ):
            await dashboard(request=req, user=u, db=db, owner_id=99, domain="sales")
        assert captured == [("sales", 99)], (
            f"{name} 应透传外部筛选（总览全局展示，不按角色收敛），实际 {captured}"
        )


async def test_dashboard_reviewer_gets_assigned_review() -> None:
    """评审人角色（非管理）：/dashboard 返回可审待审数（TD §13，与审批中心「待我审」同口径）。

    管理角色（domain_admin/platform_admin）同样按「可审」统计——domain_admin=指派给我/本域/
    未指派兜底；普通用户无评审能力，不附加 assigned_review（前端 ?? 0 不展示）。
    """
    from unittest.mock import patch

    from app.api.semantic import dashboard

    data = {"metrics": {"total": 0, "by_status": {}}, "assets": {}}
    db = MagicMock()
    req = MagicMock()
    captured: list[tuple] = []

    async def _fake_aggregate(self, domain=None, owner_id=None):
        captured.append((domain, owner_id))
        return dict(data)

    # reviewer 非管理：总览页全局展示（透传外部参数，默认全平台），另附可审待审数
    user = MagicMock()
    user.id = 7
    user.domain = "outpatient"
    user.role = "reviewer"
    user.roles_all.return_value = ["reviewer"]
    with patch(
        "app.services.semantic.repository.MetricRepository.aggregate_dashboard",
        new=_fake_aggregate,
    ), patch(
        "app.services.semantic.repository.MetricRepository.count_review_actionable",
        new=AsyncMock(return_value=5),
    ), patch(
        "app.services.collector.service.CollectorService.count_jobs_by_status",
        new=AsyncMock(return_value={}),
    ):
        result = await dashboard(request=req, user=user, db=db)

    # reviewer 非管理：全局统计（无外部参数时 domain/owner_id 均为 None = 全平台）
    assert captured == [(None, None)]
    assert result.data.get("assigned_review") == 5

    # 普通用户（analyst）：不附加 assigned_review
    captured.clear()
    normal = MagicMock()
    normal.id = 3
    normal.domain = "outpatient"
    normal.role = "analyst"
    normal.roles_all.return_value = ["analyst"]
    with patch(
        "app.services.semantic.repository.MetricRepository.aggregate_dashboard",
        new=_fake_aggregate,
    ), patch(
        "app.services.collector.service.CollectorService.count_jobs_by_status",
        new=AsyncMock(return_value={}),
    ):
        result2 = await dashboard(request=req, user=normal, db=db)

    assert "assigned_review" not in result2.data

    # domain_admin（管理角色）：同样附加 assigned_review（可审口径），且聚合按本域收敛
    captured.clear()
    da = MagicMock()
    da.id = 9
    da.domain = "outpatient"
    da.role = "domain_admin"
    da.roles_all.return_value = ["domain_admin"]
    with patch(
        "app.services.semantic.repository.MetricRepository.aggregate_dashboard",
        new=_fake_aggregate,
    ), patch(
        "app.services.semantic.repository.MetricRepository.count_review_actionable",
        new=AsyncMock(return_value=2),
    ), patch(
        "app.services.collector.service.CollectorService.count_jobs_by_status",
        new=AsyncMock(return_value={}),
    ):
        result3 = await dashboard(request=req, user=da, db=db, owner_id=5, domain="outpatient")

    # 总览页全局展示：domain_admin 同样透传外部筛选（不按角色收敛），并附加可审待审数
    assert captured == [("outpatient", 5)], (
        "domain_admin 应透传外部筛选（总览全局展示），实际 " + str(captured)
    )
    assert result3.data.get("assigned_review") == 2
