"""HealthScorer 单元测试（对齐 TD §12.3 五维加权模型）。

覆盖：
1. 全字段齐全 → completeness=100, total ≥85 → EXCELLENT
2. 缺失关键字段 → completeness < 100
3. PII 指标未合规 → quality=30
4. 无 backup_owner → owner_response=45
5. 无 dependencies → lineage_coverage=10
6. _grade 分级函数正确性
7. 批量计算：部分成功部分跳过
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.metric import Metric
from app.services.semantic.health_scorer import HealthScorer, _grade

# ---- Fixtures ----


@pytest.fixture
def mock_db() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def scorer(mock_db: AsyncMock) -> HealthScorer:
    return HealthScorer(mock_db)


def _make_metric(
    *,
    pii_flag: bool = False,
    compliance_reviewed: bool = True,
    backup_owner_id: int | None = 99,
    updated_at: datetime | None = None,
    definition_json: dict | None = None,
    **overrides: object,
) -> MagicMock:
    """构造一个属性齐全的 metric mock。"""
    m = MagicMock(spec=Metric)
    m.id = 1
    m.metric_code = "test_metric"
    m.name = "测试指标"
    m.domain = "sales"
    m.type = "ATOMIC"
    m.granularity = "DAY"
    m.unit = "YUAN"
    m.aggregation = "SUM"
    m.time_semantics = "ACCUMULATED"
    m.freshness = "T+1"
    m.sla = "99.9%"
    m.dw_layer = "DWD"
    m.serving_mode = "OFFLINE"
    m.additivity = "ADDITIVE"
    m.pii_flag = pii_flag
    m.compliance_reviewed = compliance_reviewed
    m.backup_owner_id = backup_owner_id
    m.updated_at = updated_at or datetime.now(UTC)
    # 注意：空 dict {} 也是有效输入（口径缺失），不能用 `or` 否则被默认值覆盖
    m.definition_json = (
        definition_json
        if definition_json is not None
        else {
            "dependencies": ["table_a"],
            "expression": "SUM(amount)",
        }
    )
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


# ---- _grade 函数测试 ----


class TestGrade:
    def test_excellent(self) -> None:
        assert _grade(85) == "EXCELLENT"
        assert _grade(100) == "EXCELLENT"

    def test_good(self) -> None:
        assert _grade(70) == "GOOD"
        assert _grade(84) == "GOOD"

    def test_warning(self) -> None:
        assert _grade(55) == "WARNING"
        assert _grade(69) == "WARNING"

    def test_critical(self) -> None:
        assert _grade(54) == "CRITICAL"
        assert _grade(0) == "CRITICAL"


# ---- 维度计算测试 ----


class TestCompleteness:
    def test_all_fields_present(self, scorer: HealthScorer) -> None:
        metric = _make_metric()
        score, missing = scorer._calc_completeness(metric)
        assert score == 100
        assert missing is None

    def test_missing_granularity(self, scorer: HealthScorer) -> None:
        metric = _make_metric(granularity=None)
        score, missing = scorer._calc_completeness(metric)
        assert score < 100
        assert "granularity" in (missing or [])

    def test_missing_multiple_fields(self, scorer: HealthScorer) -> None:
        metric = _make_metric(granularity=None, unit=None, aggregation=None)
        score, missing = scorer._calc_completeness(metric)
        assert score <= 64  # 100 - 3*12
        assert len(missing or []) == 3


class TestQuality:
    @pytest.mark.asyncio
    async def test_pii_not_reviewed(self, scorer: HealthScorer, mock_db: AsyncMock) -> None:
        # P2-12: _calc_quality 查询近 30 天 quality_event；无异常事件时维持合规基础分
        mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: []))
        metric = _make_metric(pii_flag=True, compliance_reviewed=False)
        score, _ = await scorer._calc_quality(metric)
        assert score == 30

    @pytest.mark.asyncio
    async def test_compliance_reviewed(self, scorer: HealthScorer, mock_db: AsyncMock) -> None:
        mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: []))
        metric = _make_metric(compliance_reviewed=True)
        score, _ = await scorer._calc_quality(metric)
        assert score == 90

    @pytest.mark.asyncio
    async def test_default_quality(self, scorer: HealthScorer, mock_db: AsyncMock) -> None:
        mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: []))
        metric = _make_metric(pii_flag=False, compliance_reviewed=False)
        score, _ = await scorer._calc_quality(metric)
        assert score == 70

    @pytest.mark.asyncio
    async def test_open_p0_event_deducts(self, scorer: HealthScorer, mock_db: AsyncMock) -> None:
        """P2-12: 近 30 天未关闭 P0 异常 → 质量分按等级扣减。"""
        mock_db.execute = AsyncMock(
            return_value=MagicMock(all=lambda: [("P0", 1), ("P1", 2)])
        )
        metric = _make_metric(pii_flag=False, compliance_reviewed=True)
        score, _ = await scorer._calc_quality(metric)
        # 90 - 45*1 - 25*2 = 90 - 45 - 50 = -5 → 0
        assert score == 0

    @pytest.mark.asyncio
    async def test_open_minor_event_deducts_light(
        self, scorer: HealthScorer, mock_db: AsyncMock
    ) -> None:
        """P2-12: 单个 P2 异常轻扣。"""
        mock_db.execute = AsyncMock(
            return_value=MagicMock(all=lambda: [("P2", 1)])
        )
        metric = _make_metric(pii_flag=False, compliance_reviewed=True)
        score, _ = await scorer._calc_quality(metric)
        assert score == 90 - 12


class TestOwnerResponse:
    def test_with_backup(self, scorer: HealthScorer) -> None:
        metric = _make_metric(backup_owner_id=99)
        score, _ = scorer._calc_owner_response(metric)
        assert score == 85

    def test_without_backup(self, scorer: HealthScorer) -> None:
        metric = _make_metric(backup_owner_id=None)
        score, _ = scorer._calc_owner_response(metric)
        assert score == 45


class TestLineageCoverage:
    def test_deps_and_expression(self, scorer: HealthScorer) -> None:
        metric = _make_metric(definition_json={"dependencies": ["t1"], "expression": "SUM(x)"})
        score, missing = scorer._calc_lineage_coverage(metric)
        assert score == 80
        assert missing is None

    def test_expression_only(self, scorer: HealthScorer) -> None:
        metric = _make_metric(definition_json={"expression": "SUM(x)"})
        score, missing = scorer._calc_lineage_coverage(metric)
        assert score == 50
        assert missing is None

    def test_no_lineage(self, scorer: HealthScorer) -> None:
        metric = _make_metric(definition_json={})
        score, missing = scorer._calc_lineage_coverage(metric)
        assert score == 10
        assert "lineage_coverage" in (missing or [])


# ---- calculate 集成测试 ----


class TestCalculate:
    @pytest.mark.asyncio
    async def test_excellent_metric(self, scorer: HealthScorer, mock_db: AsyncMock) -> None:
        metric = _make_metric(pii_flag=False, compliance_reviewed=True)
        mock_db.get = AsyncMock(return_value=metric)
        # P2-12: 质量维度查询 quality_event，无异常事件
        mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: []))

        # mock db.merge + flush（calculate不持久化，只返回对象）
        mock_db.merge = AsyncMock()
        mock_db.flush = AsyncMock()

        result = await scorer.calculate(metric_id=1)
        assert result.score >= 85
        assert result.level == "EXCELLENT"

    @pytest.mark.asyncio
    async def test_critical_metric(self, scorer: HealthScorer, mock_db: AsyncMock) -> None:
        # 所有关键维度都低分
        metric = _make_metric(
            pii_flag=True,
            compliance_reviewed=False,
            backup_owner_id=None,
            definition_json={},
            granularity=None,
            unit=None,
            aggregation=None,
        )
        mock_db.get = AsyncMock(return_value=metric)
        mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: []))
        mock_db.merge = AsyncMock()
        mock_db.flush = AsyncMock()

        result = await scorer.calculate(metric_id=1)
        assert result.score < 55
        assert result.level == "CRITICAL"
        assert result.missing_dimensions is not None
        assert len(result.missing_dimensions) > 0

    @pytest.mark.asyncio
    async def test_metric_not_found(self, scorer: HealthScorer, mock_db: AsyncMock) -> None:
        mock_db.get = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="指标不存在"):
            await scorer.calculate(metric_id=999)


# ---- batch_calculate 测试 ----


class TestBatchCalculate:
    @pytest.mark.asyncio
    async def test_batch_partial_success(self, scorer: HealthScorer, mock_db: AsyncMock) -> None:
        metric_ok = _make_metric()
        mock_db.get = AsyncMock(side_effect=[metric_ok, None])
        mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: []))
        mock_db.merge = AsyncMock()
        mock_db.flush = AsyncMock()

        results = await scorer.batch_calculate([1, 999])
        # 第二个不存在，但 batch_calculate 容错跳过
        assert len(results) == 1
        assert results[0].score > 0
