"""命名规范词根硬卡单测（TD §12.3：受控词根/词素校验）。

覆盖：命中词根放行、裸词/无意义命名拦截、中文/英文词根、维度类豁免、空名拦截。
"""

from __future__ import annotations

from app.services.semantic.conflict_precheck import (
    CONTROLLED_MORPHEMES,
    ConflictPrechecker,
)


class TestValidateMetricName:
    def test_business_chinese_name_passes(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("每日收入金额")
        assert ok is True
        assert err is None

    def test_gmv_name_passes(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("每日 GMV")
        assert ok is True
        assert err is None

    def test_english_morpheme_case_insensitive(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("Daily GMV Amount")
        assert ok is True

    def test_order_count_passes(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("用户订单量")
        assert ok is True

    def test_rate_passes(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("退款率")
        assert ok is True

    def test_active_abbreviation_passes(self) -> None:
        """A-6：数仓活跃类高频缩写（月活/日活/周活/年活/季活）为合法业务命名，
        词根表须覆盖——否则 SQL 推断候选 name=「月活」被 METRIC_NAME_NO_MORPHEME 误拦。"""
        for name in ("月活", "日活", "周活", "年活", "季活", "医生月活"):
            ok, err = ConflictPrechecker.validate_metric_name(name)
            assert ok is True, f"{name!r} 应命中活跃缩写词根，实际 err={err!r}"

    def test_bare_word_rejected_with_clear_error(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("新名称")
        assert ok is False
        assert err is not None
        assert "受控词根" in err
        assert "收入" in err

    def test_nonsense_name_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("abc")
        assert ok is False

    def test_empty_name_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("")
        assert ok is False

    def test_whitespace_name_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("   ")
        assert ok is False

    def test_dimension_metric_exempt(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name(
            "区域", metric_type="dimension"
        )
        assert ok is True
        assert err is None

    def test_non_dimension_not_exempt(self) -> None:
        ok, err = ConflictPrechecker.validate_metric_name("区域", metric_type="atomic")
        assert ok is False

    def test_controlled_morphemes_exposed_on_class(self) -> None:
        assert ConflictPrechecker.CONTROLLED_MORPHEMES is CONTROLLED_MORPHEMES
        assert "收入" in CONTROLLED_MORPHEMES
        assert "订单" in CONTROLLED_MORPHEMES
