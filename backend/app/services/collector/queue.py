"""采集任务队列抽象（TD §12.1：全量采集异步化）。

全量采集在请求内同步执行会拖垮 API（已知限制：collector.known_accepted）。
本模块将采集任务抽象为可注入的队列：

- ``CollectionQueue``：入队协议（返回 job_id）。
- ``InMemoryCollectionQueue``：进程内内存队列，作为默认实现与单测载体
  （无需 Redis / 外部 worker），保证无消息中间件时采集链路仍可运行。
- ``ArqCollectionQueue``：基于 ``arq``（Redis）的生产实现，将任务投递到独立
  worker 进程；``arq`` 为惰性导入，未安装时不影响其余功能。

队列与状态存储解耦：状态由 ``JobStore`` 负责（内存 / Redis 两套实现），
便于 ``GET /collect/jobs/{job_id}`` 查询进度。
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("unisense.collector.queue")


@runtime_checkable
class CollectionQueue(Protocol):
    """采集任务入队协议。"""

    async def enqueue(
        self,
        source_id: str,
        actor_id: int,
        mode: str = "FULL",
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> str:
        """入队一次采集任务（mode 指定 FULL/INCREMENTAL），返回 job_id。

        ``include_patterns`` / ``exclude_patterns`` 为本次临时表级过滤
        （仅本次采集生效，None=worker 回退到数据源配置的白黑名单）。
        """
        ...

    async def cancel(self, job_id: str) -> bool:
        """取消一次采集任务（P1-7：任务中心取消能力）。

        对已入队未运行的任务：取消投递；对运行中的任务：请求取消
        （worker 收到 CancelledError 补写 FAILED 终态）。

        Returns:
            True 表示已请求取消；False 表示任务不存在/已终态无法取消。
        """
        ...


@runtime_checkable
class JobStore(Protocol):
    """任务状态存储协议。"""

    async def set(self, job_id: str, status: str, detail: dict[str, Any]) -> None:
        """写入任务状态。"""
        ...

    async def get(self, job_id: str) -> dict[str, Any] | None:
        """读取任务状态。"""
        ...

    async def list(
        self,
        limit: int = 50,
        offset: int = 0,
        source_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """列出任务（按入队逆序，供采集任务中心展示；可按 source_id / status 过滤）。"""
        ...

    async def count(self, source_id: str | None = None, status: str | None = None) -> int:
        """统计匹配任务数（服务端分页 total 用）。"""
        ...


class InMemoryCollectionQueue:
    """进程内采集队列 + 状态存储（默认实现 / 单测载体）。"""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    async def enqueue(
        self,
        source_id: str,
        actor_id: int,
        mode: str = "FULL",
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> str:
        from datetime import UTC, datetime

        job_id = f"job-{uuid.uuid4().hex[:12]}"
        detail: dict[str, Any] = {
            "source_id": source_id,
            "actor_id": actor_id,
            "mode": mode,
        }
        if include_patterns is not None:
            detail["include_patterns"] = include_patterns
        if exclude_patterns is not None:
            detail["exclude_patterns"] = exclude_patterns
        self._jobs[job_id] = {
            "job_id": job_id,
            "source_id": source_id,
            "actor_id": actor_id,
            "status": "QUEUED",
            "detail": detail,
            "created_at": datetime.now(UTC).isoformat(),
        }
        return job_id

    async def cancel(self, job_id: str) -> bool:
        """取消一次采集任务（内存版：终态不可取消，其余标记 CANCELLED）。"""
        job = self._jobs.get(job_id)
        if job is None or job.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
            return False
        job["status"] = "CANCELLED"
        detail = dict(job.get("detail") or {})
        detail["error"] = "任务已被用户取消"
        job["detail"] = detail
        return True

    async def set(self, job_id: str, status: str, detail: dict[str, Any]) -> None:
        from datetime import UTC, datetime

        job = self._jobs.setdefault(job_id, {"job_id": job_id, "source_id": "", "actor_id": 0})
        # 首次写入（任意状态）记录创建时间，后续不覆盖（与 RedisJobStore 语义一致）
        job.setdefault("created_at", datetime.now(UTC).isoformat())
        job["status"] = status
        job["detail"] = detail
        # 同步 detail 中的 source_id/actor_id 到顶层（与 RedisJobStore.get 语义一致：
        # source_id 来自 detail；保证按源过滤的 count/list 一致）
        if detail.get("source_id") is not None:
            job["source_id"] = detail["source_id"]
        if detail.get("actor_id") is not None:
            job["actor_id"] = detail["actor_id"]

    @staticmethod
    def _kind(job_id: str) -> str:
        return "scheduled" if job_id.startswith("collect:sched:") else "manual"

    async def get(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return {
            "job_id": job["job_id"],
            "source_id": job.get("source_id"),
            "actor_id": job.get("actor_id"),
            "status": job.get("status"),
            "detail": job.get("detail", {}),
            "created_at": job.get("created_at"),
            "kind": self._kind(job_id),
        }

    async def list(
        self,
        limit: int = 50,
        offset: int = 0,
        source_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        jobs = [
            j
            for j in self._jobs.values()
            if (source_id is None or j.get("source_id") == source_id)
            and (status is None or j.get("status") == status)
        ]
        jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
        return [
            {
                "job_id": j["job_id"],
                "source_id": j.get("source_id"),
                "actor_id": j.get("actor_id"),
                "status": j.get("status"),
                "detail": j.get("detail", {}),
                "created_at": j.get("created_at"),
                "kind": self._kind(j["job_id"]),
            }
            for j in jobs[offset : offset + limit]
        ]

    async def count(self, source_id: str | None = None, status: str | None = None) -> int:
        """统计匹配任务数（与 list 相同过滤，供服务端分页 total）。"""
        return sum(
            1
            for j in self._jobs.values()
            if (source_id is None or j.get("source_id") == source_id)
            and (status is None or j.get("status") == status)
        )


class RedisJobStore:
    """基于 Redis 的任务状态存储（生产实现，供 arq worker 与状态查询共用）。"""

    # P1-6: 终态任务加固定 TTL，避免在 Redis 中永久堆积。
    # HIGH-5/M1: SKIPPED（同源并发冲突跳过）/CANCELLED（用户取消）同样是终态，
    # 若不纳入 TTL 会无限堆积（顺序索引只增不减，list/count 越来越慢）。
    _TERMINAL_STATUSES: frozenset[str] = frozenset(
        {"COMPLETED", "FAILED", "SKIPPED", "CANCELLED"}
    )
    _TERMINAL_TTL_SECONDS: int = 7 * 24 * 60 * 60  # 7 天
    # P2-16: 顺序索引（LPUSH：表头为最新任务）——list/count 避免 SCAN 全键空间
    _ORDER_KEY = "collect_job_order"

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    @staticmethod
    def _key(job_id: str) -> str:
        return f"collect_job:{job_id}"

    @staticmethod
    def _kind(job_id: str) -> str:
        """任务来源标记：定时调度（collect:sched:）或手动触发（collect-now）。"""
        return "scheduled" if job_id.startswith("collect:sched:") else "manual"

    async def set(self, job_id: str, status: str, detail: dict[str, Any]) -> None:
        import json
        from datetime import UTC, datetime

        # H1: updated_at 记录最近一次状态/进度写入（stale 清扫据此判断崩溃滞留任务）
        await self._redis.hset(
            self._key(job_id),
            mapping={
                "status": status,
                "detail": json.dumps(detail, ensure_ascii=False, default=str),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        # 任务中心创建时间：首次写入（任意状态，含定时调度直接 RUNNING 的路径）
        # 用 HSETNX 落 created_at，后续进度高频写入不覆盖。HSETNX 为 O(1)，开销可忽略。
        created = await self._redis.hsetnx(
            self._key(job_id), "created_at", datetime.now(UTC).isoformat()
        )
        if created:
            # P2-16: 首次创建时维护顺序索引（表头=最新任务）
            await self._redis.lpush(self._ORDER_KEY, job_id)
        # P1-6: 终态（COMPLETED/FAILED）设置 7 天 TTL，过期后自动回收（重试幂等键可清理）
        if status in self._TERMINAL_STATUSES:
            await self._redis.expire(self._key(job_id), self._TERMINAL_TTL_SECONDS)

    async def get(self, job_id: str) -> dict[str, Any] | None:
        import json

        raw = await self._redis.hgetall(self._key(job_id))
        if not raw:
            return None
        # redis.asyncio 未开 decode_responses 时 hgetall 返回 bytes 键值，统一解码
        decoded = {
            (k.decode("utf-8", errors="replace") if isinstance(k, bytes) else k): (
                v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
            )
            for k, v in raw.items()
        }
        detail_raw = decoded.get("detail")
        detail: dict[str, Any] = json.loads(detail_raw) if detail_raw else {}
        return {
            "job_id": job_id,
            "source_id": detail.get("source_id"),
            "actor_id": detail.get("actor_id"),
            "status": decoded.get("status"),
            "detail": detail,
            "created_at": decoded.get("created_at"),
            "updated_at": decoded.get("updated_at"),
            "kind": self._kind(job_id),
        }

    async def stale_jobs(self, now: Any, timeout_seconds: int) -> list[tuple[str, str]]:
        """H1: 返回超时未更新的 RUNNING/QUEUED 任务（worker 崩溃滞留清扫）。

        遍历顺序索引（避免 SCAN 全键空间），按 ``updated_at``（无则回退
        ``created_at``）判断超过 ``timeout_seconds`` 未更新的非终态任务。

        Args:
            now: 当前时间（UTC）。
            timeout_seconds: 视为滞留的超时阈值（应显著大于 job_timeout）。

        Returns:
            [(job_id, status)] 列表。
        """
        from datetime import datetime as _dt

        stale: list[tuple[str, str]] = []
        order = await self._redis.lrange(self._ORDER_KEY, 0, -1)
        for raw in order:
            job_id = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            job = await self.get(job_id)
            if not job:
                continue
            if job.get("status") not in ("RUNNING", "QUEUED"):
                continue
            updated = job.get("updated_at") or job.get("created_at")
            if not updated:
                continue
            try:
                ts = _dt.fromisoformat(updated)
            except ValueError:
                continue
            if (now - ts).total_seconds() > timeout_seconds:
                stale.append((job_id, str(job.get("status"))))
        return stale

    async def _scan_all_job_ids(self) -> list[str]:
        """SCAN 全键 ``collect_job:*`` 收集 job_id（存量数据/索引缺失回退路径）。"""
        keys: list[str] = []
        cursor = 0
        while True:
            cursor, batch = await self._redis.scan(cursor, match="collect_job:*", count=200)
            keys.extend(batch)
            if not cursor:
                break
        job_ids: list[str] = []
        for key in keys:
            if isinstance(key, bytes):
                key = key.decode("utf-8", errors="replace")
            job_ids.append(key.split(":", 1)[1])
        return job_ids

    async def _order_job_ids(self) -> list[str]:
        """读取顺序索引中的 job_id 列表；索引为空（存量数据）时回退 SCAN 并惰性回填。"""
        raw_ids = await self._redis.lrange(self._ORDER_KEY, 0, -1)
        job_ids = [
            (v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v)
            for v in raw_ids
        ]
        if job_ids:
            return job_ids
        # 存量数据（顺序索引尚不存在）：回退 SCAN 全键并惰性回填，后续 set 维护
        job_ids = await self._scan_all_job_ids()
        if job_ids:
            await self._redis.lpush(self._ORDER_KEY, *job_ids)
        return job_ids

    async def list(
        self,
        limit: int = 50,
        offset: int = 0,
        source_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        import json

        # P2-16: 顺序索引优先（避免 SCAN 遍历全键空间）；逐键 hgetall + 惰性清理过期
        job_ids = await self._order_job_ids()
        jobs: list[dict[str, Any]] = []
        for job_id in job_ids:
            raw = await self._redis.hgetall(self._key(job_id))
            if not raw:
                # hash 已过期（终态 TTL 7 天）——从顺序索引惰性清理，防索引无限增长
                await self._redis.lrem(self._ORDER_KEY, 0, job_id)
                continue
            decoded = {
                (k.decode("utf-8", errors="replace") if isinstance(k, bytes) else k): (
                    v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
                )
                for k, v in raw.items()
            }
            detail_raw = decoded.get("detail")
            detail = json.loads(detail_raw) if detail_raw else {}
            if source_id is not None and detail.get("source_id") != source_id:
                continue
            if status is not None and (decoded.get("status") or "UNKNOWN") != status:
                continue
            jobs.append(
                {
                    "job_id": job_id,
                    "source_id": detail.get("source_id"),
                    "actor_id": detail.get("actor_id"),
                    "status": decoded.get("status"),
                    "detail": detail,
                    "created_at": decoded.get("created_at"),
                    "kind": self._kind(job_id),
                }
            )
        # 按创建时间倒序（无 created_at 的旧任务回退到 job_id 排序，仍靠前展示）
        jobs.sort(
            key=lambda j: (j.get("created_at") or "", j.get("job_id") or ""),
            reverse=True,
        )
        return jobs[offset : offset + limit]

    async def count(self, source_id: str | None = None, status: str | None = None) -> int:
        """统计匹配任务数（P2-16：无过滤时 O(1) 索引长度，避免 SCAN + 全量 hgetall）。"""
        import json

        job_ids = await self._order_job_ids()
        if source_id is None and status is None:
            return len(job_ids)
        n = 0
        for job_id in job_ids:
            raw = await self._redis.hgetall(self._key(job_id))
            if not raw:
                continue
            decoded = {
                (k.decode("utf-8", errors="replace") if isinstance(k, bytes) else k): (
                    v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
                )
                for k, v in raw.items()
            }
            detail_raw = decoded.get("detail")
            detail = json.loads(detail_raw) if detail_raw else {}
            if source_id is not None and detail.get("source_id") != source_id:
                continue
            if status is not None and (decoded.get("status") or "UNKNOWN") != status:
                continue
            n += 1
        return n


#: 模块级 arq Redis 连接单例：避免每次 enqueue/get 新建 ArqRedis/AsyncRedis
#: 且从不 aclose 导致连接池泄漏（P1-9 修复）。
_arq_redis: Any | None = None


# ---- 采集运行日志 Redis 实时缓冲 ----
# 采集期间日志先追加到 Redis List（O(1)、实时可读，供采集记录详情页 RUNNING
# 轮询展示），任务终态由 worker/API 一次性 bulk 回写 collection_run_log 表并删除
# 缓冲 key。TTL 与 JobStore 终态对齐（7 天）：崩溃未回写时缓冲自愈，不无限堆积。
_RUN_LOG_TTL_SECONDS: int = 7 * 24 * 60 * 60


def run_log_key(run_id: int) -> str:
    """采集运行日志 Redis List key（按 run_id 隔离）。"""
    return f"collect:run_log:{run_id}"


async def append_run_log(redis: Any, run_id: int, entry: dict[str, Any]) -> None:
    """追加一条采集运行日志到 Redis 实时缓冲（RPUSH 保序 + 首次写 TTL）。

    Args:
        redis: Redis 客户端（ArqRedis/AsyncRedis，均支持 rpush/expire/llen/lrange）。
        run_id: 采集运行记录 ID。
        entry: 日志条目（ts/level/phase/entity_name/message），JSON 序列化存储。
    """
    import json
    from datetime import UTC, datetime

    if not entry.get("message"):
        return
    payload = {
        "ts": entry.get("ts") or datetime.now(UTC).isoformat(),
        "level": str(entry.get("level") or "INFO"),
        "phase": entry.get("phase"),
        "entity_name": entry.get("entity_name"),
        "message": str(entry.get("message"))[:512],
    }
    key = run_log_key(run_id)
    length = await redis.rpush(key, json.dumps(payload, ensure_ascii=False))
    # 仅首次写入设置 TTL（后续由任务终态回写后显式删除；TTL 是崩溃兜底）
    if int(length) == 1:
        await redis.expire(key, _RUN_LOG_TTL_SECONDS)


async def read_run_logs(
    redis: Any, run_id: int, offset: int, limit: int
) -> tuple[list[dict[str, Any]], int]:
    """从 Redis 实时缓冲分页读取采集运行日志（时间正序，与执行顺序一致）。

    Returns:
        (日志条目列表, 总条数)。
    """
    import json

    key = run_log_key(run_id)
    total = int(await redis.llen(key) or 0)
    if offset >= total or limit <= 0:
        return [], total
    raw = await redis.lrange(key, offset, offset + limit - 1)
    items: list[dict[str, Any]] = []
    for blob in raw:
        if isinstance(blob, bytes):
            blob = blob.decode("utf-8", errors="replace")
        try:
            item = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            items.append(item)
    return items, total


async def delete_run_logs(redis: Any, run_id: int) -> None:
    """删除采集运行日志 Redis 缓冲（终态回写 DB 后清理，防 Redis 堆积）。"""
    try:
        await redis.delete(run_log_key(run_id))
    except Exception:  # noqa: BLE001 - 清理失败仅记录，不影响主流程
        logger.warning("run_log_delete_failed: run=%s", run_id)


def _get_shared_arq_redis(url: str) -> Any:
    """获取共享的 arq Redis 连接（惰性单例，进程内复用）。

    Args:
        url: Redis 连接 URL（首次创建时使用）。

    Returns:
        共享的 ArqRedis 实例。
    """
    global _arq_redis
    if _arq_redis is None:
        from arq import ArqRedis

        _arq_redis = ArqRedis.from_url(url)
    return _arq_redis


class ArqCollectionQueue:
    """基于 ``arq``（Redis）的生产采集队列（惰性导入 arq）。

    P1-4 修复：实现 ``get()`` 并配合 ``RedisJobStore`` 落初始状态，
    使 ``GET /api/v1/data-sources/jobs/{job_id}`` 在 arq 模式下可查询
    （此前无 get()，arq 模式任务状态恒 404）。
    """

    def __init__(self, redis_url: str | None = None, redis: Any | None = None) -> None:
        self._redis_url = redis_url
        self._redis = redis

    async def enqueue(
        self,
        source_id: str,
        actor_id: int,
        mode: str = "FULL",
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> str:
        from app.core.config import settings

        redis = self._redis or _get_shared_arq_redis(self._redis_url or settings.redis_url)
        # run_collection_task 以 job_id 作第 4 位置参数（幂等键 + 状态回写）；
        # mode 与临时过滤为关键字参数透传（arq 0.28 会把普通 kwargs 原样传给任务函数）。
        # arq 0.28 的 enqueue_job 不支持 _max_tries/_timeout（会被当普通 kwargs 透传给
        # 任务函数导致 TypeError），任务超时由内部 collect_and_register 的 asyncio 保护兜底。
        job_id = f"collect:{source_id}:{uuid.uuid4().hex}"
        enqueue_kwargs: dict[str, Any] = {"mode": mode}
        if include_patterns is not None:
            enqueue_kwargs["include_patterns"] = include_patterns
        if exclude_patterns is not None:
            enqueue_kwargs["exclude_patterns"] = exclude_patterns
        # P1-4: 初始状态落 RedisJobStore（worker 完成后由 tasks.py 更新）；
        # mode 写入 detail 供任务中心展示「实际执行模式」。
        detail: dict[str, Any] = {"source_id": source_id, "actor_id": actor_id, "mode": mode}
        if include_patterns is not None:
            detail["include_patterns"] = include_patterns
        if exclude_patterns is not None:
            detail["exclude_patterns"] = exclude_patterns
        store = RedisJobStore(redis)
        # m1: 先落 QUEUED 再投递任务——避免 arq worker 极快完成（如任务即刻失败）
        # 写终态（COMPLETED/FAILED）后，QUEUED 再把状态打回，掩盖真实结果。
        await store.set(job_id, "QUEUED", detail)
        try:
            job = await redis.enqueue_job(
                "run_collection_task",
                source_id,
                actor_id,
                job_id,
                **enqueue_kwargs,
                _job_id=job_id,
            )
        except Exception:
            # 入队失败：清理孤儿 QUEUED 状态（非终态无 TTL，会永久堆积）
            with contextlib.suppress(Exception):
                await redis.delete(f"collect_job:{job_id}")
            raise
        # arq enqueue_job 返回 Job 对象，其 job_id 即传入的 _job_id；显式标注为 str
        # 以满足 mypy --strict 的 no-any-return（redis 为 Any 类型，job.job_id 被推断为 Any）。
        arq_job_id: str = job.job_id
        # 复用模块级共享连接池（不 aclose）
        return arq_job_id

    async def cancel(self, job_id: str) -> bool:
        """取消一次采集任务（arq 0.28 版：``Job.abort`` 协作取消 + 主动落 CANCELLED 终态）。

        arq 0.26+ 移除了 ``ArqRedis.cancel_job``，0.28 以 ``arq.jobs.Job.abort``
        替代：把 job_id 写入 abort 集合，worker 协作检查后抛 ``JobAborted``。
        对「已入队未运行」的任务 abort 无法确认结果（返回 False），故**无论
        abort 结果如何**，只要任务非终态，都主动把 JobStore 标记 CANCELLED——
        展示层立即反映取消，worker 侧由 abort 集合兜底跳过执行。

        Args:
            job_id: 采集任务 ID。

        Returns:
            True 表示已请求取消并落 CANCELLED 终态；任务不存在/已终态返回 False。
        """
        from arq.jobs import Job

        from app.core.config import settings

        redis = self._redis or _get_shared_arq_redis(self._redis_url or settings.redis_url)
        store = RedisJobStore(redis)
        existing = await store.get(job_id)
        if existing is None or existing.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
            return False
        try:
            # timeout 兜底：运行中任务快速返回；未运行任务等 10s 后 ResultNotFound。
            await Job(job_id, redis).abort(timeout=10, poll_delay=0.2)
        except Exception as exc:  # noqa: BLE001 - abort 请求失败不阻断落 CANCELLED 终态
            logger.warning("collect_job_abort_failed: job=%s err=%s", job_id, exc)
        detail = dict(existing.get("detail") or {})
        detail["error"] = "任务已被用户取消"
        await store.set(job_id, "CANCELLED", detail)
        return True

    async def get(self, job_id: str) -> dict[str, Any] | None:
        from app.core.config import settings

        redis = self._redis or _get_shared_arq_redis(self._redis_url or settings.redis_url)
        return await RedisJobStore(redis).get(job_id)

    async def list(
        self,
        limit: int = 50,
        offset: int = 0,
        source_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        from app.core.config import settings

        redis = self._redis or _get_shared_arq_redis(self._redis_url or settings.redis_url)
        return await RedisJobStore(redis).list(
            limit=limit, offset=offset, source_id=source_id, status=status
        )

    async def count(self, source_id: str | None = None, status: str | None = None) -> int:
        from app.core.config import settings

        redis = self._redis or _get_shared_arq_redis(self._redis_url or settings.redis_url)
        return await RedisJobStore(redis).count(source_id=source_id, status=status)


_default_queue: InMemoryCollectionQueue | None = None


def get_default_queue() -> InMemoryCollectionQueue:
    """返回模块级默认队列单例（内存实现，避免每次请求新建导致状态不可查）。"""
    global _default_queue
    if _default_queue is None:
        _default_queue = InMemoryCollectionQueue()
    return _default_queue


def create_collection_queue(
    redis_url: str | None = None, redis: Any | None = None
) -> CollectionQueue:
    """创建采集队列实例（生产环境优先 Arq，无 Redis 时降级 InMemory）。

    当 ``redis_url`` 非空时，使用 ``ArqCollectionQueue``（Redis 持久化队列）；
    当 ``redis_url`` 为空时，降级使用 ``InMemoryCollectionQueue``（进程内队列），
    并记录告警日志提示生产环境应配置 Redis。

    Args:
        redis_url: Redis 连接 URL；为 None 或空字符串时降级到内存队列。
        redis: 已有的 Redis 客户端（可选，优先于 redis_url）。

    Returns:
        采集队列实例。
    """
    from app.core.logging import get_logger

    logger = get_logger(__name__)

    if redis_url:
        logger.info("collection_queue_using_arq", redis_url_prefix=redis_url[:20])
        return ArqCollectionQueue(redis_url=redis_url, redis=redis)
    else:
        logger.warning(
            "collection_queue_fallback_inmemory: Redis URL 未配置，"
            "采集队列降级为内存实现。生产环境请设置 UNISENSE_REDIS_URL。"
        )
        return get_default_queue()
