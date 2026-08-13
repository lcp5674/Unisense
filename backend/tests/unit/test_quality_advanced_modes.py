"""质量高级模式单测（Epic 6：动态基线 / 同环比 / 跨源，TD §4.8.3）。

验证：create_rule 放开非 STATIC 模式；detect 按 rule_mode 分派到三种高级检测器；
冷启动降级（样本不足 / 无对照期 / 单来源）不误报；样本充足时按算法正确触发异常事件。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ValidationError
from app.models.quality import (
    QualityEvent,
    QualityEventStatus,
    QualityObservation,
    QualityRule,
    QualityRuleMode,
    QualityRuleType,
    QualitySeverity,
)
from app.services.quality.schemas import QualityObservationRequest, QualityRuleCreate
from app.services.quality.service import QualityService, _median, _population_std


def _make_rule(
    rule_mode: QualityRuleMode,
    threshold: dict,
    severity: QualitySeverity = QualitySeverity.P2,
    rule_type: QualityRuleType = QualityRuleType.ACCURACY,
) -> QualityRule:
    rule = QualityRule(
        metric_id=1,
        rule_type=rule_type,
        threshold=threshold,
        rule_mode=rule_mode,
        severity=severity,
        enabled=True,
        notify_targets={"owners": [1]},
        created_by=1,
    )
    rule.id = 10
    return rule


def _make_event() -> QualityEvent:
    e = QualityEvent(
        metric_id=1,
        level=QualitySeverity.P2,
        rule_type=QualityRuleType.ACCURACY,
        obs_value=Decimal("100"),
        threshold=Decimal("100"),
        status=QualityEventStatus.OPEN,
    )
    e.id = 99
    return e


def _obs(value: Decimal) -> QualityObservation:
    o = QualityObservation(
        metric_id=1,
        metric_code="m1",
        value=value,
        obs_time=datetime(2026, 1, 1, 8, 0),
    )
    o.id = 1
    return o


def _svc() -> QualityService:
    svc = QualityService(db=MagicMock())
    svc._repo = MagicMock()
    # 幂等去重：默认无既有 OPEN 事件，放行新事件落库
    svc._repo.find_open_event = AsyncMock(return_value=None)
    svc._publisher = MagicMock()
    svc._publisher.publish = AsyncMock()
    return svc


# ----------------------------------------------------------- 辅助函数
def test_median_and_std() -> None:
    assert _median([Decimal("1"), Decimal("2"), Decimal("3")]) == Decimal("2")
    assert _median([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]) == Decimal("2.5")
    assert _population_std([Decimal("2"), Decimal("4")]) == Decimal("1")


# ----------------------------------------------------------- 规则 CRUD 放开 mode
@pytest.mark.asyncio
async def test_create_rule_accepts_dynamic_baseline() -> None:
    svc = _svc()
    rule = _make_rule(QualityRuleMode.DYNAMIC_BASELINE, {"window_days": 28, "sigma": 3})
    svc._repo.create_rule = AsyncMock(return_value=rule)
    payload = QualityRuleCreate(
        metric_id=1,
        rule_type=QualityRuleType.ACCURACY,
        threshold={"window_days": 28, "sigma": 3},
        rule_mode=QualityRuleMode.DYNAMIC_BASELINE,
    )
    resp = await svc.create_rule(payload, 1)
    assert resp.rule_mode == QualityRuleMode.DYNAMIC_BASELINE


# ----------------------------------------------------------- 动态基线
@pytest.mark.asyncio
async def test_dynamic_baseline_triggers_on_sigma_deviation() -> None:
    svc = _svc()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[_make_rule(QualityRuleMode.DYNAMIC_BASELINE, {"window_days": 28, "sigma": 3})]
    )
    svc._repo.list_recent_observations = AsyncMock(
        return_value=[
            _obs(Decimal("100")),
            _obs(Decimal("100")),
            _obs(Decimal("100")),
            _obs(Decimal("100")),
        ]
    )
    svc._repo.create_event = AsyncMock(return_value=_make_event())
    # σ=0，偏离即异常
    resp = await svc.detect(1, QualityRuleType.ACCURACY, Decimal("150"))
    assert resp is not None
    assert svc._publisher.publish.await_count == 1


@pytest.mark.asyncio
async def test_dynamic_baseline_no_trigger_within_sigma() -> None:
    svc = _svc()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[_make_rule(QualityRuleMode.DYNAMIC_BASELINE, {"window_days": 28, "sigma": 3})]
    )
    svc._repo.list_recent_observations = AsyncMock(
        return_value=[
            _obs(Decimal("100")),
            _obs(Decimal("100")),
            _obs(Decimal("100")),
            _obs(Decimal("100")),
        ]
    )
    resp = await svc.detect(1, QualityRuleType.ACCURACY, Decimal("100"))
    assert resp is None


@pytest.mark.asyncio
async def test_dynamic_baseline_cold_start_no_history_no_event() -> None:
    svc = _svc()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[_make_rule(QualityRuleMode.DYNAMIC_BASELINE, {"window_days": 28, "sigma": 3})]
    )
    svc._repo.list_recent_observations = AsyncMock(return_value=[])
    resp = await svc.detect(1, QualityRuleType.ACCURACY, Decimal("999"))
    assert resp is None  # 冷启动无历史，不误报


@pytest.mark.asyncio
async def test_dynamic_baseline_cold_start_static_fallback() -> None:
    svc = _svc()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[
            _make_rule(
                QualityRuleMode.DYNAMIC_BASELINE,
                {"window_days": 28, "sigma": 3, "static_fallback": {"min": 0, "max": 100}},
            )
        ]
    )
    svc._repo.list_recent_observations = AsyncMock(return_value=[])
    svc._repo.create_event = AsyncMock(return_value=_make_event())
    # 冷启动退化为静态阈值 [0,100]，150 越界触发
    resp = await svc.detect(1, QualityRuleType.ACCURACY, Decimal("150"))
    assert resp is not None


# ----------------------------------------------------------- 同环比
@pytest.mark.asyncio
async def test_yoy_woy_triggers_on_large_diff() -> None:
    svc = _svc()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[_make_rule(QualityRuleMode.YOY_WOY, {"period": "yoy", "tolerance_pct": 20})]
    )
    base = _obs(Decimal("100"))
    svc._repo.get_same_period_observation = AsyncMock(return_value=base)
    svc._repo.create_event = AsyncMock(return_value=_make_event())
    # 当前 130 vs 对照 100 -> +30% > 20% 触发
    resp = await svc.detect(1, QualityRuleType.ACCURACY, Decimal("130"))
    assert resp is not None


@pytest.mark.asyncio
async def test_yoy_woy_no_trigger_within_tolerance() -> None:
    svc = _svc()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[_make_rule(QualityRuleMode.YOY_WOY, {"period": "yoy", "tolerance_pct": 20})]
    )
    svc._repo.get_same_period_observation = AsyncMock(return_value=_obs(Decimal("100")))
    resp = await svc.detect(1, QualityRuleType.ACCURACY, Decimal("110"))
    assert resp is None


@pytest.mark.asyncio
async def test_yoy_woy_cold_start_no_same_period() -> None:
    svc = _svc()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[_make_rule(QualityRuleMode.YOY_WOY, {"period": "yoy", "tolerance_pct": 20})]
    )
    svc._repo.get_same_period_observation = AsyncMock(return_value=None)
    resp = await svc.detect(1, QualityRuleType.ACCURACY, Decimal("130"))
    assert resp is None


# ----------------------------------------------------------- 跨源
@pytest.mark.asyncio
async def test_cross_source_triggers_on_spread() -> None:
    svc = _svc()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[_make_rule(QualityRuleMode.CROSS_SOURCE, {"tolerance_pct": 15})]
    )
    # 其他来源最新值均为 100；当前观测 130 -> spread 30% > 15%
    svc._repo.list_latest_per_source = AsyncMock(
        return_value=[_obs(Decimal("100")), _obs(Decimal("100"))]
    )
    svc._repo.create_event = AsyncMock(return_value=_make_event())
    resp = await svc.detect(1, QualityRuleType.CROSS_SOURCE, Decimal("130"))
    assert resp is not None


@pytest.mark.asyncio
async def test_cross_source_no_trigger_within_tolerance() -> None:
    svc = _svc()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[_make_rule(QualityRuleMode.CROSS_SOURCE, {"tolerance_pct": 15})]
    )
    svc._repo.list_latest_per_source = AsyncMock(
        return_value=[_obs(Decimal("100")), _obs(Decimal("102"))]
    )
    resp = await svc.detect(1, QualityRuleType.CROSS_SOURCE, Decimal("100"))
    assert resp is None


@pytest.mark.asyncio
async def test_cross_source_cold_start_single_source() -> None:
    svc = _svc()
    svc._repo.list_enabled_rules_for = AsyncMock(
        return_value=[_make_rule(QualityRuleMode.CROSS_SOURCE, {"tolerance_pct": 15})]
    )
    svc._repo.list_latest_per_source = AsyncMock(return_value=[])  # 仅当前来源，不足两来源
    resp = await svc.detect(1, QualityRuleType.CROSS_SOURCE, Decimal("999"))
    assert resp is None


# ----------------------------------------------------------- 观测写入
@pytest.mark.asyncio
async def test_record_observation_persists() -> None:
    svc = _svc()
    saved = _obs(Decimal("42"))
    saved.source_id = "s1"
    svc._repo.record_observation = AsyncMock(return_value=saved)
    req = QualityObservationRequest(
        metric_id=1,
        metric_code="m1",
        value=Decimal("42"),
        obs_time=datetime(2026, 1, 1, 8, 0),
        source_id="s1",
    )
    resp = await svc.record_observation(req)
    assert resp.metric_code == "m1"
    assert resp.value == Decimal("42")
    assert resp.source_id == "s1"


# ----------------------------------------------------------- FR-10 修复建议（TD §4.8.5）
@pytest.mark.asyncio
async def test_detect_generates_repair_suggestion() -> None:
    """异常触发时即生成修复建议（责任方/上游任务/建议SQL/观测基线）。"""
    svc = _svc()
    rule = _make_rule(QualityRuleMode.STATIC, {"max": 50}, rule_type=QualityRuleType.COMPLETENESS)
    svc._repo.list_enabled_rules_for = AsyncMock(return_value=[rule])
    captured: dict[int, QualityEvent] = {}

    async def _capture(ev: QualityEvent) -> QualityEvent:
        ev.id = 1
        captured[id(ev)] = ev
        return ev

    svc._repo.create_event = AsyncMock(side_effect=_capture)
    resp = await svc.detect(1, QualityRuleType.COMPLETENESS, Decimal("99"))
    assert resp is not None
    assert resp.repair_suggestion is not None
    sug = resp.repair_suggestion
    assert sug["rule_type"] == "COMPLETENESS"
    assert sug["pattern"] == "static_threshold_breach"
    assert sug["upstream_task"] == "collector_job:metric:1"
    assert sug["obs_value"] == "99"
    assert sug["baseline"] == "50"
    assert sug["confirmed_by"] is None
    assert sug["confirmed_at"] is None
    assert "SELECT" in sug["suggested_sql"]


@pytest.mark.asyncio
async def test_confirm_repair_records_confirmation() -> None:
    """Owner 确认修复后，confirmed_by / confirmed_at 写入修复建议并持久化。"""
    svc = _svc()
    ev = _make_event()
    ev.repair_suggestion = {
        "rule_type": "ACCURACY",
        "severity": "P2",
        "confirmed_by": None,
        "confirmed_at": None,
    }
    svc._repo.get_event = AsyncMock(return_value=ev)
    svc._repo.save_event = AsyncMock(return_value=ev)
    resp = await svc.confirm_repair(ev.id, user_id=7)
    assert resp.repair_suggestion is not None
    assert resp.repair_suggestion["confirmed_by"] == 7
    assert resp.repair_suggestion["confirmed_at"] is not None
    # 留痕写回事件并持久化
    saved = svc._repo.save_event.call_args.args[0]
    assert saved.repair_suggestion["confirmed_by"] == 7


@pytest.mark.asyncio
async def test_confirm_repair_rejects_non_open() -> None:
    """仅 OPEN 状态可确认修复，已 ACK 的事件应抛出状态非法。"""
    svc = _svc()
    ev = _make_event()
    ev.status = QualityEventStatus.ACK
    svc._repo.get_event = AsyncMock(return_value=ev)
    with pytest.raises(ValidationError):
        await svc.confirm_repair(ev.id, user_id=7)
