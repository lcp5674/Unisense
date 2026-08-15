"""质量自动调度任务单测（run_quality_checks）。

覆盖：无规则、新鲜观测触发、陈旧观测跳过、同组合去重、单组合失败不阻断。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.quality import (
    QualityEventStatus,
    QualityObservation,
    QualityRule,
    QualityRuleType,
    QualitySeverity,
)
from app.services.quality.service import QualityEventResponse
from app.services.quality.tasks import run_quality_checks


def _rule(
    metric_id: int = 1, rule_type: QualityRuleType = QualityRuleType.COMPLETENESS
) -> QualityRule:
    return QualityRule(
        id=1,
        metric_id=metric_id,
        rule_type=rule_type,
        threshold={"value": 100},
        severity=QualitySeverity.P0,
        enabled=True,
    )


def _obs(value: str = "0.5", age_hours: float = 1.0) -> QualityObservation:
    return QualityObservation(
        metric_id=1,
        metric_code="sales_gmv_amount_day",
        obs_time=datetime.now(UTC) - timedelta(hours=age_hours),
        value=Decimal(value),
    )


@pytest.fixture
def ctx() -> dict:
    return {}


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    rules: list[QualityRule],
    latest_obs: QualityObservation | None,
    detect_result: object | None = None,
    detect_error: Exception | None = None,
) -> tuple[dict[str, int], AsyncMock]:
    """搭好 mock 会话/repo/service 后执行任务，返回 (统计, detect_mock)。"""
    session = MagicMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    repo = MagicMock()
    repo.list_all_enabled_rules = AsyncMock(return_value=rules)
    repo.latest_observation = AsyncMock(return_value=latest_obs)

    # async_session_factory 是 async_sessionmaker：同步调用返回 AsyncSession，
    # 再经 async with session 进入异步上下文管理。
    def fake_factory():
        return session

    monkeypatch.setattr("app.db.mysql.async_session_factory", fake_factory)
    # 任务内为函数级 import（惰性），patch 其来源模块属性
    monkeypatch.setattr(
        "app.services.quality.repository.QualityRepository",
        lambda db: repo,
    )

    detect = AsyncMock(return_value=detect_result)
    if detect_error is not None:
        detect = AsyncMock(side_effect=detect_error)

    class _FakeService:
        def __init__(self, db: object) -> None:
            self.detect = detect

    monkeypatch.setattr("app.services.quality.service.QualityService", _FakeService)

    result = await run_quality_checks(ctx)
    return result, detect


class TestRunQualityChecks:
    @pytest.mark.asyncio
    async def test_no_rules_returns_zeros(self, ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        result, detect = await _run(monkeypatch, rules=[], latest_obs=None)
        assert result == {"combos": 0, "evaluated": 0, "triggered": 0, "skipped_no_obs": 0}
        detect.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_obs_triggers_detect(
        self,
        ctx: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        event = QualityEventResponse(
            id=1,
            metric_id=1,
            level=QualitySeverity.P1,
            rule_type=QualityRuleType.COMPLETENESS,
            obs_value=Decimal("0.5"),
            threshold=Decimal("0.3"),
            status=QualityEventStatus.OPEN,
        )
        result, detect = await _run(
            monkeypatch,
            rules=[_rule()],
            latest_obs=_obs("0.5"),
            detect_result=event,
        )
        assert result["combos"] == 1
        assert result["evaluated"] == 1
        assert result["triggered"] == 1
        assert result["skipped_no_obs"] == 0
        detect.assert_awaited_once()
        # detect 收到观测值
        args = detect.await_args.args
        assert args[0] == 1  # metric_id
        assert args[2] == Decimal("0.5")  # obs_value

    @pytest.mark.asyncio
    async def test_no_obs_skipped(self, ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        result, detect = await _run(monkeypatch, rules=[_rule()], latest_obs=None)
        assert result["combos"] == 1
        assert result["evaluated"] == 0
        assert result["skipped_no_obs"] == 1
        detect.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_obs_skipped(
        self,
        ctx: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 观测超过 48h 新鲜度窗口 → 跳过
        result, detect = await _run(
            monkeypatch, rules=[_rule()], latest_obs=_obs("0.5", age_hours=100)
        )
        assert result["skipped_no_obs"] == 1
        assert result["evaluated"] == 0
        detect.assert_not_called()

    @pytest.mark.asyncio
    async def test_naive_obs_time_handled(
        self,
        ctx: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # MySQL DATETIME(timezone=True) 读出为 naive datetime（驱动不保留时区），
        # 与 aware now 比较不应抛 TypeError——历史 bug：worker 整轮检测崩溃。
        latest = _obs("0.5")
        latest.obs_time = latest.obs_time.replace(tzinfo=None)  # 模拟驱动读出 naive
        result, detect = await _run(
            monkeypatch, rules=[_rule()], latest_obs=latest, detect_result=None
        )
        assert result["evaluated"] == 1
        assert result["skipped_no_obs"] == 0
        detect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_naive_stale_obs_skipped(
        self,
        ctx: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # naive 且超过新鲜度窗口 → 同样正确跳过（不崩溃、不误评估）
        latest = _obs("0.5", age_hours=100)
        latest.obs_time = latest.obs_time.replace(tzinfo=None)
        result, detect = await _run(monkeypatch, rules=[_rule()], latest_obs=latest)
        assert result["skipped_no_obs"] == 1
        assert result["evaluated"] == 0
        detect.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_combo_deduped(
        self,
        ctx: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 同 (metric_id, rule_type) 两条规则 → 只评估一次
        result, detect = await _run(
            monkeypatch,
            rules=[_rule(), _rule()],
            latest_obs=_obs("0.5"),
            detect_result=None,
        )
        assert result["combos"] == 1
        assert result["evaluated"] == 1
        detect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_detect_failure_does_not_block(
        self,
        ctx: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 单组合评估抛异常 → 不阻断整轮（combos 仍统计，异常被吞）
        result, detect = await _run(
            monkeypatch,
            rules=[_rule()],
            latest_obs=_obs("0.5"),
            detect_error=RuntimeError("eval boom"),
        )
        assert result["combos"] == 1
        assert result["evaluated"] == 0
        assert result["triggered"] == 0
        assert result["skipped_no_obs"] == 0
