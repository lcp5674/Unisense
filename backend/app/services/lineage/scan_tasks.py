"""血缘库级扫描 arq 定时任务（企业级批量重建的定时调度）。

按配置周期扫描 SQL 目录并写入血缘（dry_run=False），使「新增 SQL 文件 → 血缘自动
重建」闭环：运维把 ETL SQL 落入 ``UNISENSE_LINEAGE_SCAN_DIR`` 目录，worker 每日定时
扫描，新表/新字段血缘自动进入平台（含结构性 DDL 边：LIKE/COPY OF/RENAME 与
DROP 依赖失效）。

未配置目录时任务安全禁用（返回 ``enabled=False``），不抛错、不中断 worker。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("unisense.lineage.scan_tasks")


async def lineage_scan_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """arq 定时扫描任务：扫描配置目录下 SQL 文件并写血缘（每日 cron 触发）。

    与 ``sync_neo4j_assets_task`` 同模式：自建会话（不依赖 ctx 注入）、
    best-effort 降级（图不可用/DB 异常时返回统计并告警，不抛错中断 worker）。

    Args:
        ctx: arq worker 上下文（本任务自建会话，不使用 ctx）。

    Returns:
        扫描统计字典（enabled/files/边数/失败数），失败时含 error。
    """
    if not settings.lineage_scan_dir:
        logger.info("lineage_scan_task_disabled: lineage_scan_dir 未配置")
        return {"enabled": False, "reason": "lineage_scan_dir 未配置"}

    from app.db.mysql import async_session_factory
    from app.services.lineage.schemas import LineageScanRequest
    from app.services.lineage.service import LineageService

    req = LineageScanRequest(
        path=settings.lineage_scan_dir,
        dialect=settings.lineage_scan_dialect,
        dry_run=False,
    )
    try:
        async with async_session_factory() as db:
            svc = LineageService(db)
            resp = await svc.scan_directory(req, actor_id=None)
            # P0-3：任务内显式 commit（async_session 上下文退出仅 close/回滚，不自动提交），
            # 提交后再执行图写/缓存失效/事件副作用，保证扫描结果真正落库且时序正确。
            await db.commit()
            await svc.run_post_commit()
        result = {
            "enabled": True,
            "files": resp.files,
            "statements": resp.statements,
            "table_edges": resp.table_edges,
            "field_edges": resp.field_edges,
            "ddl_edges": resp.ddl_edges,
            "succeeded": resp.succeeded,
            "failed": resp.failed,
            "graph_written": resp.graph_written,
        }
        logger.info(
            "lineage_scan_task_done: files=%d statements=%d te=%d fe=%d ddl=%d failed=%d",
            resp.files,
            resp.statements,
            resp.table_edges,
            resp.field_edges,
            resp.ddl_edges,
            resp.failed,
        )
        return result
    except Exception as exc:  # noqa: BLE001 - best-effort 降级，不中断 worker
        logger.error("lineage_scan_task_failed", error=str(exc))
        return {"enabled": True, "error": str(exc)}
