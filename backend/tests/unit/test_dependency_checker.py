"""依赖指标校验单元测试（对齐 TD §12.3 / spec FR-010/FR-011）。

覆盖：
- 未发布/已废弃依赖被拒
- DAG 环检测
- 多级链式依赖通过
- 复合指标多依赖通过
- PENDING_CONFIRMATION 版本依赖允许（metric.status 为 PUBLISHED）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.services.semantic.dependency_checker import DependencyChecker


def _make_metric(code: str, status: str, *, dependencies: list[str] | None = None) -> MagicMock:
    """构造 mock Metric 对象。"""
    metric = MagicMock()
    metric.metric_code = code
    metric.status = status
    metric.definition_json = {"dependencies": dependencies or []}
    return metric


def _make_checker() -> DependencyChecker:
    """构造 DependencyChecker（mock db）。"""
    db = AsyncMock()
    return DependencyChecker(db)


class TestUnpublishedDependencyRejected:
    """未发布依赖应出现在返回列表中。"""

    async def test_draft_dependency_rejected(self) -> None:
        checker = _make_checker()
        dep_metric = _make_metric("outp_fee_amount_daily", "DRAFT")
        checker._get_metric_by_code = AsyncMock(return_value=dep_metric)

        definition = {"dependencies": ["outp_fee_amount_daily"]}
        result = await checker.check_dependencies_published(definition)

        assert "outp_fee_amount_daily" in result

    async def test_deprecated_dependency_rejected(self) -> None:
        checker = _make_checker()
        dep_metric = _make_metric("outp_fee_amount_daily", "DEPRECATED")
        checker._get_metric_by_code = AsyncMock(return_value=dep_metric)

        definition = {"dependencies": ["outp_fee_amount_daily"]}
        result = await checker.check_dependencies_published(definition)

        assert "outp_fee_amount_daily" in result

    async def test_review_dependency_rejected(self) -> None:
        """REVIEW 状态的指标也不应被依赖。"""
        checker = _make_checker()
        dep_metric = _make_metric("outp_fee_amount_daily", "REVIEW")
        checker._get_metric_by_code = AsyncMock(return_value=dep_metric)

        definition = {"dependencies": ["outp_fee_amount_daily"]}
        result = await checker.check_dependencies_published(definition)

        assert "outp_fee_amount_daily" in result

    async def test_nonexistent_dependency_rejected(self) -> None:
        """不存在的依赖应出现在未发布列表中。"""
        checker = _make_checker()
        checker._get_metric_by_code = AsyncMock(return_value=None)

        definition = {"dependencies": ["outp_fee_amount_daily"]}
        result = await checker.check_dependencies_published(definition)

        assert "outp_fee_amount_daily" in result


class TestCycleDetected:
    """环检测：A→B→A 应检测到环。"""

    async def test_two_node_cycle(self) -> None:
        """A 依赖 B，B 依赖 A → 检测到环。"""
        checker = _make_checker()

        metric_a = _make_metric(
            "outp_drug_amount_daily", "PUBLISHED", dependencies=["outp_fee_amount_daily"]
        )
        metric_b = _make_metric(
            "outp_fee_amount_daily", "PUBLISHED", dependencies=["outp_drug_amount_daily"]
        )

        async def _get(code: str) -> MagicMock | None:
            if code == "outp_drug_amount_daily":
                return metric_a
            if code == "outp_fee_amount_daily":
                return metric_b
            return None

        checker._get_metric_by_code = AsyncMock(side_effect=_get)

        cycle = await checker.detect_cycle(
            "outp_drug_amount_daily",
            {"dependencies": ["outp_fee_amount_daily"]},
        )

        assert cycle is not None
        assert len(cycle) >= 2
        # 环路径应包含重复的起始节点
        assert cycle[0] == cycle[-1]


class TestThreeLevelChainPasses:
    """三级链式依赖通过：A→B(PUBLISHED)→C(PUBLISHED)。"""

    async def test_chain_no_cycle_all_published(self) -> None:
        checker = _make_checker()

        metric_b = _make_metric(
            "outp_fee_amount_daily", "PUBLISHED", dependencies=["outp_prescription_cnt_daily"]
        )
        metric_c = _make_metric("outp_prescription_cnt_daily", "PUBLISHED")

        async def _get(code: str) -> MagicMock | None:
            if code == "outp_fee_amount_daily":
                return metric_b
            if code == "outp_prescription_cnt_daily":
                return metric_c
            return None

        checker._get_metric_by_code = AsyncMock(side_effect=_get)

        # 无环
        cycle = await checker.detect_cycle(
            "outp_drug_amount_daily",
            {"dependencies": ["outp_fee_amount_daily"]},
        )
        assert cycle is None

        # 全部发布
        unpublished = await checker.check_dependencies_published(
            {"dependencies": ["outp_fee_amount_daily"]}
        )
        assert unpublished == []


class TestCompositeMultiDepPasses:
    """复合指标多依赖通过：A depends on B and C (both PUBLISHED)。"""

    async def test_multi_dep_all_published(self) -> None:
        checker = _make_checker()

        metric_b = _make_metric("outp_fee_amount_daily", "PUBLISHED")
        metric_c = _make_metric("outp_prescription_cnt_daily", "PUBLISHED")

        async def _get(code: str) -> MagicMock | None:
            if code == "outp_fee_amount_daily":
                return metric_b
            if code == "outp_prescription_cnt_daily":
                return metric_c
            return None

        checker._get_metric_by_code = AsyncMock(side_effect=_get)

        unpublished = await checker.check_dependencies_published(
            {"dependencies": ["outp_fee_amount_daily", "outp_prescription_cnt_daily"]}
        )
        assert unpublished == []


class TestPendingVersionDepAllowed:
    """PENDING_CONFIRMATION 版本依赖允许（metric.status=PUBLISHED）。"""

    async def test_pending_version_with_published_metric_status(self) -> None:
        """依赖指标 version 为 PENDING_CONFIRMATION，但 metric.status 为 PUBLISHED。
        消费方使用的是 CURRENT 版本，应视为已发布。
        """
        checker = _make_checker()
        # metric.status = PUBLISHED，尽管 version 可能有 PENDING_CONFIRMATION 的版本
        dep_metric = _make_metric("outp_fee_amount_daily", "PUBLISHED")
        checker._get_metric_by_code = AsyncMock(return_value=dep_metric)

        definition = {"dependencies": ["outp_fee_amount_daily"]}
        result = await checker.check_dependencies_published(definition)

        # PUBLISHED 指标不应出现在未发布列表中
        assert result == []

    async def test_experimental_dep_allowed(self) -> None:
        """EXPERIMENTAL 状态的指标也应允许被依赖。"""
        checker = _make_checker()
        dep_metric = _make_metric("outp_fee_amount_daily", "EXPERIMENTAL")
        checker._get_metric_by_code = AsyncMock(return_value=dep_metric)

        definition = {"dependencies": ["outp_fee_amount_daily"]}
        result = await checker.check_dependencies_published(definition)

        assert result == []


class TestNonMetricCodeDependency:
    """非指标编码（表名/字段名）依赖不参与校验。"""

    async def test_table_name_dependency_skipped(self) -> None:
        """表名 fct_order 不是 4 段式 metric_code，不参与依赖校验。"""
        checker = _make_checker()

        definition = {"dependencies": ["fct_order", "dim_product"]}
        result = await checker.check_dependencies_published(definition)

        # 非 4 段式编码不参与检查
        assert result == []

    async def test_mixed_metric_and_table_deps(self) -> None:
        """混合依赖：表名跳过，指标编码正常校验。"""
        checker = _make_checker()
        dep_metric = _make_metric("outp_fee_amount_daily", "PUBLISHED")
        checker._get_metric_by_code = AsyncMock(return_value=dep_metric)

        definition = {"dependencies": ["fct_order", "outp_fee_amount_daily"]}
        result = await checker.check_dependencies_published(definition)

        assert result == []
        # 只查了一次指标编码
        checker._get_metric_by_code.assert_awaited_once_with("outp_fee_amount_daily")


class TestValidateCompositeFormula:
    """复合指标公式强校验（界限文档 §1.2/§4.2：只引用派生/复合指标 code，禁裸表字段）。"""

    def _derived(self, code: str = "outp_fee_amount_daily") -> MagicMock:
        m = _make_metric(code, "PUBLISHED")
        m.type = "derived"
        return m

    async def test_valid_formula_passes(self) -> None:
        """公式仅引用已存在派生指标 + 函数关键字/数字/操作符 → 无错误。"""
        checker = _make_checker()
        derived = self._derived()
        checker._get_metric_by_code = AsyncMock(return_value=derived)

        definition = {
            "expression": "sum(outp_fee_amount_daily) / sum(outp_prescription_cnt_daily) * 100",
        }
        result = await checker.validate_composite_formula(definition)

        assert result == []
        checker._get_metric_by_code.assert_awaited()

    async def test_bare_field_rejected(self) -> None:
        """公式引用裸表字段（非 4 段式标识符）→ 报错。"""
        checker = _make_checker()

        definition = {"expression": "amount / head_amount"}
        result = await checker.validate_composite_formula(definition)

        assert any("amount" in err and "非指标标识符" in err for err in result)

    async def test_nonexistent_metric_rejected(self) -> None:
        """公式引用不存在的指标 code → 报错。"""
        checker = _make_checker()
        checker._get_metric_by_code = AsyncMock(return_value=None)

        definition = {"expression": "outp_fee_amount_daily / outp_prescription_cnt_daily"}
        result = await checker.validate_composite_formula(definition)

        assert any("不存在" in err for err in result)

    async def test_atomic_reference_rejected(self) -> None:
        """公式引用原子指标（非派生/复合）→ 报错（复合层只能聚合派生指标）。"""
        checker = _make_checker()
        atomic = _make_metric("outp_fee_amount_daily", "PUBLISHED")
        atomic.type = "atomic"
        checker._get_metric_by_code = AsyncMock(return_value=atomic)

        definition = {"expression": "outp_fee_amount_daily"}
        result = await checker.validate_composite_formula(definition)

        assert any("不是派生/复合指标" in err for err in result)

    async def test_sql_mode_exempt(self) -> None:
        """SQL 模式口径 → 豁免表达式 token 解析。"""
        checker = _make_checker()

        definition = {"sql": "SELECT SUM(amount) FROM fct_order", "expression": ""}
        result = await checker.validate_composite_formula(definition)

        assert result == []

    async def test_missing_expression_rejected(self) -> None:
        """复合指标既无 SQL 也无表达式 → 报错。"""
        checker = _make_checker()

        result = await checker.validate_composite_formula({"dependencies": ["a_b_c_d"]})

        assert any("缺少计算表达式" in err for err in result)
