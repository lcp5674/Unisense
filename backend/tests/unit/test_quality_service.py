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
    svc._assert_metric_domain = AsyncMock()
    with pytest.raises(ValidationError):
        await svc.ack_event(1, "note", 1, domain="sales", is_platform_admin=False)


async def test_resolve_rejects_non_ack() -> None:
    svc = _svc()
    event = MagicMock()
    event.status = QualityEventStatus.OPEN
    svc._repo = MagicMock()
    svc._repo.get_event = AsyncMock(return_value=event)
    svc._assert_metric_domain = AsyncMock()
    with pytest.raises(ValidationError):
        await svc.resolve_event(1, 1, domain="sales", is_platform_admin=False)


async def test_close_rejects_non_resolved() -> None:
    svc = _svc()
    event = MagicMock()
    event.status = QualityEventStatus.ACK
    svc._repo = MagicMock()
    svc._repo.get_event = AsyncMock(return_value=event)
    svc._assert_metric_domain = AsyncMock()
    with pytest.raises(ValidationError):
        await svc.close_event(1, 1, domain="sales", is_platform_admin=False)


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
    svc._assert_metric_domain = AsyncMock()
    await svc.ack_event(1, "已确认误报，数据已修复", 42, domain="sales", is_platform_admin=False)
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
    svc._assert_metric_domain = AsyncMock()
    await svc.resolve_event(1, 7, domain="sales", is_platform_admin=False)
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
    svc._assert_metric_domain = AsyncMock()
    await svc.close_event(1, 9, domain="sales", is_platform_admin=False)
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


# ---- P0 安全修复 S1：质量规则/事件/对账的指标域归属校验（service 层 fail-closed）----


def _metric_row(domain: str = "sales", metric_id: int = 1) -> MagicMock:
    """构造 db.execute 返回的 Metric 行（domain 可控）。"""
    metric = MagicMock()
    metric.id = metric_id
    metric.domain = domain
    return metric


async def test_assert_metric_domain_platform_admin_bypasses() -> None:
    """platform_admin 不查库直接放行（域校验对全局管理员豁免）。"""
    svc = _svc()
    await svc._assert_metric_domain(
        metric_id=1, domain="anything", is_platform_admin=True
    )
    svc._db.execute.assert_not_called()


async def test_assert_metric_domain_in_domain_allowed() -> None:
    """指标域与用户域一致时放行。"""
    svc = _svc()
    svc._db.execute = AsyncMock()
    _db_res = MagicMock()
    _db_res.scalar_one_or_none.return_value = _metric_row("sales")
    svc._db.execute.return_value = _db_res
    await svc._assert_metric_domain(
        metric_id=1, domain="sales", is_platform_admin=False
    )


async def test_assert_metric_domain_cross_domain_rejected() -> None:
    """域 A 用户操作域 B 指标必须拒绝（S1 越权修复核心）。"""
    from app.core.exceptions import AuthError

    svc = _svc()
    svc._db.execute = AsyncMock()
    _db_res = MagicMock()
    _db_res.scalar_one_or_none.return_value = _metric_row("finance")
    svc._db.execute.return_value = _db_res
    with pytest.raises(AuthError) as exc:
        await svc._assert_metric_domain(
            metric_id=1, domain="sales", is_platform_admin=False
        )
    assert exc.value.error_code == "FORBIDDEN"


async def test_assert_metric_domain_missing_metric_not_found() -> None:
    """指标不存在时报 NotFoundError（fail-closed，不静默放行）。"""
    from app.core.exceptions import NotFoundError

    svc = _svc()
    svc._db.execute = AsyncMock()
    _db_res = MagicMock()
    _db_res.scalar_one_or_none.return_value = None
    svc._db.execute.return_value = _db_res
    with pytest.raises(NotFoundError):
        await svc._assert_metric_domain(
            metric_id=999, domain="sales", is_platform_admin=False
        )


