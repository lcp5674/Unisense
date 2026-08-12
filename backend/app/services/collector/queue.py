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

import uuid
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CollectionQueue(Protocol):
    """采集任务入队协议。"""

    async def enqueue(self, source_id: str, actor_id: int) -> str:
        """入队一次全量采集任务，返回 job_id。"""
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


class InMemoryCollectionQueue:
    """进程内采集队列 + 状态存储（默认实现 / 单测载体）。"""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    async def enqueue(self, source_id: str, actor_id: int) -> str:
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        self._jobs[job_id] = {
            "job_id": job_id,
            "source_id": source_id,
            "actor_id": actor_id,
            "status": "QUEUED",
            "detail": {},
        }
        return job_id

    async def set(self, job_id: str, status: str, detail: dict[str, Any]) -> None:
        job = self._jobs.setdefault(
            job_id, {"job_id": job_id, "source_id": "", "actor_id": 0}
        )
        job["status"] = status
        job["detail"] = detail

    async def get(self, job_id: str) -> dict[str, Any] | None:
        return self._jobs.get(job_id)


class RedisJobStore:
    """基于 Redis 的任务状态存储（生产实现，供 arq worker 与状态查询共用）。"""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    @staticmethod
    def _key(job_id: str) -> str:
        return f"collect_job:{job_id}"

    async def set(self, job_id: str, status: str, detail: dict[str, Any]) -> None:
        import json

        await self._redis.hset(
            self._key(job_id),
            mapping={
                "status": status,
                "detail": json.dumps(detail, ensure_ascii=False, default=str),
            },
        )

    async def get(self, job_id: str) -> dict[str, Any] | None:
        import json

        raw = await self._redis.hgetall(self._key(job_id))
        if not raw:
            return None
        detail = raw.get("detail")
        return {
            "job_id": job_id,
            "status": raw.get("status"),
            "detail": json.loads(detail) if detail else {},
        }


class ArqCollectionQueue:
    """基于 ``arq``（Redis）的生产采集队列（惰性导入 arq）。"""

    def __init__(self, redis_url: str | None = None, redis: Any | None = None) -> None:
        self._redis_url = redis_url
        self._redis = redis

    async def enqueue(self, source_id: str, actor_id: int) -> str:
        from arq import ArqRedis

        from app.core.config import settings

        redis = self._redis or ArqRedis.from_url(self._redis_url or settings.redis_url)
        job = await redis.enqueue_job(
            "run_collection_task",
            source_id,
            actor_id,
            _max_tries=3,
            _timeout=600,
        )
        job_id: str = job.job_id
        # FR-019: 不再调用 redis.close()，复用连接池
        return job_id


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
        return InMemoryCollectionQueue()
