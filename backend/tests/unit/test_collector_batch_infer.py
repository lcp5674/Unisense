"""跨表批量 LLM 推断任务（方案 B）单测。

覆盖：任务进度初始化、CollectorService 编排方法（字段批量/表描述，含幂等短路
与跳过语义）。无外部依赖（mock db/repo/LLM 方法）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.collector.batch_infer_tasks import _new_progress
from app.services.collector.service import BatchTaskCancelledError, CollectorService


def _svc_with_catalog(schema_json: dict, **cat_kw) -> tuple[CollectorService, MagicMock]:
    """构造服务 + mock 目录行（db.execute 返回该目录）。返回 (svc, mock_repo)。"""
    with patch("app.services.collector.service.CollectorRepository") as mock_repo:
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.flush = AsyncMock()
        cat = MagicMock()
        cat.id = 1
        cat.entity_name = "t_demo"
        cat.schema_json = schema_json
        for k, v in cat_kw.items():
            setattr(cat, k, v)
        result = MagicMock()
        result.scalar_one_or_none.return_value = cat
        db.execute = AsyncMock(return_value=result)
        svc = CollectorService(db=db)
        repo = mock_repo.return_value
        # 仓库方法默认 AsyncMock（测试内可覆盖返回值）
        repo.get_descriptions = AsyncMock(return_value=[])
        repo.upsert_description = AsyncMock(return_value=MagicMock())
        repo.update_table_description = AsyncMock(return_value=cat)
        return svc, repo


def _cols(*specs: tuple[str, str | None, str | None]) -> list[dict]:
    """按 (name, type, comment) 构造 schema columns。"""
    return [
        {"name": n, "type": t, "comment": c}
        for n, t, c in specs
        if n is not None
    ]


# ---- _new_progress ----


def test_new_progress_init_pending_in_order():
    tasks = [
        {"catalog_id": 1, "entity_name": "a", "missing_fields": 3, "needs_table_desc": True},
        {"catalog_id": 2, "entity_name": "b", "missing_fields": 0, "needs_table_desc": True},
    ]
    prog = _new_progress(tasks)
    assert [p["status"] for p in prog] == ["pending", "pending"]
    assert prog[0]["catalog_id"] == 1
    assert prog[0]["missing_fields"] == 3
    assert prog[0]["needs_table_desc"] is True
    assert prog[1]["missing_fields"] == 0
    assert prog[1]["needs_table_desc"] is True
    assert prog[0]["added"] == 0 and prog[0]["skipped"] == 0


def test_new_progress_empty_tasks():
    assert _new_progress([]) == []


# ---- infer_catalog_columns 编排 ----


@pytest.mark.asyncio
async def test_infer_catalog_columns_normal_upsert():
    """字段批量：跳过有 manual/llm 描述与有效 comment 的列，仅推断空 comment 列。"""
    schema = {
        "columns": _cols(
            ("col_a", "bigint", None),       # 待推断
            ("col_b", "string", "已有注释"),  # 有效 comment → skipped
            ("col_c", "string", "from deserializer"),  # 占位注释 → 待推断
        )
    }
    svc, repo = _svc_with_catalog(schema)
    repo.get_descriptions = AsyncMock(return_value=[])
    # col_c 也推断成功
    svc._llm_infer_batch_descriptions = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "col_a": ("字段A描述", 0.9),
            "col_c": ("字段C描述", 0.85),
        }
    )
    res = await svc.infer_catalog_columns(1)
    assert res["error"] is None
    assert [i["column_name"] for i in res["inferred"]] == ["col_a", "col_c"]
    assert res["skipped"] == ["col_b"]
    assert res["failed"] == []
    # upsert 两次（col_a/col_c），source=llm
    assert repo.upsert_description.await_count == 2


@pytest.mark.asyncio
async def test_infer_catalog_columns_skips_existing_llm():
    """已有 llm 描述的列不再重复推断。"""
    existing = MagicMock()
    existing.column_name = "col_a"
    existing.source = "llm"
    schema = {"columns": _cols(("col_a", "bigint", None), ("col_b", "string", None))}
    svc, repo = _svc_with_catalog(schema)
    repo.get_descriptions = AsyncMock(return_value=[existing])
    svc._llm_infer_batch_descriptions = AsyncMock(  # type: ignore[method-assign]
        return_value={"col_b": ("字段B描述", 0.8)}
    )
    res = await svc.infer_catalog_columns(1)
    assert res["skipped"] == ["col_a"]
    assert [i["column_name"] for i in res["inferred"]] == ["col_b"]


@pytest.mark.asyncio
async def test_infer_catalog_columns_llm_failed_fields():
    """LLM 未返回某列 → 该列记 failed（不阻断其余列）。"""
    schema = {"columns": _cols(("col_a", "bigint", None), ("col_b", "string", None))}
    svc, repo = _svc_with_catalog(schema)
    repo.get_descriptions = AsyncMock(return_value=[])
    svc._llm_infer_batch_descriptions = AsyncMock(  # type: ignore[method-assign]
        return_value={"col_a": ("字段A描述", 0.9)}
    )
    res = await svc.infer_catalog_columns(1)
    assert [i["column_name"] for i in res["inferred"]] == ["col_a"]
    assert res["failed"] == ["col_b"]


# ---- infer_catalog_table_description 编排 ----


@pytest.mark.asyncio
async def test_infer_table_description_generates():
    """无 LLM 描述 → 调 LLM 生成并落库。"""
    svc, repo = _svc_with_catalog(
        {"columns": _cols(("col_a", "bigint", None))},
        description_source=None,
        description=None,
    )
    svc._llm_infer_table_description = AsyncMock(  # type: ignore[method-assign]
        return_value={"description": "演示业务表", "confidence": 0.9}
    )
    res = await svc.infer_catalog_table_description(1)
    assert res is not None
    assert res["description"] == "演示业务表"
    repo.update_table_description.assert_awaited_once_with(
        catalog_id=1, description="演示业务表", source="llm"
    )


@pytest.mark.asyncio
async def test_infer_table_description_idempotent_shortcut():
    """已有 LLM 描述且未 force → 幂等短路，不重复调 LLM。"""
    svc, repo = _svc_with_catalog(
        {"columns": []},
        description_source="llm",
        description="已有表描述",
    )
    svc._llm_infer_table_description = AsyncMock()  # type: ignore[method-assign]
    res = await svc.infer_catalog_table_description(1)
    assert res == {"description": "已有表描述", "source": "llm", "confidence": 1.0}
    svc._llm_infer_table_description.assert_not_awaited()
    repo.update_table_description.assert_not_awaited()


@pytest.mark.asyncio
async def test_infer_table_description_llm_unavailable_returns_none():
    """LLM 不可用 → 返回 None（调用方按失败处理）。"""
    svc, repo = _svc_with_catalog(
        {"columns": []},
        description_source=None,
        description=None,
    )
    svc._llm_infer_table_description = AsyncMock(return_value=None)  # type: ignore[method-assign]
    res = await svc.infer_catalog_table_description(1)
    assert res is None
    repo.update_table_description.assert_not_awaited()


# ---- chunk 级协作取消 ----


@pytest.mark.asyncio
async def test_infer_catalog_columns_cancel_aborts_before_chunk():
    """cancel_checker 返回 False → 抛 BatchTaskCancelledError，且不发起任何 LLM 调用。"""
    schema = {"columns": _cols(("col_a", "bigint", None), ("col_b", "string", None))}
    svc, repo = _svc_with_catalog(schema)
    repo.get_descriptions = AsyncMock(return_value=[])
    svc._llm_infer_batch_descriptions = AsyncMock(  # type: ignore[method-assign]
        return_value={"col_a": ("A", 0.9)}
    )
    checker = AsyncMock(return_value=False)
    with pytest.raises(BatchTaskCancelledError):
        await svc.infer_catalog_columns(1, cancel_checker=checker)
    # 首个块前探测即中止 → LLM 未被调用、无 upsert
    svc._llm_infer_batch_descriptions.assert_not_awaited()
    repo.upsert_description.assert_not_awaited()


@pytest.mark.asyncio
async def test_infer_catalog_columns_cancel_checker_called_per_chunk():
    """cancel_checker 每块调用一次（chunk 粒度探测），放行时正常执行。"""
    schema = {
        "columns": _cols(
            ("c1", "bigint", None),
            ("c2", "string", None),
            ("c3", "string", None),
            ("c4", "string", None),
            ("c5", "string", None),
        )
    }
    svc, repo = _svc_with_catalog(schema)
    repo.get_descriptions = AsyncMock(return_value=[])
    svc._llm_infer_batch_descriptions = AsyncMock(  # type: ignore[method-assign]
        return_value={f"c{i}": (f"描述{i}", 0.9) for i in range(1, 6)}
    )
    checker = AsyncMock(return_value=True)
    res = await svc.infer_catalog_columns(1, batch_chunk=2, cancel_checker=checker)
    assert res["error"] is None
    assert len(res["inferred"]) == 5
    # 5 字段 / 2 每块 = 3 块 → checker 调 3 次
    assert checker.await_count == 3
    assert repo.upsert_description.await_count == 5


@pytest.mark.asyncio
async def test_infer_catalog_columns_cancel_midway():
    """第 2 块前探测到取消 → 抛 BatchTaskCancelledError（第 1 块已完成后中止）。"""
    schema = {
        "columns": _cols(
            ("c1", "bigint", None),
            ("c2", "string", None),
            ("c3", "string", None),
            ("c4", "string", None),
        )
    }
    svc, repo = _svc_with_catalog(schema)
    repo.get_descriptions = AsyncMock(return_value=[])
    svc._llm_infer_batch_descriptions = AsyncMock(  # type: ignore[method-assign]
        return_value={f"c{i}": (f"描述{i}", 0.9) for i in range(1, 5)}
    )
    # 第一次（第 1 块前）放行，之后（第 2 块前）取消
    checker = AsyncMock(side_effect=[True, False])
    with pytest.raises(BatchTaskCancelledError):
        await svc.infer_catalog_columns(1, batch_chunk=2, cancel_checker=checker)
    # 第 1 块已调 LLM、第 2 块未调
    assert svc._llm_infer_batch_descriptions.await_count == 1
