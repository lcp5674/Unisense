"""auto_fill 引擎单元测试。"""

from __future__ import annotations

from app.services.semantic.auto_fill import (
    auto_fill,
    extract_biz_object,
    extract_measure,
    generate_metric_code,
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
        ok, err = validate_metric_code("sales_order_amount_day")
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

    def test_infer_metric_type_atomic(self) -> None:
        result = auto_fill(domain_code="test", measure_column="order_count")
        assert result["defaults"].get("type") == "atomic"

    def test_infer_metric_type_derived(self) -> None:
        result = auto_fill(domain_code="test", measure_column="conversion_rate")
        assert result["defaults"].get("type") == "derived"