async def test_update_rule_cross_domain_rejected() -> None:
    """update_rule 对域外指标规则必须拒绝（此前仅角色校验，无归属校验）。"""
    from app.core.exceptions import AuthError
    from app.services.quality.schemas import QualityRuleUpdate

    svc = _svc()
    rule = MagicMock()
    rule.id = 1
    rule.metric_id = 2
    rule.threshold = {"max": 100}
    rule.rule_mode = QualityRuleMode.STATIC
    svc._repo.get_rule = AsyncMock(return_value=rule)
    svc._db.execute = AsyncMock()
    _db_res = MagicMock()
    _db_res.scalar_one_or_none.return_value = _metric_row("finance")
    svc._db.execute.return_value = _db_res
    with pytest.raises(AuthError):
        await svc.update_rule(
            1,
            QualityRuleUpdate(enabled=False),
            domain="sales",
            is_platform_admin=False,
        )


async def test_get_rule_cross_domain_rejected() -> None:
    """get_rule 读路径同样按域守卫（此前任意读角色可读任意规则详情）。"""
    from app.core.exceptions import AuthError

    svc = _svc()
    rule = MagicMock()
    rule.id = 1
    rule.metric_id = 2
    svc._repo.get_rule = AsyncMock(return_value=rule)
    svc._db.execute = AsyncMock()
    _db_res = MagicMock()
    _db_res.scalar_one_or_none.return_value = _metric_row("finance")
    svc._db.execute.return_value = _db_res
    with pytest.raises(AuthError):
        await svc.get_rule(1, domain="sales", is_platform_admin=False)


async def test_get_rule_same_domain_allowed() -> None:
    """get_rule 本域规则可读；平台管理员可读任意域。"""
    svc = _svc()
    rule = _rule(QualitySeverity.P1, {"max": 100})
    rule.id = 1
    rule.metric_id = 2
    rule.created_by = 1
    svc._repo.get_rule = AsyncMock(return_value=rule)
    svc._db.execute = AsyncMock()
    _db_res = MagicMock()
    _db_res.scalar_one_or_none.return_value = _metric_row("finance")
    svc._db.execute.return_value = _db_res

    resp = await svc.get_rule(1, domain="finance", is_platform_admin=False)
    assert resp.id == 1
    resp = await svc.get_rule(1, domain="sales", is_platform_admin=True)
    assert resp.id == 1


async def test_ack_event_cross_domain_rejected() -> None:
    """ack_event 对域外指标事件必须拒绝（此前可跨域处置事件）。"""
    from app.core.exceptions import AuthError

    svc = _svc()
    event = MagicMock()
    event.id = 1
    event.metric_id = 2
    event.status = QualityEventStatus.OPEN
    svc._repo.get_event = AsyncMock(return_value=event)
    svc._db.execute = AsyncMock()
    _db_res = MagicMock()
    _db_res.scalar_one_or_none.return_value = _metric_row("finance")
    svc._db.execute.return_value = _db_res
    with pytest.raises(AuthError):
        await svc.ack_event(
            1, "note", 7, domain="sales", is_platform_admin=False
        )


async def test_run_reconciliation_cross_domain_rejected() -> None:
    """run_reconciliation 对域外指标基准必须拒绝（benchmark.metric_code 归属校验）。"""
    from app.core.exceptions import AuthError
    from app.services.quality.schemas import ReconciliationRun

    svc = _svc()
    bench = MagicMock()
    bench.id = 1
    bench.metric_code = "finance_metric"
    bench.tolerance_pct = None
    bench.bench_value = 100
    svc._repo.get_benchmark = AsyncMock(return_value=bench)
    svc._db.execute = AsyncMock()
    _db_res = MagicMock()
    _db_res.scalar_one_or_none.return_value = _metric_row("finance")
    svc._db.execute.return_value = _db_res
    with pytest.raises(AuthError):
        await svc.run_reconciliation(
            ReconciliationRun(benchmark_id=1, metric_value=Decimal("101")),
            user_id=7,
            domain="sales",
            is_platform_admin=False,
        )
