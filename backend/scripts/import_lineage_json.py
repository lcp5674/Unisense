"""从 lineage_out/lineage.json 导入血缘到平台（provenance=dp_csv）。

读取解析器产物 ``lineage_out/lineage.json``（含 nodes/edges，边为 ``库.表`` 格式），
提取 ``(source, target)`` 边集，走与 ``import_dp_lineage`` 相同的增量采集语义
（``LineageService.ingest_batch``：幂等 upsert + 已见刷新 + 失效观察 + 运行摘要），
使平台血缘视图/资产地图直接展示这套 SQL 解析血缘。

血缘来源（provenance=dp_csv，与原始 CSV 导入同通道）：
- 用同一通道导入，``ingest_batch`` 会：新增本次解析新出现的边、更新重叠边、
  对旧解析中未再出现的边累加 ``missing_count``（观察期内不删除，达到阈值
  ``--stale-threshold`` 才进入失效队列，由「血缘视图 → 采集通道」人工处置）。

用法:
    poetry run python -m scripts.import_lineage_json \\
        [--json 路径] [--dry-run] [--no-graph] [--stale-threshold N]

参数:
    --json:  lineage.json 路径（默认仓库根 ``lineage_out/lineage.json``）
    --dry-run: 只解析统计，不写库
    --no-graph: 不写 Neo4j 图存储（默认同时写 MySQL + Neo4j，best-effort 可降级）
    --stale-threshold: 失效观察期（连续未出现轮次，默认取配置 lineage_stale_observation_runs）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 将 backend/ 加入 sys.path，确保能 import app 与 scripts
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import structlog  # noqa: E402

from app.core.logging import configure_logging  # noqa: E402
from app.db.mysql import async_session_factory  # noqa: E402
# 复用现有 dp_csv 通道的持久化逻辑（ingest_batch 增量语义 + Neo4j best-effort）
from scripts.import_dp_lineage import _PROVENANCE, persist_edges, persist_graph  # noqa: E402

logger = structlog.get_logger("unisense.import_lineage_json")

# 默认 lineage.json 路径：仓库根 lineage_out/ 目录下
DEFAULT_JSON = Path(__file__).resolve().parent.parent.parent / "lineage_out" / "lineage.json"


def collect_edges(json_path: Path) -> tuple[set[tuple[str, str]], int]:
    """从 lineage.json 提取 (source, target) 血缘边集（去重、跳过自环）。

    Returns:
        (edges, node_count)
    """
    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    edges: set[tuple[str, str]] = set()
    for edge in data.get("edges", []):
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        if source and target and source != target:
            edges.add((source, target))
    return edges, len(data.get("nodes", []) or [])


async def run(
    json_path: Path,
    dry_run: bool = False,
    no_graph: bool = False,
    stale_threshold: int | None = None,
) -> None:
    """执行导入（增量采集语义）。"""
    configure_logging()
    logger.info(
        "lineage_json_start",
        json=str(json_path),
        dry_run=dry_run,
        no_graph=no_graph,
        stale_threshold=stale_threshold,
    )
    edges, node_count = collect_edges(json_path)
    logger.info(
        "lineage_json_parsed",
        nodes=node_count,
        distinct_edges=len(edges),
    )
    if dry_run:
        logger.info("lineage_json_dry_run_done", distinct_edges=len(edges))
        return
    if not edges:
        logger.warning("lineage_json_no_edges")
        return
    async with async_session_factory() as db:
        try:
            summary = await persist_edges(db, edges, stale_threshold)
            logger.info(
                "lineage_json_complete",
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
            logger.exception("lineage_json_failed")
            raise
    if not no_graph:
        graph_written = await persist_graph(edges)
        logger.info("lineage_json_graph", written=graph_written)
    # 显式关闭连接池，避免 asyncio.run 结束后 aiomysql 连接在 __del__ 时
    # 访问已关闭事件循环（"Event loop is closed" 告警）
    from app.db.mysql import engine as db_engine

    await db_engine.dispose()


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="导入 lineage.json 血缘到平台")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON, help="lineage.json 路径")
    parser.add_argument("--dry-run", action="store_true", help="只解析统计，不写库")
    parser.add_argument("--no-graph", action="store_true", help="不写 Neo4j 图存储")
    parser.add_argument(
        "--stale-threshold",
        type=int,
        default=None,
        help="失效观察期（连续未出现轮次，默认取配置 lineage_stale_observation_runs）",
    )
    args = parser.parse_args()
    if not args.json.exists():
        logger.error("lineage_json_not_found", path=str(args.json))
        sys.exit(1)
    asyncio.run(
        run(
            args.json,
            dry_run=args.dry_run,
            no_graph=args.no_graph,
            stale_threshold=args.stale_threshold,
        )
    )


if __name__ == "__main__":
    main()
