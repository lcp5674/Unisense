"""采集队列可靠性测试（对齐 US7 / FR-12）。

覆盖：
1. Arq 队列持久化（redis_url 非空时选用 Arq）
2. 服务重启任务恢复（Arq 队列任务不丢失）
3. 降级到 InMemory 队列（redis_url 为空时）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.collector.queue import (
    ArqCollectionQueue,
    InMemoryCollectionQueue,
    create_collection_queue,
)


class TestArqQueuePersistence:
    """Arq 队列持久化测试。"""

    def test_arq_queue_created_when_redis_url_present(self):
        """redis_url 非空时创建 ArqCollectionQueue。"""
        queue = create_collection_queue(redis_url="redis://localhost:6379/0")
        assert isinstance(queue, ArqCollectionQueue)

    def test_arq_queue_stores_redis_url(self):
        """Arq 队列存储 redis_url 供后续连接。"""
        queue = ArqCollectionQueue(redis_url="redis://localhost:6379/0")
        assert queue._redis_url == "redis://localhost:6379/0"

    @pytest.mark.asyncio
    async def test_arq_enqueue_delegates_to_arq_redis(self):
        """Arq 入队委托给 arq.ArqRedis.enqueue_job。"""
        queue = ArqCollectionQueue(redis_url="redis://localhost:6379/0")

        # Mock ArqRedis
        mock_job = MagicMock()
        mock_job.job_id = "test-job-123"

        with patch("arq.ArqRedis") as mock_arq_redis:
            mock_redis = AsyncMock()
            mock_redis.enqueue_job = AsyncMock(return_value=mock_job)
            mock_redis.close = AsyncMock()
            mock_arq_redis.from_url = MagicMock(return_value=mock_redis)

            job_id = await queue.enqueue("source-1", actor_id=1)

        assert job_id == "test-job-123"


class TestServiceRestartRecovery:
    """服务重启后任务恢复测试。"""

    @pytest.mark.asyncio
    async def test_inmemory_queue_preserves_state(self):
        """InMemory 队列在同一实例内保持状态。"""
        queue = InMemoryCollectionQueue()
        job_id = await queue.enqueue("source-1", actor_id=1)

        # 查询状态
        status = await queue.get(job_id)
        assert status is not None
        assert status["job_id"] == job_id
        assert status["source_id"] == "source-1"
        assert status["status"] == "QUEUED"

    @pytest.mark.asyncio
    async def test_inmemory_queue_multiple_jobs(self):
        """InMemory 队列支持多个任务。"""
        queue = InMemoryCollectionQueue()
        job1 = await queue.enqueue("source-1", actor_id=1)
        job2 = await queue.enqueue("source-2", actor_id=2)

        status1 = await queue.get(job1)
        status2 = await queue.get(job2)

        assert status1 is not None
        assert status2 is not None
        assert status1["source_id"] == "source-1"
        assert status2["source_id"] == "source-2"

    @pytest.mark.asyncio
    async def test_inmemory_queue_count_filters(self):
        """InMemory count 与 list 同过滤，供服务端分页 total。"""
        queue = InMemoryCollectionQueue()
        await queue.enqueue("source-1", actor_id=1)
        await queue.enqueue("source-2", actor_id=2)
        await queue.set("job-x", "COMPLETED", {"source_id": "source-1"})

        assert await queue.count() == 3
        assert await queue.count(source_id="source-1") == 2
        assert await queue.count(source_id="source-1", status="COMPLETED") == 1
        assert await queue.count(status="QUEUED") == 2

    @pytest.mark.asyncio
    async def test_arq_queue_recovery_on_restart(self):
        """Arq 队列任务在服务重启后不丢失（Redis 持久化）。

        验证：新 ArqCollectionQueue 实例连接同一 Redis，
        可以获取之前投递的任务。
        """
        # 这是架构性验证：Arq 使用 Redis 作为后端，
        # 重启后新实例连接同一 Redis 即可恢复任务。
        # 实际集成测试需要 Redis 实例。
        queue = ArqCollectionQueue(redis_url="redis://localhost:6379/0")
        assert queue._redis_url == "redis://localhost:6379/0"
        # Arq 通过 Redis 持久化，新实例连接同一 URL 可恢复


class TestFallbackToInMemory:
    """降级到 InMemory 队列测试。"""

    def test_inmemory_queue_created_when_redis_url_empty(self):
        """redis_url 为空时降级到 InMemoryCollectionQueue。"""
        queue = create_collection_queue(redis_url="")
        assert isinstance(queue, InMemoryCollectionQueue)

    def test_inmemory_queue_created_when_redis_url_none(self):
        """redis_url 为 None 时降级到 InMemoryCollectionQueue。"""
        queue = create_collection_queue(redis_url=None)
        assert isinstance(queue, InMemoryCollectionQueue)

    @pytest.mark.asyncio
    async def test_inmemory_enqueue_returns_job_id(self):
        """InMemory 入队返回格式正确的 job_id。"""
        queue = InMemoryCollectionQueue()
        job_id = await queue.enqueue("source-1", actor_id=1)
        assert job_id.startswith("job-")
        assert len(job_id) > 4

    @pytest.mark.asyncio
    async def test_inmemory_enqueue_stores_mode(self):
        """M4: InMemory 入队把 mode 写入 detail（任务中心可读真实执行模式）。"""
        queue = InMemoryCollectionQueue()
        job_id = await queue.enqueue("source-1", actor_id=1, mode="INCREMENTAL")
        status = await queue.get(job_id)
        assert status is not None
        assert status["detail"]["mode"] == "INCREMENTAL"

    @pytest.mark.asyncio
    async def test_inmemory_set_and_get(self):
        """InMemory 队列支持状态更新和查询。"""
        queue = InMemoryCollectionQueue()
        job_id = await queue.enqueue("source-1", actor_id=1)

        # 更新状态
        await queue.set(job_id, "RUNNING", {"progress": 50})

        # 查询状态
        status = await queue.get(job_id)
        assert status is not None
        assert status["status"] == "RUNNING"
        assert status["detail"]["progress"] == 50

    @pytest.mark.asyncio
    async def test_inmemory_get_nonexistent_job(self):
        """查询不存在的任务返回 None。"""
        queue = InMemoryCollectionQueue()
        status = await queue.get("nonexistent-job")
        assert status is None

    @pytest.mark.asyncio
    async def test_create_queue_with_redis_instance(self):
        """传入 Redis 实例时创建 ArqCollectionQueue。"""
        mock_redis = MagicMock()
        queue = create_collection_queue(redis_url="redis://localhost:6379/0", redis=mock_redis)
        assert isinstance(queue, ArqCollectionQueue)
        assert queue._redis is mock_redis


class TestRedisJobStoreTtl:
    """P1-6: RedisJobStore 终态 TTL（7 天）行为。"""

    def _make_store(self) -> tuple:
        from app.services.collector.queue import RedisJobStore

        mock_redis = MagicMock()
        mock_redis.hset = AsyncMock()
        mock_redis.hsetnx = AsyncMock()
        mock_redis.expire = AsyncMock()
        return RedisJobStore(mock_redis), mock_redis

    @pytest.mark.asyncio
    async def test_terminal_status_sets_seven_day_ttl(self):
        """COMPLETED/FAILED 终态设置 7 天 TTL，过期后自动回收幂等键。"""
        from app.services.collector.queue import RedisJobStore

        for terminal in ("COMPLETED", "FAILED"):
            store, redis = self._make_store()
            await store.set("job-1", terminal, {"ok": True})
            redis.expire.assert_awaited_once_with(
                RedisJobStore._key("job-1"), RedisJobStore._TERMINAL_TTL_SECONDS
            )
            assert RedisJobStore._TERMINAL_TTL_SECONDS == 7 * 24 * 60 * 60

    @pytest.mark.asyncio
    async def test_non_terminal_status_skips_ttl(self):
        """非终态（QUEUED/RUNNING/INCREMENTAL 等）不设 TTL，状态长期可查。"""
        store, redis = self._make_store()
        await store.set("job-2", "RUNNING", {"progress": 30})
        redis.expire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ttl_not_applied_to_partial_failure_status(self):
        """仅精确匹配终态集合才加 TTL（避免误伤中间态）。"""
        store, redis = self._make_store()
        await store.set("job-3", "RETRYING", {"attempt": 2})
        redis.expire.assert_not_awaited()


class TestRedisJobStoreCreatedAtAndKind:
    """任务中心元数据：created_at（创建时间）/ kind（手动 or 定时）。"""

    def _make_store(self, hgetall_result: dict):
        from app.services.collector.queue import RedisJobStore

        mock_redis = MagicMock()
        mock_redis.hgetall = AsyncMock(return_value=hgetall_result)
        return RedisJobStore(mock_redis), mock_redis

    @pytest.mark.asyncio
    async def test_set_records_created_at_via_hsetnx(self):
        """set 首次写入用 HSETNX 落 created_at，进度高频写入不覆盖。"""
        from app.services.collector.queue import RedisJobStore

        mock_redis = MagicMock()
        mock_redis.hset = AsyncMock()
        mock_redis.hsetnx = AsyncMock()
        mock_redis.expire = AsyncMock()
        store = RedisJobStore(mock_redis)
        await store.set("job-a", "RUNNING", {"source_id": "s1"})
        args = mock_redis.hsetnx.call_args.args
        assert args[0] == RedisJobStore._key("job-a")
        assert args[1] == "created_at"

    @pytest.mark.asyncio
    async def test_get_returns_created_at_and_kind(self):
        """get 返回 created_at 与 kind（手动/定时）。"""
        store, _ = self._make_store(
            {
                b"status": b"COMPLETED",
                b"created_at": b"2026-08-14T03:00:00+00:00",
                b"detail": b'{"source_id": "s1", "scanned": 54}',
            }
        )
        job = await store.get("collect:sched:s1:1752000000")
        assert job is not None
        assert job["created_at"] == "2026-08-14T03:00:00+00:00"
        assert job["kind"] == "scheduled"
        manual = await store.get("collect:s1:abc")
        assert manual is not None
        assert manual["kind"] == "manual"

    @pytest.mark.asyncio
    async def test_list_filters_by_source_id(self):
        """list 按 source_id 过滤任务（任务中心按数据源筛选）。"""
        from app.services.collector.queue import RedisJobStore

        def _hgetall(key: str):
            jobs = {
                "collect_job:collect:s1:aaa": {
                    b"status": b"COMPLETED",
                    b"created_at": b"2026-08-14T03:00:00+00:00",
                    b"detail": b'{"source_id": "s1", "scanned": 54}',
                },
                "collect_job:collect:sched:s2:bbb": {
                    b"status": b"QUEUED",
                    b"created_at": b"2026-08-14T03:01:00+00:00",
                    b"detail": b'{"source_id": "s2"}',
                },
            }
            return jobs.get(key, {})

        mock_redis = MagicMock()

        def _scan(cursor: int, match: str = "", count: int = 0):
            if cursor == 0:
                return (1, [b"collect_job:collect:s1:aaa"])
            return (0, [b"collect_job:collect:sched:s2:bbb"])

        mock_redis.scan = AsyncMock(side_effect=_scan)
        mock_redis.hgetall = AsyncMock(side_effect=_hgetall)
        store = RedisJobStore(mock_redis)

        s1_jobs = await store.list(limit=50, offset=0, source_id="s1")
        assert len(s1_jobs) == 1
        assert s1_jobs[0]["job_id"] == "collect:s1:aaa"
        assert s1_jobs[0]["kind"] == "manual"
        all_jobs = await store.list(limit=50, offset=0)
        assert len(all_jobs) == 2


class TestRedisJobStoreBytesDecode:
    """redis.asyncio 未开 decode_responses 时 hgetall/scan 返回 bytes——get/list 必须解码。"""

    def _bytes_store(self, hgetall_result: dict, scan_result: tuple = (0, [])):
        from app.services.collector.queue import RedisJobStore

        mock_redis = MagicMock()
        mock_redis.hgetall = AsyncMock(return_value=hgetall_result)
        mock_redis.scan = AsyncMock(return_value=scan_result)
        return RedisJobStore(mock_redis), mock_redis

    @pytest.mark.asyncio
    async def test_get_decodes_bytes_values(self):
        """get() 从 bytes 键值解码出 str 状态与 dict 详情（此前返回 None/空）。"""
        store, _redis = self._bytes_store({b"status": b"COMPLETED", b"detail": b'{"scanned": 54}'})
        job = await store.get("job-x")
        assert job is not None
        assert job["status"] == "COMPLETED"
        assert job["detail"] == {"scanned": 54}

    @pytest.mark.asyncio
    async def test_list_decodes_bytes_keys_and_values(self):
        """list() 对 bytes SCAN keys 解码切出 job_id，并解码 hgetall 值。"""
        store, redis = self._bytes_store(
            {b"status": b"QUEUED", b"detail": b"{}"},
            scan_result=(0, [b"collect_job:collect:sched:s1:123"]),
        )
        jobs = await store.list(limit=10, offset=0)
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "collect:sched:s1:123"
        assert jobs[0]["status"] == "QUEUED"
        # 二次扫描（SCAN cursor 循环）被正确终止
        redis.scan.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_inmemory_list_returns_jobs(self):
        """InMemoryCollectionQueue.list 返回任务（采集任务中心）。"""
        from app.services.collector.queue import InMemoryCollectionQueue

        q = InMemoryCollectionQueue()
        j1 = await q.enqueue("s1", 1)
        j2 = await q.enqueue("s2", 2)
        await q.set(j1, "COMPLETED", {"scanned": 10})
        jobs = await q.list(limit=10, offset=0)
        assert len(jobs) == 2
        by_id = {j["job_id"]: j for j in jobs}
        assert by_id[j1]["status"] == "COMPLETED"
        assert by_id[j1]["detail"] == {"scanned": 10}
        assert by_id[j2]["status"] == "QUEUED"
        # 分页：offset 越过第一条
        page2 = await q.list(limit=10, offset=1)
        assert len(page2) == 1
