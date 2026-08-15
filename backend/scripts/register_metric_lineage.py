"""为存量指标注册 L3 指标级血缘边（metric:{code} ↔ table:{t}）。

背景：``app/api/metrics.py`` 的 create/update_metric 已接线
``LineageService.register_metric_from_definition``（新增指标自动注册 L3 边），
本脚本用于**迁移存量**：遍历 ``metric`` 表全部指标，按 ``definition_json`` 的
``source_table``（落地/物化表 → metric→table 下游边）与 ``source_tables``
（上游源表 → table→metric 上游边）注册，provenance=metric_definition，幂等。

与 DP 血缘（dp_csv）/ SQL 解析（sqlglot）表级血缘衔接，形成
「源表 → 指标 → 落地表」完整链路，使指标节点出现在血缘视图/影响分析中。

用法:
    poetry run python -m scripts.register_metric_lineage [--dry-run] [--code sales_e2e_gmv_day]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 将 backend/ 加入 sys.path，确保能 import app
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import structlog  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.logging import configure_logging  # noqa: E402
from app.db.mysql import async_session_factory  # noqa: E402
from app.db.mysql import engine as db_engine  # noqa: E402
from app.models.metric import Metric  # noqa: E402
from app.services.lineage.service import LineageService  # noqa: E402

logger = structlog.get_logger("unisense.register_metric_lineage")


async def run(dry_run: bool = False, code: str | None = None) -> None:
    configure_logging()
    async with async_session_factory() as db:
        stmt = select(Metric).where(Metric.deleted_at.is_(None))
        if code:
            stmt = stmt.where(Metric.metric_code == code)
        metrics = list((await db.execute(stmt)).scalars().all())
        logger.info("metric_lineage_scan", total=len(metrics))
        svc = LineageService(db)
        total_edges = 0
        for m in metrics:
            edges = await svc.register_metric_from_definition(m, commit=False)
            total_edges += len(edges)
            logger.info(
                "metric_lineage_registered",
                metric_code=m.metric_code,
                status=m.status,
                edges=len(edges),
            )
        if dry_run:
            logger.info("metric_lineage_dry_run", total_edges=total_edges)
            await db.rollback()
            return
        await db.commit()
        logger.info("metric_lineage_complete", total_metrics=len(metrics), total_edges=total_edges)
    await db_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="为存量指标注册 L3 指标级血缘边")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument("--code", type=str, default=None, help="只处理指定指标编码")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run, code=args.code))


if __name__ == "__main__":
    main()
