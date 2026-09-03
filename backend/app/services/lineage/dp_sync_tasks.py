"""dp 调度血缘同步 arq 周期任务（D7：1 分钟 ticker 按配置间隔触发扫描）。

- ``dp_lineage_poll_task`` 注册为 worker 每分钟 cron；读 ``dp_sync_config``
  判断是否到轮询间隔（默认 5 分钟，前端可配 1~60，改配置即时生效无需重启）。
- 未初始化配置 / 未启用 → 直接跳过（默认 enabled=false，不自动扫 dp）。
- 通过 ``fetch_collector`` 注入 dp 数据源真实连接（build_collector，已落库源
  放行内网但仍拒回环）；LLM 走平台默认 LlmClient（异常转空 content 由协议
  层降级建单，不拖垮扫描）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.models.data_source import DataSource
from app.services.collector.spi import build_collector

logger = logging.getLogger(__name__)


async def _fetch_collector(db: Any, source_id: str) -> Any:
    """构建 dp 数据源只读采集器（source_id 由配置指定）。"""
    src = (
        await db.execute(
            select(DataSource).where(DataSource.source_id == source_id)
        )
    ).scalar_one_or_none()
    if src is None:
        raise RuntimeError(f"dp 数据源不存在: source_id={source_id}")
    return build_collector(src.source_type, src.connection_config)


def _make_llm_chat(db: Any):
    """构造 LLM 调用闭包：平台默认客户端；异常转空 content（协议层降级）。"""

    async def llm_chat(messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        from app.services.llm.client import LlmClient

        client = LlmClient()
        try:
            return await client.chat(
                messages,
                temperature=0.0,
                max_tokens=int(kwargs.get("max_tokens") or 2000),
            )
        except Exception as exc:  # noqa: BLE001 —— LLM 故障转空输出，由协议层建单
            logger.warning("dp_sync_llm_call_failed: %s", exc)
            return {"content": ""}

    return llm_chat


async def dp_lineage_poll_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """dp 血缘同步轮询任务（worker 每分钟触发，按配置间隔执行扫描）。"""
    from app.db.mysql import async_session_factory
    from app.services.lineage.dp_sync_service import DpSyncService

    async with async_session_factory() as db:
        svc = DpSyncService(db, llm_chat=_make_llm_chat(db))
        return await svc.scan_once(lambda sid: _fetch_collector(db, sid))
