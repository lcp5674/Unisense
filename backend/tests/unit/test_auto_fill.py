"""auto_fill 引擎单元测试。"""

from __future__ import annotations

from app.services.semantic.auto_fill import (
    _cn_column_label,
    _metric_name_morpheme_missing,
    auto_fill,
    build_profile,
    extract_biz_object,
    extract_measure,
    generate_metric_code,
    infer_metric,
    validate_metric_code,
)


class TestExtractBizObject:
    def test_simple_table(self) -> None:
        assert extract_biz_object("orders") == "orders"

    def test_with_warehouse_prefix(self) -> None:
        assert extract_biz_object("dwd.sales_detail") == "sales"

    def test_ods_prefix(self) -> None:
        assert extract_biz_object("ods_raw_events") == "raw"

    def test_dim_prefix(self) -> None:
        assert extract_biz_object("dim_product_category") == "product"


class TestExtractMeasure:
    def test_simple(self) -> None:
        assert extract_measure("amount") == "amount"

    def test_with_underscores(self) -> None:
        assert extract_measure("order_amount") == "orderamount"


class TestGenerateMetricCode:
    def test_basic(self) -> None:
        code = generate_metric_code("sales", "dwd.sales_detail", "amount", "day")
        assert code == "sales_sales_amount_day"

    def test_with_prefix(self) -> None:
        code = generate_metric_code("finance", "ods.raw_revenue", "total_amount", "month")
        assert code == "finance_raw_totalamount_month"


class TestValidateMetricCode:
    def test_valid_code(self) -> None:
        ok, err = validate_metric_code("sales_gmv_amount_day")
        assert ok is True
        assert err == ""

    def test_too_few_segments(self) -> None:
        ok, err = validate_metric_code("sales_amount")
        assert ok is False
        assert "4段" in err

    def test_uppercase_rejected(self) -> None:
        ok, err = validate_metric_code("Sales_order_amount_day")
        assert ok is False

    def test_reserved_word(self) -> None:
        ok, err = validate_metric_code("sales_select_amount_day")
        assert ok is False
        assert "保留词" in err


class TestAutoFill:
    def test_basic_fill(self) -> None:
        result = auto_fill(
            domain_code="sales",
            source_table="dwd.sales_detail",
            measure_column="amount",
            period="day",
        )
        assert result["metric_code_suggestion"] == "sales_sales_amount_day"
        assert result["defaults"]["dw_layer"] == "DWD"
        assert result["defaults"]["granularity"] == "day"
        assert result["segments"]["domain"] == "sales"
        assert result["segments"]["biz_object"] == "sales"

    def test_with_domain_defaults(self) -> None:
        result = auto_fill(
            domain_code="finance",
            domain_defaults={"unit": "CNY", "aggregation": "SUM"},
        )
        assert result["defaults"]["unit"] == "CNY"
        assert result["defaults"]["aggregation"] == "SUM"

    def test_no_source_table(self) -> None:
        result = auto_fill(domain_code="user")
        assert result["metric_code_suggestion"] is None
        assert result["segments"]["domain"] == "user"

    def test_infer_dw_layer(self) -> None:
        result = auto_fill(domain_code="test", source_table="dwd.test_table")
        assert result["defaults"].get("dw_layer") == "DWD"

    def test_infer_metric_type_sql_physical_derived(self) -> None:
        """OneData 语义（方案 A）：SQL 物理口径（列单度量直算）→ 派生指标——
        原子只从逻辑度量目录创建，SQL 推断一律不产原子。"""
        result = auto_fill(domain_code="test", measure_column="order_count")
        assert result["defaults"].get("type") == "derived"

    def test_infer_metric_type_ratio_composite(self) -> None:
        """OneData 语义：列名含比率语义（rate/ratio/pct）= 多指标比率 → 复合指标。"""
        result = auto_fill(domain_code="test", measure_column="conversion_rate")
        assert result["defaults"].get("type") == "composite"

    def test_infer_metric_type_period_month_derived(self) -> None:
        """OneData 语义：month 周期 = 原子（活跃医生数）+ 时间周期 → 派生指标。"""
        result = auto_fill(
            domain_code="outpatient",
            source_table="wedw_dw.doctor_visit_agent_info_da",
            measure_column="doctor_code",
            period="month",
        )
        assert result["defaults"].get("type") == "derived"

    def test_infer_metric_type_period_day_derived(self) -> None:
        """OneData 语义（方案 A）：日粒度 = 派生最小周期——SQL 物理口径无论
        周期一律归派生（原子只从逻辑度量目录创建）。"""
        result = auto_fill(
            domain_code="sales",
            source_table="dwd.sales_detail",
            measure_column="amount",
            period="day",
        )
        assert result["defaults"].get("type") == "derived"


