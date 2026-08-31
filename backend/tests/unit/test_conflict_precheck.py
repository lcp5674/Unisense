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

    def test_domain_with_underscore_passes(self) -> None:
        # 域编码本身可含下划线（如 online_consultation）：字面 >4 段但语义 4 段，
        # 后 3 段无下划线时合并前面为域段 → 放行
        ok, err = ConflictPrechecker.validate_code_format(
            "online_consultation_wy_imageconsultcntday_day"
        )
        assert ok is True
        assert err is None
        ok, err = ConflictPrechecker.validate_code_format("a_b_c_d_e")
        assert ok is True
        assert err is None

    def test_multi_segment_bad_tail_fails(self) -> None:
        # 多段字面但后 3 段（业务对象/度量/周期）含大写或数字开头 → 拒
        ok, err = ConflictPrechecker.validate_code_format("a_b_c_d_E")
        assert ok is False
        assert "后 3 段" in err
        ok, err = ConflictPrechecker.validate_code_format("a_b_c_d_1e")
        assert ok is False
        assert "后 3 段" in err

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

    def test_four_segments_with_invalid_char_rejected(self) -> None:
        # 恰好 4 段，但某段以小写字母外字符开头（命中"每段格式"分支）
        ok, err = ConflictPrechecker.validate_code_format("sales_gmv_2amount_day")
        assert ok is False
        assert "小写字母" in err

    def test_four_segments_with_uppercase_rejected(self) -> None:
        ok, err = ConflictPrechecker.validate_code_format("sales_GMV_amount_day")
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

    async def test_precheck_without_loader_degrades_to_none(self) -> None:
        # 未注入 existing_loader → 显式降级为空操作，不抛异常
        prechecker = ConflictPrechecker()
        result = await prechecker.precheck("sales_gmv_amount_day", {"definition": "GMV"})
        assert result is None

    async def test_precheck_empty_existing_returns_none(self) -> None:
        async def loader():
            return []

        prechecker = ConflictPrechecker(existing_loader=loader)
        result = await prechecker.precheck("sales_gmv_amount_day", {"definition": "GMV"})
        assert result is None

    async def test_precheck_detects_same_def_diff_name(self) -> None:
        async def loader():
            return [
                {
                    "metric_code": "sales_gmv_amount_daily",
                    "domain": "sales",
                    "definition": "当日支付 GMV 总额",
                    "source_tables": ["ods.order"],
                    "status": "PUBLISHED",
                    "metric_id": 1,
                }
            ]

        prechecker = ConflictPrechecker(existing_loader=loader)
        result = await prechecker.precheck(
            "sales_gmv_amount_day", {"domain": "sales", "definition": "当日支付 GMV 总额"}
        )
        assert result is not None
        assert result["conflict_type"] == "same_def_diff_name"
        assert result["existing_code"] == "sales_gmv_amount_daily"
        assert result["block_publish"] is False  # 软冲突

    async def test_precheck_detects_same_name_diff_def(self) -> None:
        async def loader():
            return [
                {
                    "metric_code": "sales_gmv_amount_day",
                    "domain": "sales",
                    "definition": "当日平均客单价",
                    "status": "PUBLISHED",
                    "metric_id": 3,
                }
            ]

        prechecker = ConflictPrechecker(existing_loader=loader)
        result = await prechecker.precheck(
            "sales_gmv_amount_day", {"domain": "finance", "definition": "当日支付 GMV 总额"}
        )
        assert result is not None
        assert result["conflict_type"] == "same_name_diff_def"
        assert result["severity"] == "hard"
        assert result["block_publish"] is True

    async def test_precheck_detects_pii_unauthorized(self) -> None:
        async def loader():
            return [
                {
                    "metric_code": "sales_gmv_amount_day",
                    "domain": "sales",
                    "definition": "GMV",
                    "status": "PUBLISHED",
                    "metric_id": 1,
                }
            ]

        prechecker = ConflictPrechecker(existing_loader=loader)
        result = await prechecker.precheck(
            "crm_user_phone_cnt_day",
            {"domain": "crm", "definition": "手机号去重计数", "pii": True, "pii_authorized": False},
        )
        assert result is not None
        assert result["conflict_type"] == "pii"
        assert result["severity"] == "hard"
        assert result["block_publish"] is True

    async def test_precheck_detects_dependency_unpublished(self) -> None:
        # 已存在口径与被依赖指标编码不同（避免相似度冲突先触发），但状态为 DRAFT
        async def loader():
            return [
                {
                    "metric_code": "crm_customer_cnt_day",
                    "domain": "crm",
                    "definition": "客户去重数",
                    "status": "DRAFT",
                    "metric_id": 1,
                }
            ]

        prechecker = ConflictPrechecker(existing_loader=loader)
        result = await prechecker.precheck(
            "sales_gmv_rate_day",
            {
                "domain": "sales",
                "definition": "GMV 增长率",
                "dependencies": ["crm_customer_cnt_day"],
            },
        )
        assert result is not None
        assert result["conflict_type"] == "DEPENDENCY_UNPUBLISHED"
        assert "crm_customer_cnt_day" in result["reason"]
        assert result["block_publish"] is False

    async def test_precheck_ignores_non_metric_dependencies(self) -> None:
        async def loader():
            return [
                {
                    "metric_code": "crm_customer_cnt_day",
                    "domain": "crm",
                    "definition": "客户去重数",
                    "status": "DRAFT",
                    "metric_id": 1,
                }
            ]

        prechecker = ConflictPrechecker(existing_loader=loader)
        # 依赖为表名/字段名（非 4 段式指标编码）→ 不触发依赖未发布
        result = await prechecker.precheck(
            "sales_gmv_rate_day",
            {"domain": "sales", "definition": "GMV 增长率", "dependencies": ["ods.order"]},
        )
        assert result is None

    async def test_precheck_no_conflict_returns_none(self) -> None:
        async def loader():
            return [
                {
                    "metric_code": "crm_customer_cnt_day",
                    "domain": "crm",
                    "definition": "客户去重数",
                    "status": "PUBLISHED",
                    "metric_id": 9,
                }
            ]

        prechecker = ConflictPrechecker(existing_loader=loader)
        result = await prechecker.precheck(
            "sales_gmv_amount_day", {"domain": "sales", "definition": "当日支付 GMV 总额"}
        )
        assert result is None


class TestToCandidateMountAuthority:
    """OneData 挂载层权威：extra_source_tables（挂载实体的 source_table）并入预检比对。"""

    def test_to_candidate_merges_extra_source_tables(self) -> None:
        """挂载源表并入 candidate.source_tables（挂载独立更新后预检基于最新物理来源）。"""
        candidate = ConflictPrechecker._to_candidate(
            "sales_gmv_amount_day",
            {"domain": "sales", "definition": "GMV", "source_tables": ["dwd.sales"]},
            extra_source_tables=["dwd.sales_detail"],
        )
        assert candidate["source_tables"] == ["dwd.sales", "dwd.sales_detail"]

    def test_to_candidate_extra_source_tables_dedup(self) -> None:
        """挂载源表与 definition 已有源表重复时去重，空值忽略。"""
        candidate = ConflictPrechecker._to_candidate(
            "sales_gmv_amount_day",
            {"source_tables": ["dwd.sales_detail"]},
            extra_source_tables=["dwd.sales_detail", "ods.order", "", None],
        )
        assert candidate["source_tables"] == ["dwd.sales_detail", "ods.order"]
