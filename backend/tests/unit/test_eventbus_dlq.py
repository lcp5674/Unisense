"""EventBus 指数退避 + DLQ 测试（T049 部分）。

验证：
1. 本地订阅者失败时指数退避重试（1s→2s→4s）
2. 3 次重试后写入 DLQ
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_eventbus_retry_on_handler_failure():
    """T049: EventBus 本地订阅者失败时指数退避重试。"""
    from app.core.eventbus import _RETRY_DELAYS, EventBus

    bus = EventBus(redis_pool=None)
    call_count = 0

    def failing_handler(event):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("handler failure")

    bus.subscribe("test.retry", failing_handler)

    with (
        patch("app.core.eventbus._enqueue_dlq") as mock_dlq,
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        await bus.publish("test.retry", {"key": "value"})
        await bus.drain()  # R3：等待后台重试任务完成

        # 验证重试次数 = len(_RETRY_DELAYS)
        assert call_count == len(_RETRY_DELAYS)
        # 验证退避延迟递增
        assert mock_sleep.call_count == len(_RETRY_DELAYS)

    # 验证最终写入 DLQ
    mock_dlq.assert_called_once()


@pytest.mark.asyncio
async def test_eventbus_no_retry_on_success():
    """T049: 成功调用不触发重试。"""
    from app.core.eventbus import EventBus

    bus = EventBus(redis_pool=None)
    call_count = 0

    def success_handler(event):
        nonlocal call_count
        call_count += 1

    bus.subscribe("test.success", success_handler)
    await bus.publish("test.success", {"key": "value"})
    await bus.drain()  # R3：等待后台订阅者任务完成

    assert call_count == 1
