"""sql_infer SQL 解析画像单元测试。"""

from __future__ import annotations

from app.services.semantic.sql_infer import parse_sql_profile


class TestParseSqlProfile:
    def test_simple_sum_group_by(self) -> None:
        sql = """
        SELECT shop_id, SUM(amount) AS amount
        FROM dwd.sales_detail
        WHERE dt = DATE_SUB(CURRENT_DATE, INTERVAL 1 DAY)
        GROUP BY shop_id, dt
        """
        p = parse_sql_profile(sql)
        assert "dwd.sales_detail" in p.source_tables
        assert "shop_id" in p.group_by
        assert "dt" in p.group_by
        assert p.measures == [{"column": "amount", "agg": "SUM"}]
        assert p.time_column == "dt"

    def test_count_distinct(self) -> None:
        sql = "SELECT COUNT(DISTINCT user_id) AS uv FROM dwd.user_active"
        p = parse_sql_profile(sql)
        assert p.measures == [{"column": "user_id", "agg": "COUNT_DISTINCT"}]

    def test_ratio_derived(self) -> None:
        sql = (
            "SELECT SUM(pay_amount)/SUM(order_amount) AS rate "
            "FROM dwd.sales_detail WHERE dt = DATE_SUB(CURRENT_DATE, INTERVAL 1 DAY)"
        )
        p = parse_sql_profile(sql)
        assert "/" in (p.sql or "")

    def test_ctas_extracts_source(self) -> None:
        sql = (
            "CREATE TABLE ads.gmv_day AS "
            "SELECT dt, SUM(gmv) AS gmv FROM dws.sales_agg GROUP BY dt"
        )
        p = parse_sql_profile(sql)
        assert "dws.sales_agg" in p.source_tables
        assert "dt" in p.group_by

    def test_empty_sql(self) -> None:
        assert parse_sql_profile("").source_tables == []
        assert parse_sql_profile("   ").sql is None

    def test_invalid_sql_degrades(self) -> None:
        p = parse_sql_profile("SELECT FROM WHERE (((NOT VALID")
        assert isinstance(p, object)
        # 降级：不抛异常
        assert p.source_tables == []
