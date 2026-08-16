"""回填存量指标↔维度 / 指标↔字段血缘边（TD §12.2 / FR-18）。

背景：
- 指标级血缘图谱此前仅覆盖「指标↔表」（L3 DERIVED_FROM）；本次枚举扩展新增
  ``USES_DIMENSION``（指标↔维度）与 ``READS_COLUMN``（指标↔字段）两类关系，
  但**存量指标**（创建于枚举扩展前）尚未生成这两类边。
- 本脚本对存量 ``metric`` 做一次性回填：
  1. 维度边：从 ``metric_dimension`` 绑定表（metric_id → dim_code）批量注册
     ``metric:{code}`` → ``dimension:{dim_code}``（USES_DIMENSION，L3）；
  2. 字段边：从 ``metric.definition_json`` 解析 ``source_table`` +
     ``measure_column`` / ``measures[].name``，注册
     ``column:{tbl}.{col}`` → ``metric:{code}``（READS_COLUMN，L3）。

幂等：复用 ``_upsert`` 唯一键（source/target/edge_type/granularity），重复执行
不产生重复边，可安全地纳入定时任务或 CI 流水线。

用法:
    poetry run python -m scripts.backfill_metric_lineage [--dry-run] [--limit N]

参数:
    --dry-run: 只统计将写入的边数，不写库
    --limit: 仅处理前 N 个指标（调试用，默认全部）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

# 将 backend/ 加入 sys.path，确保能 import app
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import structlog  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db.mysql import async_session_factory  # noqa: E402
from app.models.dimension import MetricDimension  # noqa: E402
from app.models.metric import Metric  # noqa: E402
from app.services.lineage.service import LineageService  # noqa: E402

logger = structlog.get_logger("unisense.backfill_metric_lineage")


async def _collect_dim_codes(session: Any) -> dict[int, list[str]]:
    """按 metric_id 聚合 metric_dimension 的 dim_code 列表。"""
    rows = list(
        (
            await session.execute(
                select(MetricDimension.metric_id, MetricDimension.dim_code)
            )
        ).all()
    )
    grouped: dict[int, list[str]] = {}
    for metric_id, dim_code in rows:
        grouped.setdefault(metric_id, []).append(dim_code)
    return grouped


def _collect_columns(definition: Any) -> list[tuple[str, str]]:
    """从 definition_json 提取 (table, column) 字段边候选。"""
    if not isinstance(definition, dict):
        return []
    table = definition.get("source_table")
    if not isinstance(table, str) or not table:
        return []
    out: list[tuple[str, str]] = []
    measure_column = definition.get("measure_column")
    if isinstance(measure_column, str) and measure_column:
        out.append((table, measure_column))
    for m in definition.get("measures") or []:
        col = m.get("name") or m.get("column") if isinstance(m, dict) else m
        if isinstance(col, str) and col:
            out.append((table, col))
    return out


async def run(dry_run: bool, limit: int | None) -> None:
    async with async_session_factory() as session:
        svc = LineageService(db=session)
        stmt = select(Metric).where(Metric.deleted_at.is_(None)).order_by(Metric.id)
        if limit:
            stmt = stmt.limit(limit)
        metrics = list((await session.execute(stmt)).scalars().all())
        dim_map = await _collect_dim_codes(session)

        planned_dim = 0
        planned_col = 0
        written_dim = 0
        written_col = 0
        skipped = 0
        for metric in metrics:
            code = metric.metric_code
            # 1. 维度边
            dim_codes = dim_map.get(metric.id, [])
            if dim_codes:
                planned_dim += len(dim_codes)
                if not dry_run:
                    edges = await svc.register_metric_dimension_edges(
                        code, dim_codes, commit=False
                    )
                    written_dim += len(edges)
            # 2. 字段边
            for table, col in _collect_columns(metric.definition_json):
                planned_col += 1
                if not dry_run:
                    edge = await svc.register_metric_column_edge(
                        code, table, col, commit=False
                    )
                    if edge is not None:
                        written_col += 1
            # 跳过计数：既无维度绑定也无字段候选
            if not dim_codes and not _collect_columns(metric.definition_json):
                skipped += 1

        if not dry_run:
            await session.commit()

        logger.info(
            "backfill_metric_lineage_done",
            metrics=len(metrics),
            planned_dim=planned_dim,
            planned_col=planned_col,
            written_dim=written_dim if not dry_run else 0,
            written_col=written_col if not dry_run else 0,
            skipped=skipped,
            dry_run=dry_run,
        )

    # 显式释放连接池，避免脚本退出时 Event loop is closed 告警
    from app.db.mysql import engine as db_engine

    await db_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="回填存量指标↔维度/字段血缘边")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 个指标")
    args = parser.parse_args()
    asyncio.run(run(args.dry_run, args.limit))


if __name__ == "__main__":
    main()