class TestInferMetricSql:
    """SQL 驱动的多字段推断。"""

    def test_full_inference_from_sql(self) -> None:
        sql = """
        SELECT shop_id, dt, SUM(amount) AS amount
        FROM dwd.sales_detail
        WHERE dt = DATE_SUB(CURRENT_DATE, INTERVAL 1 DAY)
        GROUP BY shop_id, dt
        """
        profile = build_profile(
            sql=sql,
            period="day",
            measure_meta={"type": "decimal(18,2)", "comment": "订单金额"},
        )
        # 注入 domain_code（endpoint 会设置）
        profile["domain_code"] = ""
        result = infer_metric(profile)
        f = result["fields"]
        assert f["aggregation"]["value"] == "SUM"
        assert f["aggregation"]["source"] == "sql_parse"
        assert f["granularity"]["value"] == "day"
        # 方案 A：SQL 物理口径（源表+列聚合）→ 派生（原子只从逻辑度量目录创建）
        assert f["type"]["value"] == "derived"
        assert f["additivity"]["value"] == "ADDITIVE"
        assert f["serving_mode"]["value"] == "BATCH_ONLY"
        assert f["definition_mode"]["value"] == "sql"
        assert "dwd.sales_detail" in f["definition_json"]["value"]["source_tables"]
        # 名称：列注释优先
        assert f["name"]["value"] == "日订单金额"
        assert f["name"]["source"] == "column_meta"

    def test_fields_include_source_table_and_measure_column(self) -> None:
        """fields 须回填 source_table/measure_column（SQL 解析来源），供前端回填 Step 2。"""
        sql = """
        SELECT dt, SUM(gmv) AS gmv
        FROM dwd.sales_detail
        WHERE dt >= '2024-01-01'
        GROUP BY dt
        """
        profile = build_profile(sql=sql, period="day")
        profile["domain_code"] = ""
        result = infer_metric(profile)
        f = result["fields"]
        assert f["source_table"]["value"] == "dwd.sales_detail"
        assert f["source_table"]["source"] == "sql_parse"
        assert f["measure_column"]["value"] == "gmv"
        assert f["measure_column"]["source"] == "sql_parse"

    def test_fields_source_table_from_input(self) -> None:
        """显式传入 source_table/measure_column 时，fields 原样回填且来源为 input。"""
        profile = build_profile(
            source_table="dws.account_balance", measure_column="end_bal", period="day"
        )
        profile["domain_code"] = ""
        result = infer_metric(profile)
        f = result["fields"]
        assert f["source_table"]["value"] == "dws.account_balance"
        assert f["source_table"]["source"] == "input"
        assert f["measure_column"]["value"] == "end_bal"
        assert f["measure_column"]["source"] == "input"

    def test_count_distinct_from_sql(self) -> None:
        sql = "SELECT dt, COUNT(DISTINCT user_id) AS uv FROM dwd.user_active GROUP BY dt"
        profile = build_profile(sql=sql, period="day")
        profile["domain_code"] = ""
        result = infer_metric(profile)
        f = result["fields"]
        assert f["aggregation"]["value"] == "COUNT_DISTINCT"
        assert f["additivity"]["value"] == "ADDITIVE"

    def test_time_semantics_ytd(self) -> None:
        sql = (
            "SELECT SUM(gmv) AS gmv FROM dwd.sales_detail "
            "WHERE YEAR(dt)=YEAR(CURRENT_DATE) GROUP BY dt"
        )
        profile = build_profile(sql=sql, period="day")
        profile["domain_code"] = ""
        result = infer_metric(profile)
        assert result["fields"]["time_semantics"]["value"] == "YTD"

    def test_ratio_type_composite(self) -> None:
        """OneData 语义：比率/跨度量运算（SUM/SUM）= 复合指标（B1 修正，此前误判派生）。"""
        sql = (
            "SELECT SUM(pay)/SUM(order_cnt) AS rate "
            "FROM dwd.sales_detail WHERE dt = DATE_SUB(CURRENT_DATE, INTERVAL 1 DAY)"
        )
        profile = build_profile(sql=sql, period="day")
        profile["domain_code"] = ""
        result = infer_metric(profile)
        assert result["fields"]["type"]["value"] == "composite"

    def test_ratio_type_composite_mul_single_projection(self) -> None:
        """R2：单投影双聚合（SELECT SUM(a)*SUM(b)）含乘法运算 → 复合指标。

        修复前 `_is_ratio_expression` 只识别 `/` 与 ≥2 度量分支的 `/|+|-`，
        缺 Mul/Mod，且 measures==1 的单投影双聚合不判复合（与批量路径
        `_build_composite_candidate` 的 `sql_has_arithmetic` 判定不一致）。
        """
        sql = (
            "SELECT SUM(pay)*SUM(order_cnt) AS weighted "
            "FROM dwd.sales_detail WHERE dt = DATE_SUB(CURRENT_DATE, INTERVAL 1 DAY)"
        )
        profile = build_profile(sql=sql, period="day")
        profile["domain_code"] = ""
        result = infer_metric(profile)
        assert result["fields"]["type"]["value"] == "composite"

    def test_semi_additive_for_balance(self) -> None:
        profile = build_profile(
            source_table="dws.account_balance", measure_column="end_bal", period="day"
        )
        profile["domain_code"] = ""
        result = infer_metric(profile)
        f = result["fields"]
        assert f["aggregation"]["value"] == "LAST_VALUE"
        assert f["additivity"]["value"] == "SEMI_ADDITIVE"

    def test_count_unit_uses_valid_dict_code(self) -> None:
        """计数类列（cnt/count/num）unit 须为字典合法 code TIMES，而非不存在的 COUNT。

        unit 字典无 COUNT code；修复前推断 COUNT 导致批量注册报
        「字典值不存在: unit/COUNT」。
        """
        # 列名计数语义
        profile = build_profile(
            source_table="dwd.tj_cf_drug_prescription_df",
            measure_column="visit_cnt",
            period="day",
        )
        profile["domain_code"] = "outpatient"
        result = infer_metric(profile)
        f = result["fields"]
        assert f["unit"]["value"] == "TIMES"
        assert f["unit"]["source"] == "rule"
        # 聚合仍为 COUNT（aggregation 字典含 COUNT，合法）
        assert f["aggregation"]["value"] == "COUNT"

    def test_count_unit_from_column_meta(self) -> None:
        """列元数据计数类（次数/数量）unit 推断为 TIMES。"""
        profile = build_profile(
            source_table="dwd.tj_cf_drug_prescription_df",
            measure_column="prescription_cnt",
            period="day",
            measure_meta={"type": "int", "comment": "处方次数"},
        )
        profile["domain_code"] = "outpatient"
        result = infer_metric(profile)
        f = result["fields"]
        assert f["unit"]["value"] == "TIMES"
        assert f["unit"]["source"] == "column_meta"

    def test_count_column_label_hits_controlled_morpheme(self) -> None:
        """无列注释的计数列生成中文标签，命中受控词根（修复前为英文 slug 被命名校验拦截）。

        列注释优先逻辑不受影响（本用例 measure_meta 无 comment）。
        """
        profile = build_profile(
            source_table="ods_his_register",
            measure_column="register_cnt",
            period="day",
        )
        profile["domain_code"] = "outpatient"
        result = infer_metric(profile)
        f = result["fields"]
        # label「挂号次数」→ name「日挂号次数」，命中「挂号/次数」受控词根
        assert f["name"]["value"] == "日挂号次数"
        assert f["name"]["source"] == "rule"

    def test_unknown_count_column_label_falls_back_to_times(self) -> None:
        """未知计数列名主干保底「xx次数」，仍命中「次数」受控词根。"""
        profile = build_profile(
            source_table="ods_his_register",
            measure_column="xyz_cnt",
            period="day",
        )
        profile["domain_code"] = "outpatient"
        result = infer_metric(profile)
        f = result["fields"]
        assert f["name"]["value"] == "日xyz次数"
        assert f["name"]["source"] == "rule"
        # 非计数列（如 gmv）映射为业务中文标签，同样命中「额」词根
        profile2 = build_profile(
            source_table="dwd.sales_detail", measure_column="gmv", period="day"
        )
        profile2["domain_code"] = "sales"
        result2 = infer_metric(profile2)
        assert result2["fields"]["name"]["value"] == "日成交额"

    def test_amount_column_label_hits_controlled_morpheme(self) -> None:
        """金额/费用类列名生成中文标签（fee_amount→费用），命中「费/额」词根。"""
        profile = build_profile(
            source_table="dwd.tj_cf_drug_prescription_df",
            measure_column="fee_amount",
            period="day",
        )
        profile["domain_code"] = "outpatient"
        result = infer_metric(profile)
        assert result["fields"]["name"]["value"] == "日费用"
        assert result["fields"]["unit"]["value"] == "CNY"


