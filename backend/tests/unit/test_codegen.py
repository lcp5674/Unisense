"""通用编码生成工具 + 各模块编码辅助函数测试（FR-010）。

覆盖：
- codegen.slugify_code / generate_unique_code（核心工具）
- consume._generate_client_id（API 客户端 ID 自动生成）
- semantic._generate_template_code（指标模板 code 自动生成）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.codegen import MAX_CODE_LEN, generate_unique_code, slugify_code
from app.services.semantic.schemas import MetricTemplateCreateRequest


def test_slugify_code_ascii() -> None:
    assert slugify_code("Marketing Insights") == "marketing_insights"
    assert slugify_code("GMV!@#  Trend") == "gmv_trend"
    assert slugify_code("  spaced  ") == "spaced"


def test_slugify_code_chinese_to_english() -> None:
    assert slugify_code("供应链") == "supply_chain"
    assert slugify_code("销售订单GMV") == "sales_order_gmv"


def test_slugify_code_untranslatable_returns_empty() -> None:
    assert slugify_code("") == ""
    assert slugify_code("!@# ") == ""


async def test_generate_unique_code_no_conflict() -> None:
    async def exists(_: str) -> bool:
        return False

    assert await generate_unique_code("sales_order", exists) == "sales_order"


async def test_generate_unique_code_conflict_suffix() -> None:
    taken = {"sales_order", "sales_order_2"}

    async def exists(code: str) -> bool:
        return code in taken

    assert await generate_unique_code("sales_order", exists) == "sales_order_3"


async def test_generate_unique_code_truncates() -> None:
    base = "x" * 200
    async def exists(_: str) -> bool:
        return False

    out = await generate_unique_code(base, exists)
    assert len(out) == MAX_CODE_LEN


async def test_generate_unique_code_exhausts() -> None:
    async def exists(_: str) -> bool:
        return True

    with pytest.raises(RuntimeError):
        await generate_unique_code("sales", exists, max_attempts=3)


async def test_generate_client_id_prefix() -> None:
    from app.api.consume import _generate_client_id

    repo = MagicMock()
    repo.get_by_client_id = AsyncMock(return_value=None)
    cid = await _generate_client_id(repo)
    assert cid.startswith("app_")
    assert len(cid) >= 5
    repo.get_by_client_id.assert_awaited_once()


async def test_generate_client_id_retries_on_conflict() -> None:
    from app.api.consume import _generate_client_id

    repo = MagicMock()
    # 第一次冲突，第二次成功
    repo.get_by_client_id = AsyncMock(side_effect=[MagicMock(), None])
    cid = await _generate_client_id(repo)
    assert cid.startswith("app_")
    assert repo.get_by_client_id.await_count == 2


async def test_generate_template_code() -> None:
    from app.api.semantic import _generate_template_code

    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=result)

    validated = MetricTemplateCreateRequest(code=None, name="GMV Trend", domain="sales")
    code = await _generate_template_code(db, validated)
    assert code.startswith("tpl_sales_gmv_trend")


async def test_generate_template_code_conflict_suffix() -> None:
    from app.api.semantic import _generate_template_code

    db = MagicMock()
    taken = {"tpl_sales_gmv_trend"}

    async def fake_execute(stmt):
        res = MagicMock()
        # 从编译后绑定参数中提取 code 做存在性判定
        params = stmt.compile().params if hasattr(stmt, "compile") else {}
        code = params.get("code_1") or params.get("code")
        if code in taken:
            res.scalar_one_or_none = MagicMock(return_value=MagicMock())
        else:
            res.scalar_one_or_none = MagicMock(return_value=None)
        return res

    db.execute = fake_execute
    validated = MetricTemplateCreateRequest(code=None, name="GMV Trend", domain="sales")
    code = await _generate_template_code(db, validated)
    assert code == "tpl_sales_gmv_trend_2"
