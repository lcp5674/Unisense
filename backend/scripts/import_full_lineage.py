"""导入 DP 平台完整血缘（SQL + 同步 + DAG 三类边并集）。

为什么需要合并：
- ``import_dp_lineage.py`` 走平台 ``extract_table_lineage``（hive 方言），完整覆盖
  SQL 边（nodeType=2）+ 同步边（nodeType=6/8/10）+ DAG 边（parentIds），但部分
  SQL（``CREATE TABLE if not exists`` + ``alter table replace``、含 Hive 变量）会被
  降级为 Command 解析，可能漏边。
- ``analyze_dp_lineage.py`` 的 SQL 解析更强（注释剥离 + Hive 变量替换 + 正则降级，
  0 解析失败），产出 ``lineage_out/lineage.json`` 的 5215 条纯 SQL 边。

因此取「平台完整解析边集 ∪ 我的 SQL 边集」的**并集**导入，既保留同步/DAG 边，
又补齐平台解析器可能漏掉的 SQL 边。同一通道（provenance=dp_csv）走
``LineageService.ingest_batch`` 增量语义：合并边全部 mark_seen，旧边不再进观察期。

用法:
    poetry run python -m scripts.import_full_lineage \\
        [--csv 路径] [--lineage-json 路径] [--dry-run] [--no-graph]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# 将 backend/ 加入 sys.path，确保能 import app
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import structlog  # noqa: E402
from scripts.import_dp_lineage import collect_edges as _dp_collect_edges  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.logging import configure_logging  # noqa: E402
from app.db.mysql import async_session_factory  # noqa: E402
from app.services.lineage.graph import LineageGraphClient  # noqa: E402
from app.services.lineage.service import LineageService  # noqa: E402

logger = structlog.get_logger("unisense.import_full_lineage")

_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CSV = _ROOT / "dp元数据.csv"
DEFAULT_LINEAGE_JSON = _ROOT / "lineage_out" / "lineage.json"

_EDGE_TYPE = "DERIVED_FROM"
_PROVENANCE = "dp_csv"
_CHANGE_REASON = "import"


def load_sql_edges(json_path: Path) -> set[tuple[str, str]]:
    """从 ``lineage_out/lineage.json`` 提取 (source, target) 纯 SQL 边（去重/跳自环）。"""
    edges: set[tuple[str, str]] = set()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    for e in data.get("edges", []):
        s = str(e.get("source") or "").strip()
        t = str(e.get("target") or "").strip()
        if s and t and s != t:
            edges.add((s, t))
    return edges


async def persist_edges(
    db: AsyncSession, edges: set[tuple[str, str]], stale_threshold: int | None
) -> dict[str, Any]:
    svc = LineageService(db)
    return await svc.ingest_batch(
        _PROVENANCE,
        edges,
        threshold=stale_threshold,
        change_reason=_CHANGE_REASON,
    )


async def persist_graph(edges: set[tuple[str, str]]) -> bool:
    from app.services.lineage.parser import node_table

    graph = LineageGraphClient()
    triples = [(node_table(source), node_table(target), _EDGE_TYPE) for source, target in edges]
    return await graph.write_edges(triples)


async def run(
    csv_path: Path,
    lineage_json: Path,
    dry_run: bool = False,
    no_graph: bool = False,
    stale_threshold: int | None = None,
) -> None:
    configure_logging()
    dp_edges, node_types, task_count = _dp_collect_edges(csv_path)
    sql_edges = load_sql_edges(lineage_json)
    merged = dp_edges | sql_edges
    only_sql = sql_edges - dp_edges
    only_dp = dp_edges - sql_edges
    logger.info(
        "full_lineage_parsed",
        tasks=task_count,
        dp_edges=len(dp_edges),
        sql_edges=len(sql_edges),
        merged_edges=len(merged),
        only_sql=len(only_sql),
        only_dp=len(only_dp),
        node_types=node_types,
    )
    if dry_run:
        logger.info("full_lineage_dry_run_done", merged_edges=len(merged))
        return
    if not merged:
        logger.warning("full_lineage_no_edges")
        return
    async with async_session_factory() as db:
        try:
            summary = await persist_edges(db, merged, stale_threshold)
            logger.info(
                "full_lineage_complete",
                run_id=summary["run_id"],
                source=summary["source"],
                total_edges=summary["total_edges"],
                added=summary["added"],
                updated=summary["updated"],
                missing=summary["missing"],
                stale_flagged=summary["stale_flagged"],
                restored=summary["restored"],
            )
        except Exception:
            await db.rollback()
            logger.exception("full_lineage_failed")
            raise
    if not no_graph:
        graph_written = await persist_graph(merged)
        logger.info("full_lineage_graph", written=graph_written)
    from app.db.mysql import engine as db_engine

    await db_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="导入 DP 平台完整血缘（SQL+同步+DAG）")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="DP 元数据 CSV 路径")
    parser.add_argument(
        "--lineage-json", type=Path, default=DEFAULT_LINEAGE_JSON, help="SQL 解析 lineage.json 路径"
    )
    parser.add_argument("--dry-run", action="store_true", help="只解析统计，不写库")
    parser.add_argument("--no-graph", action="store_true", help="不写 Neo4j 图存储")
    parser.add_argument(
        "--stale-threshold", type=int, default=None, help="失效观察期（连续未出现轮次，默认取配置）"
    )
    args = parser.parse_args()
    if not args.csv.exists():
        logger.error("csv_not_found", path=str(args.csv))
        sys.exit(1)
    if not args.lineage_json.exists():
        logger.error("lineage_json_not_found", path=str(args.lineage_json))
        sys.exit(1)
    asyncio.run(
        run(
            args.csv,
            args.lineage_json,
            dry_run=args.dry_run,
            no_graph=args.no_graph,
            stale_threshold=args.stale_threshold,
        )
    )


if __name__ == "__main__":
    main()
