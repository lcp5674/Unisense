"""告警升级服务单测（escalation）。

覆盖：策略解析（P0/P1/P2/非法回落）、事件发布成功/失败降级、负载透传。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.notify.escalation import (
    EscalationService,
    resolve_policy,
)


class TestResolvePolicy:
    def test_p0_policy(self) -> None:
        p = resolve_policy("P0")
        assert p["level"] == "P0"
        assert p["repeat_minutes"] == 10
        assert "platform_admin" in p["target_roles"]

    def test_p1_policy(self) -> None:
        p = resolve_policy("P1")
        assert p["level"] == "P1"
        assert p["repeat_minutes"] == 30
        assert "metric_owner" in p["target_roles"]

    def test_p2_policy(self) -> None:
        p = resolve_policy("P2")
        assert p["level"] == "P2"
        assert p["repeat_minutes"] == 0

    def test_lowercase_normalized(self) -> None:
        p = resolve_policy("p0")
        assert p["level"] == "P0"

    def test_unknown_level_falls_back_p2(self) -> None:
        p = resolve_policy("P9")
        assert p["level"] == "P2"
        p2 = resolve_policy(None)
        assert p2["level"] == "P2"
        p3 = resolve_policy("")
        assert p3["level"] == "P2"


class TestEscalationService:
    @pytest.mark.asyncio
    async def test_escalate_publishes_event(self) -> None:
        bus = MagicMock()
        bus.publish = AsyncMock()
        svc = EscalationService(bus=bus)
        result = await svc.escalate(
            "quality.anomaly",
            {"metric_id": 7, "level": "P0"},
            level="P0",
            actor_id=1,
        )
        assert result["published"] is True
        assert result["level"] == "P0"
        assert result["event_type"] == "quality.anomaly"
        # 发布的事件类型与负载
        call = bus.publish.await_args
        assert call.args[0] == "escalation.triggered"
        payload = call.args[1]
        assert payload["source_event_type"] == "quality.anomaly"
        assert payload["level"] == "P0"
        assert payload["actor_id"] == "1"
        assert payload["payload"] == {"metric_id": 7, "level": "P0"}

    @pytest.mark.asyncio
    async def test_escalate_publish_failure_is_best_effort(self) -> None:
        bus = MagicMock()
        bus.publish = AsyncMock(side_effect=RuntimeError("bus down"))
        svc = EscalationService(bus=bus)
        result = await svc.escalate("conflict.escalated", level="P1")
        assert result["published"] is False
        assert result["level"] == "P1"  # 策略仍返回，供上层审计

    @pytest.mark.asyncio
    async def test_escalate_unknown_level_defaults_p2(self) -> None:
        bus = MagicMock()
        bus.publish = AsyncMock()
        svc = EscalationService(bus=bus)
        result = await svc.escalate("quality.anomaly", level="NOPE")
        assert result["level"] == "P2"
        payload = bus.publish.await_args.args[1]
        assert payload["level"] == "P2"

    @pytest.mark.asyncio
    async def test_escalate_default_level_p2(self) -> None:
        bus = MagicMock()
        bus.publish = AsyncMock()
        svc = EscalationService(bus=bus)
        result = await svc.escalate("quality.anomaly", {})
        assert result["level"] == "P2"
