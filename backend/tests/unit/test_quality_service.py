"""质量服务单元测试（TD §12.8 / FR-10）。

聚焦纯逻辑：检测引擎阈值评估（静态 op / range / 无效）、异常事件状态机非法转移拒绝。
DB/发布器以 MagicMock 隔离，不触碰真实依赖。
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ValidationError
from app.models.quality import (
    QualityEvent,
    QualityEventStatus,
    QualityRule,
    QualityRuleMode,
    QualityRuleType,
    QualitySeverity,
)
from app.services.quality.schemas import QualityRuleCreate
from app.services.quality.service import QualityService


def _svc() -> QualityService:
    svc = QualityService(MagicMock(), publisher=MagicMock())
    # 幂等去重默认无既有 OPEN 事件（detect 正常落事件）
    svc._repo.find_open_event = AsyncMock(return_value=None)  # noqa: SLF001
    return svc


@pytest.mark.parametrize(
    "threshold,obs,expected",
    [
        # op 描述「正常值应满足的条件」，越界即异常
        ({"op": "<=", "value": 100}, Decimal("120"), True),
        ({"op": "<=", "value": 100}, Decimal("90"), False),
        ({"op": ">", "value": 100}, Decimal("120"), False),
        ({"op": ">=", "value": 100}, Decimal("100"), False),
        ({"op": "==", "value": 100}, Decimal("100"), False),
        ({"op": "!=", "value": 100}, Decimal("99"), False),
        ({"min": 0, "max": 100}, Decimal("150"), True),
        ({"min": 0, "max": 100}, Decimal("50"), False),
        ({"foo": 1}, Decimal("5"), False),
    ],
)
def test_eval_threshold(threshold: dict, obs: Decimal, expected: bool) -> None:
    assert _svc()._eval(threshold, obs) is expected


async def test_ack_rejects_non_open() -> None:
    svc = _svc()
    event = MagicMock()
    event.status = QualityEventStatus.ACK
    svc._repo = MagicMock()
    svc._repo.get_event = AsyncMock(return_value=event)
    with pytest.raises(ValidationError):
        await svc.ack_event(1, "note", 1)


async def test_resolve_rejects_non_ack() -> None:
    svc = _svc()
    event = MagicMock()
    event.status = QualityEventStatus.OPEN
    svc._repo = MagicMock()
    svc._repo.get_event = AsyncMock(return_value=event)
    with pytest.raises(ValidationError):
        await svc.resolve_event(1, 1)


async def test_close_rejects_non_resolved() -> None:
    svc = _svc()
    event = MagicMock()
    event.status = QualityEventStatus.ACK
    svc._repo = MagicMock()
    svc._repo.get_event = AsyncMock(return_value=event)
    with pytest.raises(ValidationError):
        await svc.close_event(1, 1)


async def test_ack_persists_operator_and_note() -> None:
    """ACK 必须落操作人留痕（ack_by）与处理备注（ack_note），user_id 不再为死参数。"""
    svc = _svc()
    event = QualityEvent(
        metric_id=1,
        level=QualitySeverity.P2,
        rule_type=QualityRuleType.COMPLETENESS,
        status=QualityEventStatus.OPEN,
    )
    event.id = 1
    svc._repo = MagicMock()
    svc._repo.get_event = AsyncMock(return_value=event)
    svc._repo.transition_event = AsyncMock(return_value=event)
    await svc.ack_event(1, "已确认误报，数据已修复", 42)
    svc._repo.transition_event.assert_awaited_once()
    args = svc._repo.transition_event.call_args.args
    kwargs = svc._repo.transition_event.call_args.kwargs
    assert args[1] == QualityEventStatus.ACK
    assert args[2] == 42
    assert kwargs.get("ack_note") == "已确认误报，数据已修复"


async def test_resolve_persists_operator() -> None:
    svc = _svc()
    event = QualityEvent(
        metric_id=1,
        level=QualitySeverity.P2,
        rule_type=QualityRuleType.COMPLETENESS,
        status=QualityEventStatus.ACK,
    )
    event.id = 1
    svc._repo = MagicMock()
    svc._repo.get_event = AsyncMock(return_value=event)
    svc._repo.transition_event = AsyncMock(return_value=event)
    await svc.resolve_event(1, 7)
    args = svc._repo.transition_event.call_args.args
    assert args[1] == QualityEventStatus.RESOLVED
    assert args[2] == 7


async def test_close_persists_operator() -> None:
    svc = _svc()
    event = QualityEvent(
        metric_id=1,
        level=QualitySeverity.P2,
        rule_type=QualityRuleType.COMPLETENESS,
        status=QualityEventStatus.RESOLVED,
    )
    event.id = 1
    svc._repo = MagicMock()
    svc._repo.get_event = AsyncMock(return_value=event)
    svc._repo.transition_event = AsyncMock(return_value=event)
    await svc.close_event(1, 9)
    args = svc._repo.transition_event.call_args.args
    assert args[1] == QualityEventStatus.CLOSED
    assert args[2] == 9


def _persist(event: QualityEvent) -> QualityEvent:
    event.id = 1
    return event


def _rule(
    severity: QualitySeverity,
    threshold: dict,
    mode: QualityRuleMode = QualityRuleMode.STATIC,
) -> QualityRule:
    return QualityRule(
        metric_id=1,
        rule_type=QualityRuleType.COMPLETENESS,
        threshold=threshold,
        rule_mode=mode,
        severity=severity,
        enabled=True,
    )


async def test_detect_records_triggered_bound_direction() -> None:
    """双边阈值越界时，事件记录的 threshold 是被越界的边界（方向正确）。"""
    svc = _svc()
    svc._publisher = AsyncMock()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[_rule(QualitySeverity.P2, {"min": 0, "max": 100})]
    )
    svc._repo.create_event = AsyncMock(side_effect=_persist)
    result = await svc.detect(1, QualityRuleType.COMPLETENESS, Decimal("-5"))
    assert result is not None
    assert result.threshold == Decimal("0")  # 越界的是下界 0，而非上界 100


async def test_detect_picks_highest_severity() -> None:
    """同指标同类型多条命中时，落库事件取最高严重级（P0 不被随机丢弃）。"""
    svc = _svc()
    svc._publisher = AsyncMock()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[
            _rule(QualitySeverity.P2, {"max": 100}),
            _rule(QualitySeverity.P0, {"max": 1000}),
        ]
    )
    svc._repo.create_event = AsyncMock(side_effect=_persist)
    result = await svc.detect(1, QualityRuleType.COMPLETENESS, Decimal("5000"))
    assert result is not None
    assert result.level == QualitySeverity.P0


async def test_detect_skips_unsupported_mode() -> None:
    """未实现的非 STATIC 模式被跳过并告警，不落事件（杜绝静默失效）。"""
    svc = _svc()
    svc._publisher = AsyncMock()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[
            _rule(QualitySeverity.P2, {"static_value": 100}, QualityRuleMode.DYNAMIC_BASELINE)
        ]
    )
    svc._repo.create_event = AsyncMock(side_effect=_persist)
    result = await svc.detect(1, QualityRuleType.COMPLETENESS, Decimal("5000"))
    assert result is None
    svc._repo.create_event.assert_not_called()


async def test_create_rule_accepts_advanced_modes() -> None:
    """Epic 6：动态基线 / 同环比 / 跨源等高级模式已落地，创建时不再拒绝。"""
    svc = _svc()
    rule = _rule(
        QualitySeverity.P2,
        {"window_days": 28, "sigma": 3},
        QualityRuleMode.DYNAMIC_BASELINE,
    )
    rule.id = 1
    rule.created_by = 1
    svc._repo.create_rule = AsyncMock(return_value=rule)
    payload = QualityRuleCreate(
        metric_id=1,
        rule_type=QualityRuleType.COMPLETENESS,
        threshold={"window_days": 28, "sigma": 3},
        rule_mode=QualityRuleMode.DYNAMIC_BASELINE,
        severity=QualitySeverity.P2,
    )
    resp = await svc.create_rule(payload, 1)
    assert resp.rule_mode == QualityRuleMode.DYNAMIC_BASELINE
    svc._repo.create_rule.assert_awaited_once()


async def test_create_rule_static_mode_regression() -> None:
    """STATIC 静态阈值模式仍正常创建（Epic 6 之前的核心能力不退化）。"""
    svc = _svc()
    rule = _rule(QualitySeverity.P2, {"max": 100}, QualityRuleMode.STATIC)
    rule.id = 1
    rule.created_by = 1
    svc._repo.create_rule = AsyncMock(return_value=rule)
    payload = QualityRuleCreate(
        metric_id=1,
        rule_type=QualityRuleType.COMPLETENESS,
        threshold={"max": 100},
        rule_mode=QualityRuleMode.STATIC,
        severity=QualitySeverity.P2,
    )
    resp = await svc.create_rule(payload, 1)
    assert resp.rule_mode == QualityRuleMode.STATIC
    svc._repo.create_rule.assert_awaited_once()


async def test_detect_skips_when_open_event_exists() -> None:
    """同 (metric_id, rule_type) 已有 OPEN 事件时不再重复落事件+告警（幂等）。"""
    svc = _svc()
    svc._publisher = AsyncMock()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[_rule(QualitySeverity.P1, {"max": 100})]
    )
    svc._repo.create_event = AsyncMock(side_effect=_persist)
    # 存在既有 OPEN 事件 → detect 跳过，不落新事件
    svc._repo.find_open_event = AsyncMock(return_value=object())
    result = await svc.detect(1, QualityRuleType.COMPLETENESS, Decimal("5000"))
    assert result is None
    svc._repo.create_event.assert_not_called()
    svc._publisher.publish.assert_not_called()
