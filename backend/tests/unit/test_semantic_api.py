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
    from fastapi import HTTPException

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

    # 5) owner_id 非法（0）→ HTTPException 422
    r5 = MagicMock()
    r5.scalar_one_or_none.return_value = _make_template()
    db.execute = AsyncMock(side_effect=[r5])
    with pytest.raises(HTTPException) as exc:
        await update_template_owner(
            user=user, template_id=1, request=req, body={"owner_id": 0}, db=db
        )
    assert exc.value.status_code == 422


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
    template.defaults_json = {"granularity": "day", "definition_json": {"expression": "sum(x)"}}
    template.required_fields = ["granularity"]
    for f, v in (
        ("type", "atomic"), ("unit", "元"), ("aggregation", "SUM"),
        ("time_semantics", "PERIOD"), ("freshness", "T1"), ("dw_layer", "DWS"),
        ("serving_mode", "BATCH_ONLY"), ("additivity", "ADDITIVE"), ("metric_tier", "T3"),
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
    from fastapi import HTTPException

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

    # 4) is_active 非布尔 → HTTPException 422
    r4 = MagicMock()
    r4.scalar_one_or_none.return_value = _make_template()
    db.execute = AsyncMock(side_effect=[r4])
    with pytest.raises(HTTPException) as exc:
        await update_template_active(
            user=user, template_id=1, request=req, body={"is_active": "yes"}, db=db
        )
    assert exc.value.status_code == 422


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
    }
    template.required_fields = []
    for f, v in (
        ("type", "atomic"), ("unit", "元"), ("aggregation", "SUM"),
        ("time_semantics", "PERIOD"), ("freshness", "T1"), ("dw_layer", "DWS"),
        ("serving_mode", "BATCH_ONLY"), ("additivity", "ADDITIVE"), ("metric_tier", "T3"),
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
