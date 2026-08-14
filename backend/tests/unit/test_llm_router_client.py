"""LlmRouterClient 单测：多实例轮询路由 + 故障转移 + 冷却摘除。

核心验证（对齐产品目标「某个 LLM 不可用不造成服务不可用」）：
- 单实例失败自动切换下一个可用实例（failover）
- 连续失败实例进入冷却期，由剩余健康实例承接流量
- 全部实例失败才抛 LlmError
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.llm.client import LlmError, LlmRouterClient


class _FakeClient:
    """最小 LLM 客户端替身：可配置每次 chat 成功/抛 LlmError。"""

    def __init__(self, *, always_fail: bool = False, name: str = "fake") -> None:
        self._always_fail = always_fail
        self.name = name
        self.calls = 0
        self.closed = False

    @property
    def enabled(self) -> bool:
        return True

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self._always_fail:
            raise LlmError(f"{self.name} 不可用")
        return {"content": f"ok-{self.name}", "model": self.name, "usage": {}}

    async def close(self) -> None:
        self.closed = True


class TestRoundRobin:
    async def test_rotates_across_instances(self) -> None:
        a, b, c = _FakeClient(name="a"), _FakeClient(name="b"), _FakeClient(name="c")
        router = LlmRouterClient([a, b, c])
        results = [
            (await router.chat([{"role": "user", "content": "x"}]))["model"],
            (await router.chat([{"role": "user", "content": "x"}]))["model"],
            (await router.chat([{"role": "user", "content": "x"}]))["model"],
            (await router.chat([{"role": "user", "content": "x"}]))["model"],
        ]
        assert results == ["a", "b", "c", "a"]  # 轮询顺序

    async def test_no_instances_raises(self) -> None:
        router = LlmRouterClient([])
        assert router.enabled is False
        with pytest.raises(LlmError):
            await router.chat([{"role": "user", "content": "x"}])


class TestFailover:
    async def test_failover_to_next_when_primary_down(self) -> None:
        a, b = _FakeClient(always_fail=True, name="a"), _FakeClient(name="b")
        router = LlmRouterClient([a, b])
        result = await router.chat([{"role": "user", "content": "x"}])
        assert result["model"] == "b"  # a 失败自动切换到 b
        assert a.calls == 1
        assert b.calls == 1

    async def test_fallback_to_last_when_all_down(self) -> None:
        a, b = _FakeClient(always_fail=True, name="a"), _FakeClient(always_fail=True, name="b")
        router = LlmRouterClient([a, b])
        with pytest.raises(LlmError) as excinfo:
            await router.chat([{"role": "user", "content": "x"}])
        assert "所有 LLM 实例均不可用" in str(excinfo.value)
        assert a.calls == 1
        assert b.calls == 1

    async def test_recovers_when_failed_instance_heals(self) -> None:
        """a 连续失败达到阈值进入冷却后，仍由 b 承接；b 也失败时回落到 a（冷却兜底）。"""
        a = _FakeClient(name="a")
        b = _FakeClient(always_fail=True, name="b")

        # 第一次：a 成功，推进轮询
        assert (await router_chat([a, b]))["model"] == "a"
        # 后续：轮询起点落在 b（a 成功后被推进到下一实例），b 连续失败
        router = LlmRouterClient([a, b])
        # 手动先让 a 成功一次使 rotation 指向 b
        await router.chat([{"role": "user", "content": "x"}])  # a 成功
        # b 连续失败 3 次（达到阈值）→ 全部冷却时回落到 a
        for _ in range(3):
            result = await router.chat([{"role": "user", "content": "x"}])
            assert result["model"] == "a"
        # a 始终成功，服务不中断
        assert a.calls >= 3


async def router_chat(clients: list[_FakeClient]) -> dict[str, Any]:
    return await LlmRouterClient(clients).chat([{"role": "user", "content": "x"}])


class TestCooldown:
    async def test_consecutive_failures_trigger_cooldown(self) -> None:
        a = _FakeClient(always_fail=True, name="a")
        b = _FakeClient(name="b")
        router = LlmRouterClient([a, b])
        # 轮询顺序：0(a),1(b),0(a),1(b),...
        # 第一次：a 失败(1) → b 成功，rotation 推进到 a
        await router.chat([{"role": "user", "content": "x"}])
        # 第二次：a 失败(2) → b 成功
        await router.chat([{"role": "user", "content": "x"}])
        # 第三次：a 失败(3) → 达到阈值进入冷却，b 成功
        result = await router.chat([{"role": "user", "content": "x"}])
        assert result["model"] == "b"
        assert a.calls == 3  # 连续失败 3 次达到阈值
        assert 0 in router._cooldown_until  # type: ignore[attr-defined] - a 已进入冷却
        # 冷却期内：轮询起点在 a，但 a 被跳过，直接由 b 承接（a 不再被调用）
        for _ in range(2):
            result = await router.chat([{"role": "user", "content": "x"}])
            assert result["model"] == "b"
        assert a.calls == 3  # 冷却期内 a 未被再次调用

    async def test_close_closes_all_instances(self) -> None:
        a, b = _FakeClient(name="a"), _FakeClient(name="b")
        router = LlmRouterClient([a, b])
        await router.close()
        assert a.closed is True
        assert b.closed is True


class TestWithAsyncMock:
    async def test_router_works_with_async_client_mock(self) -> None:
        """路由客户端应与真实 LlmClient（AsyncMock 模拟）兼容。"""
        ok_client = AsyncMock()
        ok_client.enabled = True
        ok_client.chat.return_value = {"content": "ok", "model": "m1", "usage": {}}
        fail_client = AsyncMock()
        fail_client.enabled = True
        fail_client.chat.side_effect = LlmError("down")
        router = LlmRouterClient([fail_client, ok_client])
        result = await router.chat([{"role": "user", "content": "x"}])
        assert result["model"] == "m1"
        fail_client.chat.assert_awaited_once()
        ok_client.chat.assert_awaited_once()
