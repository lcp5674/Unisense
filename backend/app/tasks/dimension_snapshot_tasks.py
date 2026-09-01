"""引用型维度快照定时刷新任务（TD §12.15 扩展）。

Arq 定时任务（每 30 分钟）：扫描 ``sync_mode='snapshot'`` 且满足
``last_snapshot_at + refresh_interval_hours < now``（或从未刷新）的维度，
逐条执行 ``refresh_dimension_snapshot``。

设计（对齐 collect_scheduler 扫描模式）：
- arq ``cron_jobs`` 是静态配置，无法 per-dimension 动态注册——用单一扫描任务
  按维度自身 ``refresh_interval_hours`` 判定是否到期，天然支持差异化刷新频率。
- 逐条刷新内部异常由 service 记为 run FAILED 并回滚，不阻断其余维度。
- ``@task_locked("dimension-snapshot")`` 防多副本双跑。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.mysql import async_session_factory
from app.models.dimension import Dimension, DimensionStatus, SyncMode
from app.services.dimension.service import DimensionService
from app.tasks.lock import task_locked

logger = get_logger("unisense.dimension_snapshot")


@task_locked("dimension-snapshot", ttl=3600)
async def refresh_dimension_snapshots_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """扫描到期引用型维度并刷新快照（单任务最多处理 200 个，防超时拖垮 worker）。"""
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(Dimension).where(
                        Dimension.sync_mode == SyncMode.SNAPSHOT.value,
                        Dimension.status != DimensionStatus.DEPRECATED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        due: list[Dimension] = []
        for dim in rows:
            interval = dim.refresh_interval_hours or 24
            if dim.last_snapshot_at is None:
                due.append(dim)
                continue
            last = dim.last_snapshot_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if now - last >= timedelta(hours=interval):
                due.append(dim)

        if not due:
            return {"status": "SUCCESS", "scanned": len(rows), "refreshed": 0}

        service = DimensionService(db)
        refreshed = 0
        failed: list[str] = []
        for dim in due[:200]:
            try:
                await service.refresh_dimension_snapshot(dim.dim_code, trigger="cron")
                refreshed += 1
            except Exception as exc:  # noqa: BLE001 - 单维度失败不阻断整体
                failed.append(dim.dim_code)
                logger.warning(
                    "dimension_snapshot_failed",
                    dim_code=dim.dim_code,
                    error=str(exc),
                )
        await db.commit()
        return {
            "status": "SUCCESS",
            "scanned": len(rows),
            "due": len(due),
            "refreshed": refreshed,
            "failed": failed,
        }
