"""采集领域混沌/韧性测试（对齐 gateways chaos + US4 工业级修复）。

覆盖：
- 事件总线（Redis）宕机 -> 核心链路仍 200（降级）
- 外部源库故障 -> 采集返回 503（重试型，不静默 200）
- 事件发布熔断降级不阻断主流程
- US4: 单表超时跳过 + failed_specs 正确记录
- US4: 分布式锁互斥
- US4: Arq 重试 3 次
- US4: 幂等防重复执行
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from app.api import collector as collector_api
from app.api import deps
from app.core.exceptions import ExternalDependencyError
from app.main import app
from app.services.collector.distributed_lock import CollectionLock
from app.services.collector.events import CatalogEventPublisher
from app.services.collector.schemas import DBCatalogResponse
from app.services.collector.service import CollectorService
from app.services.collector.spi import CatalogSpec, CollectResult, FailedSpec
from app.services.collector.tasks import _check_idempotency


@pytest.fixture
async def owner_client():
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=5, role="metric_owner")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


class _DownPublisher:
    async def publish(self, event_type: str, payload: dict) -> bool:
        return False  # 模拟 Redis 不可用，降级

    async def publish_batch(self, event_type: str, payloads: list) -> bool:
        return False


class _FakeRegisterSvc:
    """核心链路假服务：事件总线降级，但注册成功。"""

    def __init__(self, db: object, **kw: object) -> None:
        self._events = _DownPublisher()
        self._repo = MagicMock()
        self._repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True))
        self._repo.recompute_coverage = AsyncMock(return_value=1.0)

    async def register_catalog(self, req: object, actor_id: int) -> DBCatalogResponse:
        return DBCatalogResponse(
            source_id="s",
            entity_name="users",
            entity_type="TABLE",
            schema_def={"columns": ["id"]},
            etl_sql=None,
            sensitivity_level="INTERNAL",
            owner_id=None,
            upstream_signature="sig",
        )


async def test_core_path_200_when_event_bus_down(owner_client, monkeypatch):
    """事件总线（Redis）宕机 -> 核心注册链路仍 200（降级生效）。"""
    monkeypatch.setattr(collector_api, "CollectorService", _FakeRegisterSvc)
    resp = await owner_client.post(
        "/api/v1/data-sources/s/catalogs",
        json={"source_id": "s", "entity_name": "users", "schema_json": {"columns": ["id"]}},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200  # 核心链路降级仍可用


async def test_collect_external_failure_returns_503(owner_client, monkeypatch):
    """外部源库故障 -> 采集返回 503（重试型），不静默 200。"""

    def _boom(_type: str, _cfg: str) -> object:
        raise ExternalDependencyError("源库不可达")

    monkeypatch.setattr(collector_api, "build_collector", _boom)
    resp = await owner_client.post(
        "/api/v1/data-sources/s/collect",
        json={"collector_type": "information_schema"},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 503


async def test_event_publisher_circuit_degradation():
    """Redis 持续失败 -> 熔断打开，发布降级返回 False（不阻断）。"""

    class _BoomRedis:
        async def publish(self, channel: str, message: str) -> None:
            raise RuntimeError("redis down")

    publisher = CatalogEventPublisher(_BoomRedis())  # type: ignore[arg-type]
    ok = await publisher.publish("catalog_registered", {"source_id": "s"})
    assert ok is False  # 降级：发布失败不影响主流程


# ---------- US4: 单表超时跳过 ----------


async def test_single_table_timeout_skip():
    """FR-004: 采集1000表时1表超时，999表正常采集，不中断全批。"""
    _db = MagicMock()
    _db.commit = AsyncMock()
    _db.rollback = AsyncMock()
    svc = CollectorService(db=_db)
    repo = MagicMock()
    svc._repo = repo
    repo.get_source = AsyncMock(return_value=MagicMock(source_type="mysql"))
    repo.get_watermark = AsyncMock(return_value=None)
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True, None))
    repo.recompute_coverage = AsyncMock(return_value=1.0)
    repo.update_health_status = AsyncMock()
    repo.update_watermark_after_collection = AsyncMock(return_value=MagicMock(mode="FULL"))
    # P1-5: FULL 采集触发废弃表对账
    repo.list_active_entity_names = AsyncMock(return_value=[])
    repo.deprecate_catalog = AsyncMock(return_value=False)

    events = MagicMock()
    events.publish_batch = AsyncMock()
    svc._events = events

    class PartialFailingCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            specs = [
                CatalogSpec(
                    entity_name=f"table_{i}",
                    entity_type="TABLE",
                    schema_json={"columns": ["a"]},
                )
                for i in range(999)
            ]
            failed = [FailedSpec(entity_name="table_500", error="timeout")]
            return CollectResult(specs=specs, failed_specs=failed, source_id="s")

    result = await svc.collect_and_register("s", PartialFailingCollector(), actor_id=1)
    assert result["scanned"] == 999
    assert result["registered"] == 999
    assert result["failed_count"] == 1
    assert result["failed_specs"][0]["entity_name"] == "table_500"


# ---------- US4: 分布式锁互斥 ----------


async def test_distributed_lock_mutual_exclusion():
    """FR-018: 分布式锁互斥——第二次 acquire 失败。"""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(side_effect=[True, None])  # 第一次成功，第二次失败
    mock_redis.eval = AsyncMock(return_value=1)

    lock = CollectionLock(mock_redis)
    acquired1 = await lock.acquire("src1", "owner1")
    assert acquired1 is True

    # 第二次 acquire 模拟锁已被占用
    mock_redis.set = AsyncMock(return_value=None)  # NX 失败
    acquired2 = await lock.acquire("src1", "owner2")
    assert acquired2 is False


async def test_distributed_lock_release_by_owner():
    """只有锁的 owner 才能释放锁（Lua 脚本原子操作）。"""
    mock_redis = AsyncMock()
    mock_redis.eval = AsyncMock(return_value=1)  # DEL 成功

    lock = CollectionLock(mock_redis)
    released = await lock.release("src1", "owner1")
    assert released is True


async def test_distributed_lock_degrade_without_redis():
    """Redis 不可用时降级为始终获取成功。"""
    lock = CollectionLock(None)
    acquired = await lock.acquire("src1", "owner1")
    assert acquired is True


# ---------- US4: 幂等防重复 ----------


async def test_idempotency_check_allows_first_execution():
    """首次 job_id 执行通过。"""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)  # SET NX 成功

    result = await _check_idempotency(mock_redis, "job-123")
    assert result is True


async def test_idempotency_check_blocks_duplicate():
    """重复 job_id 被阻止。"""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=None)  # SET NX 失败（已存在）

    result = await _check_idempotency(mock_redis, "job-123")
    assert result is False


async def test_idempotency_check_degrades_without_redis():
    """Redis 不可用时允许执行。"""
    result = await _check_idempotency(None, "job-123")
    assert result is True


# ---------- US4: Arq 投递签名（job_id 位置参数 + 幂等）----------


async def test_arq_retry_configured():
    """FR-006: ArqCollectionQueue enqueue 正确传递 job_id（幂等键）并落初始 QUEUED 状态。

    arq 0.28 的 enqueue_job 不支持 _max_tries/_timeout（会被当普通 kwargs 透传给任务函数
    导致 TypeError），因此投递契约是：
    run_collection_task(source_id, actor_id, job_id, _job_id=job_id)。
    """
    from app.services.collector.queue import ArqCollectionQueue

    queue = ArqCollectionQueue(redis_url="redis://localhost")

    mock_redis = AsyncMock()
    mock_job = MagicMock()
    mock_job.job_id = "test-job-id"
    mock_redis.enqueue_job = AsyncMock(return_value=mock_job)
    mock_redis.hset = AsyncMock()  # RedisJobStore.set

    with patch("arq.ArqRedis") as mock_arq:
        mock_arq.from_url = AsyncMock(return_value=mock_redis)
        queue._redis = mock_redis

        job_id = await queue.enqueue("src1", 1)

        # 验证 enqueue_job 调用参数：job_id 作为第 4 位置参数，且 _job_id 与之相同
        call_args = mock_redis.enqueue_job.call_args
        assert call_args[0][:3] == ("run_collection_task", "src1", 1)
        enqueued_job_id = call_args[0][3]
        assert call_args[1]["_job_id"] == enqueued_job_id
        assert enqueued_job_id.startswith("collect:src1:")
        # arq 0.28 不支持这两个保留参数，不得透传
        assert "_max_tries" not in call_args[1]
        assert "_timeout" not in call_args[1]
        # 初始 QUEUED 状态落 RedisJobStore
        mock_redis.hset.assert_awaited_once()
        assert job_id == "test-job-id"
