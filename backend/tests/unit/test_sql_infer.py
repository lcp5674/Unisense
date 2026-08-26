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
        # A-1/2：顶层投影也带 enrich 键（alias/table/expression 原始口径）
        assert {m["column"]: m["agg"] for m in p.measures} == {"amount": "SUM"}
        assert p.measures[0]["expression"] == "SUM(amount)"
        assert p.time_column == "dt"

    def test_count_distinct(self) -> None:
        sql = "SELECT COUNT(DISTINCT user_id) AS uv FROM dwd.user_active"
        p = parse_sql_profile(sql)
        assert {m["column"]: m["agg"] for m in p.measures} == {"user_id": "COUNT_DISTINCT"}
        assert p.measures[0]["expression"] == "COUNT(DISTINCT user_id)"

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
        """直接投影聚合（非下沉）：measures 结构含基础 column/agg + enrich 键
        （alias/table/expression 原始口径，A-1/2 防 CASE/窗口口径丢失）。"""
        p = parse_sql_profile(
            "SELECT dt, SUM(amount) AS gmv, COUNT(DISTINCT user_id) AS uv "
            "FROM dwd_order_di GROUP BY dt"
        )
        assert {m["column"]: m["agg"] for m in p.measures} == {
            "amount": "SUM",
            "user_id": "COUNT_DISTINCT",
        }
        assert {m["alias"] for m in p.measures} == {"gmv", "uv"}
        # 顶层投影不挂 table（源表由候选构建的 _physical_source_tables 过滤 CTE 后决定）
        assert all(m.get("table") is None for m in p.measures)

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

    def test_first_last_value_normalized_to_registry_enum(self) -> None:
        """P1-4：FIRST_VALUE/LAST_VALUE 窗口函数归一为注册 schema 枚举（FIRSTVALUE→FIRST_VALUE），
        批量创建不再因聚合不匹配 Literal 整批失败。"""
        p = parse_sql_profile(
            "SELECT dt, FIRST_VALUE(amount) AS first_amt, LAST_VALUE(amount) AS last_amt "
            "FROM dwd.balance GROUP BY dt"
        )
        aggs = {(m["column"], m["agg"]) for m in p.measures}
        assert ("amount", "FIRST_VALUE") in aggs
        assert ("amount", "LAST_VALUE") in aggs

    def test_dialect_agg_no_crash_and_normalized(self) -> None:
        """P0-A：方言聚合（uniqExact/quantile/topK）不再崩溃整批，且归一为注册枚举。

        此前 ``AnonymousAggFunc.this`` 是函数名字符串，``_extract_col_name`` 对 str
        调用 ``.walk()`` 抛 AttributeError → ``infer_sql_batch`` 整批 500（ClickHouse
        DAU/UV 常用函数必炸）。修复后 uniqExact→COUNT_DISTINCT、quantile→PERCENTILE、
        topK→COUNT（近似计数）。
        """
        p = parse_sql_profile(
            "SELECT uniqExact(user_id) AS uv, quantile(0.5)(amount) AS p50, "
            "topK(5)(product) AS top FROM orders"
        )
        aggs = {(m["column"], m["agg"]) for m in p.measures}
        # topK(5)(product) 列取 '*'（参数非首个 Column），聚合归 COUNT——三者都不崩溃
        assert ("user_id", "COUNT_DISTINCT") in aggs
        assert ("amount", "PERCENTILE") in aggs
        assert ("*", "COUNT") in aggs
        # 不支持的统计聚合（corr/stddev/var）诚实跳过，不产出非法候选
        p2 = parse_sql_profile(
            "SELECT corr(x, y) AS c, stddev(amount) AS sd FROM t"
        )
        assert p2.measures == []

    def test_bare_aggregation_projection(self) -> None:
        """P0-B：无别名裸聚合投影（SELECT sum(amount) FROM t）被识别。

        ETL 最普遍写法此前因投影非 Alias/Column 被跳过 → measures=0，规则层失效。
        裸聚合用 sqlglot 生成别名（_col_1）与候选结构对齐。
        """
        p = parse_sql_profile("SELECT sum(amount) FROM orders")
        assert {m["column"]: m["agg"] for m in p.measures} == {"amount": "SUM"}
        assert p.measures[0]["alias"] == "_col_1"

    def test_case_expression_preserved(self) -> None:
        """A-1：CASE 聚合口径完整保留（此前落库简化 SUM(col)，CASE 条件丢失变全表聚合）。"""
        p = parse_sql_profile(
            "SELECT SUM(CASE WHEN status='paid' THEN amount END) AS paid_amt FROM orders"
        )
        assert p.measures[0]["expression"] == (
            "SUM(CASE WHEN status = 'paid' THEN amount END)"
        )

    def test_window_expression_preserved(self) -> None:
        """A-2：窗口函数投影保留 OVER 语义（此前落库 SUM(col) 丢窗口 → 语义错误）。"""
        p = parse_sql_profile(
            "SELECT SUM(amount) OVER (PARTITION BY dt) AS running FROM orders"
        )
        assert "OVER" in p.measures[0]["expression"].upper()

    def test_join_same_name_column_table_attribution(self) -> None:
        """A-3：join 同名列按列前缀归属物理表（sum(a.amount)/sum(b.amount) 分属 a/b 表）。"""
        p = parse_sql_profile(
            "SELECT SUM(a.amount) AS a_amt, SUM(b.amount) AS b_amt "
            "FROM dwd.x a JOIN dwd.y b ON a.id = b.id"
        )
        tables = {m.get("table") for m in p.measures}
        assert tables == {"dwd.x", "dwd.y"}

    def test_subquery_alias_column_resolved(self) -> None:
        """A-4：子查询/CTE 投影别名列解析为物理列（SUM(x) FROM (SELECT amount AS x) → amount）。"""
        p = parse_sql_profile(
            "SELECT t.g, SUM(t.x) FROM "
            "(SELECT amount AS x, city AS g FROM dwd.orders) t GROUP BY t.g"
        )
        assert {m["column"] for m in p.measures} == {"amount"}



    def test_positional_group_by_resolved(self) -> None:
        """方言覆盖：GROUP BY 位置序号（GROUP BY 1, 2）回映 SELECT 投影列名。

        Postgres/Trino/Oracle 惯用写法，sqlglot 解析为 Literal 数字（非 Column），
        此前 group_by 为空——位置序号按投影下标映射回列名。
        """
        p = parse_sql_profile(
            "SELECT date_trunc('month', create_date) AS month_id, hosp_code, "
            "COUNT(DISTINCT doctor_code) AS cnt FROM wedw_dw.t GROUP BY 1, 2",
        )
        assert p.group_by == ["month_id", "hosp_code"]
        assert p.time_granularity == "month"

    def test_approx_percentile_normalized(self) -> None:
        """方言覆盖：Trino/Presto approx_percentile 归一为 PERCENTILE（注册枚举合法）。"""
        p = parse_sql_profile(
            "SELECT approx_distinct(user_id) AS uv, "
            "approx_percentile(amount, 0.5) AS p50 FROM t",
        )
        aggs = {m["agg"] for m in p.measures}
        assert "COUNT_DISTINCT" in aggs
        assert "PERCENTILE" in aggs

    def test_multistatement_cn_comment_ddl_dialect_fallback(self) -> None:
        """方言覆盖：多语句 ETL（set + 中文 comment 建表 DDL + insert overwrite）解析不为空。

        默认方言对 `create table ... comment "中文"` 抛 ParseError（多语句拆分分支被
        吞）→ 方言择优须用 parse（复数）拆分选产出语句——修复前整段解析为空画像。
        """
        sql = (
            "set hive.vectorized.execution.enabled=false;\n"
            "create table if not exists wedw_dws.t1(\n"
            ' month_id string comment "统计月",\n'
            ' hosp_code string comment "医院编码"\n'
            ") stored as orc;\n"
            "insert overwrite table wedw_dws.t1\n"
            "select t1.month_id, t1.hosp_code, "
            "count(distinct t1.doctor_code) as current_month_active_doctor_cnt\n"
            "from (select substr(create_date,1,7) as month_id, hosp_code, "
            "doctor_code from wedw_dw.src "
            "group by substr(create_date,1,7), hosp_code, doctor_code) t1\n"
            "group by t1.month_id, t1.hosp_code;"
        )
        p = parse_sql_profile(sql)
        assert p.measures, "多语句中文 comment ETL 不应解析为空"
        assert p.measures[0]["agg"] == "COUNT_DISTINCT"
        assert p.measures[0]["alias"] == "current_month_active_doctor_cnt"
        assert "wedw_dw.src" in p.source_tables
        assert p.time_granularity == "month"

    # ------------------------------------------------------------
    # 方言覆盖（第二轮补齐）：默认方言识别为 AggFunc 子类但 key 未映射 → 非法枚举
    # （P1-4 同类：批量创建 pydantic 整批失败）；hint 未命中 → 方言聚合降级 measures=0
    # ------------------------------------------------------------

    def test_dialect_first_last_normalized(self) -> None:
        """Spark/Hive/CH first()/last()（First/Last 类，key=FIRST/LAST）→ FIRST_VALUE/LAST_VALUE。

        此前产出非法枚举 FIRST/LAST → 批量创建 pydantic 整批失败（P1-4 同类缺陷）。
        """
        p = parse_sql_profile(
            "SELECT first(amount) AS f, last(amount) AS l FROM t GROUP BY dept"
        )
        aggs = {(m["column"], m["agg"]) for m in p.measures}
        assert ("amount", "FIRST_VALUE") in aggs
        assert ("amount", "LAST_VALUE") in aggs

    def test_dialect_array_and_bool_aggs_normalized(self) -> None:
        """PG/Spark array_agg、PG bool_and/bool_or、BQ ANY_VALUE、Snow APPROX_TOP_K
        → 近似计数 COUNT（此前产出非法枚举 ARRAYAGG/LOGICALAND/LOGICALOR/ANYVALUE/
        APPROXTOPK → 批量创建整批失败）。"""
        p = parse_sql_profile(
            "SELECT array_agg(user_id) AS uids, bool_and(flag) AS ba, bool_or(flag) AS bo "
            "FROM t GROUP BY dept"
        )
        assert {m["agg"] for m in p.measures} == {"COUNT"}
        p2 = parse_sql_profile("SELECT ANY_VALUE(status) AS s FROM t GROUP BY dept")
        assert {m["agg"] for m in p2.measures} == {"COUNT"}
        p3 = parse_sql_profile("SELECT APPROX_TOP_K(status, 5) AS t FROM t GROUP BY dept")
        assert {m["agg"] for m in p3.measures} == {"COUNT"}

    def test_dialect_clickhouse_conditional_extreme(self) -> None:
        """CH maxIf/minIf/argMaxIf（CombinedAggFunc）→ MAX/MIN。

        此前 _COMBINED_AGG_MAP 无 maxif/minif → 兜底归 SUM（语义错误：maxIf 是 MAX
        不是 SUM），且 hint 未命中不触发方言择优 → measures=0 推断退化。
        """
        p = parse_sql_profile(
            "SELECT maxIf(amount, status='ok') AS m, minIf(amount, status='ok') AS mn, "
            "argMaxIf(amount, ts, flag=1) AS a FROM t"
        )
        aggs = {(m["column"], m["agg"]) for m in p.measures}
        assert ("amount", "MAX") in aggs
        assert ("amount", "MIN") in aggs
        assert ("amount", "MAX") in aggs  # argMaxIf 同 MAX

    def test_dialect_ch_overflow_weighted(self) -> None:
        """CH sumWithOverflow → SUM、avgWeighted → AVG（AnonymousAggFunc 函数名形态）。"""
        p = parse_sql_profile(
            "SELECT sumWithOverflow(amount) AS s, avgWeighted(amount, w) AS aw FROM t"
        )
        assert {m["agg"] for m in p.measures} == {"SUM", "AVG"}

    def test_dialect_approx_percentile_and_arbitrary(self) -> None:
        """Snow/Spark/Trino approx_percentile → PERCENTILE（hint 缺导致 measures=0 退化）；
        Trino arbitrary（AnyValue 类）→ COUNT。"""
        p = parse_sql_profile(
            "SELECT approx_percentile(amount, 0.5) AS p50, arbitrary(status) AS s FROM t"
        )
        aggs = {m["agg"] for m in p.measures}
        assert "PERCENTILE" in aggs
        assert "COUNT" in aggs

    def test_dialect_collect_and_countbig(self) -> None:
        """Spark collect_list/collect_set（ArrayAgg/ArrayUniqueAgg）→ COUNT；
        T-SQL COUNT_BIG（tsql 方言 Count 类）→ COUNT（hint 用带下划线函数名才触发择优）。"""
        p = parse_sql_profile(
            "SELECT collect_list(user_id) AS u, collect_set(user_id) AS us FROM t GROUP BY dept"
        )
        assert {m["agg"] for m in p.measures} == {"COUNT"}
        p2 = parse_sql_profile("SELECT COUNT_BIG(*) AS c FROM t")
        assert {m["agg"] for m in p2.measures} == {"COUNT"}

    def test_dialect_listagg_and_groupuniq(self) -> None:
        """Snow LISTAGG（GroupConcat 类）→ COUNT；CH groupUniqArray → COUNT（hint 触发择优）。"""
        p = parse_sql_profile(
            "SELECT LISTAGG(status, ',') AS l, groupUniqArray(user_id) AS u FROM t GROUP BY dept"
        )
        assert {m["agg"] for m in p.measures} == {"COUNT"}

    def test_dialect_stat_aggs_skipped_honestly(self) -> None:
        """T-SQL STDEV/VAR、MySQL STD/STDDEV_POP（Stddev/Variance 类）→ 诚实跳过（空画像），
        不产出非法枚举——与 corr/stddev 统计聚合降级哲学一致。"""
        for sql in (
            "SELECT STDEV(amount) AS sd, VAR(amount) AS vr FROM dbo.t",
            "SELECT STD(amount) AS s, STDDEV_POP(amount) AS sp FROM t_trade",
        ):
            assert parse_sql_profile(sql).measures == []

    def test_dialect_unknown_aggs_degrade_empty(self) -> None:
        """sqlglot 各方言均不识别的罕见聚合（Oracle WM_CONCAT / CH medianExact /
        Trino map_agg / BQ APPROX_TOP_COUNT）→ 诚实降级空画像，不产出非法候选。"""
        for sql in (
            "SELECT WM_CONCAT(status) AS s FROM t GROUP BY dept",
            "SELECT medianExact(amount) AS m FROM t",
            "SELECT map_agg(k, v) AS m FROM t GROUP BY dept",
            "SELECT APPROX_TOP_COUNT(status, 10) AS t FROM t",
        ):
            assert parse_sql_profile(sql).measures == []

    # ---- P0-2 / P0-3（第六轮）：INSERT 带列清单源表 + 复杂 ETL 列→物理表溯源 ----

    def test_insert_with_column_list_excludes_target_table(self) -> None:
        """P0-2：INSERT INTO ... (a, b) SELECT ... 带列清单——目标表被 sqlglot 包成
        Schema，旧判断 `parent is exp.Insert` 失配把目标表混入 source_tables（口径/
        血缘错挂）。修复后目标表排除、源表保留。"""
        sql = (
            "INSERT INTO dws.daily_summary (dt, gmv) "
            "SELECT dt, SUM(amount) AS gmv FROM ods.fact_order GROUP BY dt"
        )
        p = parse_sql_profile(sql)
        assert "dws.daily_summary" not in p.source_tables, "目标表不应混入源表"
        assert "ods.fact_order" in p.source_tables
        assert {m["column"] for m in p.measures} == {"amount"}

    def test_nested_join_main_table_first_and_attributed(self) -> None:
        """P0-3a：多层嵌套 join 源表归属——join 右侧字典表此前在 walk 顺序中排前，
        source_tables[0] 为字典表导致无表前缀度量候选错挂；主 FROM 表排首 +
        _measure_table 穿透子查询别名使 sum(a.amount) 归属 ods.raw_event。"""
        sql = (
            "SELECT a.user_id, SUM(a.amount) AS total "
            "FROM (SELECT user_id, amount FROM ods.raw_event) a "
            "LEFT JOIN ods.ref_dict b ON a.user_id = b.id GROUP BY a.user_id"
        )
        p = parse_sql_profile(sql)
        assert p.source_tables[0] == "ods.raw_event", "主 FROM 表应排首"
        amount = next(m for m in p.measures if m["column"] == "amount")
        assert amount["table"] == "ods.raw_event", "sum(a.amount) 应归属 ods.raw_event"

    def test_sinking_same_name_two_subqueries_not_merged(self) -> None:
        """P0-3b：下沉透传两个子查询同名列（t1.cnt/t2.cnt）——去重键此前
        (alias, agg) 不含 table，第二支被合并丢；加 table 后两支都保留且分属各自表。"""
        sql = (
            "INSERT INTO dws.t SELECT t1.user_id, t1.cnt, t2.cnt "
            "FROM (SELECT user_id, COUNT(*) cnt FROM ods.a GROUP BY user_id) t1 "
            "FULL JOIN (SELECT user_id, COUNT(*) cnt FROM ods.b GROUP BY user_id) t2 "
            "ON t1.user_id = t2.user_id"
        )
        p = parse_sql_profile(sql)
        tables = {m.get("table") for m in p.measures}
        assert tables == {"ods.a", "ods.b"}, "两个子查询同名列都应保留且分属各自表"

    def test_cte_derived_agg_column_mapped_to_physical(self) -> None:
        """P0-3c：CTE 派生聚合列 sum(amount) AS day_amt 被外层引用——
        alias_map 扩展 Alias(AggFunc) → 底层物理列 amount，source_fields 不再落
        不存在的 day_amt。"""
        sql = (
            "WITH base AS (SELECT dt, user_id, SUM(amount) AS day_amt "
            "FROM dwd.fact_order GROUP BY dt, user_id) "
            "SELECT dt, COUNT(DISTINCT user_id) AS uv, SUM(day_amt) AS total "
            "FROM base GROUP BY dt"
        )
        p = parse_sql_profile(sql)
        cols = {m["column"] for m in p.measures}
        assert "amount" in cols, "sum(day_amt) 应映射到底层物理列 amount"
        assert "day_amt" not in cols, "CTE 派生别名不应出现在 source_fields 列"

    def test_derived_ratio_columns_produced_with_expression(self) -> None:
        """P0-3d：无聚合包裹的派生比率列（ROUND(SUM/NULLIF(COUNT)) 客单价、
        SUM(CASE)/NULLIF(COUNT(*)) 退货率）应产出候选且带 derived 标记 + 完整
        expression；内嵌聚合不再作为独立聚合重复提取（避免撞码/命名失败）。"""
        sql = (
            "INSERT INTO dws.daily_summary (dt, gmv, buyer_cnt, avg_price, refund_rate) "
            "SELECT dt, SUM(amount) AS gmv, COUNT(DISTINCT order_id) AS buyer_cnt, "
            "ROUND(SUM(amount)/NULLIF(COUNT(DISTINCT order_id),0),2) AS avg_price, "
            "SUM(CASE WHEN is_refund=1 THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0) AS refund_rate "
            "FROM ods.orders GROUP BY dt"
        )
        p = parse_sql_profile(sql)
        derived = [m for m in p.measures if m.get("derived")]
        assert len(derived) == 2, f"应产出 2 个派生比率候选，实际 {len(derived)}"
        aliases = {d.get("alias") for d in derived}
        assert aliases == {"avg_price", "refund_rate"}
        for d in derived:
            assert d.get("expression"), "派生候选应携带完整表达式"
            assert (
                "SUM" in (d["expression"] or "").upper()
                or "COUNT" in (d["expression"] or "").upper()
            )
        # 独立聚合：仅 gmv/buyer_cnt（内嵌聚合不重复提取）
        plain = [m for m in p.measures if not m.get("derived")]
        plain_keys = {(m.get("alias"), m["agg"]) for m in plain}
        assert ("gmv", "SUM") in plain_keys and ("buyer_cnt", "COUNT_DISTINCT") in plain_keys
        # 派生比率不产内嵌聚合（is_refund 不再作为独立 SUM 提取）
        assert not any(m.get("column") == "is_refund" and not m.get("derived") for m in p.measures)
