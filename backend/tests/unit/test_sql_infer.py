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

    # ------------------------------------------------------------ 时间粒度识别

    def test_time_granularity_substr_month(self) -> None:
        """substr(x,1,7) 截月 → time_granularity=month（识别截断表达式粒度）。"""
        p = parse_sql_profile(
            "SELECT substr(create_date,1,7) AS month_id, SUM(amt) AS amt "
            "FROM wedw_dw.t GROUP BY substr(create_date,1,7)"
        )
        assert p.time_granularity == "month"
        assert p.time_column == "month_id"

    def test_time_granularity_substr_month_6(self) -> None:
        """substr(x,1,6)（YYYYMM）→ month。"""
        p = parse_sql_profile(
            "SELECT substr(create_date,1,6) AS m, SUM(amt) AS amt "
            "FROM t GROUP BY substr(create_date,1,6)"
        )
        assert p.time_granularity == "month"

    def test_time_granularity_substr_year(self) -> None:
        """substr(x,1,4) 截年 → year。"""
        p = parse_sql_profile(
            "SELECT substr(create_date,1,4) AS y, SUM(amt) AS amt "
            "FROM t GROUP BY substr(create_date,1,4)"
        )
        assert p.time_granularity == "year"

    def test_time_granularity_date_trunc_month(self) -> None:
        """date_trunc('month', x) → month。"""
        p = parse_sql_profile(
            "SELECT date_trunc('month', create_date) AS m, SUM(amt) AS amt "
            "FROM t GROUP BY date_trunc('month', create_date)"
        )
        assert p.time_granularity == "month"

    def test_time_granularity_date_format(self) -> None:
        """date_format(x, '%Y-%m') → month。"""
        p = parse_sql_profile(
            "SELECT date_format(create_date, '%Y-%m') AS m, SUM(amt) AS amt "
            "FROM t GROUP BY date_format(create_date, '%Y-%m')"
        )
        assert p.time_granularity == "month"

    def test_time_granularity_plain_month_column(self) -> None:
        """group by month_id（裸列别名）→ month。"""
        p = parse_sql_profile(
            "SELECT month_id, SUM(amt) AS amt FROM t GROUP BY month_id"
        )
        assert p.time_granularity == "month"
        assert p.time_column == "month_id"

    def test_time_granularity_plain_dt_keeps_none(self) -> None:
        """dt 日分区 → 无显式粒度信号（time_granularity=None，period 走 token 推断 day）。"""
        p = parse_sql_profile(
            "SELECT dt, SUM(amount) AS gmv FROM dwd_order_di GROUP BY dt"
        )
        assert p.time_granularity is None
        assert p.time_column == "dt"

    def test_time_granularity_no_time_signal(self) -> None:
        """无时间维度 → 两者皆 None（触发上层 LLM 周期兜底）。"""
        p = parse_sql_profile("SELECT SUM(amount) AS gmv FROM dwd_order_di")
        assert p.time_granularity is None
        assert p.time_column is None

    def test_time_granularity_etl_passthrough_sinks(self) -> None:
        """ETL 透传 INSERT：最外层无时间信号 → 下沉聚合子查询识别 substr 截月。"""
        sql = """
        INSERT OVERWRITE TABLE wedw_dws.doctor_active_month_di
        SELECT a.month_id, a.current_month_active_doctor_cnt
        FROM (
            SELECT substr(create_date,1,7) AS month_id,
                   count(distinct doctor_code) AS current_month_active_doctor_cnt
            FROM wedw_dw.doctor_visit_agent_info_da
            GROUP BY substr(create_date,1,7)
        ) a
        """
        p = parse_sql_profile(sql)
        assert p.time_granularity == "month"

    def test_doris_ctas_physical_attrs_degrade_fallback(self) -> None:
        """Doris CTAS（DUPLICATE KEY + DISTRIBUTED BY + PROPERTIES + BUCKETS）画像解析。

        默认方言把整句降级为 Command（无 Select 子树）→ 依赖 ``_preprocess_dialect``
        剥离物理分布/副本属性后按默认方言解析，聚合度量/源表须正确提取。
        """
        sql = """
        CREATE TABLE IF NOT EXISTS wedw_dws.doctor_func_index_df
        DUPLICATE KEY(create_date, doctor_code, hosp_code)
        COMMENT '家医智能体-功能使用分析'
        DISTRIBUTED BY HASH(create_date, doctor_code, hosp_code) BUCKETS 5
        PROPERTIES ("replication_allocation" = "tag.location.default: 1")
        AS
        SELECT
            a.event_date AS create_date,
            a.quality_control_qc_report_cnt,
            a.remote_clinic_cnt
        FROM (
            SELECT
                to_date(t1.event_time) AS event_date,
                SUM(CASE WHEN get_json_string(t1.biz_data,'$.skillId')='quality-control-qc-report'
                    THEN 1 ELSE 0 END) AS quality_control_qc_report_cnt,
                SUM(CASE WHEN get_json_string(t1.biz_data,'$.skillId')='remote-clinic'
                    THEN 1 ELSE 0 END) AS remote_clinic_cnt
            FROM footprint_service_ctl.footprint_service.ods_track_event t1
            WHERE t1.click_event='skill-call'
            GROUP BY to_date(t1.event_time)
        ) a
        """
        p = parse_sql_profile(sql)
        # 物理属性剥离后默认方言可解析 → 度量/源表不丢
        assert len(p.measures) == 2
        assert {m["alias"] for m in p.measures} == {
            "quality_control_qc_report_cnt",
            "remote_clinic_cnt",
        }
        assert p.measures[0]["agg"] == "SUM"
        assert "footprint_service_ctl.footprint_service.ods_track_event" in p.source_tables
        # event_date 透传 → 时间列可识别（下沉子查询 to_date 截日）
        assert p.time_column is not None

    # ------------------------------------------------------------
    # 工业方言聚合识别（ClickHouse/Oracle/Trino/PostgreSQL/MySQL/T-SQL 等）
    # ------------------------------------------------------------

    def test_clickhouse_merge_conditional_aggregates(self) -> None:
        """ClickHouse sumMerge/sumIf/countIf 方言聚合 → 规范聚合名 + 列提取。

        默认方言把 sumMerge/sumIf 降级为 Anonymous（非 AggFunc）→ 依赖
        ``_best_dialect_ast`` 按聚合识别数择优选 clickhouse 方言，且
        ``CombinedAggFunc`` 的参数在 ``expressions[0]``（this 是函数名字符串）。
        """
        p = parse_sql_profile(
            "SELECT toDate(ts) AS stat_date, city_id, "
            "sumMerge(amount_state) AS amount, "
            "sumIf(amount, is_valid=1) AS valid_amount, "
            "countIf(user_id <> '') AS user_cnt "
            "FROM dwd.agg_city_daily GROUP BY toDate(ts), city_id"
        )
        assert {m["column"]: m["agg"] for m in p.measures} == {
            "amount_state": "SUM",
            "amount": "SUM",
            "user_id": "COUNT",
        }

    def test_oracle_nvl_wrapped_column_extracted(self) -> None:
        """Oracle sum(nvl(amount,0)) → 列从复杂表达式内提取（COALESCE 包裹）。"""
        p = parse_sql_profile(
            "SELECT trunc(create_date,'MM') AS month_id, dept_code, "
            "SUM(nvl(amount,0)) AS amount, "
            "COUNT(DISTINCT CASE WHEN status='Y' THEN order_id END) AS valid_cnt "
            "FROM dwd.ord_detail GROUP BY trunc(create_date,'MM'), dept_code"
        )
        assert {m["column"]: m["agg"] for m in p.measures} == {
            "amount": "SUM",
            "order_id": "COUNT_DISTINCT",
        }

    def test_trino_approx_distinct_normalized(self) -> None:
        """Trino/Presto approx_distinct → COUNT_DISTINCT（key 无下划线映射）。"""
        p = parse_sql_profile(
            "SELECT date_trunc('month', create_date) AS month_id, store_code, "
            "approx_distinct(user_id) AS uv, SUM(amount) AS gmv "
            "FROM dwd.store_sales GROUP BY 1, 2"
        )
        assert {m["column"]: m["agg"] for m in p.measures} == {
            "user_id": "COUNT_DISTINCT",
            "amount": "SUM",
        }

    def test_postgres_filter_count(self) -> None:
        """PostgreSQL count(*) FILTER (WHERE ...) → COUNT(*)（FILTER 修饰符不干扰）。"""
        p = parse_sql_profile(
            "SELECT date_trunc('month', created_at) AS month_id, org_id, "
            "COUNT(*) FILTER (WHERE status='paid') AS paid_cnt, "
            "SUM(amount) AS amount "
            "FROM ods.payments GROUP BY 1, 2"
        )
        assert {m["column"]: m["agg"] for m in p.measures} == {
            "*": "COUNT",
            "amount": "SUM",
        }

    def test_mysql_ifnull_and_tsql_convert(self) -> None:
        """MySQL IFNULL(SUM) + T-SQL CONVERT 分组 → 聚合提取不受方言函数影响。"""
        mysql = parse_sql_profile(
            "SELECT DATE_FORMAT(create_time, '%Y-%m') AS month_id, shop_id, "
            "IFNULL(SUM(amount),0) AS amount, COUNT(DISTINCT user_id) AS uv "
            "FROM t_trade GROUP BY 1, 2"
        )
        assert {m["column"]: m["agg"] for m in mysql.measures} == {
            "amount": "SUM",
            "user_id": "COUNT_DISTINCT",
        }
        tsql = parse_sql_profile(
            "SELECT CONVERT(VARCHAR(7), create_date, 120) AS month_id, shop_code, "
            "SUM(amount) AS gmv, COUNT(DISTINCT user_id) AS uv "
            "FROM dbo.trade GROUP BY CONVERT(VARCHAR(7), create_date, 120), shop_code"
        )
        assert {m["column"]: m["agg"] for m in tsql.measures} == {
            "amount": "SUM",
            "user_id": "COUNT_DISTINCT",
        }

    def test_normal_sql_uses_default_dialect_unchanged(self) -> None:
        """普通 SQL（无方言聚合）→ 默认方言解析，行为与方言择优前一致。"""
        p = parse_sql_profile(
            "SELECT dt, shop_id, SUM(amount) AS gmv, COUNT(DISTINCT user_id) AS uv "
            "FROM dwd.sales WHERE dt >= '2026-01-01' GROUP BY dt, shop_id"
        )
        assert {m["column"]: m["agg"] for m in p.measures} == {
            "amount": "SUM",
            "user_id": "COUNT_DISTINCT",
        }
        assert p.time_column == "dt"
        assert p.time_granularity is None

