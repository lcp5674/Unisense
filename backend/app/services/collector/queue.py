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
import uuid
from typing import Any, Protocol, runtime_checkable


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

    # P1-6: 终态任务加固定 TTL，避免重试幂等键在 Redis 中永久堆积
    _TERMINAL_STATUSES: frozenset[str] = frozenset({"COMPLETED", "FAILED"})
    _TERMINAL_TTL_SECONDS: int = 7 * 24 * 60 * 60  # 7 天

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

        await self._redis.hset(
            self._key(job_id),
            mapping={
                "status": status,
                "detail": json.dumps(detail, ensure_ascii=False, default=str),
            },
        )
        # 任务中心创建时间：首次写入（任意状态，含定时调度直接 RUNNING 的路径）
        # 用 HSETNX 落 created_at，后续进度高频写入不覆盖。HSETNX 为 O(1)，开销可忽略。
        await self._redis.hsetnx(self._key(job_id), "created_at", datetime.now(UTC).isoformat())
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
            "kind": self._kind(job_id),
        }

    async def list(
        self,
        limit: int = 50,
        offset: int = 0,
        source_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        import json

        # SCAN 遍历 collect_job:*（生产模式下任务状态由 worker 回写，带 7 天 TTL）
        keys: list[str] = []
        cursor = 0
        while True:
            cursor, batch = await self._redis.scan(cursor, match="collect_job:*", count=200)
            keys.extend(batch)
            if not cursor:
                break
        jobs: list[dict[str, Any]] = []
        for key in keys:
            # redis.asyncio 的 SCAN/KEYS 返回 bytes；str(bytes) 会带 b'...' 包装，
            # 必须显式 decode 后再按 "collect_job:" 前缀切出 job_id。
            if isinstance(key, bytes):
                key = key.decode("utf-8", errors="replace")
            job_id = key.split(":", 1)[1]
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
        """统计匹配任务数（与 list 相同过滤，服务端分页 total 用）。"""
        import json

        keys: list[str] = []
        cursor = 0
        while True:
            cursor, batch = await self._redis.scan(cursor, match="collect_job:*", count=200)
            keys.extend(batch)
            if not cursor:
                break
        n = 0
        for key in keys:
            if isinstance(key, bytes):
                key = key.decode("utf-8", errors="replace")
            job_id = key.split(":", 1)[1]
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
