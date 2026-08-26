"""SQL 智能推断评测运行记录服务（持久化 + 趋势查询）。

评测本身是确定性纯函数（``runner.run_eval``），每次运行结果一致；历史记录
用于前端可视化「成功率趋势」（何时解析器改动导致成功率波动），每次
「运行评测」落一行。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC
from typing import Any

from sqlalchemy import desc, select

from app.models.sql_infer_eval import SqlInferEvalRun
from app.services.semantic.sql_infer_eval.runner import report_to_dict, run_eval

logger = logging.getLogger(__name__)


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
    """运行评测集并落一条历史记录。

    Returns:
        ``{"report": {...}, "run_id": int}``——report 含逐用例明细与聚合指标。
    """
    started = time.monotonic()
    report = run_eval()
    elapsed_ms = int((time.monotonic() - started) * 1000)
    report_dict = report_to_dict(report)
    from datetime import datetime

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
