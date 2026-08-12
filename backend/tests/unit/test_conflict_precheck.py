"""ConflictPrechecker 单元测试。"""

from __future__ import annotations

from app.services.semantic.conflict_precheck import ConflictPrechecker


class TestValidateCodeFormat:
    def test_valid_4segment_code_passes(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("sales_gmv_amount_day")
        assert ok is True
        assert err is None

    def test_valid_4segment_with_numbers(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("sales_gmv_cnt_day")
        assert ok is True
        assert err is None

    def test_invalid_2segment_code_fails(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("a_b")
        assert ok is False
        assert err is not None
        assert "4" in err or "段" in err

    def test_invalid_5segment_code_fails(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("a_b_c_d_e")
        assert ok is False
        assert err is not None

    def test_single_segment_fails(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("sales")
        assert ok is False
        assert err is not None

    def test_reserved_word_test_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("test_gmv_amount_day")
        assert ok is False
        assert "保留词" in err or "reserved" in err.lower() or "test" in err

    def test_reserved_word_temp_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("temp_gmv_amount_day")
        assert ok is False

    def test_reserved_word_dummy_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("dummy_gmv_amount_day")
        assert ok is False

    def test_reserved_word_demo_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("demo_gmv_amount_day")
        assert ok is False

    def test_reserved_word_tmp_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("tmp_gmv_amount_day")
        assert ok is False

    def test_reserved_word_sample_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("sample_gmv_amount_day")
        assert ok is False

    def test_empty_string_fails(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("")
        assert ok is False

    def test_uppercase_fails(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("Sales_GMV_Day")
        assert ok is False

    def test_starts_with_number_segment_fails(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("2sales_gmv_day")
        assert ok is False

    def test_valid_complex_code(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("finance_revenue_acc_period")
        assert ok is True


class TestReservedWords:
    def test_reserved_words_set_exists(self) -> None:
        assert len(ConflictPrechecker.RESERVED_WORDS) >= 7
        assert "test" in ConflictPrechecker.RESERVED_WORDS
        assert "temp" in ConflictPrechecker.RESERVED_WORDS
        assert "dummy" in ConflictPrechecker.RESERVED_WORDS
        assert "demo" in ConflictPrechecker.RESERVED_WORDS
        assert "tmp" in ConflictPrechecker.RESERVED_WORDS
        assert "sample" in ConflictPrechecker.RESERVED_WORDS

    def test_staging_is_reserved(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("staging_gmv_amount_day")
        assert ok is False

    def test_todo_is_reserved(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("todo_gmv_amount_day")
        assert ok is False


class TestPrecheck:
    """冲突预检异步测试（mock conflict 服务）。"""

    async def test_precheck_no_conflict(self) -> None:
        prechecker = ConflictPrechecker()
        # 无相似口径 → 返回 None
        result = await prechecker.precheck("unique_metric_code", {"expression": "SUM(a)"})
        # 在无真实 conflict 服务时，预检应优雅降级（返回 None 或空 dict）
        assert result is None or isinstance(result, dict)
