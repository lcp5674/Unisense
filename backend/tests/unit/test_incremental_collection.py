"""增量采集单元测试（对齐 US3 / spec FR-012/FR-014）。

覆盖：
- 增量 SQL 生成（MySQL/ClickHouse 支持，Doris/StarRocks/PostgreSQL/Hive/Kafka 不支持）
- 水位保存与更新
- 降级为全量的条件判定
- 水印更新后采集结果正确
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.collector.incremental import (
    IncrementalCollectorMixin,
    build_incremental_query,
    should_degrade_to_full,
    supports_incremental,
)
from app.services.collector.repository import CollectorRepository
from app.services.collector.service import CollectorService
from app.services.collector.spi import CatalogSpec, CollectResult

# ---------- 增量支持判定 ----------


def test_mysql_supports_incremental():
    assert supports_incremental("mysql") is True


def test_clickhouse_supports_incremental():
    assert supports_incremental("clickhouse") is True


def test_postgres_not_supports_incremental():
    assert supports_incremental("postgres") is False


def test_hive_not_supports_incremental():
    assert supports_incremental("hive") is False


def test_kafka_not_supports_incremental():
    assert supports_incremental("kafka") is False


def test_doris_not_supports_incremental():
    """Doris information_schema 无 UPDATE_TIME，不支持增量（Decision 2）。"""
    assert supports_incremental("doris") is False


def test_starrocks_not_supports_incremental():
    """StarRocks 同 Doris，不支持增量。"""
    assert supports_incremental("starrocks") is False


# ---------- 增量 SQL 生成 ----------


def test_mysql_incremental_query_with_watermark():
    ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    sql = build_incremental_query("mysql", "mydb", ts)
    assert sql is not None
    assert "UPDATE_TIME" in sql
    assert ":watermark" in sql


def test_mysql_incremental_query_without_watermark():
    """水位缺失时返回 None（降级为全量）。"""
    sql = build_incremental_query("mysql", "mydb", None)
    assert sql is None


def test_clickhouse_incremental_query_with_watermark():
    ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    sql = build_incremental_query("clickhouse", "mydb", ts)
    assert sql is not None
    assert "metadata_modification_time" in sql
    assert "2026-08-01" in sql


def test_doris_incremental_query_returns_none():
    """Doris 不支持增量，返回 None。"""
    ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    sql = build_incremental_query("doris", "mydb", ts)
    assert sql is None


def test_postgres_incremental_query_returns_none():
    ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    sql = build_incremental_query("postgres", "mydb", ts)
    assert sql is None


# ---------- 降级判定 ----------


def test_should_degrade_unsupported_type():
    assert should_degrade_to_full("postgres") is True
    assert should_degrade_to_full("hive") is True


def test_should_degrade_no_watermark():
    """MySQL 支持增量但无水位时降级。"""
    assert should_degrade_to_full("mysql", None) is True


def test_should_not_degrade_with_watermark():
    """MySQL 有水位时不降级。"""
    ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    assert should_degrade_to_full("mysql", ts) is False


# ---------- IncrementalCollectorMixin ----------


def test_mixin_get_incremental_tables_query():
    mixin = IncrementalCollectorMixin()
    ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    sql = mixin.get_incremental_tables_query("mysql", "mydb", ts)
    assert sql is not None
    assert "UPDATE_TIME" in sql


def test_mixin_is_incremental_degrade():
    mixin = IncrementalCollectorMixin()
    assert mixin.is_incremental_degrade("postgres") is True
    assert mixin.is_incremental_degrade("mysql", None) is True
    ts = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    assert mixin.is_incremental_degrade("mysql", ts) is False


# ---------- 水位保存与更新（仓储层） ----------


def _session(scalar_one_or_none=None) -> MagicMock:
    s = MagicMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = scalar_one_or_none
    s.execute = AsyncMock(return_value=res)
    s.add = MagicMock()
    s.flush = AsyncMock()
    s.commit = AsyncMock()
    return s


async def test_repo_get_watermark_none():
    repo = CollectorRepository(_session(scalar_one_or_none=None))
    result = await repo.get_watermark("nonexistent")
    assert result is None


async def test_repo_update_watermark_creates_when_missing():
    """首次采集创建水位记录。"""
    repo = CollectorRepository(_session(scalar_one_or_none=None))
    watermark = await repo.update_watermark_after_collection(
        source_id="src1",
        mode="FULL",
        scanned_count=10,
        failed_count=0,
    )
    assert watermark.source_id == "src1"
    assert watermark.mode == "FULL"
    assert watermark.scanned_count == 10
    assert watermark.failed_count == 0


async def test_repo_update_watermark_updates_existing():
    """已有水位时更新记录。"""
    existing = MagicMock()
    existing.source_id = "src1"
    existing.last_collected_at = datetime(2026, 1, 1, tzinfo=UTC)
    existing.mode = "FULL"
    existing.scanned_count = 5
    existing.failed_count = 0
    repo = CollectorRepository(_session(scalar_one_or_none=existing))
    watermark = await repo.update_watermark_after_collection(
        source_id="src1",
        mode="INCREMENTAL",
        scanned_count=3,
        failed_count=1,
    )
    assert watermark.mode == "INCREMENTAL"
    assert watermark.scanned_count == 3
    assert watermark.failed_count == 1


# ---------- 服务层增量降级集成 ----------


async def test_service_incremental_degrades_to_full_for_unsupported():
    """PostgreSQL 请求增量采集时自动降级为全量。"""
    with patch("app.services.collector.service.CollectorRepository") as mock_repo:
        svc = CollectorService(db=_session())
        repo = mock_repo.return_value
        repo.get_source = AsyncMock(return_value=MagicMock(source_type="postgres"))
        repo.get_watermark = AsyncMock(return_value=None)
        repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True, None))
        repo.recompute_coverage = AsyncMock(return_value=1.0)
        repo.update_health_status = AsyncMock()
        repo.update_watermark_after_collection = AsyncMock(return_value=MagicMock(mode="FULL"))
        # P1-5: 降级为 FULL 后触发废弃表对账
        repo.list_active_entity_names = AsyncMock(return_value=[])
        repo.deprecate_catalog = AsyncMock(return_value=False)

        events = MagicMock()
        events.publish_batch = AsyncMock()
        events.publish = AsyncMock()  # 降级事件 publish("collect_degraded")
        svc._events = events

        class StubCollector:
            def set_incremental_context(self, mode: str, watermark: object) -> None:
                self._incremental_mode = mode
                self._incremental_watermark = watermark

            async def collect(self, source: object) -> CollectResult:
                return CollectResult(
                    specs=[
                        CatalogSpec(
                            entity_name="t1",
                            entity_type="TABLE",
                            schema_json={"columns": ["a"]},
                        )
                    ],
                    failed_specs=[],
                    source_id="s",
                )

        result = await svc.collect_and_register(
            "s", StubCollector(), actor_id=1, mode="INCREMENTAL"
        )
        assert result["mode"] == "FULL"  # 降级为全量


async def test_service_incremental_stays_incremental_for_mysql_with_watermark():
    """MySQL 有水位时保持增量模式。"""
    with patch("app.services.collector.service.CollectorRepository") as mock_repo:
        svc = CollectorService(db=_session())
        repo = mock_repo.return_value
        src_mock = MagicMock(source_type="mysql")
        repo.get_source = AsyncMock(return_value=src_mock)
        watermark = MagicMock()
        watermark.last_collected_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
        repo.get_watermark = AsyncMock(return_value=watermark)
        repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True, None))
        repo.recompute_coverage = AsyncMock(return_value=1.0)
        repo.update_health_status = AsyncMock()
        repo.update_watermark_after_collection = AsyncMock(
            return_value=MagicMock(mode="INCREMENTAL")
        )

        events = MagicMock()
        events.publish_batch = AsyncMock()
        events.publish = AsyncMock()  # 降级事件 publish("collect_degraded")
        svc._events = events

        class StubCollector:
            def set_incremental_context(self, mode: str, watermark: object) -> None:
                self._incremental_mode = mode
                self._incremental_watermark = watermark

            async def collect(self, source: object) -> CollectResult:
                return CollectResult(
                    specs=[
                        CatalogSpec(
                            entity_name="t1",
                            entity_type="TABLE",
                            schema_json={"columns": ["a"]},
                        )
                    ],
                    failed_specs=[],
                    source_id="s",
                )

        result = await svc.collect_and_register(
            "s", StubCollector(), actor_id=1, mode="INCREMENTAL"
        )
        assert result["mode"] == "INCREMENTAL"  # 保持增量


# ---------- should_mix_in 阈值可配置化（遗留问题修复） ----------


def test_should_mix_in_honors_ratio_threshold():
    """阈值可配置：低于配置阈值降级全量，高于则保持增量。

    X-5：should_mix_in 改原生 async（此前同步版在 running loop 上
    run_until_complete 恒抛 RuntimeError 被吞返 False，静默漏采）。
    """
    import asyncio

    from app.services.collector.incremental import should_mix_in

    async def _run() -> tuple[bool, bool, bool]:
        connector = MagicMock()
        # ratio = 2/8 = 0.25（25% 表有 UPDATE_TIME）
        connector.query = AsyncMock(return_value=[{"total": 8, "with_time": 2}])
        # 默认阈值 0.1 → 0.25 >= 0.1 不降级
        default_res = await should_mix_in("mysql", connector)
        # 显式阈值 0.3 → 0.25 < 0.3 降级全量
        explicit_res = await should_mix_in("mysql", connector, ratio_threshold=0.3)
        # 非 mysql 源不降级
        non_mysql_res = await should_mix_in("postgres", connector, ratio_threshold=0.3)
        return default_res, explicit_res, non_mysql_res

    default_res, explicit_res, non_mysql_res = asyncio.new_event_loop().run_until_complete(_run())
    assert default_res is False
    assert explicit_res is True
    assert non_mysql_res is False


def test_should_mix_in_ratio_function():
    """_get_mysql_update_time_ratio_async 正确计算占比。"""
    import asyncio

    from app.services.collector.incremental import _get_mysql_update_time_ratio_async

    connector = MagicMock()
    connector.query = AsyncMock(return_value=[{"total": 3, "with_time": 2}])
    ratio = asyncio.new_event_loop().run_until_complete(
        _get_mysql_update_time_ratio_async(connector)
    )
    assert ratio == 2 / 3