class TestAutoFillBackwardCompat:
    """auto_fill 旧结构 + 新附加字段兼容。"""

    def test_returns_legacy_and_new_keys(self) -> None:
        result = auto_fill(
            domain_code="sales",
            source_table="dwd.sales_detail",
            measure_column="amount",
            period="day",
            sql="SELECT dt, SUM(amount) AS a FROM dwd.sales_detail GROUP BY dt",
        )
        assert result["metric_code_suggestion"] == "sales_sales_amount_day"
        assert result["defaults"]["dw_layer"] == "DWD"
        # 方案 A：SQL 物理口径 → 派生（原子只从逻辑度量目录创建）
        assert result["defaults"]["type"] == "derived"
        # 新字段
        assert "fields" in result
        assert result["definition_mode"] == "sql"
        assert result["definition_json"]["source_tables"] == ["dwd.sales_detail"]

    def test_domain_default_overrides(self) -> None:
        result = auto_fill(
            domain_code="finance",
            sql="SELECT SUM(amount) AS a FROM dwd.t",
            domain_defaults={"unit": "CNY", "aggregation": "SUM", "metric_tier": "T1"},
        )
        f = result["fields"]
        # 域默认值覆盖并标记来源
        assert f["unit"]["value"] == "CNY"
        assert f["unit"]["source"] == "domain_default"
        assert f["metric_tier"]["value"] == "T1"
        # 仍保留 SQL 推断的聚合（与域默认一致）
        assert f["aggregation"]["value"] == "SUM"


