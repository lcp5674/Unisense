"""ES 索引自动同步任务（B1 审查修复）。

此前 EsIndexer 仅由管理员手动端点触发，无定时任务/变更钩子 → 全局搜索与库长期
不一致（搜不到刚发布指标、搜到已彻底删除指标点击 404）。本任务提供每日全量
兜底同步；指标 purge/软删后的增量删除由 purge 链路直接调用 ``EsIndexer.delete_*``
（见 semantic/repository.py 的 purge 级联）。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

logger = get_logger("unisense.tasks.search")


async def sync_es_indexes_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """每日全量同步 metric_idx / term_idx（ES 不可用/未配置时跳过并返回 skipped）。

    Returns:
        ``{"skipped": True}`` 或 ``{"metric_idx": n, "term_idx": n}``。
    """
    from app.db.mysql import async_session_factory
    from app.services.search.es_indexer import EsIndexer

    async with async_session_factory() as db:
        indexer = EsIndexer(db)
        if not indexer.enabled:
            logger.info("es_index_sync_skipped", reason="es_disabled")
            return {"skipped": True}
        # 先确保索引存在（analyzer 版本检测，幂等），再全量重灌
        await indexer.ensure_indexes()
        counts = await indexer.sync_all()
        logger.info("es_index_sync_done", metric_idx=counts["metric_idx"], term_idx=counts["term_idx"])
        return counts
