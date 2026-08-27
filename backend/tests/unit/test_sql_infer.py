"""sql_infer SQL 解析画像单元测试。"""

from __future__ import annotations

from app.services.semantic.sql_infer import extract_dimension_columns, parse_sql_profile


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

    def test_coalesce_wrapped_case_agg_not_lost(self) -> None:
        """COALESCE 包裹 + CASE 内嵌的条件去重聚合不丢失（用户真实 ETL）。

        真实 Hive 医生月活 SQL：``coalesce(count(distinct case when ... then
        doctor_code end), 0) as last_month_active_doctor_cnt``——target 是
        Coalesce，内嵌 AggFunc+Case。此前 ``_is_wrapped_aggregate`` 把 CASE 误当
        派生组合节点 → 单聚合包裹被跳过 → 双度量只剩 1 个（推断退化为「原子」）。
        修复：CASE/IF 是聚合内部过滤条件，不算组合节点；派生比率仍由多聚合或
        Div/Mul 检查排除。
        """
        sql = """
        INSERT OVERWRITE TABLE dws.doctor_active_month_di
        SELECT a.month_id, a.hosp_code, coalesce(b.org_name, '-99') AS hosp_name,
               a.current_month_active_doctor_cnt, a.last_month_active_doctor_cnt
        FROM (
            SELECT t1.month_id,
                   count(distinct t1.doctor_code) AS current_month_active_doctor_cnt,
                   coalesce(count(distinct case when t2.doctor_code is not null
                           then t2.doctor_code end), 0) AS last_month_active_doctor_cnt
            FROM wedw_dw.doctor_visit_agent_info_da t1
            LEFT JOIN wedw_dw.doctor_visit_agent_info_da t2
              ON t1.doctor_code = t2.doctor_code
            GROUP BY t1.month_id
        ) a
        LEFT JOIN (
            SELECT DISTINCT rel_code, org_name FROM wedw_dw.disease_care_sys_org_staff_relation_df
        ) b ON a.hosp_code = b.rel_code
        """
        p = parse_sql_profile(sql)
        assert len(p.measures) == 2
        by_alias = {m["alias"]: m for m in p.measures}
        assert set(by_alias) == {"current_month_active_doctor_cnt", "last_month_active_doctor_cnt"}
        last = by_alias["last_month_active_doctor_cnt"]
        assert last["agg"] == "COUNT_DISTINCT"
        assert last["column"] == "doctor_code"
        assert "CASE WHEN" in (last["expression"] or "").upper()

    def test_coalesce_wrapped_plain_agg_kept_as_measure(self) -> None:
        """COALESCE(COUNT(DISTINCT x),0) 纯计数包裹仍是独立聚合（非派生候选）。"""
        p = parse_sql_profile(
            "SELECT dt, COALESCE(COUNT(DISTINCT user_id), 0) AS uv "
            "FROM dwd_order_di GROUP BY dt"
        )
        assert {m["alias"]: m["agg"] for m in p.measures} == {"uv": "COUNT_DISTINCT"}
        assert not any(m.get("derived") for m in p.measures)

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
            # W7：countIf(user_id<>'') 是布尔条件计数——计数满足条件的行（*），
            # 不是条件里的 user_id 列；列改 * + needs_review（避免注册成
            # 「user_id 值计数」误导）
            "*": "COUNT",
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
        # W4：topK(5)(product) 列从 ParameterizedAgg 的 params 提取（product 而非
        # '*'——调用参数列更准确），聚合归 COUNT——三者都不崩溃
        assert ("user_id", "COUNT_DISTINCT") in aggs
        assert ("amount", "PERCENTILE") in aggs
        assert ("product", "COUNT") in aggs
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
        → 归一注册枚举（U-2：array_agg 集合聚合 → COUNT_DISTINCT + needs_review，
        不再静默降级 COUNT；bool_and/bool_or/ANY_VALUE/APPROX_TOP_K → COUNT，避免
        非法枚举 ARRAYAGG/LOGICALAND/LOGICALOR/ANYVALUE/APPROXTOPK → 批量整批失败）。"""
        p = parse_sql_profile(
            "SELECT array_agg(user_id) AS uids, bool_and(flag) AS ba, bool_or(flag) AS bo "
            "FROM t GROUP BY dept"
        )
        aggs = {m["agg"] for m in p.measures}
        assert "COUNT_DISTINCT" in aggs
        assert "COUNT" in aggs
        assert any(m.get("needs_review") for m in p.measures if m["column"] == "user_id")
        p2 = parse_sql_profile("SELECT ANY_VALUE(status) AS s FROM t GROUP BY dept")
        # X7：any_value（任取一值）无注册枚举 → 诚实跳过（不产出语义错误的 COUNT 指标）
        assert {m["agg"] for m in p2.measures} == set()
        p3 = parse_sql_profile("SELECT APPROX_TOP_K(status, 5) AS t FROM t GROUP BY dept")
        assert {m["agg"] for m in p3.measures} == {"COUNT"}

    def test_dialect_clickhouse_conditional_extreme(self) -> None:
        """CH maxIf/minIf → MAX/MIN；argMaxIf → 诚实跳过（X6）。

        此前 _COMBINED_AGG_MAP 无 maxif/minif → 兜底归 SUM（语义错误：maxIf 是 MAX
        不是 SUM），且 hint 未命中不触发方言择优 → measures=0 推断退化。
        ``argMaxIf(amount, ts, cond)`` 语义是「cond 内 ts 最大那一行的 amount」，
        非 MAX(amount)——映射 MAX 会产出语义错误的指标（X6 与 Trino min_by 同类），
        诚实跳过由 LLM/人工处理。
        """
        p = parse_sql_profile(
            "SELECT maxIf(amount, status='ok') AS m, minIf(amount, status='ok') AS mn, "
            "argMaxIf(amount, ts, flag=1) AS a FROM t"
        )
        aggs = {(m["column"], m["agg"]) for m in p.measures}
        assert ("amount", "MAX") in aggs
        assert ("amount", "MIN") in aggs
        # argMaxIf 被诚实跳过：不产出 MAX(amount) 错误候选
        assert all(m.get("alias") != "a" for m in p.measures)

    def test_dialect_ch_overflow_weighted(self) -> None:
        """CH sumWithOverflow → SUM、avgWeighted → AVG（AnonymousAggFunc 函数名形态）。"""
        p = parse_sql_profile(
            "SELECT sumWithOverflow(amount) AS s, avgWeighted(amount, w) AS aw FROM t"
        )
        assert {m["agg"] for m in p.measures} == {"SUM", "AVG"}

    def test_dialect_approx_percentile_and_arbitrary(self) -> None:
        """Snow/Spark/Trino approx_percentile → PERCENTILE（hint 缺导致 measures=0 退化）；
        Trino arbitrary（AnyValue 类）→ 诚实跳过（X7：任取一值非计数，映射 COUNT 会
        产出语义错误的指标；由 LLM 兜底处理）。"""
        p = parse_sql_profile(
            "SELECT approx_percentile(amount, 0.5) AS p50, arbitrary(status) AS s FROM t"
        )
        aggs = {m["agg"] for m in p.measures}
        assert "PERCENTILE" in aggs
        assert "COUNT" not in aggs

    def test_dialect_collect_and_countbig(self) -> None:
        """Spark collect_list/collect_set（ArrayAgg/ArrayUniqueAgg）→ COUNT_DISTINCT
        （U-2：集合聚合语义=去重集合，不再静默降级 COUNT）+ needs_review；
        T-SQL COUNT_BIG（tsql 方言 Count 类）→ COUNT（hint 用带下划线函数名才触发择优）。"""
        p = parse_sql_profile(
            "SELECT collect_list(user_id) AS u, collect_set(user_id) AS us FROM t GROUP BY dept"
        )
        assert {m["agg"] for m in p.measures} == {"COUNT_DISTINCT"}
        assert all(m.get("needs_review") for m in p.measures)
        p2 = parse_sql_profile("SELECT COUNT_BIG(*) AS c FROM t")
        assert {m["agg"] for m in p2.measures} == {"COUNT"}

    def test_dialect_listagg_and_groupuniq(self) -> None:
        """Snow LISTAGG（GroupConcat 类）、CH groupUniqArray → COUNT_DISTINCT
        （U-2：串/数组聚合不再静默降级 COUNT，标记 needs_review）。"""
        p = parse_sql_profile(
            "SELECT LISTAGG(status, ',') AS l, groupUniqArray(user_id) AS u FROM t GROUP BY dept"
        )
        assert {m["agg"] for m in p.measures} == {"COUNT_DISTINCT"}
        assert all(m.get("needs_review") for m in p.measures)

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

    def test_named_agg_arithmetic_derived_column_collected(self) -> None:
        """A7：外层透传 + 内层聚合的宽表 ETL——外层算术派生列（a-b-c AS d）引用
        已命名聚合别名时也产出派生候选（此前静默缺失），且带 deps_aliases。"""
        sql = (
            "select all_order_cnt, session_side_order_cnt, region_org_order_cnt, "
            "all_order_cnt - session_side_order_cnt - region_org_order_cnt as "
            "old_page_transfer_order_cnt "
            "from (select count(1) as all_order_cnt, "
            "count(case when ds='a' then id end) as session_side_order_cnt, "
            "count(case when ds='b' then id end) as region_org_order_cnt "
            "from ods.t where date_id='2026-08-18' "
            "group by hosp_code) result"
        )
        p = parse_sql_profile(sql)
        aggs = [m for m in p.measures if not m.get("derived")]
        assert len(aggs) == 3, f"下沉聚合原子应 3 个，实际 {len(aggs)}"
        derived = [m for m in p.measures if m.get("derived")]
        assert len(derived) == 1, f"A7 派生列应 1 个，实际 {len(derived)}"
        d = derived[0]
        assert d["alias"] == "old_page_transfer_order_cnt"
        assert d.get("deps_aliases"), "A7 派生候选应携带被引用的聚合别名"
        assert "all_order_cnt" in d["deps_aliases"]
        assert "-" in (d["expression"] or ""), "口径应为完整算术表达式"

    def test_named_agg_arithmetic_pure_dimension_no_derived(self) -> None:
        """A7 反例：纯维度 SELECT（无聚合子查询、无算术派生列）不产出假候选。"""
        p = parse_sql_profile("SELECT a, b, a + b AS c FROM ods.t")
        assert p.measures == [], f"纯维度 SELECT 不应产出度量：{p.measures}"

    def test_union_all_branches_merged(self) -> None:
        """U-1：顶层 UNION ALL 多源合并——两分支度量 + 两侧源表都保留（此前
        ast.find(Select) 只取首分支 → measures=0 静默 0 候选）。"""
        sql = (
            "select d, sum(amt) as amt from ods.a group by d "
            "union all "
            "select d, count(distinct uid) as uv from ods.b group by d"
        )
        p = parse_sql_profile(sql)
        aggs = {(m["agg"], m.get("alias") or m["column"]) for m in p.measures}
        assert ("SUM", "amt") in aggs
        assert ("COUNT_DISTINCT", "uv") in aggs
        assert "ods.a" in p.source_tables and "ods.b" in p.source_tables
        assert "d" in p.group_by

    def test_correlated_subquery_projection_skipped(self) -> None:
        """U-3：相关子查询标量（(SELECT max(amt) FROM b WHERE b.d=a.d)）不是分组聚合，
        跳过——只产出真实投影聚合，不把子查询表混进度量源。"""
        sql = (
            "select (select max(amt) from ods.b b where b.d=a.d) as mx, "
            "sum(amt) as s from ods.a a group by a.d"
        )
        p = parse_sql_profile(sql)
        assert len(p.measures) == 1
        assert p.measures[0]["agg"] == "SUM"
        assert p.measures[0]["column"] == "amt"

    def test_coalesce_multi_agg_all_collected(self) -> None:
        """U-4：coalesce(sum(amt), sum(refund), 0) 的两个聚合参数都收集（此前只取
        首个且 agg=None，sum(refund) 静默丢失）；单聚合 coalesce 仍按格式包裹。"""
        p = parse_sql_profile("SELECT coalesce(sum(amt), sum(refund), 0) AS x FROM ods.a")
        aggs = [m["agg"] for m in p.measures]
        assert aggs.count("SUM") == 2, f"coalesce 双聚合应 2 个 SUM：{p.measures}"
        cols = {m["column"] for m in p.measures}
        assert cols == {"amt", "refund"}
        # 单聚合 coalesce 不受影响
        p2 = parse_sql_profile("SELECT coalesce(sum(amt), 0) AS x FROM ods.a")
        assert [m["agg"] for m in p2.measures] == ["SUM"]

    def test_if_semantic_wrap_released(self) -> None:
        """U-5：if(sum(amt) is null, 0, sum(amt)) 语义包裹（Case 双分支同一聚合）
        按格式包裹放行，产出 1 个 SUM 且不重复派生。"""
        p = parse_sql_profile("SELECT if(sum(amt) is null, 0, sum(amt)) AS x FROM ods.a")
        assert [m["agg"] for m in p.measures] == ["SUM"], f"{p.measures}"

    def test_window_wrapped_agg_marked_review(self) -> None:
        """U-6：窗口函数包裹（sum(amt) over(...)）产出但标记 needs_review（非分组
        聚合语义需人工核对）；普通 SUM 分组聚合不受影响。"""
        p = parse_sql_profile(
            "SELECT sum(amt) over (partition by d) AS w, sum(amt) AS s "
            "FROM ods.a GROUP BY d"
        )
        by_alias = {m.get("alias"): m for m in p.measures}
        assert by_alias["w"]["agg"] == "SUM"
        assert by_alias["w"].get("needs_review"), "窗口列应标记 needs_review"
        assert by_alias["s"]["agg"] == "SUM"
        assert not by_alias["s"].get("needs_review")

    def test_cte_name_excluded_from_source_tables(self) -> None:
        """U-7：CTE 名不视为物理表——WITH base AS (...) SELECT ... FROM base 的
        source_tables 只含真实物理表 ods.a，不混入 base。"""
        p = parse_sql_profile(
            "WITH base AS (SELECT d, sum(amt) AS amt FROM ods.a GROUP BY d) "
            "SELECT d, amt FROM base"
        )
        assert "base" not in p.source_tables, f"{p.source_tables}"
        assert "ods.a" in p.source_tables

    def test_pivot_aggregation_extracted(self) -> None:
        """U-8：PIVOT 展开——SELECT * FROM t PIVOT(SUM(amt)...) 的聚合在 Pivot 节点
        内部，产出 SUM(amt) 度量 + needs_review（此前 measures=0 完全丢失）。"""
        p = parse_sql_profile(
            "SELECT * FROM ods.a PIVOT (sum(amt) FOR d IN ('a','b')) p"
        )
        assert len(p.measures) == 1
        assert p.measures[0]["agg"] == "SUM"
        assert p.measures[0]["column"] == "amt"
        assert p.measures[0].get("needs_review")
        assert "ods.a" in p.source_tables

    def test_grouping_sets_dimensions_extracted(self) -> None:
        """U-9：GROUPING SETS 分组维度进入 group_by（此前 group.expressions 为空 →
        维度列丢失；ROLLUP 已支持，补齐对齐）。"""
        p = parse_sql_profile(
            "SELECT d, region, sum(amt) FROM ods.a "
            "GROUP BY GROUPING SETS ((d),(region),(d,region))"
        )
        assert "d" in p.group_by
        assert "region" in p.group_by

    def test_bitmap_hll_union_extracted(self) -> None:
        """V-1：Doris 位图/HLL 去重聚合（bitmap_union/hll_union，工业 UV/DAU 标准
        写法）解析为 Anonymous 非 AggFunc，此前整段静默 0 候选；现映射 COUNT_DISTINCT
        + needs_review，列从内层 to_bitmap(uid)/hll_hash(uid) 提取。"""
        p = parse_sql_profile(
            "SELECT d, bitmap_union(to_bitmap(uid)) AS uv FROM ods.a GROUP BY d"
        )
        assert len(p.measures) == 1, f"{p.measures}"
        assert p.measures[0]["agg"] == "COUNT_DISTINCT"
        assert p.measures[0]["column"] == "uid"
        assert p.measures[0].get("needs_review")
        assert "ods.a" in p.source_tables
        p2 = parse_sql_profile(
            "SELECT d, hll_union(hll_hash(uid)) AS uv FROM ods.a GROUP BY d"
        )
        assert p2.measures and p2.measures[0]["agg"] == "COUNT_DISTINCT"
        assert p2.measures[0]["column"] == "uid"

    def test_nested_aggregate_marked_review(self) -> None:
        """V-2：嵌套聚合（sum(avg(x)) 聚合的聚合）不再静默产出 SUM(x)——expression
        保留原结构且标记 needs_review 让用户人工核对（聚合的聚合语义 ≠ SUM(x)）。"""
        p = parse_sql_profile("SELECT sum(avg(x)) AS s FROM ods.a GROUP BY d")
        assert len(p.measures) == 1
        assert p.measures[0]["agg"] == "SUM"
        assert p.measures[0].get("needs_review"), "嵌套聚合必须标记 needs_review"
        assert "AVG(" in p.measures[0].get("expression", "").upper()
        # 普通聚合不误标
        p2 = parse_sql_profile("SELECT sum(x) AS s FROM ods.a GROUP BY d")
        assert not p2.measures[0].get("needs_review")

    def test_where_scalar_subquery_not_in_source_tables(self) -> None:
        """V-3：WHERE 标量子查询（维表/查找表）不混入 source_tables——此前 walk 整棵
        AST 把 ods.lookup 也收进来，血缘错挂无关表。"""
        p = parse_sql_profile(
            "SELECT d, count(1) AS c FROM ods.a "
            "WHERE d = (SELECT max(d) FROM ods.lookup) GROUP BY d"
        )
        assert p.source_tables == ["ods.a"], f"{p.source_tables}"

    def test_cte_source_tables_recursed(self) -> None:
        """V-3：主 FROM 引用 CTE 时递归收集 CTE 体的 FROM 子树（真实源表在 CTE 体
        里）；CTE 体 WHERE 内的查找子查询不混入；递归 CTE 不死循环。"""
        p = parse_sql_profile(
            "WITH u AS (SELECT d, sum(amt) s FROM ods.a GROUP BY d "
            "UNION ALL SELECT d, sum(amt) s FROM ods.b GROUP BY d) "
            "SELECT d, sum(s) AS total FROM u GROUP BY d"
        )
        assert "ods.a" in p.source_tables and "ods.b" in p.source_tables, f"{p.source_tables}"
        p2 = parse_sql_profile(
            "WITH b AS (SELECT d, sum(amt) s FROM ods.a "
            "WHERE d IN (SELECT d FROM ods.lookup) GROUP BY d) "
            "SELECT d, sum(s) t FROM b GROUP BY d"
        )
        assert p2.source_tables == ["ods.a"], f"{p2.source_tables}"
        # 递归 CTE 不抛异常
        parse_sql_profile(
            "WITH RECURSIVE c AS (SELECT 1 AS n UNION ALL "
            "SELECT n+1 FROM c WHERE n<10) SELECT n, count(1) c2 FROM c GROUP BY n"
        )

    def test_percentile_cont_column_extracted(self) -> None:
        """V-4：percentile_cont(0.5) WITHIN GROUP (ORDER BY amt)——分位数 Literal 是
        this，真实列在 ORDER BY；此前列=*，现提取 amt。"""
        p = parse_sql_profile(
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY amt) AS med FROM ods.a"
        )
        assert len(p.measures) == 1, f"{p.measures}"
        assert p.measures[0]["agg"] == "PERCENTILE"
        assert p.measures[0]["column"] == "amt", f"{p.measures[0]}"

    def test_multi_column_distinct_merged(self) -> None:
        """V-5：count(distinct col1, col2) 多列去重（Spark/Hive）——此前只取首列
        丢失语义；现合并列名展示。"""
        p = parse_sql_profile("SELECT count(distinct col1, col2) AS c FROM ods.a")
        assert len(p.measures) == 1
        assert p.measures[0]["agg"] == "COUNT_DISTINCT"
        assert p.measures[0]["column"] == "col1+col2", f"{p.measures[0]}"

    def test_cube_dimensions_extracted(self) -> None:
        """V-6：GROUP BY CUBE(d, region)——维度在 group.args['cube']（与 grouping_sets
        不同节点），此前 group.expressions 为空 → 维度整体丢失被判全局聚合；现展开
        进 group_by。"""
        p = parse_sql_profile(
            "SELECT d, sum(amt) AS s FROM ods.a GROUP BY CUBE(d, region)"
        )
        assert "d" in p.group_by
        assert "region" in p.group_by

    def test_quantile_exact_weighted_normalized(self) -> None:
        """W4：ClickHouse 加权分位 quantileExactWeighted(0.5)(amount, weight) 归一到
        PERCENTILE 且列从 ParameterizedAgg 的 params 提取（amount）——此前产出非法
        枚举 QUANTILEEXACTWEIGHTED（批量创建 pydantic 整批失败）且列 '*'。"""
        p = parse_sql_profile(
            "SELECT toDate(event_time) AS d, "
            "quantileExactWeighted(0.5)(amount, weight) AS p50, "
            "quantile(0.95)(amount) AS p95 FROM ods.pay_event_df GROUP BY d"
        )
        aggs = {m["agg"]: m["column"] for m in p.measures if m["agg"] != "SUM"}
        assert aggs == {"PERCENTILE": "amount"}

    def test_conditional_aggregates_collected(self) -> None:
        """W7：条件聚合 count_if/sum_if（default 方言下 sum_if 解析为 Anonymous）——
        此前 sum_if 静默丢失（Anonymous 非 AggFunc → find 返回 None → 整投影跳过）；
        countIf 列误取条件里的 status。修复后 count_if → COUNT(*)、sum_if → SUM(amt)
        且均标记 needs_review（条件口径需人工核对）。"""
        p = parse_sql_profile(
            "SELECT d, count_if(status='paid') AS paid_cnt, "
            "sum_if(status='refund', amt) AS refund_amt "
            "FROM ods.order_df GROUP BY d"
        )
        measures = {(m["agg"], m["column"]) for m in p.measures}
        assert ("COUNT", "*") in measures  # count_if：布尔条件计数 → 列 *
        assert ("SUM", "amt") in measures  # sum_if：条件求和 → 列 amt
        for m in p.measures:
            assert m.get("needs_review") is True  # 条件口径需人工核对

    def test_collect_list_struct_skipped(self) -> None:
        """W8：collect_list(struct(user_id, amt)) 明细快照收集——硬映射
        COUNT_DISTINCT(user_id) 语义完全错误（不是去重计数，是收集明细对象数组）；
        跳过该投影（纯单列 collect_set(x) 的去重集合近似仍保留）。"""
        p = parse_sql_profile(
            "SELECT d, collect_list(struct(user_id, amt)) AS detail, "
            "sum(amt) AS total FROM ods.fact_df GROUP BY d"
        )
        cols = {m["column"] for m in p.measures}
        assert "user_id" not in cols  # collect_list(struct) 不再产出 COUNT_DISTINCT
        assert "total" in {m.get("alias", "") for m in p.measures}
        assert any(m["agg"] == "SUM" for m in p.measures)
        # 纯单列 collect_set 仍保留去重集合近似
        p2 = parse_sql_profile(
            "SELECT d, collect_set(product) AS prods, count(1) AS c FROM ods.f GROUP BY d"
        )
        assert any(
            m["agg"] == "COUNT_DISTINCT" and m["column"] == "product"
            for m in p2.measures
        )

    def test_grouping_sets_deduped(self) -> None:
        """W11：GROUPING SETS 多组并集维度去重——((d,region),(d,channel),()) 展开后
        d 重复（此前 group_by=[d,region,d,channel]），空集 () 无维度；现去重保序。"""
        p = parse_sql_profile(
            "SELECT d, region, channel, sum(amt) AS amt FROM ods.fact_df "
            "GROUP BY GROUPING SETS ((d, region), (d, channel), ())"
        )
        assert p.group_by == ["d", "region", "channel"]
        assert len(p.group_by) == len(set(p.group_by))

    def test_nested_agg_alias_not_overwritten_to_star(self) -> None:
        """W16：``sum(inner_amt)`` 引用子查询 ``count(1) AS inner_amt`` 派生别名——
        alias_map 把 inner_amt 映射到 *（count(1) 无列）后外层列被错误覆盖成 *；
        映射到 * 不替换，保留 inner_amt 派生列语义（血缘指向派生列而非通配符）。"""
        p = parse_sql_profile(
            "SELECT d, sum(inner_amt) AS amt FROM "
            "(SELECT d, count(1) AS inner_amt FROM "
            "(SELECT date_id AS d FROM ods.base_df WHERE is_deleted=0) x GROUP BY d) y "
            "GROUP BY d"
        )
        assert any(
            m["agg"] == "SUM" and m["column"] == "inner_amt" for m in p.measures
        )
        assert not any(m["column"] == "*" for m in p.measures)

    def test_array_join_not_in_source_tables(self) -> None:
        """W20：ClickHouse ARRAY JOIN——hive/default 方言把 ``ARRAY JOIN sku_list``
        的数组名解析成无 db 的 Table 混入 source_tables（血缘挂到不存在的表）；
        ARRAY JOIN hint 强制 clickhouse 方言（kind='ARRAY' 被 _extract_source_tables
        跳过）。LATERAL VIEW（hive 数组展开）不受影响。"""
        p = parse_sql_profile(
            "SELECT d, sku, sum(amt) AS amt FROM ods.sale_df "
            "ARRAY JOIN sku_list AS sku GROUP BY d, sku"
        )
        assert p.source_tables == ["ods.sale_df"]
        assert "sku_list" not in p.source_tables
        p2 = parse_sql_profile(
            "SELECT d, sku, sum(amt) AS amt FROM ods.sale_df "
            "LATERAL VIEW explode(sku_list) t AS sku GROUP BY d, sku"
        )
        assert p2.source_tables == ["ods.sale_df"]

    def test_intersect_except_setop_branches_merged(self) -> None:
        """X-1：INTERSECT/EXCEPT（exp.SetOperation 子类）——对账/差异查询也遍历
        全部分支合并度量 + 两侧源表（此前只识别 exp.Union，交/差整段静默 0 候选
        + 0 源表）。"""
        for op in ("INTERSECT", "EXCEPT", "INTERSECT ALL"):
            sql = (
                f"SELECT d, sum(amt) AS s FROM ods.a GROUP BY d {op} "
                f"SELECT d, sum(amt) FROM ods.b GROUP BY d"
            )
            p = parse_sql_profile(sql)
            assert len(p.measures) == 2, f"{op}: {p.measures}"
            assert all(m["agg"] == "SUM" for m in p.measures)
            assert "ods.a" in p.source_tables and "ods.b" in p.source_tables
            assert "d" in p.group_by

    def test_min_by_max_by_skipped_not_min_max(self) -> None:
        """X-6：Trino min_by(uid, amt)/max_by(uid, amt)（exp.ArgMin/ArgMax）——
        语义是「amt 极值那一行的 uid」，非 MIN/MAX(uid)；映射 MIN/MAX 会产出语义
        错误的指标，诚实跳过（由 LLM 兜底处理）。"""
        p = parse_sql_profile(
            "SELECT d, min_by(uid, amt) AS top_user, max_by(uid, amt) AS low_user "
            "FROM ods.a GROUP BY d"
        )
        assert p.measures == [], f"min_by/max_by 应被诚实跳过：{p.measures}"

    def test_any_value_skipped_not_count(self) -> None:
        """X-7：any_value(name)（exp.AnyValue）——「任取一值」非计数，映射 COUNT
        会产出语义错误的指标（any_value(name)→COUNT(name) 无意义），诚实跳过。"""
        p = parse_sql_profile(
            "SELECT d, any_value(name) AS n FROM ods.a GROUP BY d"
        )
        assert p.measures == [], f"any_value 应被诚实跳过：{p.measures}"

    def test_group_concat_distinct_order_extracted(self) -> None:
        """X-9：MySQL group_concat(DISTINCT x ORDER BY ... SEPARATOR ...)——this 是
        Order(Distinct(x)) 包裹（非裸 Distinct），剥离后与普通 group_concat 一致
        产出 COUNT_DISTINCT + needs_review（此前被 W8 复杂参数跳过 → 0 度量）。"""
        p = parse_sql_profile(
            "SELECT d, group_concat(DISTINCT product ORDER BY product SEPARATOR ',') "
            "AS plist FROM ods.a GROUP BY d"
        )
        ms = [m for m in p.measures if m.get("alias") == "plist"]
        assert len(ms) == 1, f"group_concat(DISTINCT) 应产出候选：{p.measures}"
        assert ms[0]["agg"] == "COUNT_DISTINCT"
        assert ms[0]["column"] == "product"
        assert ms[0].get("needs_review") is True

    def test_quantile_timing_normalized_to_percentile(self) -> None:
        """Y2：ClickHouse quantileTiming/quantileTimingWeighted（工业耗时/时延分位
        ——P95 接口耗时等）——此前缺映射产出非法枚举 QUANTILETIMING → 批量创建
        pydantic 整批失败（W4 quantileExactWeighted 同类但 quantileTiming 变体被
        遗漏）；quantile* 前缀统一归一 PERCENTILE + 列从 ParameterizedAgg 提取。"""
        p = parse_sql_profile(
            "SELECT d, quantileTiming(0.95)(duration) AS p95 FROM ods.a GROUP BY d"
        )
        ms = [m for m in p.measures if m.get("alias") == "p95"]
        assert len(ms) == 1, f"quantileTiming 应产出候选：{p.measures}"
        assert ms[0]["agg"] == "PERCENTILE"
        assert ms[0]["column"] == "duration"
        p2 = parse_sql_profile(
            "SELECT d, quantileTimingWeighted(0.5)(duration, weight) AS p50 "
            "FROM ods.a GROUP BY d"
        )
        ms2 = [m for m in p2.measures if m.get("alias") == "p50"]
        assert ms2 and ms2[0]["agg"] == "PERCENTILE"

    def test_lag_lead_skipped_not_illegal_enum(self) -> None:
        """Y5：窗口偏移函数 lag/lead over（sqlglot 把 Lag/Lead 定义为 AggFunc 子类）
        ——语义是「上一/下一行取值」非分组聚合；此前产出非法枚举 LAG/LEAD → 批量
        创建 pydantic 整批失败。诚实跳过（由 LLM/人工处理）。"""
        p = parse_sql_profile(
            "SELECT d, lag(amt) OVER (PARTITION BY d ORDER BY t) AS prev, "
            "lead(amt) OVER (PARTITION BY d ORDER BY t) AS nxt, "
            "count(1) AS c FROM ods.a GROUP BY d"
        )
        assert all(m["agg"] in ("COUNT",) for m in p.measures), (
            f"lag/lead 应被诚实跳过（仅剩 COUNT）：{p.measures}"
        )

    def test_sum_distinct_keeps_sum_with_needs_review(self) -> None:
        """Y23：``sum(distinct amt)``/``avg(distinct amt)`` 窗口或分组聚合——DISTINCT
        修饰 ≠ 去重计数，仅 COUNT(DISTINCT x) 才是 COUNT_DISTINCT；SUM/AVG 去重是
        「去重后求和/均值」，保留原聚合名 + needs_review（此前无条件改 COUNT_DISTINCT
        致语义错误：sum(distinct amt) 被注册成「去重计数」）。"""
        p = parse_sql_profile(
            "SELECT d, sum(distinct amt) AS s FROM ods.a GROUP BY d"
        )
        ms = [m for m in p.measures if m.get("alias") == "s"]
        assert len(ms) == 1 and ms[0]["agg"] == "SUM", (
            f"sum(distinct) 应保留 SUM：{p.measures}"
        )
        assert ms[0]["column"] == "amt"
        assert ms[0].get("needs_review") is True

    def test_collect_map_wrapped_by_udf_detected(self) -> None:
        """Y1：Hive 动态分组聚合被外层 UDF 包裹（``map_keys(collect_map(k, v))``——
        动态 map 聚合取 key 集合）——sqlglot 把 collect_map 解析为嵌套 Anonymous
        （非 AggFunc），此前整投影静默丢失（动态分组口径指标缺失）；walk 检测
        collect_* 聚合映射去重集合语义 + needs_review。"""
        p = parse_sql_profile(
            "SELECT d, map_keys(collect_map(k, v)) AS ks, count(1) AS c "
            "FROM ods.a GROUP BY d"
        )
        ms = [m for m in p.measures if m.get("alias") == "ks"]
        assert len(ms) == 1, f"map_keys(collect_map) 应产出候选：{p.measures}"
        assert ms[0]["agg"] == "COUNT_DISTINCT"
        assert ms[0]["column"] == "k"
        assert ms[0].get("needs_review") is True

    def test_multi_column_pivot_all_measures(self) -> None:
        """Y3：多列 PIVOT（``PIVOT(sum(amt) AS amt_sum, count(1) AS cnt FOR ...)``）
        ——聚合被 Alias 包裹（exprs=[Alias(Sum), Alias(Count)]），此前 isinstance
        AggFunc 把 Alias 跳过 → 多列 PIVOT 整段 0 度量；剥离 Alias 后全部聚合产出
        + needs_review（PIVOT 展开为宽表，口径需人工核对）。"""
        p = parse_sql_profile(
            "SELECT * FROM (SELECT d, region, amt FROM ods.a) "
            "PIVOT (sum(amt) AS amt_sum, count(1) AS cnt FOR region IN ('east','west'))"
        )
        aggs = sorted(m["agg"] for m in p.measures)
        assert aggs == ["COUNT", "SUM"], f"多列 PIVOT 应产出两个度量：{p.measures}"
        assert all(m.get("needs_review") is True for m in p.measures)
        assert "ods.a" in p.source_tables

    def test_composite_agg_arg_needs_review(self) -> None:
        """Y15：聚合参数是多列算术表达式（``sum(a.amt * b.price)``）——col 只取
        首个 Column、其余列口径丢失，标记 needs_review 让用户人工核对（expression
        已保留完整；此前无标记，用户可能误以为单列聚合而直接注册）。"""
        p = parse_sql_profile(
            "SELECT d, sum(a.amt * b.price) AS rev FROM ods.a a "
            "JOIN ods.b b ON a.id = b.id GROUP BY d"
        )
        ms = [m for m in p.measures if m.get("alias") == "rev"]
        assert len(ms) == 1 and ms[0]["agg"] == "SUM"
        assert ms[0].get("needs_review") is True
        assert "ods.a" in p.source_tables and "ods.b" in p.source_tables



class TestExtractDimensionColumns:
    """SQL 智能推断关联维度提取（A 增强）：GROUP BY 非时间键回填维度候选。"""

    def test_keeps_business_columns_excludes_time(self) -> None:
        # 医生月活 SQL：GROUP BY month_id/hosp_code/enter_source，时间列 month_id 剔除
        dims = extract_dimension_columns(
            ["month_id", "hosp_code", "enter_source"], "month_id"
        )
        assert dims == ["hosp_code", "enter_source"]

    def test_excludes_time_hint_and_function(self) -> None:
        # 时间 hint 列（create_date/stat_date）与时间函数包裹表达式一律剔除
        dims = extract_dimension_columns(
            [
                "create_date",
                "stat_month",
                "substr(create_date,1,7)",
                "org_id",
                "channel",
            ],
            "create_date",
        )
        assert dims == ["org_id", "channel"]

    def test_empty_and_none_safe(self) -> None:
        assert extract_dimension_columns([], None) == []
        assert extract_dimension_columns(None, None) == []  # type: ignore[arg-type]

    def test_no_time_column_keeps_all_business(self) -> None:
        # 无时间列（time_column=None）时 GROUP BY 全部视为维度
        dims = extract_dimension_columns(["hosp_code", "enter_source"], None)
        assert dims == ["hosp_code", "enter_source"]

    def test_alias_like_month_id_excluded(self) -> None:
        # _TIME_GRAIN_ALIAS（month_id/week_id 等）命中即剔除
        dims = extract_dimension_columns(
            ["month_id", "region_id", "doctor_code"], "month_id"
        )
        assert dims == ["region_id", "doctor_code"]