class TestCnColumnLabelMedical:
    """A-5：医疗词表扩充——建表注释缺失时词表兜底不再产出「doctor次数」等英文残片。"""

    def test_doctor_cnt_maps_to_medical_label(self) -> None:
        # 计数后缀取主干最后一个 token，命中扩充词表 → 中文标签
        assert _cn_column_label("current_month_active_doctor_cnt") == "医生数"
        assert _cn_column_label("last_month_active_doctor_cnt") == "医生数"
        assert _cn_column_label("nurse_cnt") == "护士数"
        assert _cn_column_label("hosp_cnt") == "医院数"
        assert _cn_column_label("inpatient_cnt") == "住院人次"
        assert _cn_column_label("ward_cnt") == "病区数"
        assert _cn_column_label("bed_cnt") == "床位数"

    def test_full_token_medical_labels(self) -> None:
        # 非计数后缀直接命中词表
        assert _cn_column_label("avg_stay") == "平均住院日"
        assert _cn_column_label("diagnosis") == "诊断数"
        assert _cn_column_label("operation") == "手术人次"


class TestMetricNameMorphemeMissing:
    """决策 2：推断名未命中受控词根时**保留原样 + 软提示**（不追加词根、不硬拒）——
    词根硬卡只留给手动命名；推断名来自数仓注释/LLM，本身更权威。"""

    def test_hits_morpheme_not_missing(self) -> None:
        # 「月活」已补进词根表（活跃缩写系列），命中则不算缺失
        assert _metric_name_morpheme_missing("月活") is False
        assert _metric_name_morpheme_missing("日活") is False
        assert _metric_name_morpheme_missing("门诊挂号人次") is False
        assert _metric_name_morpheme_missing("每日收入金额") is False

    def test_semantic_words_kept_original(self) -> None:
        # 完全未命中词根的合法业务新词（如「管理事项」「某专项」）——
        # 决策 2 不再追加「金额/次数」污染名称，判定为缺失（前端软提示人工确认）
        assert _metric_name_morpheme_missing("管理事项") is True
        assert _metric_name_morpheme_missing("abc") is True
        assert _metric_name_morpheme_missing("某指标") is True

    def test_infer_name_comment_morpheme_not_rewritten(self) -> None:
        # _infer_name 注释分支：「月活」命中词根 → 名称保持「月活」不被改写
        f = infer_metric(
            build_profile(
                source_table="t",
                measure_column="cnt",
                period="month",
                measure_meta={"comment": "月活"},
            ),
            domain_defaults={},
        )
        assert f["fields"]["name"]["value"] == "月活"
        assert f["fields"]["name"]["source"] == "column_meta"
        assert "未命中受控词根" not in f["fields"]["name"]["reason"]

    def test_infer_name_fallback_keeps_original_with_soft_hint(self) -> None:
        # _infer_name 规则分支：无注释英文列名未命中 → **保留原样不追加**，
        # reason 标注软提示「建议人工确认」（此前行为是追加「次数」并注明追加）
        f = infer_metric(
            build_profile(
                source_table="t",
                measure_column="abc_metric",
                period="day",
                measure_meta={},
            ),
            domain_defaults={},
        )
        name = f["fields"]["name"]["value"]
        # 决策 2：名称保留原样（不再以「次数」结尾；周期前缀「日」仍按既有规则保留）
        assert not name.endswith("次数")
        assert name == "日abc metric"
        assert "未命中受控词根（推断名，建议人工确认）" in f["fields"]["name"]["reason"]

