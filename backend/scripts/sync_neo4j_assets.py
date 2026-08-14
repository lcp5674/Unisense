"""同步 MySQL 资产元数据属性到 Neo4j Asset 节点（资产地图 Neo4j 路径数据补全）。

背景（TD §12.11 / FR-18 资产地图）：血缘导入脚本（``import_dp_lineage.py``）
只写节点 ``id`` 与 ``LINEAGE`` 关系，资产地图 Neo4j 查询所需展示属性
``type/label/domain/pii/owner`` 缺失，导致图渲染为满屏 unknown/空标签散点
（此前靠「缺 label 回退 MySQL」兜底）。

本脚本从 MySQL 权威元数据（db_catalog 表/视图 + metric 指标）补齐这些属性：
- ``table:{name}`` 节点：匹配 db_catalog 活跃数据源表（domain/sensitivity/owner）；
  未匹配（如 DP 平台表）时用 id 推导 ``label=name, type=table``；
- ``field:{...}`` 节点：``type=field``、``label=id 去前缀``（域/PII 无法推断置空）；
- ``metric:{code}`` 节点：匹配 metric 表（domain/pii_flag/owner_id）。

用法:
    poetry run python -m scripts.sync_neo4j_assets [--url bolt://localhost:7687]

参数:
    --url:  覆盖 Neo4j 连接 URL（默认取配置 settings.neo4j_url）
    --dry-run: 只统计待同步节点，不写 Neo4j
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
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.db.mysql import async_session_factory  # noqa: E402
from app.db.mysql import engine as db_engine  # noqa: E402
from app.models.data_source import DataSource, DBCatalog  # noqa: E402
from app.models.metric import Metric  # noqa: E402
from app.services.lineage.graph import LineageGraphClient  # noqa: E402

logger = structlog.get_logger("unisense.sync_neo4j_assets")


async def load_catalog_attrs(db: AsyncSession) -> dict[str, dict[str, Any]]:
    """活跃数据源下的表/视图资产属性（entity_name -> 属性字典）。"""
    rows = (
        await db.execute(
            select(
                DBCatalog.entity_name,
                DBCatalog.sensitivity_level,
                DBCatalog.owner_id,
                DataSource.domain,
            )
            .join(DataSource, DataSource.source_id == DBCatalog.source_id)
            .where(
                DBCatalog.deleted_at.is_(None),
                DBCatalog.entity_type.in_(["TABLE", "VIEW"]),
                DataSource.deleted_at.is_(None),
            )
        )
    ).all()
    return {
        r.entity_name: {
            "type": "table",
            "label": r.entity_name,
            "pii": bool(r.sensitivity_level and "PII" in r.sensitivity_level),
            "domain": r.domain,
            "owner": str(r.owner_id) if r.owner_id else None,
        }
        for r in rows
    }


async def load_metric_attrs(db: AsyncSession) -> dict[str, dict[str, Any]]:
    """指标资产属性（metric_code -> 属性字典）。"""
    rows = (
        await db.execute(
            select(Metric.metric_code, Metric.domain, Metric.pii_flag, Metric.owner_id).where(
                Metric.deleted_at.is_(None)
            )
        )
    ).all()
    return {
        r.metric_code: {
            "type": "metric",
            "label": r.metric_code,
            "pii": bool(r.pii_flag),
            "domain": r.domain,
            "owner": str(r.owner_id) if r.owner_id else None,
        }
        for r in rows
    }


def _fallback_attrs(prefix: str, name: str) -> dict[str, Any]:
    """未匹配 MySQL 时的降级属性：由节点 id 推导 label/type。"""
    return {"type": "table" if prefix == "table" else prefix, "label": name, "pii": False}


def build_assets(
    node_ids: list[str],
    catalog_attrs: dict[str, dict[str, Any]],
    metric_attrs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """为 Neo4j 现有节点构造属性：匹配 MySQL 用完整属性，未匹配用 id 推导。"""
    assets: list[dict[str, Any]] = []
    for node_id in node_ids:
        if ":" not in node_id:
            continue
        prefix, name = node_id.split(":", 1)
        if prefix == "table":
            attrs = catalog_attrs.get(name) or _fallback_attrs("table", name)
        elif prefix == "metric":
            attrs = metric_attrs.get(name) or _fallback_attrs("metric", name)
        elif prefix == "field":
            attrs = {"type": "field", "label": name, "pii": False}
        else:
            continue
        assets.append({"id": node_id, **attrs})
    return assets


async def list_asset_ids(url: str) -> list[str]:
    """读取 Neo4j 全部 Asset 节点 id（未配置/不可达时返回空列表）。"""
    if not url:
        return []
    try:
        from neo4j import AsyncGraphDatabase
    except Exception:  # pragma: no cover - 依赖缺失时降级
        return []
    driver = AsyncGraphDatabase.driver(url, auth=(settings.neo4j_user, settings.neo4j_password))
    try:
        async with driver.session() as session:
            result = await session.run("MATCH (n:Asset) RETURN n.id AS id")
            return [record["id"] async for record in result]
    except Exception as exc:
        logger.warning("neo4j_list_ids_failed", error=str(exc))
        return []
    finally:
        await driver.close()


async def run(url: str | None, dry_run: bool = False) -> None:
    """执行资产属性同步（幂等：MERGE + SET，不删除既有节点）。"""
    configure_logging()
    url = url or settings.neo4j_url
    logger.info("sync_neo4j_assets_start", url=url, dry_run=dry_run)

    async with async_session_factory() as db:
        catalog_attrs = await load_catalog_attrs(db)
        metric_attrs = await load_metric_attrs(db)

    graph = LineageGraphClient(url)
    node_ids = await list_asset_ids(url)
    assets = build_assets(node_ids, catalog_attrs, metric_attrs)
    logger.info(
        "sync_neo4j_assets_prepared",
        nodes=len(node_ids),
        catalog_assets=len(catalog_attrs),
        metric_assets=len(metric_attrs),
        to_sync=len(assets),
    )
    if dry_run or not assets:
        logger.info("sync_neo4j_assets_done", written=False, dry_run=dry_run)
        await graph.dispose()
        await db_engine.dispose()
        return

    written = await graph.upsert_assets(assets)
    await graph.dispose()
    await db_engine.dispose()
    logger.info("sync_neo4j_assets_done", written=written, synced=len(assets))


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="同步 MySQL 资产元数据属性到 Neo4j")
    parser.add_argument("--url", type=str, default=None, help="Neo4j 连接 URL（默认取配置）")
    parser.add_argument("--dry-run", action="store_true", help="只统计待同步节点，不写 Neo4j")
    args = parser.parse_args()
    asyncio.run(run(args.url, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
