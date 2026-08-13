"""quality 规则引擎单测（工业级复审：占位符规则 / 真实求值 / 边界）。

覆盖：
① STATIC 模式畸形阈值（无 op/value/min/max、非数值）在 create/update 时被拒，
   杜绝「永不触发」的静默占位符规则；
② 真实 STATIC 阈值在越界时确实触发异常（规则引擎非桩）；
③ 边界阈值（obs == 边界）不误报。
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ValidationError
from app.models.quality import (
    QualityRule,
    QualityRuleMode,
    QualityRuleType,
    QualitySeverity,
)
from app.services.quality.schemas import QualityRuleCreate, QualityRuleUpdate
from app.services.quality.service import _OPS, QualityService


def _svc() -> QualityService:
    db = MagicMock()
    svc = QualityService(db)
    svc._repo = MagicMock()  # noqa: SLF001
    return svc


async def test_static_rule_rejects_no_usable_keys() -> None:
    """无 op/value/min/max 的 STATIC 阈值是死规则，必须被拒。"""
    svc = _svc()
    payload = QualityRuleCreate(
        metric_id=1,
        rule_type=QualityRuleType.COMPLETENESS,
        threshold={"foo": "bar"},
        rule_mode=QualityRuleMode.STATIC,
    )
    with pytest.raises(ValidationError):
        await svc.create_rule(payload, user_id=9)


async def test_static_rule_rejects_non_numeric_value() -> None:
    """value 非数值会在 detect 时静默失效，创建时须拦截。"""
    svc = _svc()
    payload = QualityRuleCreate(
        metric_id=1,
        rule_type=QualityRuleType.COMPLETENESS,
        threshold={"op": ">", "value": "not_a_number"},
        rule_mode=QualityRuleMode.STATIC,
    )
    with pytest.raises(ValidationError):
        await svc.create_rule(payload, user_id=9)


async def test_static_rule_accepts_valid_bounds() -> None:
    """合法 min/max 边界阈值得以创建。"""
    svc = _svc()
    saved = QualityRule(
        id=1,
        metric_id=1,
        rule_type=QualityRuleType.COMPLETENESS,
        threshold={"min": 0, "max": 1},
        rule_mode=QualityRuleMode.STATIC,
        severity=QualitySeverity.P2,
        enabled=True,
        created_by=9,
    )
    svc._repo.create_rule = AsyncMock(return_value=saved)  # noqa: SLF001
    resp = await svc.create_rule(
        QualityRuleCreate(
            metric_id=1,
            rule_type=QualityRuleType.COMPLETENESS,
            threshold={"min": 0, "max": 1},
            rule_mode=QualityRuleMode.STATIC,
        ),
        user_id=9,
    )
    assert resp.id == 1


async def test_update_rule_validates_threshold_against_mode() -> None:
    """update 提供畸形阈值时，按有效模式（payload 或既有）校验并拒绝。"""
    svc = _svc()
    existing = QualityRule(
        id=1,
        metric_id=1,
        rule_type=QualityRuleType.COMPLETENESS,
        threshold={"min": 0, "max": 1},
        rule_mode=QualityRuleMode.STATIC,
        severity=QualitySeverity.P2,
        enabled=True,
        created_by=9,
    )
    svc._repo.get_rule = AsyncMock(return_value=existing)  # noqa: SLF001
    svc._repo.update_rule = AsyncMock(return_value=existing)  # noqa: SLF001
    with pytest.raises(ValidationError):
        await svc.update_rule(
            1,
            QualityRuleUpdate(threshold={"op": ">", "value": "xx"}),
        )


async def test_static_rule_triggers_on_out_of_bounds() -> None:
    """真实求值：op 描述「正常值应满足的条件」，obs 不满足即异常（证明引擎非桩）。

    op='>' value=100 含义为「正常值应 > 100」，故 obs=50 越界须触发。
    """
    svc = _svc()
    triggered, bound = svc._evaluate({"op": ">", "value": 100}, Decimal("50"))
    assert triggered is True
    assert bound == Decimal("100")


async def test_static_rule_no_false_positive_at_boundary() -> None:
    """边界不误报：op='>=' 时 obs == value 视为满足正常条件，不触发。"""
    svc = _svc()
    triggered, _ = svc._evaluate({"op": ">=", "value": 100}, Decimal("100"))
    assert triggered is False


async def test_ops_defined_for_all_operators() -> None:
    """已知操作符集合非空且可调用，确保检测分派不漏。"""
    assert set(_OPS) >= {">", "<", ">=", "<=", "==", "!="}
