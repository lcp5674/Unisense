"""semantic 缓存单测（补齐覆盖率）。

针对 semantic/cache.py 的 52% 覆盖率，补充以下场景：
- get: 命中/未命中/禁用/熔断打开/Redis 异常/JSON 解析失败
- set: 正常写入/禁用/熔断打开/Redis 异常
- invalidate: 正常失效/禁用/Redis 异常
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.semantic.cache import MetricCache


class FakeBreaker:
    """可控制 allow/record_failure 的熔断器替身。"""

    def __init__(self, allow: bool = True) -> None:
        self._allow = allow
        self.failures = 0

    def allow(self) -> bool:
        return self._allow

    def record_failure(self) -> None:
        self.failures += 1

    def record_success(self) -> None:
        pass


@pytest.fixture
def fake_metric() -> MagicMock:
    m = MagicMock()
    m.metric_code = "M1"
    m.name = "GMV"
    m.domain = "sales"
    m.type = "atomic"
    m.granularity = "daily"
    m.unit = "yuan"
    m.currency = "CNY"
    m.aggregation = "SUM"
    m.time_semantics = "PERIOD"
    m.freshness = "T1"
    m.sla = "06:00"
    m.dw_layer = "DWD"
    m.metric_tier = "T3"
    m.serving_mode = "BATCH_ONLY"
    m.additivity = "ADDITIVE"
    m.non_additive_dimensions = None
    m.definition_json = {"expression": "SUM(amount)"}
    m.version = 1
    m.row_version = 1
    m.term_id = None
    m.status = "PUBLISHED"
    m.owner_id = 1
    m.backup_owner_id = None
    m.approver_id = None
    m.pii_flag = False
    m.compliance_reviewed = False
    m.effective_version = None
    m.consumption_guide = None
    m.batch_id = None
    m.successor_code = None
    m.deprecated_at = None
    m.sunset_until = None
    m.id = 1
    m.created_at = datetime.now(UTC)
    m.updated_at = datetime.now(UTC)
    m.emergency_publish = False
    m.emergency_reason = None
    m.pending_conflict = False
    m.pending_conflict_detail = None
    m.deleted_at = None
    return m


class TestMetricCacheGet:
    async def test_get_disabled_returns_none(self) -> None:
        cache = MetricCache(redis=None)
        result = await cache.get("M1")
        assert result is None

    async def test_get_breaker_open_returns_none(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        breaker = FakeBreaker(allow=False)
        cache = MetricCache(redis=redis, breaker=breaker)
        result = await cache.get("M1")
        assert result is None
        redis.get.assert_not_called()

    async def test_get_hit(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=json.dumps({"metric_code": "M1", "name": "GMV"}))
        breaker = FakeBreaker(allow=True)
        cache = MetricCache(redis=redis, breaker=breaker)
        result = await cache.get("M1")
        assert result is not None
        assert result["metric_code"] == "M1"

    async def test_get_miss(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        breaker = FakeBreaker(allow=True)
        cache = MetricCache(redis=redis, breaker=breaker)
        result = await cache.get("M1")
        assert result is None

    async def test_get_redis_error_records_failure(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))
        breaker = FakeBreaker(allow=True)
        cache = MetricCache(redis=redis, breaker=breaker)
        result = await cache.get("M1")
        assert result is None
        assert breaker.failures == 1

    async def test_get_bad_json_returns_none(self) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value="not-json{")
        breaker = FakeBreaker(allow=True)
        cache = MetricCache(redis=redis, breaker=breaker)
        result = await cache.get("M1")
        assert result is None


class TestMetricCacheSet:
    async def test_set_disabled_noop(self, fake_metric: MagicMock) -> None:
        cache = MetricCache(redis=None)
        await cache.set(fake_metric)
        # 无异常即通过

    async def test_set_breaker_open_noop(self, fake_metric: MagicMock) -> None:
        redis = MagicMock()
        redis.set = AsyncMock()
        breaker = FakeBreaker(allow=False)
        cache = MetricCache(redis=redis, breaker=breaker)
        await cache.set(fake_metric)
        redis.set.assert_not_called()

    async def test_set_success(self, fake_metric: MagicMock) -> None:
        redis = MagicMock()
        redis.set = AsyncMock()
        breaker = FakeBreaker(allow=True)
        cache = MetricCache(redis=redis, breaker=breaker)
        await cache.set(fake_metric)
        redis.set.assert_called_once()

    async def test_set_redis_error_records_failure(self, fake_metric: MagicMock) -> None:
        redis = MagicMock()
        redis.set = AsyncMock(side_effect=ConnectionError("Redis down"))
        breaker = FakeBreaker(allow=True)
        cache = MetricCache(redis=redis, breaker=breaker)
        await cache.set(fake_metric)
        assert breaker.failures == 1


class TestMetricCacheInvalidate:
    async def test_invalidate_disabled_noop(self) -> None:
        cache = MetricCache(redis=None)
        await cache.invalidate("M1")

    async def test_invalidate_success(self) -> None:
        redis = MagicMock()
        redis.delete = AsyncMock()
        redis.scan = AsyncMock(return_value=(0, ["metric:def:M1:v1"]))
        cache = MetricCache(redis=redis, breaker=FakeBreaker(allow=True))
        await cache.invalidate("M1")
        redis.delete.assert_called_once()

    async def test_invalidate_redis_error_silent(self) -> None:
        redis = MagicMock()
        redis.delete = AsyncMock(side_effect=ConnectionError("Redis down"))
        cache = MetricCache(redis=redis, breaker=FakeBreaker(allow=True))
        # 不应抛异常
        await cache.invalidate("M1")


class TestMetricCacheFromDefaults:
    def test_from_defaults_with_redis(self) -> None:
        redis = MagicMock()
        cache = MetricCache.from_defaults(redis)
        assert cache._enabled is True

    def test_from_defaults_without_redis(self) -> None:
        cache = MetricCache.from_defaults(None)
        assert cache._enabled is False
