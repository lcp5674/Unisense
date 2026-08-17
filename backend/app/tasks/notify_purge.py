"""通知/事件日志保留清理定时任务（生产数据增长治理）。

Arq 定时任务（每日 01:30）：按可配置保留期清理过期数据，防止 notification 与
event_log 表随生产运行无限增长、拖慢列表查询与撑大存储。

保留策略（产品语义）：
- 通知：已读或已办结且超过 ``notify_retention_days``（默认 90 天）→ 物理删除；
  **未读（用户未看）与 FAILED（待重试）永不清理**。
- 事件日志：超过 ``event_log_retention_days``（默认 180 天，审计留痕更长）→ 物理删除。

与 ``audit_archive_task`` 分工：audit_log 有独立 MinIO 归档机制（先导出再标记），
本任务仅处理 notify 域的业务事件流与收件箱，直接物理清理、无需归档。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


async def notify_purge_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """Arq 定时任务：清理过期通知与事件日志。

    任务自建 DB 会话（对齐 quality/semantic tasks 模式），不依赖 ctx 注入 db。
    返回 {status, notifications, event_logs}，供 worker 日志观测。
    """
    from app.db.mysql import async_session_factory
    from app.services.notify.service import NotifyService

    async with async_session_factory() as db:
        svc = NotifyService(db)
        result = await svc.purge_expired(
            notify_retention_days=settings.notify_retention_days,
            event_log_retention_days=settings.event_log_retention_days,
        )
        logger.info(
            "notify_purge_task: purged %d notifications, %d event_logs",
            result["notifications"],
            result["event_logs"],
        )
        return {"status": "SUCCESS", **result}
