"""quality 混沌测试（对齐 gateways chaos，TD §15）。

覆盖：
① 通知（notify）依赖不可用时，质量检测主流程仍可落库不阻断（告警降级）；
② 事件发布器隔离故障不向上游传播（熔断/捕获）；
③ 未配置 notify 端点时静默降级（best-effort）。
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from app.models.quality import (
    QualityEvent,
    QualityRule,
    QualityRuleMode,
    QualityRuleType,
    QualitySeverity,
)
from app.services.quality.events import QualityEventPublisher
from app.services.quality.service import QualityService


def _rule() -> QualityRule:
    return QualityRule(
        metric_id=1,
        rule_type=QualityRuleType.COMPLETENESS,
        threshold={"max": 100},
        rule_mode=QualityRuleMode.STATIC,
        severity=QualitySeverity.P0,
        enabled=True,
        notify_targets=["ops@unisense.io"],
    )


async def test_detect_lands_event_when_notify_down() -> None:
    """notify 不可达时，检测主流程仍成功落异常事件（告警降级）。"""
    svc = QualityService(db=MagicMock())
    svc._repo.list_enabled_rules_for = AsyncMock(return_value=[_rule()])
    # 幂等去重：无既有 OPEN 事件，放行
    svc._repo.find_open_event = AsyncMock(return_value=None)

    def _persist(event: QualityEvent) -> QualityEvent:
        event.id = 1
        return event

    svc._repo.create_event = AsyncMock(side_effect=_persist)
    pub = QualityEventPublisher(notify_url="http://test/notify")
    pub._send = AsyncMock(side_effect=RuntimeError("notify 故障"))
    svc._publisher = pub

    result = await svc.detect(1, QualityRuleType.COMPLETENESS, Decimal("150"))
    assert result is not None
    assert result.id == 1
    # 单点故障未击穿熔断（失败阈值 5，仅 1 次）
    assert pub._allow() is True


async def test_publisher_isolated_failure_not_propagated() -> None:
    """发布器内部故障被捕获，不向上游调用方传播。"""
    pub = QualityEventPublisher(notify_url="http://test/notify")
    pub._send = AsyncMock(side_effect=RuntimeError("boom"))
    await pub.publish({"event_type": "quality.anomaly"})


async def test_publisher_noop_without_notify_url() -> None:
    """未配置 notify 端点时静默降级，发布不抛异常。"""
    pub = QualityEventPublisher()
    await pub.publish({"event_type": "quality.anomaly"})
