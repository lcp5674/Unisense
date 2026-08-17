"""采集 worker 任务（arq 生产入口 + 可单测的任务体）。

``run_collection_task`` 同时被两处调用：
- 生产：arq worker 通过 ``enqueue_job("run_collection_task", source_id, actor_id)`` 触发，
  由 worker 的 ``on_startup`` 注入 db 会话工厂、collector 构建与 ``RedisJobStore``。
- 单测：直接以 ``ctx`` 注入 MagicMock db / fake collector / 内存 JobStore 调用。

任务体优先复用 ``ctx`` 中已注入的 db / collector（测试与依赖注入场景），
否则自行从数据源构建（生产默认路径），确保采集在后台异步完成并回写任务状态。

增强（工业级修复）：
- US3: 支持 mode 参数，采集完成后更新采集水位
- US4: job_id 幂等检查（Redis SET NX）
- US5: 成功/失败后更新 health_status
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.db.mysql import async_session_factory
from app.services.collector.repository import CollectorRepository
from app.services.collector.service import CollectorService
from app.services.collector.spi import build_collector

logger = logging.getLogger("unisense.collector.tasks")

#: 进度日志保留上限（避免长任务在 Redis/内存中无限膨胀）
_MAX_PROGRESS_MESSAGES = 300


def _make_progress_cb(
    store: Any,
    job_id: str,
    source_id: str,
    actor_id: int,
    mode: str = "FULL",
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """构造写入 JobStore 的采集进度回调（供 SSE 实时推送）。

    每次回调把最新进度快照写入 ``store.set(job_id, "RUNNING", {...})``；
    消息列表保留最近 N 条，Redis/内存中不无限增长。``mode`` 写入 detail，
    保证任务中心在 RUNNING 期间也能展示真实执行模式（不被进度覆盖）。
    """
    messages: list[str] = []

    async def cb(event: dict[str, Any]) -> None:
        msg = str(event.get("message") or "")
        if msg:
            messages.append(msg)
            if len(messages) > _MAX_PROGRESS_MESSAGES:
                del messages[: len(messages) - _MAX_PROGRESS_MESSAGES]
        progress = {
            "phase": event.get("phase"),
            "message": msg,
            "messages": messages[-50:],
            "index": event.get("index"),
            "total": event.get("total"),
            "entity_name": event.get("entity_name"),
            "scanned": event.get("scanned"),
            "sensitivity": event.get("sensitivity"),
        }
        await store.set(
            job_id,
            "RUNNING",
            {"source_id": source_id, "actor_id": actor_id, "mode": mode, "progress": progress},
        )

    return cb


async def _check_idempotency(redis: Any | None, job_id: str) -> bool:
    """US4: 幂等检查——Redis SET NX 判断 collect_job:{job_id} 是否已 COMPLETED。

    Args:
        redis: Redis 客户端（可选）。
        job_id: 任务 ID。

    Returns:
        True 如果任务可以执行（未完成过），False 如果任务已 COMPLETED。
    """
    if redis is None:
        return True
    try:
        key = f"collect_job_idempotent:{job_id}"
        result = await redis.set(key, "COMPLETED", nx=True, ex=86400)  # 24h TTL
        return result is not None  # SET NX 成功=首次执行
    except Exception as exc:
        logger.warning("idempotency_check_failed: %s, 允许执行", exc)
        return True


async def run_collection_task(
    ctx: dict[str, Any], source_id: str, actor_id: int, job_id: str, *, mode: str = "FULL"
) -> dict[str, Any]:
    """执行一次采集任务，并将状态回写 ``ctx["job_store"]``。

    Args:
        ctx: arq worker 上下文，可包含 db/collector/svc/job_store/redis。
        source_id: 数据源标识。
        actor_id: 触发者 ID。
        job_id: 任务 ID。
        mode: 采集模式（FULL/INCREMENTAL）。

    Returns:
        采集结果字典。
    """
    store = ctx.get("job_store")
    db = ctx.get("db")
    collector = ctx.get("collector")
    redis = ctx.get("redis")
    own_session = False
    # 采集运行历史记录 ID（创建失败时为 None，不阻断采集主流程）
    run_id: int | None = None

    # US4: 幂等检查
    if not await _check_idempotency(redis, job_id):
        logger.info("job_idempotent_skip: job=%s 已完成，跳过", job_id)
        if store is not None:
            existing = await store.get(job_id)
            if existing is not None:
                detail = existing.get("detail", {})
                return dict(detail) if isinstance(detail, dict) else {}
        return {"status": "IDEMPOTENT_SKIP"}

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
                collector = collector or build_collector(src.source_type, src.connection_config)
            svc = CollectorService(db)

        if collector is None:
            raise RuntimeError(f"采集器不可用: {source_id}")

        # 采集运行历史：创建 RUNNING 记录（独立提交，进程崩溃不丢）。
        # 触发方式按 job_id 前缀推导：定时调度 collect:sched: / 手动 collect-now。
        try:
            trigger = "scheduled" if job_id.startswith("collect:sched:") else "manual"
            run_id = await svc.start_collection_run(
                source_id=source_id,
                trigger=trigger,
                mode=mode,
                job_id=job_id,
                actor_id=actor_id,
            )
        except Exception:  # noqa: BLE001 - 运行记录创建失败不应阻断采集主流程
            logger.warning(
                "collection_run_start_failed: source=%s job=%s", source_id, job_id, exc_info=True
            )
            run_id = None

        # 构造进度回调：worker 侧把 RUNNING 进度写入 JobStore，供 SSE 实时推送
        progress_cb = (
            _make_progress_cb(store, job_id, source_id, actor_id, mode=mode)
            if store is not None
            else None
        )
        result = await svc.collect_and_register(
            source_id, collector, actor_id, mode=mode, progress_cb=progress_cb
        )
        # P0-4: 成功路径必须提交——worker 会话无外部调用方 commit，
        # 否则 upsert 的 catalogs / watermark / health 全部不落库（采集等于没执行）。
        if db is not None:
            await db.commit()

        # 采集运行历史：收尾 COMPLETED（回填指标 + 提交）
        if run_id is not None:
            await svc.complete_collection_run(run_id, result)

        # US5: 成功 → 更新健康状态（service 层已处理）
        if store is not None:
            await store.set(job_id, "COMPLETED", result)
        return result
    except Exception as exc:  # noqa: BLE001 - 任务失败需回写状态并上抛供 arq 重试
        logger.exception("采集任务失败 source=%s job=%s", source_id, job_id)

        # 采集异常可能已让 session 进入 PendingRollback（flush/commit 失败）。
        # 必须先 rollback 释放会话，否则 fail_collection_run / update_health_status
        # 的 flush 会抛 PendingRollbackError——运行记录滞留 RUNNING、健康状态
        # 不更新（副作用静默丢失），且掩盖原始异常。
        if db is not None:
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001 - rollback 异常不影响后续副作用写入
                logger.warning("采集失败路径 rollback 异常: source=%s", source_id, exc_info=True)

        # 采集运行历史：失败收尾 FAILED（记录错误 + 提交）
        if run_id is not None and svc is not None:
            try:
                await svc.fail_collection_run(run_id, str(exc))
            except Exception:  # noqa: BLE001 - 失败收尾异常不影响上抛
                logger.warning("collection_run_fail_commit_failed: run=%s", run_id)

        # US5: 失败 → 更新健康状态
        try:
            if db is not None:
                repo = CollectorRepository(db)
                await repo.update_health_status(source_id, "unhealthy", error=str(exc))
                # P0-4: 失败路径同样提交 unhealthy，避免回滚丢失
                await db.commit()
        except Exception:
            logger.warning("更新健康状态失败: source=%s", source_id)

        if store is not None:
            # 失败回写保留 source_id/actor_id（任务中心需按源标识展示），
            # 避免仅存 error 导致任务列表 source_id 列变空。
            await store.set(
                job_id,
                "FAILED",
                {"source_id": source_id, "actor_id": actor_id, "error": str(exc)},
            )
        raise
    finally:
        if own_session and db is not None:
            try:
                await db.close()
            finally:
                if collector is not None:
                    await collector.dispose()
