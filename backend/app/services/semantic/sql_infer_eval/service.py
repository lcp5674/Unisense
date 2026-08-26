"""SQL 智能推断评测运行记录服务（持久化 + 趋势查询）。

评测本身是确定性纯函数（``runner.run_eval``），每次运行结果一致；历史记录
用于前端可视化「成功率趋势」（何时解析器改动导致成功率波动），每次
「运行评测」落一行。

自定义样本（``SqlInferEvalSample``）由业务用户通过评测页 CRUD 管理，运行时与
内置基线 ``GOLDEN`` 合并（``merged_cases``）——「解析成功率」可随样本持续
扩充而度量；缺陷样本可入库追踪待修缺口（允许失败、不阻断 CI 门禁）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select

from app.models.sql_infer_eval import SqlInferEvalRun
from app.models.sql_infer_eval_sample import SqlInferEvalSample
from app.services.semantic.sql_infer import parse_sql_profile
from app.services.semantic.sql_infer_eval.dataset import GOLDEN, ExpectedMeasure, SqlInferCase
from app.services.semantic.sql_infer_eval.runner import report_to_dict, run_eval

logger = logging.getLogger(__name__)

#: 合法聚合枚举（与指标模型 agg_type 对齐；校验样本期望用，防非法期望入库）。
_AGG_ENUM = frozenset(
    {
        "SUM",
        "AVG",
        "COUNT",
        "COUNT_DISTINCT",
        "LAST_VALUE",
        "FIRST_VALUE",
        "MAX",
        "MIN",
        "MEDIAN",
        "PERCENTILE",
    }
)
#: 合法周期枚举（与 normalize_period 对齐）。
_PERIOD_ENUM = frozenset({"hour", "day", "week", "month", "quarter", "year"})


def _to_summary(run: SqlInferEvalRun) -> dict[str, Any]:
    """运行记录 → 摘要 dict（前端趋势图/历史表用）。"""
    return {
        "id": run.id,
        "ran_at": run.ran_at.isoformat() if run.ran_at else None,
        "total": run.total,
        "exact_count": run.exact_count,
        "exact_rate": run.exact_rate,
        "measure_precision": run.measure_precision,
        "measure_recall": run.measure_recall,
        "table_precision": run.table_precision,
        "table_recall": run.table_recall,
        "period_match_rate": run.period_match_rate,
        "elapsed_ms": run.elapsed_ms,
        "actor_id": run.actor_id,
    }


async def run_and_record(
    db: Any, actor_id: int | None = None
) -> dict[str, Any]:
    """运行评测集（内置 + 自定义样本合并）并落一条历史记录。

    Returns:
        ``{"report": {...}, "run_id": int}``——report 含逐用例明细与聚合指标。
    """
    started = time.monotonic()
    cases = await merged_cases(db)
    report = run_eval(samples=cases)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    report_dict = report_to_dict(report)

    run = SqlInferEvalRun(
        total=report.total,
        exact_count=report.exact_count,
        exact_rate=report.exact_rate,
        measure_precision=report.measure_precision,
        measure_recall=report.measure_recall,
        table_precision=report.table_precision,
        table_recall=report.table_recall,
        period_match_rate=report.period_match_rate,
        cases_json=report_dict.get("cases", []),
        elapsed_ms=elapsed_ms,
        actor_id=actor_id,
        ran_at=datetime.now(UTC),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return {"report": report_dict, "run_id": run.id}


async def list_runs(db: Any, limit: int = 20) -> list[dict[str, Any]]:
    """最近 N 次评测运行摘要（时间倒序）。"""
    stmt = (
        select(SqlInferEvalRun)
        .where(SqlInferEvalRun.deleted_at.is_(None))
        .order_by(desc(SqlInferEvalRun.ran_at))
        .limit(max(1, min(limit, 100)))
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_summary(r) for r in rows]


async def latest_run_cases(db: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """最新一次运行的完整报告（含逐用例明细），无则返回 ``(None, [])``。"""
    stmt = (
        select(SqlInferEvalRun)
        .where(SqlInferEvalRun.deleted_at.is_(None))
        .order_by(desc(SqlInferEvalRun.ran_at))
        .limit(1)
    )
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        return None, []
    return _to_summary(row), list(row.cases_json or [])


# ----------------------------------------------------------------
# 自定义样本 CRUD（内置 GOLDEN 只读；自定义落库可管理）
# ----------------------------------------------------------------


def _row_to_case(row: SqlInferEvalSample) -> SqlInferCase:
    """DB 样本行 → 评测用例（期望度量 JSON → 结构对象）。"""
    measures = tuple(
        ExpectedMeasure(
            column=str(m.get("column") or ""),
            agg=str(m["agg"]) if m.get("agg") else None,
            alias=str(m["alias"]) if m.get("alias") else None,
            table=str(m["table"]) if m.get("table") else None,
        )
        for m in (row.expected_measures or [])
    )
    return SqlInferCase(
        case_id=row.case_id,
        dialect=row.dialect,
        sql=row.sql,
        expected_measures=measures,
        expected_tables=tuple(row.expected_tables or []),
        expected_period=row.expected_period,
        note=row.note,
    )


def _sample_to_dict(row: SqlInferEvalSample) -> dict[str, Any]:
    """DB 样本行 → 前端可读 dict（含结构化期望度量，弹窗回填用）。"""
    return {
        "id": row.id,
        "case_id": row.case_id,
        "dialect": row.dialect,
        "sql": row.sql,
        "expected_measures": [
            {
                "column": m.get("column") or "",
                "agg": m.get("agg"),
                "alias": m.get("alias"),
                "table": m.get("table"),
            }
            for m in (row.expected_measures or [])
        ],
        "expected_tables": list(row.expected_tables or []),
        "expected_period": row.expected_period,
        "note": row.note,
        "enabled": row.enabled,
        "is_builtin": row.is_builtin,
        "created_by": row.created_by,
    }


async def merged_cases(db: Any) -> list[SqlInferCase]:
    """内置基线 + DB 自定义启用样本合并（case_id 去重：内置优先，自定义冲突跳过）。

    自定义样本与内置同名（case_id 冲突）时以内置为准并跳过该自定义——内置是
    pytest 门禁依赖的不可变基线，不因自定义重复而破坏确定性。
    """
    stmt = select(SqlInferEvalSample).where(
        SqlInferEvalSample.deleted_at.is_(None),
        SqlInferEvalSample.enabled.is_(True),
    )
    rows = (await db.execute(stmt)).scalars().all()
    builtin_ids = {c.case_id for c in GOLDEN}
    custom = [
        _row_to_case(r)
        for r in rows
        if r.case_id not in builtin_ids and (r.sql or "").strip()
    ]
    return [*GOLDEN, *custom]


async def list_samples(db: Any, include_disabled: bool = True) -> list[dict[str, Any]]:
    """自定义样本清单（含停用；内置基线只读展示合并视图见 dataset）。"""
    stmt = select(SqlInferEvalSample).where(SqlInferEvalSample.deleted_at.is_(None))
    if not include_disabled:
        stmt = stmt.where(SqlInferEvalSample.enabled.is_(True))
    stmt = stmt.order_by(desc(SqlInferEvalSample.created_at))
    rows = (await db.execute(stmt)).scalars().all()
    return [_sample_to_dict(r) for r in rows]


def _validate_sample_payload(
    sql: str,
    expected_period: str,
    expected_measures: Sequence[dict[str, Any]] | None,
    expected_tables: Sequence[str] | None,
) -> None:
    """样本期望合法性校验（防非法期望入库导致评测失真/报错）。"""
    if not (sql or "").strip():
        raise ValueError("样本 SQL 不能为空")
    if expected_period not in _PERIOD_ENUM:
        raise ValueError(f"期望周期必须为 {sorted(_PERIOD_ENUM)} 之一")
    for m in expected_measures or []:
        agg = m.get("agg")
        if agg is not None and str(agg) not in _AGG_ENUM:
            raise ValueError(f"期望聚合 {agg!r} 不在合法枚举 {sorted(_AGG_ENUM)} 中")
        if not str(m.get("column") or "").strip():
            raise ValueError("期望度量的列名不能为空")


async def create_sample(
    db: Any,
    case_id: str,
    dialect: str,
    sql: str,
    expected_period: str,
    expected_measures: list[dict[str, Any]] | None,
    expected_tables: list[str] | None,
    note: str,
    actor_id: int | None = None,
) -> dict[str, Any]:
    """创建自定义样本（case_id 唯一 + 期望合法性校验）。

    ``expected_measures`` 为空表示纯期望（仅表/周期）样本，允许。
    """
    _validate_sample_payload(sql, expected_period, expected_measures, expected_tables)
    cid = (case_id or "").strip()
    if not cid:
        raise ValueError("样本编码不能为空")
    if cid in {c.case_id for c in GOLDEN}:
        raise ValueError(f"样本编码 {cid!r} 与内置基线冲突，请换一个编码")
    exists = await db.execute(
        select(SqlInferEvalSample.id).where(
            SqlInferEvalSample.deleted_at.is_(None),
            SqlInferEvalSample.case_id == cid,
        )
    )
    if exists.scalar_one_or_none() is not None:
        raise ValueError(f"样本编码 {cid!r} 已存在")
    row = SqlInferEvalSample(
        case_id=cid,
        dialect=(dialect or "hive").strip(),
        sql=sql,
        expected_measures=list(expected_measures or []),
        expected_tables=list(expected_tables or []),
        expected_period=expected_period,
        note=(note or "").strip(),
        enabled=True,
        is_builtin=False,
        created_by=actor_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _sample_to_dict(row)


async def get_sample(db: Any, sample_id: int) -> SqlInferEvalSample:
    """按 id 取未删除样本（不存在/已删抛 ValueError）。"""
    row = await db.execute(
        select(SqlInferEvalSample).where(
            SqlInferEvalSample.deleted_at.is_(None),
            SqlInferEvalSample.id == sample_id,
        )
    )
    found = row.scalar_one_or_none()
    if found is None:
        raise ValueError(f"评测样本不存在或已删除: id={sample_id}")
    return found


async def update_sample(
    db: Any,
    sample_id: int,
    *,
    case_id: str | None = None,
    dialect: str | None = None,
    sql: str | None = None,
    expected_period: str | None = None,
    expected_measures: list[dict[str, Any]] | None = None,
    expected_tables: list[str] | None = None,
    note: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """更新自定义样本（内置样本拒绝；仅提交的字段变更）。"""
    row = await get_sample(db, sample_id)
    if row.is_builtin:
        raise ValueError("内置基线样本只读，不可修改")
    new_sql = sql if sql is not None else row.sql
    new_period = expected_period if expected_period is not None else row.expected_period
    _validate_sample_payload(
        new_sql,
        new_period,
        expected_measures if expected_measures is not None else row.expected_measures,
        expected_tables if expected_tables is not None else row.expected_tables,
    )
    if case_id is not None and case_id != row.case_id:
        cid = (case_id or "").strip()
        if not cid:
            raise ValueError("样本编码不能为空")
        if cid in {c.case_id for c in GOLDEN}:
            raise ValueError(f"样本编码 {cid!r} 与内置基线冲突")
        dup = await db.execute(
            select(SqlInferEvalSample.id).where(
                SqlInferEvalSample.deleted_at.is_(None),
                SqlInferEvalSample.case_id == cid,
                SqlInferEvalSample.id != sample_id,
            )
        )
        if dup.scalar_one_or_none() is not None:
            raise ValueError(f"样本编码 {cid!r} 已存在")
        row.case_id = cid
    if dialect is not None:
        row.dialect = dialect.strip()
    if sql is not None:
        row.sql = sql
    if expected_measures is not None:
        row.expected_measures = list(expected_measures)
    if expected_tables is not None:
        row.expected_tables = list(expected_tables)
    if expected_period is not None:
        row.expected_period = expected_period
    if note is not None:
        row.note = note.strip()
    if enabled is not None:
        row.enabled = enabled
    await db.commit()
    await db.refresh(row)
    return _sample_to_dict(row)


async def delete_sample(db: Any, sample_id: int) -> None:
    """软删自定义样本（内置拒绝；软删可恢复）。"""
    row = await get_sample(db, sample_id)
    if row.is_builtin:
        raise ValueError("内置基线样本只读，不可删除")
    row.deleted_at = datetime.now(UTC)
    await db.commit()


def preview_sample(sql: str) -> dict[str, Any]:
    """即时解析预览（不落库）：规则解析该 SQL 的实际画像，供用户对照期望确认。

    纯函数（同步）：``parse_sql_profile`` 是确定性规则解析，无需 DB/await。
    """
    profile = parse_sql_profile(sql)
    return {
        "measures": [
            {
                "column": m.get("column"),
                "agg": m.get("agg"),
                "alias": m.get("alias"),
                "table": m.get("table"),
            }
            for m in profile.measures
        ],
        "source_tables": list(profile.source_tables),
        "period": _period_from_profile(profile),
    }


def _period_from_profile(profile: Any) -> str | None:
    """解析画像 → 周期（复用 sql_split 的归一逻辑，避免重复实现）。"""
    from app.services.semantic.sql_split import _period_from_profile as _norm

    return _norm(profile)
