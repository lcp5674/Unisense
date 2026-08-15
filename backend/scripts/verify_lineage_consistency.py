"""血缘体系一致性校验脚本（dp_csv / sqlglot / metric_definition 三通道整合审计）。

校验目标：
1. 同键重复：``lineage_edge`` 唯一键 (source_node, target_node, edge_type,
   granularity) 应保证无完全重复行（索引约束兜底，此处双重校验）。
2. 规范化重复：跨通道同一对表级血缘（去 ``table:`` 前缀 + 小写）不应有
   重复边——避免同一血缘关系被多个通道重复导入造成歧义。
3. metric 节点连通：指标级血缘（L3，provenance=metric_definition）边应存在，
   且每个活跃指标至少有一条 L3 边（source_table / source_tables 有定义时）。
4. 三通道汇总：各通道边数/节点数/失效数，便于「采集通道」视图核对。

用法:
    poetry run python -m scripts.verify_lineage_consistency
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 将 backend/ 加入 sys.path，确保能 import app
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import structlog  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.logging import configure_logging  # noqa: E402
from app.db.mysql import async_session_factory  # noqa: E402
from app.db.mysql import engine as db_engine  # noqa: E402

logger = structlog.get_logger("unisense.verify_lineage_consistency")


async def run() -> None:
    configure_logging()
    async with async_session_factory() as db:
        # 1. 同键重复（唯一键约束兜底，双重校验）
        dup = (
            await db.execute(
                text(
                    "SELECT source_node, target_node, edge_type, granularity, COUNT(*) c "
                    "FROM lineage_edge WHERE deleted_at IS NULL "
                    "GROUP BY source_node, target_node, edge_type, granularity HAVING c > 1"
                )
            )
        ).all()
        logger.info("check_duplicate_keys", duplicate_pairs=len(dup))

        # 2. 规范化重复（跨通道同一对表级血缘）
        norm_dup = (
            await db.execute(
                text(
                    "SELECT s, t, COUNT(*) c FROM ("
                    "  SELECT LOWER(REPLACE(source_node, 'table:', '')) s,"
                    "         LOWER(REPLACE(target_node, 'table:', '')) t"
                    "  FROM lineage_edge WHERE deleted_at IS NULL AND granularity = 'L1'"
                    ") x GROUP BY s, t HAVING c > 1"
                )
            )
        ).all()
        logger.info("check_normalized_duplicates", duplicate_pairs=len(norm_dup))

        # 3. metric 节点连通性：活跃指标 vs 是否有 L3 边
        metrics = (
            await db.execute(
                text(
                    "SELECT metric_code, status, definition_json "
                    "FROM metric WHERE deleted_at IS NULL"
                )
            )
        ).all()
        has_l3 = {
            r[0]
            for r in (
                await db.execute(
                    text(
                        "SELECT DISTINCT REPLACE(target_node, 'metric:', '') "
                        "FROM lineage_edge WHERE deleted_at IS NULL AND granularity = 'L3' "
                        "AND target_node LIKE 'metric:%' "
                        "UNION "
                        "SELECT DISTINCT REPLACE(source_node, 'metric:', '') "
                        "FROM lineage_edge WHERE deleted_at IS NULL AND granularity = 'L3' "
                        "AND source_node LIKE 'metric:%'"
                    )
                )
            ).all()
        }
        missing_l3 = [m[0] for m in metrics if m[0] not in has_l3]
        logger.info(
            "check_metric_l3", metrics=len(metrics), with_l3=len(has_l3), missing=missing_l3
        )

        # 4. 三通道汇总
        ch = (
            await db.execute(
                text(
                    "SELECT provenance, COUNT(*) edges,"
                    "  COUNT(DISTINCT source_node) src, COUNT(DISTINCT target_node) tgt,"
                    "  SUM(stale) stale FROM lineage_edge WHERE deleted_at IS NULL"
                    "  GROUP BY provenance ORDER BY edges DESC"
                )
            )
        ).all()
        for r in ch:
            logger.info(
                "channel_summary",
                source=r[0],
                edges=r[1],
                src_nodes=r[2],
                tgt_nodes=r[3],
                stale=r[4],
            )

        ok = not dup and not norm_dup
        logger.info("consistency_done", ok=ok)
    await db_engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
