"""采集运行历史保留清理定时任务（P2-13：数据增长治理）。

Arq 定时任务（每日 03:00）：按保留期清理过期采集运行记录，防止
``collection_run`` 表随生产运行无限增长、拖慢历史列表查询与撑大存储。

保留策略（产品语义）：
- 仅清理 **终态**（COMPLETED/FAILED）且 ``started_at`` 早于保留期（默认 90 天）的记录；
  **RUNNING（采集中/崩溃未收尾）永不清理**——保留现场供排查。
- 物理删除（运行历史非审计主链，无需归档；审计留痕由 audit_log 独立承担）。

与 ``audit_archive_task`` / ``notify_purge_task`` 分工明确，互不重叠。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


async def purge_collection_runs_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """Arq 定时任务：清理过期采集运行历史（终态 + 超保留期）。

    任务自建 DB 会话（对齐 notify_purge/audit_archive 模式）。
    返回 {status, purged, before} 供 worker 日志观测。
    """
    from app.db.mysql import async_session_factory
    from app.services.collector.repository import CollectorRepository

    before = datetime.now(UTC) - timedelta(days=settings.collection_run_retention_days)
    async with async_session_factory() as db:
        repo = CollectorRepository(db)
        purged = await repo.purge_collection_runs(before)
        await db.commit()
    logger.info(
        "collection_run_purge_task: purged=%d before=%s",
        purged,
        before.isoformat(),
    )
    return {"status": "SUCCESS", "purged": purged, "before": before.isoformat()}
