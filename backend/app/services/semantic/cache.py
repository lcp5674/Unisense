"""指标读缓存（cache-aside）+ 熔断舱壁。

对齐 TD §11（韧性）/ DEV_GUIDE §17.2（缓存策略）：读多写少的指标定义用 Redis
缓存热点对象，写操作（创建/更新/发布/废弃/合规复核）触发失效，满足
module-status 中 semantic 的 perf_contract「版本缓存失效延迟 < 1s」。

Redis 属可选依赖；所有 Redis 调用均包裹 CircuitBreaker：Redis 抖动/宕机时
熔断打开，读取自动降级到 MySQL，核心链路不受影响（舱壁隔离）。
"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis

from app.core.logging import get_logger
from app.core.resilience import CircuitBreaker
from app.models.metric import Metric
from app.services.semantic.schemas import MetricResponse

logger = get_logger("unisense.semantic.cache")

_TTL_SECONDS = 300
_PREFIX = "metric:def:"


class MetricCache:
    """指标定义读缓存（cache-aside），可选依赖 Redis 由熔断器保护。"""

    def __init__(self, redis: Redis | None, breaker: CircuitBreaker | None = None) -> None:
        """初始化缓存。

        Args:
            redis: Redis 异步客户端；为 None 时缓存禁用（所有调用降级到 DB）。
            breaker: 熔断器；缺省使用默认参数（连续 5 次失败熔断，30s 后半开）。
        """
        self._redis = redis
        self._breaker = breaker or CircuitBreaker()
        self._enabled = redis is not None

    @classmethod
    def from_defaults(cls, redis: Redis | None) -> MetricCache:
        """使用默认熔断参数构建缓存。"""
        return cls(redis)

    async def get(self, metric_code: str) -> dict[str, Any] | None:
        """读取缓存。

        命中返回序列化后的 dict；未命中、缓存禁用或熔断打开时返回 None，
        由调用方降级到 MySQL。

        Args:
            metric_code: 指标编码。

        Returns:
            缓存的 MetricResponse dict，或 None。
        """
        if not self._enabled or not self._breaker.allow():
            return None
        key = _PREFIX + metric_code
        try:
            raw = await self._redis.get(key)  # type: ignore[union-attr]
        except Exception:
            self._breaker.record_failure()
            logger.warning("metric_cache_get_failed", metric_code=metric_code)
            return None
        if raw is None:
            return None
        try:
            data: dict[str, Any] = json.loads(raw)
        except Exception:
            return None
        return data

    async def set(self, metric: Metric) -> None:
        """写入缓存（写穿）。降级或不可用时静默跳过，不阻断主流程。

        Args:
            metric: 指标 ORM 对象。
        """
        if not self._enabled or not self._breaker.allow():
            return
        key = _PREFIX + metric.metric_code
        try:
            payload = json.dumps(
                MetricResponse.model_validate(metric).model_dump(mode="json"),
                ensure_ascii=False,
            )
            await self._redis.set(key, payload, ex=_TTL_SECONDS)  # type: ignore[union-attr]
        except Exception:
            self._breaker.record_failure()
            logger.warning("metric_cache_set_failed", metric_code=metric.metric_code)

    async def invalidate(self, metric_code: str) -> None:
        """失效缓存（版本缓存失效）。失败不影响写路径。

        Args:
            metric_code: 指标编码。
        """
        if not self._enabled:
            return
        key = _PREFIX + metric_code
        try:
            await self._redis.delete(key)  # type: ignore[union-attr]
        except Exception:
            logger.warning("metric_cache_invalidate_failed", metric_code=metric_code)
