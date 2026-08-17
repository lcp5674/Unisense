"""Neo4j 定时对账任务（M2：图存储自愈）。

背景（TD §12.2 / §12.11）：MySQL 为权威边存储，Neo4j 为可选图存储。历史路径
（批量入库 ``ingest_batch``、删除/恢复）曾不写/不回写图，导致图与 MySQL 漂移。

本模块为 arq worker 提供周期性对账任务 ``sync_neo4j_assets_task``：
- 从 MySQL 权威数据（``db_catalog`` / ``metric`` / ``lineage_edge``）加载资产属性
  与血缘边；
- 全量补全节点展示属性（``upsert_assets``）+ 写入表级/指标级血缘边
  （``write_edges``），幂等（MERGE + SET）不删既有节点；
- 与 CLI 脚本 ``scripts/sync_neo4j_assets.py`` 的区别：不调用 ``configure_logging``
  （worker 已配置）、不 ``dispose`` 全局 db engine（worker 进程共享）、额外同步
  ``lineage_edge`` 表级血缘边（修复批量入库历史未写图的漂移）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.data_source import DataSource, DBCatalog
from app.models.lineage import LineageEdge
from app.models.metric import Metric
from app.services.lineage.graph import LineageGraphClient

logger = get_logger("unisense.lineage.neo4j_sync")


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
            select(
                Metric.metric_code,
                Metric.domain,
                Metric.pii_flag,
                Metric.owner_id,
                Metric.definition_json,
            ).where(Metric.deleted_at.is_(None))
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


async def load_table_edges(db: AsyncSession) -> list[tuple[str, str, str]]:
    """加载权威库全部活跃表级血缘边（DERIVED_FROM / L1）。

    修复 M1 历史漂移：``ingest_batch`` 早期只落 MySQL 不入图，对账时全量重扫
    写入图存储（幂等 MERGE），保证图与权威库收敛。
    """
    rows = (
        await db.execute(
            select(
                LineageEdge.source_node,
                LineageEdge.target_node,
                LineageEdge.edge_type,
            ).where(
                LineageEdge.deleted_at.is_(None),
                LineageEdge.granularity == "L1",
                LineageEdge.edge_type == "DERIVED_FROM",
            )
        )
    ).all()
    return [(str(r.source_node), str(r.target_node), str(r.edge_type)) for r in rows]


def parse_metric_edges(
    metric_code: str, definition: dict[str, Any] | None
) -> list[tuple[str, str, str]]:
    """从指标口径定义解析血缘边 ``(source, target, edge_type)``。

    方向约定与 ``lineage_edge`` 一致：source 为上游、target 为下游。
    - ``source_tables`` 上游源表 -> ``table:{t}`` → ``metric:{code}``
    - ``dependencies`` 依赖指标 -> ``metric:{dep}`` → ``metric:{code}``
    - ``source_table`` 落地物化表 -> ``metric:{code}`` → ``table:{t}``
    """
    if not isinstance(definition, dict):
        return []
    edges: list[tuple[str, str, str]] = []
    node = f"metric:{metric_code}"
    for table in definition.get("source_tables") or []:
        if isinstance(table, str) and table:
            edges.append((f"table:{table}", node, "DERIVED_FROM"))
    for dep in definition.get("dependencies") or []:
        if isinstance(dep, str) and dep:
            edges.append((f"metric:{dep}", node, "DERIVED_FROM"))
    source_table = definition.get("source_table")
    if isinstance(source_table, str) and source_table:
        edges.append((node, f"table:{source_table}", "DERIVED_FROM"))
    return edges


async def load_metric_edges(db: AsyncSession) -> list[tuple[str, str, str]]:
    """加载全部指标血缘边（从 definition_json 解析）。"""
    rows = (
        await db.execute(
            select(Metric.metric_code, Metric.definition_json).where(
                Metric.deleted_at.is_(None)
            )
        )
    ).all()
    edges: list[tuple[str, str, str]] = []
    for r in rows:
        edges.extend(parse_metric_edges(r.metric_code, r.definition_json))
    return edges


def build_metric_nodes(
    metric_attrs: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """为 Neo4j 缺失的指标节点构造展示属性（创建节点用，id=``metric:{code}``）。"""
    return [
        {"id": f"metric:{code}", **attrs}
        for code, attrs in sorted(metric_attrs.items())
    ]


def filter_metric_edges(
    edges: list[tuple[str, str, str]], existing_tables: set[str]
) -> list[tuple[str, str, str]]:
    """过滤指标边：仅保留表端已存在于 Neo4j 的边（避免引入无属性孤立表节点）。

    指标-指标依赖边（两端均 ``metric:``）始终保留；表端不在 ``existing_tables``
    （如 DP 平台表未导入）的边丢弃，保证图连通且不扩散无属性节点。
    """
    kept: list[tuple[str, str, str]] = []
    for source, target, etype in edges:
        if (source.startswith("metric:") and target.startswith("metric:")) or (
            source.startswith("table:") and source in existing_tables
        ) or (
            target.startswith("table:") and target in existing_tables
        ):
            kept.append((source, target, etype))
    return kept


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


async def run_sync(db: AsyncSession, graph: LineageGraphClient) -> dict[str, Any]:
    """执行一次资产属性 + 血缘边全量对账（幂等，不删除既有节点/边）。

    Args:
        db: 权威库会话（复用调用方注入，不在此处 dispose）。
        graph: 图客户端（复用调用方注入，由调用方负责 dispose）。

    Returns:
        对账统计 ``{nodes, metric_nodes, table_edges, metric_edges, written_nodes,
        written_edges}``。
    """
    catalog_attrs = await load_catalog_attrs(db)
    metric_attrs = await load_metric_attrs(db)
    table_edges = await load_table_edges(db)
    metric_edges = await load_metric_edges(db)

    node_ids = await graph.list_asset_ids()
    existing_tables = {nid for nid in node_ids if nid.startswith("table:")}
    assets = build_assets(node_ids, catalog_attrs, metric_attrs)
    metric_nodes = build_metric_nodes(metric_attrs)
    kept_metric_edges = filter_metric_edges(metric_edges, existing_tables)
    all_edges = table_edges + kept_metric_edges
    logger.info(
        "neo4j_sync_prepared",
        graph_nodes=len(node_ids),
        catalog_assets=len(catalog_attrs),
        metric_assets=len(metric_attrs),
        table_edges=len(table_edges),
        metric_edges=len(kept_metric_edges),
    )
    written_nodes = await graph.upsert_assets([*assets, *metric_nodes])
    written_edges = await graph.write_edges(all_edges) if all_edges else True
    return {
        "nodes": len(assets),
        "metric_nodes": len(metric_nodes),
        "table_edges": len(table_edges),
        "metric_edges": len(kept_metric_edges),
        "written_nodes": written_nodes,
        "written_edges": written_edges,
    }


async def sync_neo4j_assets_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """arq 定时对账任务：MySQL 权威数据全量同步 Neo4j（资产属性 + 血缘边）。

    注册于 worker cron（每日凌晨），自愈历史/偶发图漂移（M2）：
    - 批量入库早期未写图的表级边 → 全量重扫 ``lineage_edge`` 写入；
    - 节点展示属性缺失/陈旧 → 从 ``db_catalog`` / ``metric`` 补全；
    - 指标边（definition_json）→ 写入。
    图不可用/熔断时 best-effort 降级（返回统计，不抛错中断 worker）。

    Args:
        ctx: arq worker 上下文（本任务自建会话与图连接，不依赖 ctx 注入）。

    Returns:
        对账统计字典。
    """
    from app.db.mysql import async_session_factory

    graph = LineageGraphClient()
    try:
        async with async_session_factory() as db:
            return await run_sync(db, graph)
    except Exception as exc:
        # 图/库任一不可达时告警并返回空统计，不中断 worker（熔断器已记录）
        logger.error("neo4j_sync_task_failed", error=str(exc))
        return {"written_nodes": False, "written_edges": False, "error": str(exc)}
    finally:
        await graph.dispose()
