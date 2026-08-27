"""LLM 客户端重试+熔断测试（T049 部分）。

针对真实实现（``app.services.llm.client.LlmClient``）验证：
1. 5xx/网络瞬时故障时指数退避重试，成功后返回结构化结果
2. 4xx 为永久错误不重试
3. 连续失败触发熔断（``app.core.resilience.CircuitBreaker``）
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.resilience import CircuitBreaker
from app.services.llm.client import LlmClient, LlmError


@pytest.fixture(autouse=True)
def _reset_llm_breaker() -> None:
    """每个测试前重置共享 LLM 熔断器单例，避免失败测试间的状态污染。"""
    from app.services.llm.client import _LLM_BREAKER

    _LLM_BREAKER._open = False
    _LLM_BREAKER._failures = 0
    _LLM_BREAKER._opened_at = None
    _LLM_BREAKER._probing = False
    _LLM_BREAKER._probing_since = None
    _LLM_BREAKER._recent_outcomes.clear()


def _resp(status: int, body: dict[str, Any] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}", request=MagicMock(), response=resp
        )
    resp.json.return_value = body or {
        "choices": [{"message": {"content": '{"content":"ok"}', "finish_reason": "stop"}}],
        "model": "test-model",
        "usage": {},
    }
    resp.text = ""
    return resp


def _make_client() -> LlmClient:
    return LlmClient(base_url="https://api.example.com", api_key="test-key")


@pytest.mark.asyncio
async def test_llm_client_retries_on_5xx_then_succeeds() -> None:
    """T049: 5xx 瞬时故障退避重试，成功后返回结构化结果。"""
    client = _make_client()
    post_mock = AsyncMock(side_effect=[_resp(503), _resp(200)])
    with (
        patch.object(client._client, "post", new=post_mock),
        patch("app.services.llm.client.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        result = await client.chat([{"role": "user", "content": "hi"}])
    assert result["content"] == "ok"
    assert post_mock.await_count == 2
    sleep.assert_awaited_once()  # 触发了一次退避
    await client.close()


@pytest.mark.asyncio
async def test_llm_client_does_not_retry_4xx() -> None:
    """T049: 4xx 为永久错误，不重试直接抛出。"""
    client = _make_client()
    post_mock = AsyncMock(return_value=_resp(401))
    # 仅一次调用，无重试
    with (
        patch.object(client._client, "post", new=post_mock),
        patch("app.services.llm.client.asyncio.sleep", new=AsyncMock()),
        pytest.raises(LlmError),
    ):
        await client.chat([{"role": "user", "content": "hi"}])
    assert post_mock.await_count == 1
    await client.close()


@pytest.mark.asyncio
async def test_llm_circuit_breaker_opens_after_threshold() -> None:
    """T049: 连续失败达到阈值后熔断打开，拒绝请求。"""
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout=10, name="test-llm")

    for _ in range(3):
        breaker.record_failure()

    assert breaker.state == "open"
    assert breaker.allow() is False
    # 冷却期未到，持续拒绝
    assert breaker.allow() is False


@pytest.mark.asyncio
async def test_llm_circuit_breaker_recovers_on_success() -> None:
    """T049: 熔断后 record_success 复位为关闭态。"""
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=10, name="test-llm")
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "open"

    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow() is True


@pytest.mark.asyncio
async def test_llm_client_chat_retries_override() -> None:
    """A4: chat 的 retries 参数覆盖全局重试次数——推断类调用传 1 时 429 最多
    重试 1 次（总 2 次尝试），避免限流重试大概率仍 429 而叠加放大墙钟。"""
    client = _make_client()
    post_mock = AsyncMock(return_value=_resp(429))
    with (
        patch.object(client._client, "post", new=post_mock),
        patch("app.services.llm.client.asyncio.sleep", new=AsyncMock()),
        pytest.raises(LlmError),
    ):
        await client.chat([{"role": "user", "content": "hi"}], retries=1)
    assert post_mock.await_count == 2, "retries=1 → 总计 2 次尝试（1 次重试）"
    await client.close()


@pytest.mark.asyncio
async def test_llm_client_chat_retries_zero_no_retry() -> None:
    """A4: retries=0 时完全关闭重试（429 只调 1 次即抛错）。"""
    client = _make_client()
    post_mock = AsyncMock(return_value=_resp(429))
    with (
        patch.object(client._client, "post", new=post_mock),
        patch("app.services.llm.client.asyncio.sleep", new=AsyncMock()),
        pytest.raises(LlmError),
    ):
        await client.chat([{"role": "user", "content": "hi"}], retries=0)
    assert post_mock.await_count == 1, "retries=0 → 不重试"
    await client.close()
