"""采集 worker 任务（arq 生产入口 + 可单测的任务体）。

``run_collection_task`` 同时被两处调用：
- 生产：arq worker 通过 ``enqueue_job("run_collection_task", source_id, actor_id)`` 触发，
  由 worker 的 ``on_startup`` 注入 db 会话工厂、collector 构建与 ``RedisJobStore``。
- 单测：直接以 ``ctx`` 注入 MagicMock db / fake collector / 内存 JobStore 调用。

任务体优先复用 ``ctx`` 中已注入的 db / collector（测试与依赖注入场景），
否则自行从数据源构建（生产默认路径），确保采集在后台异步完成并回写任务状态。
"""

from __future__ import annotations

import logging
from typing import Any

from app.db.mysql import async_session_factory
from app.services.collector.repository import CollectorRepository
from app.services.collector.service import CollectorService
from app.services.collector.spi import build_collector

logger = logging.getLogger("unisense.collector.tasks")


async def run_collection_task(
    ctx: dict[str, Any], source_id: str, actor_id: int, job_id: str
) -> dict[str, Any]:
    """执行一次全量采集任务，并将状态回写 ``ctx["job_store"]``。"""
    store = ctx.get("job_store")
    db = ctx.get("db")
    collector = ctx.get("collector")
    own_session = False

    try:
        svc = ctx.get("svc")
        if svc is None:
            # 生产默认路径：自行为任务构建会话与采集器
            if db is None or collector is None:
                db = async_session_factory()
                own_session = True
                repo = CollectorRepository(db)
                src = await repo.get_source(source_id)
                if src is None:
                    raise RuntimeError(f"数据源不存在: {source_id}")
                collector = collector or build_collector(
                    src.source_type, src.connection_config
                )
            svc = CollectorService(db)

        if collector is None:
            raise RuntimeError(f"采集器不可用: {source_id}")

        result = await svc.collect_and_register(source_id, collector, actor_id)
        if store is not None:
            await store.set(job_id, "COMPLETED", result)
        return result
    except Exception as exc:  # noqa: BLE001 - 任务失败需回写状态并上抛供 arq 重试
        logger.exception("采集任务失败 source=%s job=%s", source_id, job_id)
        if store is not None:
            await store.set(job_id, "FAILED", {"error": str(exc)})
        raise
    finally:
        if own_session and db is not None:
            try:
                await db.close()
            finally:
                if collector is not None:
                    await collector.dispose()
