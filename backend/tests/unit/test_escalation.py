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


class TestEscalationPersistence:
    """升级状态持久化（B2：escalation_record 落库）。"""

    @pytest.mark.asyncio
    async def test_escalate_with_session_persists_record(self) -> None:
        session = MagicMock()
        captured: dict[str, object] = {}

        def _fake_add(obj: object) -> None:
            captured["obj"] = obj

        def _fake_flush() -> None:
            captured["obj"].id = 42  # mock flush 不会真实分配 id，手动赋值

        session.add = MagicMock(side_effect=_fake_add)
        session.flush = AsyncMock(side_effect=_fake_flush)
        bus = MagicMock()
        bus.publish = AsyncMock()
        svc = EscalationService(bus=bus, session=session)
        result = await svc.escalate("quality.anomaly", {"metric_id": 7}, level="P1", actor_id=1)
        # 落库：一条记录，attempts=1，next_retry_at 有值（P1 每 30 分钟）
        assert result["record_id"] == 42
        added = session.add.call_args.args[0]
        assert added.attempts == 1
        assert added.max_attempts == 4
        assert added.status == "ESCALATED"
        assert added.next_retry_at is not None

    @pytest.mark.asyncio
    async def test_escalate_p2_no_next_retry(self) -> None:
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        bus = MagicMock()
        bus.publish = AsyncMock()
        svc = EscalationService(bus=bus, session=session)
        await svc.escalate("quality.anomaly", {}, level="P2")
        added = session.add.call_args.args[0]
        assert added.next_retry_at is None  # P2 不重复
        assert added.max_attempts == 0

    @pytest.mark.asyncio
    async def test_escalate_without_session_no_record(self) -> None:
        bus = MagicMock()
        bus.publish = AsyncMock()
        svc = EscalationService(bus=bus)  # 无 session → 不落库（保持旧契约）
        result = await svc.escalate("quality.anomaly", {}, level="P0")
        assert result["record_id"] is None
        assert result["published"] is True

    @pytest.mark.asyncio
    async def test_escalate_source_ref_persisted(self) -> None:
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        bus = MagicMock()
        bus.publish = AsyncMock()
        svc = EscalationService(bus=bus, session=session)
        await svc.escalate("quality.anomaly", {}, level="P1", source_ref="quality_event:42")
        added = session.add.call_args.args[0]
        assert added.source_ref == "quality_event:42"


class TestEscalationAcknowledge:
    @pytest.mark.asyncio
    async def test_acknowledge_success(self) -> None:
        rec = MagicMock()
        rec.status = "ESCALATED"
        session = MagicMock()
        session.get = AsyncMock(return_value=rec)
        session.flush = AsyncMock()
        svc = EscalationService(bus=MagicMock(), session=session)
        ok = await svc.acknowledge(7)
        assert ok is True
        assert rec.status == "ACKNOWLEDGED"
        assert rec.next_retry_at is None

    @pytest.mark.asyncio
    async def test_acknowledge_missing_record(self) -> None:
        session = MagicMock()
        session.get = AsyncMock(return_value=None)
        svc = EscalationService(bus=MagicMock(), session=session)
        assert await svc.acknowledge(999) is False

    @pytest.mark.asyncio
    async def test_acknowledge_already_acknowledged(self) -> None:
        rec = MagicMock()
        rec.status = "ACKNOWLEDGED"
        session = MagicMock()
        session.get = AsyncMock(return_value=rec)
        svc = EscalationService(bus=MagicMock(), session=session)
        assert await svc.acknowledge(7) is False

    @pytest.mark.asyncio
    async def test_acknowledge_without_session(self) -> None:
        svc = EscalationService(bus=MagicMock())  # 无 session → 拒绝
        assert await svc.acknowledge(7) is False


class TestEscalationRetries:
    def _svc(self, rows: list) -> EscalationService:
        session = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=result)
        bus = MagicMock()
        bus.publish = AsyncMock()
        return EscalationService(bus=bus, session=session)

    def _rec(self, level: str, attempts: int, max_attempts: int) -> MagicMock:
        rec = MagicMock()
        rec.id = 1
        rec.event_type = "quality.anomaly"
        rec.level = level
        rec.label = "高"
        rec.attempts = attempts
        rec.max_attempts = max_attempts
        rec.next_retry_at = None  # 满足 <= now
        rec.status = "ESCALATED"
        rec.last_payload = {"metric_id": 7}
        rec.actor_id = "1"
        return rec

    @pytest.mark.asyncio
    async def test_check_retries_no_session(self) -> None:
        svc = EscalationService(bus=MagicMock())
        assert await svc.check_retries() == {"due": 0, "resent": 0, "escalated": 0, "maxed_out": 0}

    @pytest.mark.asyncio
    async def test_check_retries_resends_below_limit(self) -> None:
        rec = self._rec("P1", attempts=2, max_attempts=4)
        svc = self._svc([rec])
        stats = await svc.check_retries()
        assert stats == {"due": 1, "resent": 1, "escalated": 0, "maxed_out": 0}
        assert rec.attempts == 3
        assert rec.next_retry_at is not None  # 重排下次重试
        # 重发事件携带 retry 标记
        call = svc._bus.publish.await_args
        assert call.args[0] == "escalation.triggered"
        assert call.args[1]["retry"] is True
        assert call.args[1]["attempt"] == 3

    @pytest.mark.asyncio
    async def test_check_retries_escalates_level_on_limit(self) -> None:
        rec = self._rec("P1", attempts=4, max_attempts=4)  # P1 达上限
        svc = self._svc([rec])
        stats = await svc.check_retries()
        assert stats == {"due": 1, "resent": 0, "escalated": 1, "maxed_out": 0}
        assert rec.level == "P0"  # 升级到 P0
        assert rec.attempts == 1  # 重置计数
        assert rec.max_attempts == 6
        # 升级事件带 escalated 标记
        call = svc._bus.publish.await_args
        assert call.args[1]["escalated"] is True
        assert call.args[1]["level"] == "P0"

    @pytest.mark.asyncio
    async def test_check_retries_maxed_out_at_highest_level(self) -> None:
        rec = self._rec("P0", attempts=6, max_attempts=6)  # P0 达上限（最高级）
        svc = self._svc([rec])
        stats = await svc.check_retries()
        assert stats == {"due": 1, "resent": 0, "escalated": 0, "maxed_out": 1}
        assert rec.status == "MAXED_OUT"
        assert rec.next_retry_at is None

    @pytest.mark.asyncio
    async def test_check_retries_escalate_p2_to_p1(self) -> None:
        # P2 无重试（next_retry_at=None 不命中查询），此处模拟已被手动改到点
        rec = self._rec("P2", attempts=0, max_attempts=0)
        svc = self._svc([rec])
        stats = await svc.check_retries()
        assert stats["escalated"] == 1
        assert rec.level == "P1"
