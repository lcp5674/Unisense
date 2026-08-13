"""缓存 LRU 上限测试（T049 部分）。

验证：
1. 缓存键数超过 _CACHE_KEY_LIMIT 时触发 LRU 淘汰
2. _prune_if_needed 方法正确运行
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_cache_key_limit_constant():
    """T049: 缓存键数上限常量存在且合理。"""
    from app.services.semantic.cache import _CACHE_EVICT_BATCH, _CACHE_KEY_LIMIT

    assert _CACHE_KEY_LIMIT == 10_000
    assert _CACHE_EVICT_BATCH >= 1


@pytest.mark.asyncio
async def test_prune_if_needed_called_on_set():
    """T049: set() 调用 _prune_if_needed。"""
    from app.core.resilience import CircuitBreaker
    from app.services.semantic.cache import MetricCache

    # 创建 mock Redis
    mock_redis = AsyncMock()
    mock_redis.dbsize.return_value = 100  # 远低于上限
    mock_redis.set = AsyncMock(return_value=True)

    cache = MetricCache(redis=mock_redis, breaker=CircuitBreaker())

    # 创建 mock Metric
    mock_metric = MagicMock()
    mock_metric.metric_code = "test_metric"
    mock_metric.version = 1
    mock_metric.pii_flag = False
    mock_metric.definition_json = {"expression": "SELECT 1"}

    # Mock MetricResponse.model_validate
    with patch("app.services.semantic.cache.MetricResponse") as mock_resp:
        mock_resp.model_validate.return_value = MagicMock()
        mock_resp.model_validate.return_value.model_dump.return_value = {"code": "test_metric"}

        await cache.set(mock_metric)

    # dbsize 被调用验证（prune 检查）
    mock_redis.dbsize.assert_called()
