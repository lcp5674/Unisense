"""dependency_probe 周期探针单元测试。

覆盖：
- 未配置依赖 → HEALTHY + meta.enabled=false + 清除降级注册表（未配置≠故障）；
- 已配置可达 → HEALTHY/CLOSED + 清除降级注册表；
- 已配置不可达 → DEGRADED/CLOSED + 注册降级注册表；
- 主循环 best-effort：单轮异常不终止循环。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import dependency_probe as mod


def _registry():
    registry = MagicMock()
    registry.clear_degradation = MagicMock()
    registry.register_degradation = MagicMock()
    return registry


@pytest.mark.asyncio
async def test_probe_unconfigured_dependency_marks_disabled_not_degraded():
    """未配置依赖：置 HEALTHY + meta.enabled=false + 清除降级注册表（不是故障）。"""
    updater = AsyncMock()
    registry = _registry()
    with patch.object(mod, "update_dependency_health", updater), patch.object(
        mod, "get_degradation_registry", return_value=registry
    ):
        await mod._probe_dependency("OLAP", "olap", enabled=False, alive=False)

    updater.assert_awaited_once()
    kwargs = updater.await_args.kwargs
    assert kwargs["status"] == "HEALTHY"
    assert kwargs["circuit_state"] == "CLOSED"
    assert kwargs["metadata"] == {"enabled": False, "note": "未配置，未启用"}
    registry.clear_degradation.assert_called_once_with("dependency_probe:olap")
    registry.register_degradation.assert_not_called()


@pytest.mark.asyncio
async def test_probe_configured_alive_marks_healthy():
    """已配置且可达：置 HEALTHY/CLOSED + 清除降级注册表。"""
    updater = AsyncMock()
    registry = _registry()
    with patch.object(mod, "update_dependency_health", updater), patch.object(
        mod, "get_degradation_registry", return_value=registry
    ):
        await mod._probe_dependency("GRAPH", "graph", enabled=True, alive=True)

    updater.assert_awaited_once()
    kwargs = updater.await_args.kwargs
    assert kwargs["status"] == "HEALTHY"
    assert kwargs["circuit_state"] == "CLOSED"
    assert kwargs["consecutive_failures"] == 0
    assert kwargs["metadata"] == {"enabled": True, "last_probe": "ok"}
    registry.clear_degradation.assert_called_once()
    registry.register_degradation.assert_not_called()


@pytest.mark.asyncio
async def test_probe_configured_unreachable_marks_degraded():
    """已配置但不可达：置 DEGRADED + 注册降级注册表（熔断器 OPEN 由熔断器事件保持）。"""
    updater = AsyncMock()
    registry = _registry()
    with patch.object(mod, "update_dependency_health", updater), patch.object(
        mod, "get_degradation_registry", return_value=registry
    ):
        await mod._probe_dependency("ES", "es", enabled=True, alive=False)

    updater.assert_awaited_once()
    kwargs = updater.await_args.kwargs
    assert kwargs["status"] == "DEGRADED"
    assert kwargs["circuit_state"] == "CLOSED"
    assert kwargs["metadata"] == {"enabled": True, "last_probe": "fail"}
    registry.register_degradation.assert_called_once_with(
        "dependency_probe:es", "probe_failed: es"
    )
    registry.clear_degradation.assert_not_called()


@pytest.mark.asyncio
async def test_run_dependency_probe_once_never_raises():
    """run_dependency_probe_once 全程 best-effort：依赖更新失败不阻断整体。"""
    with patch.object(mod, "_tcp_alive", new=AsyncMock(return_value=True)), patch.object(
        mod, "_llm_gateway_alive", new=AsyncMock(return_value=True)
    ), patch.object(
        mod, "_probe_dependency", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        results = await mod.run_dependency_probe_once()

    # 全部依赖探测失败也不抛异常（best-effort），返回空状态映射
    assert results == {}


@pytest.mark.asyncio
async def test_probe_loop_survives_round_error_and_cancels():
    """主循环：单轮异常仅告警不终止；CancelledError 正常退出。"""
    calls = 0

    async def _flaky_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("round failed")
        raise asyncio.CancelledError()

    with patch.object(mod, "run_dependency_probe_once", side_effect=_flaky_once), patch.object(
        mod.asyncio, "sleep", new=AsyncMock()
    ), pytest.raises(asyncio.CancelledError):
        await mod.dependency_probe_loop(interval=0.01)

    assert calls >= 2  # 第一轮异常后继续第二轮，直到取消
