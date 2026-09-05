"""dp 调度血缘同步 arq 周期任务（D7：1 分钟 ticker 按配置间隔触发扫描）。

- ``dp_lineage_poll_task`` 注册为 worker 每分钟 cron；读 ``dp_sync_config``
  判断是否到轮询间隔（默认 5 分钟，前端可配 1~60，改配置即时生效无需重启）。
- 未初始化配置 / 未启用 → 直接跳过（默认 enabled=false，不自动扫 dp）。
- 通过 ``fetch_collector`` 注入 dp 数据源真实连接（build_collector，已落库源
  放行内网但仍拒回环）；LLM 走 ``LlmConfigService.build_client``（DB 实例优先
  + env 兜底 + 路由/熔断，含 disable_thinking；异常转空 content 由协议层
  降级建单，不拖垮扫描）。
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
    """构造 LLM 调用闭包：走 LlmConfigService.build_client（DB 实例优先，含
    disable_thinking/路由/熔断 + env 兜底）；异常转空 content（协议层降级）。

    与 glossary/collector/metrics 等全库调用点一致——此前裸 ``LlmClient()``
    只读 env、绕过 DB 实例：本地 Qwen3 配在 llm_config（disable_thinking=True）
    时不被尊重，思考模式耗尽 max_tokens 致 content 空 → 待抉择单误报
    「LLM 返回空内容」。build_client 每次调用重读 DB（配置即时生效）。
    """

    async def llm_chat(messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        from app.services.llm.config_service import LlmConfigService

        client = await LlmConfigService(db).build_client()
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
    """dp 血缘同步轮询任务（worker 每分钟触发，按配置间隔执行扫描）。

    分布式锁（H3）：cron 每分钟触发、scan_once 间隔节流读的是上一轮收尾才
    写的 last_scan_at——扫描超 poll_interval 时后续 tick 会判定到点而重入，
    多副本 worker 更甚（并发双跑致 mark_missing 双倍累加 / 建单撞唯一键）。
    用 Redis SET NX 锁防重入（key 按数据源隔离；Redis 不可用降级放行）。
    """
    from app.db.mysql import async_session_factory
    from app.services.collector.distributed_lock import CollectionLock
    from app.services.lineage.dp_sync_service import DpSyncService

    redis = ctx.get("redis")
    lock = CollectionLock(redis)
    # dp 同步配置为全局单行（跨域平台能力），锁 key 固定防任意 worker/副本重入
    lock_key = "dp_lineage_poll"
    owner = f"poll-{lock_key}"
    # TTL 60min：覆盖首轮长全量；任务结束显式释放，worker 崩溃后 60min 内自愈
    acquired = await lock.acquire(lock_key, owner, ttl=3600)
    if not acquired:
        logger.info("dp_lineage_poll_skipped_locked: %s 已有扫描运行", lock_key)
        return {"skipped": "locked"}
    try:
        async with async_session_factory() as db:
            svc = DpSyncService(db, llm_chat=_make_llm_chat(db))

            async def _hb() -> None:
                # D3：扫描心跳续期锁——长扫描（>TTL）不被其它 cron/manual 抢占。
                await lock.refresh(lock_key, owner, ttl=3600)

            return await svc.scan_once(
                lambda sid: _fetch_collector(db, sid), heartbeat=_hb
            )
    finally:
        await lock.release(lock_key, owner)
