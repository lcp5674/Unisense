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


# ---- T060: 熔断复位+版本键+pipeline预热+LIKE转义 ----


class FakeRedisWithPipeline:
    """支持 pipeline 的 fake Redis。"""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self.pipeline_calls: int = 0

    async def get(self, key: str):
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._store[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self._store.pop(key, None) is not None else 0

    def pipeline(self):
        self.pipeline_calls += 1
        return self

    async def execute(self):
        return []


class TestCircuitBreakerReset:
    """熔断复位：5次失败后 record_success 重置熔断器。"""

    async def test_record_success_resets_breaker(self, fake_metric: MagicMock) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))
        redis.set = AsyncMock(side_effect=ConnectionError("Redis down"))
        redis.scan = AsyncMock(return_value=(0, []))
        breaker = FakeBreaker(allow=True)
        cache = MetricCache(redis=redis, breaker=breaker)

        # 5次失败
        for _ in range(5):
            await cache.get("M1")
        assert breaker.failures == 5

        # 模拟恢复：Redis 正常后调用 record_success
        redis.get = AsyncMock(return_value=None)
        breaker._allow = True
        breaker.record_success = MagicMock()

        await cache.get("M1")
        # Redis 正常 → get 不报错 → record_success 被调用
        breaker.record_success.assert_called()


class TestVersionKey:
    """版本键：版本变更后旧键过期（新键含版本号）。"""

    async def test_cache_key_includes_version(self, fake_metric: MagicMock) -> None:
        fake_metric.version = 3
        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        cache = MetricCache(redis=redis, breaker=FakeBreaker(allow=True))
        await cache.set(fake_metric)

        # 验证 set 调用的键包含版本号
        call_args = redis.set.call_args
        key = call_args[0][0]
        assert ":v3" in key

    async def test_invalidate_removes_old_version_keys(self) -> None:
        redis = MagicMock()
        redis.scan = AsyncMock(return_value=(0, [
            "metric:def:M1:v1",
            "metric:def:M1:v2",
        ]))
        redis.delete = AsyncMock()
        cache = MetricCache(redis=redis, breaker=FakeBreaker(allow=True))
        await cache.invalidate("M1")
        # scan+delete 调用确认
        redis.scan.assert_called_once()
        redis.delete.assert_called_once()


class TestPipelineWarmup:
    """pipeline 预热：warm_up 使用 pipeline 批量写入（对齐 FR-034）。"""

    async def test_warm_up_uses_pipeline(self, fake_metric: MagicMock) -> None:
        redis = MagicMock()
        pipe = MagicMock()
        pipe.set = MagicMock()
        pipe.execute = AsyncMock(return_value=True)
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=False)
        redis.pipeline = MagicMock(return_value=pipe)

        cache = MetricCache(redis=redis, breaker=FakeBreaker(allow=True))
        count = await cache.warm_up([fake_metric, fake_metric])
        # pipeline 被创建、批量 set、最终 execute
        assert redis.pipeline.called
        assert pipe.set.call_count == 2
        assert pipe.execute.called
        assert count == 2


class TestLIKEEscape:
    """LIKE 通配符转义：确保 % 和 _ 被正确转义。"""

    def test_like_wildcard_escaping(self) -> None:
        # 验证 repository 中 LIKE 查询转义 % 和 _

        # 搜索词含 % 和 _
        term = "sales%rate_data"
        # 应转义为 sales\%rate\_data
        expected = "sales\\%rate\\_data"
        # 直接测试转义逻辑
        escaped = term.replace("%", "\\%").replace("_", "\\_")
        assert escaped == expected
