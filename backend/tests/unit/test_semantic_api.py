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
    resp = await update_template_owner(
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
