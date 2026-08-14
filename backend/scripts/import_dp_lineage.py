"""导入 DP 平台元数据血缘（对齐 TD §12.2 / spec FR-011）。

读取 DP 平台导出的 ``dp元数据.csv``（列定义见 ``dp_table_desc.txt``），解析每个
任务的 ``task_definition``（节点 DAG），提取**表级血缘边**写入 ``lineage_edge``。

血缘来源（provenance=dp_csv）：
- nodeType=2 SQL 节点：复用 ``extract_table_lineage(sql, dialect="hive")`` 解析
  INSERT/CREATE...SELECT 的读入源表 -> 写入目标表；
- nodeType=6/8/10 同步节点：``syncSourceInfo`` 源表 -> ``syncTargetInfo`` 目标表
  （如 ``trcd_dcare_sign.patient_sign_protocol`` -> ``wedw_ods.xxx_ful_d``）；
- parentIds DAG 边：父节点输出表 -> 子节点输入表（任务内调度依赖）。

幂等：``LineageRepository.upsert_edge`` 按唯一键 ``(source_node, target_node,
edge_type, granularity)`` 幂等，重复执行不产生重复边、不重复写历史。

用法:
    poetry run python -m scripts.import_dp_lineage [--csv 路径] [--dry-run] [--no-graph]

参数:
    --csv:  CSV 路径（默认仓库根 ``dp元数据.csv``）
    --dry-run: 只解析统计，不写库
    --no-graph: 不写 Neo4j 图存储（默认同时写 MySQL + Neo4j）
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path
from typing import Any

# 将 backend/ 加入 sys.path，确保能 import app
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import structlog  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

# task_definition 为超大 JSON 字段，放宽 CSV 单字段上限（默认 128KB 不够）
csv.field_size_limit(100_000_000)

from app.core.logging import configure_logging  # noqa: E402
from app.db.mysql import async_session_factory  # noqa: E402
from app.services.lineage.graph import LineageGraphClient  # noqa: E402
from app.services.lineage.parser import extract_table_lineage, node_table  # noqa: E402
from app.services.lineage.repository import LineageRepository  # noqa: E402

logger = structlog.get_logger("unisense.import_dp_lineage")

# 默认 CSV 路径：仓库根目录下
DEFAULT_CSV = Path(__file__).resolve().parent.parent.parent / "dp元数据.csv"

# 每批提交的边数（控制单事务大小）
_BATCH_SIZE = 500

# 表级血缘统一参数
_EDGE_TYPE = "DERIVED_FROM"
_GRANULARITY = "L1"
_PROVENANCE = "dp_csv"
_CHANGE_REASON = "import"
_CONFIDENCE = 1.0

# 同步节点类型：6/8/10
_SYNC_NODE_TYPES = frozenset({6, 8, 10})


def parse_task_definition(raw: str) -> list[dict[str, Any]]:
    """解析 ``task_definition`` 列，返回节点列表（结构异常返回空列表）。"""
    if not raw or not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(value, dict):
        value = value.get("nodes", [])
    return value if isinstance(value, list) else []


def sync_source_tables(source_info: object) -> list[str]:
    """提取同步源表：``syncSourceInfo.dbInfoBOList[].{databaseName}.{tableName[]}``。"""
    if not isinstance(source_info, dict):
        return []
    tables: list[str] = []
    for db in source_info.get("dbInfoBOList") or []:
        if not isinstance(db, dict):
            continue
        db_name = str(db.get("databaseName") or "")
        raw_tables = db.get("tableName") or []
        if isinstance(raw_tables, str):
            raw_tables = [raw_tables]
        for table in raw_tables:
            table = str(table or "").strip()
            if not table:
                continue
            if "." in table:
                tables.append(table)
            elif db_name:
                tables.append(f"{db_name}.{table}")
            else:
                tables.append(table)
    return tables


def sync_target_table(target_info: object) -> list[str]:
    """提取同步目标表：``syncTargetInfo.databaseName + tableName``。"""
    if not isinstance(target_info, dict):
        return []
    table = str(target_info.get("tableName") or "").strip()
    if not table:
        return []
    if "." in table:
        return [table]
    db_name = str(target_info.get("databaseName") or "").strip()
    return [f"{db_name}.{table}"] if db_name else [table]


def node_lineage(node: dict[str, Any]) -> tuple[list[str], list[str]]:
    """提取单节点的 (源表, 目标表)。

    - nodeType=2：SQL 解析（复用平台解析器，hive 方言）；
    - nodeType=6/8/10：同步节点，源表来自 syncSourceInfo，目标表来自 syncTargetInfo；
    - 其余（3=脚本/4=drop）：无成边。
    """
    node_type = node.get("nodeType")
    if node_type == 2:
        command = node.get("command") or ""
        edges = extract_table_lineage(command, dialect="hive")
        return [e.source for e in edges], [e.target for e in edges]
    if node_type in _SYNC_NODE_TYPES:
        params = node.get("params") or {}
        return (
            sync_source_tables(params.get("syncSourceInfo")),
            sync_target_table(params.get("syncTargetInfo")),
        )
    return [], []


def edges_from_nodes(nodes: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """从任务节点列表提取去重后的 (源表, 目标表) 血缘边。

    三类来源：
    - SQL 节点（nodeType=2）：``extract_table_lineage`` 解析的读入源表 -> 写入目标表；
    - 同步节点（nodeType=6/8/10）：``syncSourceInfo`` 源表 -> ``syncTargetInfo`` 目标表；
    - parentIds DAG 边：父节点输出表 -> 子节点输入表（任务内调度依赖）。

    自环边（source == target）一律跳过：同表覆盖写/任务内表流转在血缘图上无信息量，
    且会污染环检测与影响分析。
    """
    node_map = {node.get("nodeId") or "": node for node in nodes}
    node_sources: dict[str, set[str]] = {}
    node_targets: dict[str, set[str]] = {}
    for node_id, node in node_map.items():
        src, tgt = node_lineage(node)
        node_sources[node_id] = set(src)
        node_targets[node_id] = set(tgt)

    edges: set[tuple[str, str]] = set()
    # SQL 边 / 同步边：源表 -> 目标表（跳过自环）
    for node_id in node_map:
        for s in node_sources.get(node_id, ()):
            for t in node_targets.get(node_id, ()):
                if s != t:
                    edges.add((s, t))
    # DAG 边：父节点输出表 -> 子节点输入表（跳过自环）
    for node_id, node in node_map.items():
        for parent_id in node.get("parentIds") or []:
            if parent_id not in node_map:
                continue
            parent_out = node_targets.get(parent_id, set())
            child_in = node_sources.get(node_id, set())
            for s in parent_out:
                for t in child_in:
                    if s != t:
                        edges.add((s, t))
    return edges


def collect_edges(
    csv_path: Path,
) -> tuple[set[tuple[str, str]], dict[str, int], int]:
    """解析 CSV，聚合全部 (source, target) 血缘边。

    Returns:
        (edges, node_type_counter, task_count)
    """
    edges: set[tuple[str, str]] = set()
    node_types: dict[str, int] = {}
    task_count = 0
    tasks_with_edges = 0

    with open(csv_path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            task_count += 1
            nodes = parse_task_definition(row.get("task_definition", ""))
            if not nodes:
                continue
            for node in nodes:
                nt = node.get("nodeType")
                node_types[str(nt)] = node_types.get(str(nt), 0) + 1
            edges |= edges_from_nodes(nodes)
            if any(
                node_lineage(node)[0] or node_lineage(node)[1] for node in nodes
            ):
                tasks_with_edges += 1

    return edges, node_types, task_count


async def persist_edges(db: AsyncSession, edges: set[tuple[str, str]]) -> int:
    """将血缘边写入 ``lineage_edge``（幂等，分批 commit）。"""
    repo = LineageRepository(db)
    written = 0
    pending = 0
    for source, target in sorted(edges):
        await repo.upsert_edge(
            source_node=node_table(source),
            target_node=node_table(target),
            edge_type=_EDGE_TYPE,
            granularity=_GRANULARITY,
            confidence=_CONFIDENCE,
            provenance=_PROVENANCE,
            change_reason=_CHANGE_REASON,
        )
        written += 1
        pending += 1
        if pending >= _BATCH_SIZE:
            await db.commit()
            logger.info("batch_committed", batch=written)
            pending = 0
    if pending:
        await db.commit()
    return written


async def persist_graph(edges: set[tuple[str, str]]) -> bool:
    """将血缘边写入 Neo4j 图存储（best-effort：不可达/熔断时降级 False 不影响主流程）。

    与 MySQL 写入共用同一份 ``node_table()`` 规范化节点 id，保证图/库节点一致。
    """
    graph = LineageGraphClient()
    triples = [
        (node_table(source), node_table(target), _EDGE_TYPE) for source, target in edges
    ]
    return await graph.write_edges(triples)


async def run(csv_path: Path, dry_run: bool = False, no_graph: bool = False) -> None:
    """执行导入。"""
    configure_logging()
    logger.info("dp_lineage_start", csv=str(csv_path), dry_run=dry_run, no_graph=no_graph)
    edges, node_types, task_count = collect_edges(csv_path)
    logger.info(
        "dp_lineage_parsed",
        tasks=task_count,
        node_types=node_types,
        distinct_edges=len(edges),
    )
    if dry_run:
        logger.info("dp_lineage_dry_run_done", distinct_edges=len(edges))
        return
    if not edges:
        logger.warning("dp_lineage_no_edges")
        return
    async with async_session_factory() as db:
        try:
            written = await persist_edges(db, edges)
            logger.info("dp_lineage_complete", written=written)
        except Exception:
            await db.rollback()
            logger.exception("dp_lineage_failed")
            raise
    if not no_graph:
        graph_written = await persist_graph(edges)
        logger.info("dp_lineage_graph", written=graph_written)
    # 显式关闭连接池，避免 asyncio.run 结束后 aiomysql 连接在 __del__ 时
    # 访问已关闭事件循环（"Event loop is closed" 告警）
    from app.db.mysql import engine as db_engine

    await db_engine.dispose()


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="导入 DP 平台元数据血缘")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="DP 元数据 CSV 路径")
    parser.add_argument("--dry-run", action="store_true", help="只解析统计，不写库")
    parser.add_argument("--no-graph", action="store_true", help="不写 Neo4j 图存储")
    args = parser.parse_args()
    if not args.csv.exists():
        logger.error("csv_not_found", path=str(args.csv))
        sys.exit(1)
    asyncio.run(run(args.csv, dry_run=args.dry_run, no_graph=args.no_graph))


if __name__ == "__main__":
    main()
