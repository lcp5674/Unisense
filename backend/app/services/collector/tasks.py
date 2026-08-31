"""采集 worker 任务（arq 生产入口 + 可单测的任务体）。

``run_collection_task`` 同时被两处调用：
- 生产：arq worker 通过 ``enqueue_job("run_collection_task", source_id, actor_id)`` 触发，
  由 worker 的 ``on_startup`` 注入 db 会话工厂、collector 构建与 ``RedisJobStore``。
- 单测：直接以 ``ctx`` 注入 MagicMock db / fake collector / 内存 JobStore 调用。

任务体优先复用 ``ctx`` 中已注入的 db / collector（测试与依赖注入场景），
否则自行从数据源构建（生产默认路径），确保采集在后台异步完成并回写任务状态。

增强（工业级修复）：
- US3: 支持 mode 参数，采集完成后更新采集水位
- US4: job_id 幂等（成功后标记，崩溃/失败可重试同 job_id；TTL 与终态对齐）
- US5: 成功/失败后更新 health_status
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.exceptions import ExternalDependencyError
from app.db.mysql import async_session_factory
from app.services.collector.distributed_lock import CollectionLock
from app.services.collector.queue import (
    append_run_log,
    delete_run_logs,
    read_run_logs,
)
from app.services.collector.repository import CollectorRepository
from app.services.collector.service import CollectorService
from app.services.collector.spi import build_collector

logger = logging.getLogger("unisense.collector.tasks")

#: 进度日志保留上限（避免长任务在 Redis/内存中无限膨胀）
_MAX_PROGRESS_MESSAGES = 300
#: 幂等键 TTL（秒）——与 JobStore 终态 TTL（7 天）对齐，避免键生命周期短于任务记录
_IDEMPOTENT_TTL_SECONDS = 7 * 24 * 60 * 60
#: P1-7 失败自动重试：瞬时错误类型（源库连接/超时/外部依赖类）
_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ExternalDependencyError,
    ConnectionError,
    TimeoutError,
    OSError,
)
#: 瞬时失败最大尝试次数（首次 + 2 次退避重试）
_MAX_RETRIES = 3
#: 退避基数（秒）：第 N 次重试前等待 base * N（asyncio.sleep 不阻塞事件循环）
_RETRY_BACKOFF_SECONDS = 5


def _make_progress_cb(
    store: Any,
    job_id: str,
    source_id: str,
    actor_id: int,
    mode: str = "FULL",
    *,
    run_id: int | None = None,
    redis: Any | None = None,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """构造写入 JobStore 的采集进度回调（供 SSE 实时推送）。

    每次回调把最新进度快照写入 ``store.set(job_id, "RUNNING", {...})``；
    消息列表保留最近 N 条，Redis/内存中不无限增长。``mode`` 写入 detail，
    保证任务中心在 RUNNING 期间也能展示真实执行模式（不被进度覆盖）。

    ``run_id``/``redis`` 提供时，同步把每条进度追加到采集运行日志 Redis 实时
    缓冲（``collect:run_log:{run_id}``）——任务终态由 ``_flush_run_logs``
    一次性回写 ``collection_run_log`` 表，供采集记录详情页长期查看。
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
        # 采集运行日志实时缓冲（best-effort：Redis 写入失败不影响采集主流程）
        if redis is not None and run_id is not None and msg:
            from datetime import UTC, datetime

            try:
                await append_run_log(
                    redis,
                    run_id,
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "level": "ERROR" if event.get("phase") == "fail" else "INFO",
                        "phase": event.get("phase"),
                        "entity_name": event.get("entity_name"),
                        "message": msg,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - 日志写入是辅助能力
                logger.warning("run_log_append_failed: run=%s err=%s", run_id, exc)

    return cb


def _make_run_log_cb(
    redis: Any | None, run_id: int | None
) -> Callable[[dict[str, Any]], Awaitable[None]] | None:
    """构造仅写采集运行日志实时缓冲的进度回调（同步采集 API 路径用）。

    与 ``_make_progress_cb`` 不同：同步路径无 JobStore，只需把进度追加到
    Redis 实时缓冲（``collect:run_log:{run_id}``），终态由 API 收尾
    ``_flush_run_logs`` 回写 DB。Redis 不可用或 run_id 缺失时返回 None。

    Args:
        redis: Redis 客户端（可空——不可用则日志能力降级为 no-op）。
        run_id: 采集运行记录 ID（创建失败为 None 时降级 no-op）。
    """
    if redis is None or run_id is None:
        return None
    from datetime import UTC, datetime

    async def cb(event: dict[str, Any]) -> None:
        msg = str(event.get("message") or "")
        if not msg:
            return
        try:
            await append_run_log(
                redis,
                run_id,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "level": "ERROR" if event.get("phase") == "fail" else "INFO",
                    "phase": event.get("phase"),
                    "entity_name": event.get("entity_name"),
                    "message": msg,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 日志写入是辅助能力
            logger.warning("run_log_append_failed: run=%s err=%s", run_id, exc)

    return cb


async def _lock_heartbeat(
    lock: CollectionLock,
    source_id: str,
    job_id: str,
    interval: float = 60,
) -> None:
    """采集分布式锁心跳：每 ``interval`` 秒续期（TTL 120s），worker 崩溃后锁快速过期。

    旧设计 acquire 时用 1800s 长 TTL 且不续期——worker 被 SIGKILL/崩溃后
    finally 的 release 不执行，锁残留 30 分钟，同源后续采集全部被
    SKIPPED_CONCURRENT 跳过（用户点「立即采集」看似卡住）。心跳使锁 TTL
    始终短（120s），崩溃后最多 2 分钟自然过期，同源可恢复采集。
    ``interval`` 供测试缩短心跳周期。
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await lock.refresh(source_id, job_id, ttl=120)
        except Exception as exc:  # noqa: BLE001 - 续期失败下次心跳再试
            logger.warning(
                "collect_lock_heartbeat_failed: source=%s job=%s err=%s",
                source_id,
                job_id,
                exc,
            )


async def _flush_run_logs(
    redis: Any | None,
    svc: CollectorService | None,
    run_id: int | None,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """把 Redis 实时缓冲的采集日志一次性回写 DB 并清理缓冲（任务终态收尾）。

    实时缓冲只存进度事件（start/scanning/registering），终态信息
    （失败原因 / 失败实体明细）在此处补充为 ERROR 日志一并落库，保证
    采集记录详情页能完整回看一次采集的全程（含失败细节）。

    Args:
        redis: Redis 客户端（可空——无缓冲则跳过）。
        svc: 采集服务（可空——无服务则跳过）。
        run_id: 采集运行记录 ID（可空——无记录则跳过）。
        result: 采集结果（成功收尾时提供，补充 failed_specs ERROR 日志）。
        error: 失败原因（失败收尾时提供，补充一条 ERROR 日志）。
    """
    if redis is None or svc is None or run_id is None:
        return
    try:
        entries, _ = await read_run_logs(redis, run_id, 0, -1)
        from datetime import UTC, datetime

        ts = datetime.now(UTC).isoformat()
        if error:
            entries.append(
                {
                    "ts": ts,
                    "level": "ERROR",
                    "phase": "fail",
                    "entity_name": None,
                    "message": f"采集失败：{error[:480]}",
                }
            )
        else:
            for fs in (result or {}).get("failed_specs") or []:
                entity = str(fs.get("entity_name") or "")
                entries.append(
                    {
                        "ts": ts,
                        "level": "ERROR",
                        "phase": "registering",
                        "entity_name": entity,
                        "message": f"注册失败：{entity} — {str(fs.get('error') or '')[:300]}",
                    }
                )
        if entries:
            await svc.flush_run_logs(run_id, entries)
        await delete_run_logs(redis, run_id)
    except Exception as exc:  # noqa: BLE001 - 日志回写是辅助能力，失败不阻断收尾
        logger.warning("run_log_flush_failed: run=%s err=%s", run_id, exc)


async def _check_idempotency(redis: Any | None, job_id: str) -> bool:
    """US4: 幂等检查——同 job_id 已成功完成（幂等键存在）时跳过执行。

    幂等键只在任务**成功后**写入（``_mark_idempotent_completed``）：任务崩溃/
    失败不写键，arq 重试同一 job_id 可重新执行；已成功完成的任务被重复投递
    （网络重放）则被短路。区别于旧的「任务开始时 SET NX 认领」——后者崩溃
    任务在 TTL 内永久阻塞同 job_id 重试，且认领值（COMPLETED）语义失真。

    Args:
        redis: Redis 客户端（可选）。
        job_id: 任务 ID。

    Returns:
        True 如果任务可以执行（同 job_id 尚未成功完成过），否则 False。
    """
    if redis is None:
        return True
    try:
        key = f"collect_job_idempotent:{job_id}"
        # 幂等键仅在成功完成后存在；EXISTS 只读不认领，崩溃/失败任务可重试
        return not bool(await redis.exists(key))
    except Exception as exc:  # Redis 不可用时放行（幂等是优化，不阻塞采集）
        logger.warning("idempotency_check_failed: %s, 允许执行", exc)
        return True


async def _mark_idempotent_completed(redis: Any | None, job_id: str) -> None:
    """US4: 任务成功完成后标记幂等键（TTL 7 天与终态对齐）。

    仅成功路径写入：失败/崩溃不标记，允许 arq 重试同一 job_id。
    """
    if redis is None:
        return
    try:
        key = f"collect_job_idempotent:{job_id}"
        await redis.set(key, "COMPLETED", ex=_IDEMPOTENT_TTL_SECONDS)
    except Exception as exc:
        logger.warning("idempotent_mark_failed: %s", exc)


async def _record_task_failure(
    *,
    db: Any,
    svc: CollectorService | None,
    run_id: int | None,
    source_id: str,
    job_id: str,
    actor_id: int,
    store: Any,
    error: str,
    redis: Any | None = None,
) -> None:
    """采集任务失败/超时的副作用收尾（except Exception 与 CancelledError 共用）。

    统一处理：释放 PendingRollback 会话 + 运行记录 FAILED + 健康状态 + JobStore
    终态 + 运行日志回写——避免超时（``asyncio.CancelledError`` 是 BaseException
    不落入 ``except Exception``）导致 JobStore 与 collection_run 永久卡 RUNNING。

    Args:
        db: 数据库会话（可为 None）。
        svc: 采集服务（可为 None）。
        run_id: 采集运行记录 ID（创建失败时为 None）。
        source_id: 数据源标识。
        job_id: 任务 ID。
        actor_id: 触发者 ID。
        store: JobStore（可为 None）。
        error: 失败原因文本。
        redis: Redis 客户端（可为 None，用于回写运行日志实时缓冲）。
    """
    # 采集异常可能已让 session 进入 PendingRollback（flush/commit 失败）。
    # 必须先 rollback 释放会话，否则 fail_collection_run / update_health_status
    # 的 flush 会抛 PendingRollbackError——运行记录滞留 RUNNING、健康状态不更新，
    # 且掩盖原始异常。
    if db is not None:
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 - rollback 异常不影响后续副作用写入
            logger.warning("采集失败路径 rollback 异常: source=%s", source_id, exc_info=True)
    # 采集运行历史：失败收尾 FAILED（记录错误 + 提交）
    if run_id is not None and svc is not None:
        try:
            await svc.fail_collection_run(run_id, error)
        except Exception:  # noqa: BLE001 - 失败收尾异常不影响上抛
            logger.warning("collection_run_fail_commit_failed: run=%s", run_id)
        # 运行日志实时缓冲回写 DB（失败路径补充 ERROR 日志后落库）
        try:
            await _flush_run_logs(redis, svc, run_id, error=error)
        except Exception:  # noqa: BLE001 - 日志回写失败不影响上抛
            logger.warning("collection_run_log_flush_failed: run=%s", run_id)
    # P2-14: 任务失败定向通知源 Owner（best-effort；独立 session，不干扰失败主流程）
    if svc is not None:
        try:
            await svc._notify_source_owner_failure(
                "collect.failed",
                "采集任务失败",
                source_id,
                reason=error[:500],
            )
        except Exception:  # noqa: BLE001 - 通知失败不影响主流程
            logger.warning("采集失败通知异常: source=%s", source_id, exc_info=True)
    # 失败 → 更新健康状态
    try:
        if db is not None:
            repo = CollectorRepository(db)
            await repo.update_health_status(source_id, "unhealthy", error=error)
            await db.commit()
    except Exception:  # noqa: BLE001 - 健康状态更新失败不影响主流程
        logger.warning("更新健康状态失败: source=%s", source_id)
    # 失败回写保留 source_id/actor_id（任务中心需按源标识展示）
    if store is not None:
        await store.set(
            job_id,
            "FAILED",
            {"source_id": source_id, "actor_id": actor_id, "error": error},
        )


async def _collect_with_retry(
    svc: CollectorService,
    source_id: str,
    collector: Any,
    actor_id: int,
    *,
    mode: str,
    progress_cb: Callable[[dict[str, Any]], Awaitable[None]] | None,
    include_patterns: list[str] | None,
    exclude_patterns: list[str] | None,
) -> dict[str, Any]:
    """带退避重试的采集执行（P1-7）。

    源库连接/超时/外部依赖类瞬时错误（``ExternalDependencyError`` /
    ``ConnectionError`` / ``TimeoutError`` / ``OSError``）自动退避重试
    （首次 + 最多 2 次重试，间隔 5s * N）；业务错误（源不存在、数据格式等）
    直接上抛，不消耗重试配额。upsert 幂等保证重试不产生重复实体。
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await svc.collect_and_register(
                source_id,
                collector,
                actor_id,
                mode=mode,
                progress_cb=progress_cb,
                include_patterns=include_patterns,
                exclude_patterns=exclude_patterns,
            )
        except _RETRYABLE_EXCEPTIONS as exc:
            if attempt >= _MAX_RETRIES:
                logger.warning(
                    "collect_retry_exhausted: source=%s attempts=%d err=%s",
                    source_id,
                    attempt,
                    exc,
                )
                raise
            logger.warning(
                "collect_retryable_failure: source=%s attempt=%d/%d err=%s",
                source_id,
                attempt,
                _MAX_RETRIES,
                exc,
            )
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS * attempt)


async def run_collection_task(
    ctx: dict[str, Any],
    source_id: str,
    actor_id: int,
    job_id: str,
    *,
    mode: str = "FULL",
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """执行一次采集任务，并将状态回写 ``ctx["job_store"]``。

    Args:
        ctx: arq worker 上下文，可包含 db/collector/svc/job_store/redis。
        source_id: 数据源标识。
        actor_id: 触发者 ID。
        job_id: 任务 ID。
        mode: 采集模式（FULL/INCREMENTAL）。
        include_patterns: 本次临时白名单（None=按数据源配置）。
        exclude_patterns: 本次临时黑名单（None=按数据源配置）。

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
    # P1-5: 同源并发锁（acquire 成功才置 True，finally 按需 release）
    lock: CollectionLock | None = None
    lock_acquired = False
    #: 锁心跳任务：任务运行期间定期续期，worker 崩溃后锁快速过期
    lock_heartbeat: asyncio.Task[None] | None = None

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
        # P1-5: 异步链路并发锁——同源串行化（防定时+手动/多次触发并发采集，
        # 造成水位/DSD 竞态）。获取失败说明同源已有采集任务在运行，本次跳过。
        lock = CollectionLock(redis)
        if not await lock.acquire(source_id, job_id, ttl=120):
            logger.warning(
                "collect_concurrent_skip: source=%s job=%s 同源已有采集任务在运行",
                source_id,
                job_id,
            )
            if store is not None:
                await store.set(
                    job_id,
                    "SKIPPED",
                    {
                        "source_id": source_id,
                        "actor_id": actor_id,
                        "error": "同源已有采集任务在运行",
                    },
                )
            # SKIPPED 非成功完成，不标记幂等键
            return {"status": "SKIPPED_CONCURRENT"}
        lock_acquired = True
        # 锁心跳：每 60s 续期（TTL 120s）。worker 崩溃/被 SIGKILL 后心跳停止，
        # 锁最多 120s 自然过期——同源后续采集可恢复，而非旧设计残留 30 分钟。
        lock_heartbeat = asyncio.create_task(_lock_heartbeat(lock, source_id, job_id))

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

        # 构造进度回调：worker 侧把 RUNNING 进度写入 JobStore（SSE 实时推送），
        # 并追加采集运行日志实时缓冲（run_id/redis 提供时）——终态回写 DB。
        progress_cb = (
            _make_progress_cb(
                store,
                job_id,
                source_id,
                actor_id,
                mode=mode,
                run_id=run_id,
                redis=redis,
            )
            if store is not None
            else _make_run_log_cb(redis, run_id)
        )
        # P1-7: 瞬时错误（源库连接/超时/外部依赖）自动退避重试
        result = await _collect_with_retry(
            svc,
            source_id,
            collector,
            actor_id,
            mode=mode,
            progress_cb=progress_cb,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        # P0-4: 成功路径必须提交——worker 会话无外部调用方 commit，
        # 否则 upsert 的 catalogs / watermark / health 全部不落库（采集等于没执行）。
        if db is not None:
            await db.commit()

        # 采集运行历史：收尾 COMPLETED（回填指标 + 提交）
        if run_id is not None:
            await svc.complete_collection_run(run_id, result)
            # 运行日志实时缓冲回写 DB（成功路径补充 failed_specs ERROR 日志）
            await _flush_run_logs(redis, svc, run_id, result=result)

        # US5: 成功 → 更新健康状态（service 层已处理）
        if store is not None:
            await store.set(job_id, "COMPLETED", result)
        # US4: 成功后才标记幂等键（不在任务开始认领——崩溃/失败可重试同 job_id）
        await _mark_idempotent_completed(redis, job_id)
        return result
    except Exception as exc:  # noqa: BLE001 - 任务失败需回写状态并上抛供 arq 重试
        logger.exception("采集任务失败 source=%s job=%s", source_id, job_id)
        await _record_task_failure(
            db=db,
            svc=svc,
            run_id=run_id,
            source_id=source_id,
            job_id=job_id,
            actor_id=actor_id,
            store=store,
            error=str(exc),
            redis=redis,
        )
        raise
    except asyncio.CancelledError:
        # arq job_timeout 超时/取消：CancelledError 是 BaseException，不落入上方
        # except Exception——若不处理，JobStore 与 collection_run 永久卡 RUNNING
        # （非终态无 TTL 回收）。补写 FAILED 终态后重新抛出，保持取消语义。
        #
        # H2 修正：用户主动取消（cancel API 已把 JobStore 置 CANCELLED）时**保持
        # CANCELLED 终态**——不覆盖为 FAILED、不误发 collect.failed 失败通知；
        # 仅收尾 collection_run（标 FAILED + 「任务已取消」）。仅 arq 超时
        # （非用户取消）才走失败收尾 + 通知。
        user_cancelled = False
        if store is not None:
            existing = await store.get(job_id)
            user_cancelled = (existing or {}).get("status") == "CANCELLED"
        if user_cancelled:
            logger.info("采集任务已被用户取消 source=%s job=%s", source_id, job_id)
            if run_id is not None and svc is not None:
                try:
                    # 2026-08-28：取消收尾标 CANCELLED（与 JobStore 终态对齐），
                    # 不再标 FAILED——运行历史里取消与真实失败可区分。
                    await svc.cancel_collection_run(run_id)
                    # 取消前执行的进度日志回写（让用户看到任务进行到哪一步）
                    await _flush_run_logs(redis, svc, run_id, error="任务已取消")
                except Exception:  # noqa: BLE001 - 取消收尾异常不影响上抛
                    logger.warning("collection_run_cancel_commit_failed: run=%s", run_id)
            raise
        logger.warning("采集任务超时 source=%s job=%s", source_id, job_id)
        await _record_task_failure(
            db=db,
            svc=svc,
            run_id=run_id,
            source_id=source_id,
            job_id=job_id,
            actor_id=actor_id,
            store=store,
            error="采集超时或任务取消",
            redis=redis,
        )
        raise
    finally:
        # P1-5: 释放同源并发锁（仅 owner 可释放；Redis 不可用时 no-op）
        if lock_heartbeat is not None:
            lock_heartbeat.cancel()
        if lock_acquired and lock is not None:
            try:
                await lock.release(source_id, job_id)
            except Exception:  # noqa: BLE001 - 锁释放失败不影响任务收尾
                logger.warning(
                    "collect_lock_release_failed: source=%s job=%s", source_id, job_id
                )
        if own_session and db is not None:
            try:
                await db.close()
            finally:
                if collector is not None:
                    await collector.dispose()
