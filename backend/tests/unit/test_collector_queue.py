"""采集异步队列单测（Epic B：全量采集接采集队列）。

验证：入队返回 job_id、任务体 run_collection_task 在注入 ctx 下调用
collect_and_register 并回写任务状态、以及 schedule_collection/get_job_status 联动。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.collector.queue import InMemoryCollectionQueue
from app.services.collector.service import CollectorService
from app.services.collector.tasks import run_collection_task


class _FakeCollector:
    """测试采集器：返回两条规格，验证 collect_and_register 被调用。"""

    def __init__(self) -> None:
        self.collected = False

    async def collect(self, source: object) -> list:
        self.collected = True
        spec_cls = type(
            "CatalogSpec",
            (),
            {
                "entity_name": "t",
                "entity_type": "TABLE",
                "schema_json": {"columns": ["id"]},
                "etl_sql": None,
            },
        )
        s = spec_cls()
        return [s, s]

    async def dispose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_queue_enqueue_returns_job_id() -> None:
    q = InMemoryCollectionQueue()
    job_id = await q.enqueue("src1", 1)
    assert job_id.startswith("job-")
    status = await q.get(job_id)
    assert status is not None
    assert status["status"] == "QUEUED"
    assert status["source_id"] == "src1"


def _stub_svc() -> CollectorService:
    svc = CollectorService(db=MagicMock())
    repo = MagicMock()
    repo.get_source = AsyncMock(return_value=MagicMock())
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True))
    repo.recompute_coverage = AsyncMock(return_value=1.0)
    svc._repo = repo
    svc._events = MagicMock()
    svc._events.publish = AsyncMock()
    svc._classifier = MagicMock()
    svc._classifier.classify.return_value = "INTERNAL"
    return svc


@pytest.mark.asyncio
async def test_run_collection_task_completes_and_writes_status() -> None:
    collector = _FakeCollector()
    store = InMemoryCollectionQueue()

    # 使用 MagicMock 作为 db，但确保 ctx 中有 svc 和 collector，避免真实 DB 连接
    ctx = {"svc": _stub_svc(), "collector": collector, "job_store": store, "db": MagicMock()}
    result = await run_collection_task(ctx, "src1", 7, "job-abc")

    assert result["registered"] == 2
    assert collector.collected is True
    status = await store.get("job-abc")
    assert status["status"] == "COMPLETED"
    assert status["detail"]["registered"] == 2


@pytest.mark.asyncio
async def test_run_collection_task_writes_failed_on_error() -> None:
    collector = _FakeCollector()

    def _boom(*a, **k):
        raise RuntimeError("采集失败")

    collector.collect = _boom  # type: ignore[assignment]
    store = InMemoryCollectionQueue()
    ctx = {"svc": _stub_svc(), "collector": collector, "job_store": store, "db": MagicMock()}

    with pytest.raises(RuntimeError):
        await run_collection_task(ctx, "src1", 7, "job-fail")

    status = await store.get("job-fail")
    assert status["status"] == "FAILED"
    assert "采集失败" in status["detail"]["error"]


@pytest.mark.asyncio
async def test_service_schedule_and_status_share_queue() -> None:
    q = InMemoryCollectionQueue()
    svc = CollectorService(db=MagicMock())
    svc._repo = MagicMock()
    svc._repo.get_source = AsyncMock(return_value=MagicMock())

    job_id = await svc.schedule_collection("src1", 1, queue=q)
    status = await svc.get_job_status(job_id, queue=q)
    assert status is not None
    assert status["status"] == "QUEUED"
    assert status["source_id"] == "src1"


@pytest.mark.asyncio
async def test_schedule_raises_for_missing_source() -> None:
    from app.core.exceptions import NotFoundError

    q = InMemoryCollectionQueue()
    svc = CollectorService(db=MagicMock())
    svc._repo = MagicMock()
    svc._repo.get_source = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError):
        await svc.schedule_collection("missing", 1, queue=q)
