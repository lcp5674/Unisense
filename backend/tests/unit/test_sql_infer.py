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

    def test_insert_overwrite_passthrough_sinks_to_agg(self) -> None:
        """ETL 透传 INSERT：最外层投影无聚合 → 下沉子查询找聚合度量。

        典型落宽表形态 ``insert overwrite ... select a.col, a.cnt ... from (聚合子查询) a``，
        度量在内层子查询（含 UNION/字典 join），须下沉提取且带 alias/table/expression。
        """
        sql = """
        INSERT OVERWRITE TABLE dws.doctor_active_month_di
        SELECT a.month_id, a.hosp_code, coalesce(b.org_name, '-99') AS hosp_name,
               a.current_month_active_doctor_cnt, a.last_month_active_doctor_cnt
        FROM (
            SELECT t1.month_id, t1.hosp_code,
                   count(distinct t1.doctor_code) AS current_month_active_doctor_cnt,
                   count(distinct case when t2.doctor_code is not null then t2.doctor_code end)
                       AS last_month_active_doctor_cnt
            FROM wedw_dw.doctor_visit_agent_info_da t1
            LEFT JOIN wedw_dw.doctor_visit_agent_info_da t2
              ON t1.doctor_code = t2.doctor_code
            GROUP BY t1.month_id, t1.hosp_code
        ) a
        LEFT JOIN (
            SELECT DISTINCT rel_code, org_name FROM wedw_dw.disease_care_sys_org_staff_relation_df
        ) b ON a.hosp_code = b.rel_code
        """
        p = parse_sql_profile(sql)
        assert len(p.measures) == 2
        by_alias = {m["alias"]: m for m in p.measures}
        assert set(by_alias) == {"current_month_active_doctor_cnt", "last_month_active_doctor_cnt"}
        cur = by_alias["current_month_active_doctor_cnt"]
        assert cur["column"] == "doctor_code"
        assert cur["agg"] == "COUNT_DISTINCT"
        # 源表取聚合所在子查询的表（非 join 右侧字典表）
        assert cur["table"] == "wedw_dw.doctor_visit_agent_info_da"
        last = by_alias["last_month_active_doctor_cnt"]
        assert last["column"] == "doctor_code"  # case then 分支列提取
        assert "CASE WHEN" in (last["expression"] or "").upper()

    def test_direct_aggregation_keeps_legacy_shape(self) -> None:
        """直接投影聚合（非下沉）：measures 保持基础 column/agg 结构，不附加 enrich 键。"""
        p = parse_sql_profile(
            "SELECT dt, SUM(amount) AS gmv, COUNT(DISTINCT user_id) AS uv "
            "FROM dwd_order_di GROUP BY dt"
        )
        assert p.measures == [
            {"column": "amount", "agg": "SUM"},
            {"column": "user_id", "agg": "COUNT_DISTINCT"},
        ]
