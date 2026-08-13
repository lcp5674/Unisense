"""指标读缓存（cache-aside）+ 熔断舱壁。

对齐 TD §11（韧性）/ DEV_GUIDE §17.2（缓存策略）：读多写少的指标定义用 Redis
缓存热点对象，写操作（创建/更新/发布/废弃/合规复核）触发失效，满足
module-status 中 semantic 的 perf_contract「版本缓存失效延迟 < 1s」。

Redis 属可选依赖；所有 Redis 调用均包裹 CircuitBreaker：Redis 抖动/宕机时
熔断打开，读取自动降级到 MySQL，核心链路不受影响（舱壁隔离）。

并发防护：进程内 per-key 互斥锁 + double-checked locking，冷 key 高并发 miss
时把并发穿透串行化，避免缓存击穿（P3-1）；坏数据触发熔断失败计数，避免反复
命中坏数据不降级（P3-6）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from redis.asyncio import Redis

from app.core.logging import get_logger
from app.core.resilience import CircuitBreaker
from app.models.metric import Metric
from app.services.semantic.schemas import MetricResponse

logger = get_logger("unisense.semantic.cache")

_TTL_SECONDS = 600  # 10 分钟（对齐 US10 / FR-5 消费性能基线）
_PREFIX = "metric:def:"  # 键格式: metric:def:{code}:v{version}

# 进程内 per-key 互斥锁（singleflight）：防止同一冷 key 高并发击穿到 DB。
# 锁用后即从字典移除以避免内存泄漏；移除后并发请求会重建新锁，同一 key 在
# 极短窗口内可能并行穿透，属可接受的竞态。
_LOCKS: dict[str, asyncio.Lock] = {}
# 保护 _LOCKS 的并发 get-or-create（asyncio 环境，等待者需异步原语）。
_LOCKS_GUARD = asyncio.Lock()


async def _lock_for(key: str) -> asyncio.Lock:
    """获取 key 对应的进程内互斥锁（不存在则创建）。

    Args:
        key: 缓存键（含前缀）。

    Returns:
        该 key 的互斥锁。
    """
    async with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[key] = lock
        return lock


def _release_lock(key: str) -> None:
    """释放 key 的锁引用，防止锁集合无界增长。

    dict.pop 在 GIL 下原子；移除后新请求会重建锁（可接受的竞态）。
    """
    _LOCKS.pop(key, None)


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

    async def get(self, metric_code: str, version: int | None = None) -> dict[str, Any] | None:
        """读取缓存。

        命中返回序列化后的 dict；未命中、缓存禁用或熔断打开时返回 None，
        由调用方降级到 MySQL。

        singleflight（防缓存击穿）：干净 miss 时按 key 加进程内互斥锁并二次
        检查（double-check），把并发穿透串行化；Redis 异常/坏数据已由 _read
        记录熔断失败，直接降级无需重复回读。

        Args:
            metric_code: 指标编码。
            version: 版本号（用于构建版本化缓存键）。

        Returns:
            缓存的 MetricResponse dict，或 None。
        """
        if not self._enabled or not self._breaker.allow():
            return None
        key = self._build_key(metric_code, version)
        is_miss, data = await self._read(key, metric_code)
        if data is not None:
            self._breaker.record_success()
            return data
        if not is_miss:
            return None
        # 干净 miss：Redis 已成功响应（键不存在）→ 复位熔断（对齐 T011：
        # get() 成功即 record_success，Redis 恢复后熔断尽快复位）。
        self._breaker.record_success()
        lock = await _lock_for(key)
        try:
            async with lock:
                _, data = await self._read(key, metric_code)
                if data is not None:
                    self._breaker.record_success()
                return data
        finally:
            _release_lock(key)

    async def _read(self, key: str, metric_code: str) -> tuple[bool, dict[str, Any] | None]:
        """读取并解析缓存值（含熔断失败计数）。

        Args:
            key: 缓存键（含前缀）。
            metric_code: 指标编码（用于日志）。

        Returns:
            (is_miss, data)：is_miss=True 表示键不存在（可触发 singleflight
            二次检查）；data 为解析后的 dict；Redis 异常/坏数据时 data 为
            None（并已记录熔断失败）。
        """
        try:
            raw = await self._redis.get(key)  # type: ignore[union-attr]
        except Exception:
            self._breaker.record_failure()
            logger.warning("metric_cache_get_failed", metric_code=metric_code)
            return False, None
        if raw is None:
            return True, None
        try:
            data: dict[str, Any] = json.loads(raw)
        except Exception:
            self._breaker.record_failure()
            logger.warning("metric_cache_bad_json", metric_code=metric_code)
            return False, None
        return False, data

    async def set(self, metric: Metric) -> None:
        """写入缓存（写穿）。降级或不可用时静默跳过，不阻断主流程。

        Args:
            metric: 指标 ORM 对象。
        """
        if not self._enabled or not self._breaker.allow():
            return
        key = self._build_key(metric.metric_code, metric.version)
        try:
            payload = json.dumps(
                MetricResponse.model_validate(metric).model_dump(mode="json"),
                ensure_ascii=False,
            )
            await self._redis.set(key, payload, ex=_TTL_SECONDS)  # type: ignore[union-attr]
            self._breaker.record_success()
        except Exception:
            self._breaker.record_failure()
            logger.warning("metric_cache_set_failed", metric_code=metric.metric_code)

    async def invalidate(self, metric_code: str, version: int | None = None) -> None:
        """失效缓存（版本缓存失效）。失败不影响写路径。

        Args:
            metric_code: 指标编码。
            version: 版本号（如提供则只删版本键；否则删所有版本键）。
        """
        if not self._enabled:
            return
        if version is not None:
            key = self._build_key(metric_code, version)
            try:
                await self._redis.delete(key)  # type: ignore[union-attr]
            except Exception:
                logger.warning("metric_cache_invalidate_failed", metric_code=metric_code)
        else:
            # 删除所有版本的键（用 SCAN + DEL）
            pattern = _PREFIX + metric_code + ":v*"
            try:
                cursor = 0
                while True:
                    cursor, keys = await self._redis.scan(  # type: ignore[union-attr]
                        cursor=cursor, match=pattern, count=100
                    )
                    if keys:
                        await self._redis.delete(*keys)  # type: ignore[union-attr]
                    if cursor == 0:
                        break
            except Exception:
                logger.warning("metric_cache_invalidate_failed", metric_code=metric_code)

    async def invalidate_batch(self, metric_codes: list[str]) -> None:
        """批量失效缓存。失败不影响写路径。

        Args:
            metric_codes: 指标编码列表。
        """
        if not self._enabled or not metric_codes:
            return
        # 批量删除所有版本的键
        try:
            all_keys: list[str] = []
            for code in metric_codes:
                pattern = _PREFIX + code + ":v*"
                cursor = 0
                while True:
                    cursor, keys = await self._redis.scan(  # type: ignore[union-attr]
                        cursor=cursor, match=pattern, count=100
                    )
                    all_keys.extend(keys)
                    if cursor == 0:
                        break
            if all_keys:
                await self._redis.delete(*all_keys)  # type: ignore[union-attr]
        except Exception:
            logger.warning("metric_cache_invalidate_batch_failed", count=len(metric_codes))

    async def warm_up(self, metrics: list[Metric]) -> int:
        """预热缓存：使用 Redis pipeline 批量写入指标定义。

        对齐 FR-034：warm_up 必须使用 pipeline 批量写入。

        Args:
            metrics: 指标 ORM 对象列表。

        Returns:
            成功写入的数量。
        """
        if not self._enabled:
            return 0
        if not metrics:
            return 0

        count = 0
        try:
            async with self._redis.pipeline() as pipe:  # type: ignore[union-attr]
                for metric in metrics:
                    if not self._breaker.allow():
                        break
                    key = self._build_key(metric.metric_code, metric.version)
                    try:
                        payload = json.dumps(
                            MetricResponse.model_validate(metric).model_dump(mode="json"),
                            ensure_ascii=False,
                        )
                        pipe.set(key, payload, ex=_TTL_SECONDS)
                        count += 1
                    except Exception:
                        self._breaker.record_failure()
                        logger.warning(
                            "metric_cache_warmup_payload_failed",
                            metric_code=metric.metric_code,
                        )
                if count > 0:
                    await pipe.execute()
                    self._breaker.record_success()
        except Exception:
            self._breaker.record_failure()
            logger.warning("metric_cache_warmup_pipeline_failed", count=len(metrics))
        return count

    @staticmethod
    def _build_key(metric_code: str, version: int | None) -> str:
        """构建版本化缓存键: metric:def:{code}:v{version}。

        对齐 FR-032：缓存键必须含版本号。
        版本变更时旧键自然过期（TTL=600s），无需主动 invalidate。

        Args:
            metric_code: 指标编码。
            version: 版本号（None 时使用 v0 占位）。

        Returns:
            缓存键。
        """
        return f"{_PREFIX}{metric_code}:v{version or 0}"

    # ---- 消费指南缓存 ----

    _GUIDE_PREFIX = "metric:guide:"

    async def get_guide(self, metric_code: str) -> dict[str, Any] | None:
        """获取消费指南缓存。

        Args:
            metric_code: 指标编码。

        Returns:
            缓存的消费指南字典，未命中返回 None。
        """
        if not self._enabled:
            return None
        key = f"{self._GUIDE_PREFIX}{metric_code}"
        try:
            if not self._breaker.allow():
                return None
            raw = await self._redis.get(key)  # type: ignore[union-attr]
            if raw is None:
                return None
            self._breaker.record_success()
            return cast(dict[str, Any], json.loads(raw))
        except Exception:
            self._breaker.record_failure()
            return None

    async def set_guide(self, metric_code: str, guide: dict[str, Any]) -> None:
        """写入消费指南缓存。

        Args:
            metric_code: 指标编码。
            guide: 消费指南字典。
        """
        if not self._enabled:
            return
        key = f"{self._GUIDE_PREFIX}{metric_code}"
        try:
            if not self._breaker.allow():
                return
            payload = json.dumps(guide, ensure_ascii=False)
            await self._redis.set(key, payload, ex=_TTL_SECONDS)  # type: ignore[union-attr]
            self._breaker.record_success()
        except Exception:
            self._breaker.record_failure()
            logger.warning("cache_set_guide_failed", metric_code=metric_code)
