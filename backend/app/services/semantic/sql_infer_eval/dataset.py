# ruff: noqa: E501 —— 评测样本 SQL 字符串须保持原样（行长度无关，改动会改变解析结果）
"""SQL 智能推断评测集——真实生产 SQL 与方言样本（人工核对期望答案）。

每条用例声明：
- ``sql``：待解析的完整 SQL 脚本（多语句 ETL / 方言写法 / 复杂聚合）
- ``dialect``：方言/场景标注（用于报告分组）
- ``expected_measures``：期望识别出的度量列集合（列名 + 聚合方式 + 可选别名/源表）
- ``expected_tables``：期望源表集合
- ``expected_period``：期望统计周期（day/week/month/quarter/year/hour）

期望答案与 ``parse_sql_profile``（规则解析）逐项比对，度量级精确率/召回率与
用例级完全匹配率即"解析成功率"的可度量定义（见 ``sql_infer_eval.py``）。

样本构成：
- 真实生产 ETL（医生月活：COALESCE 包裹 + CASE 内嵌条件去重；GMV 日/月汇总）
- 方言覆盖：Oracle（ROWNUM 分页 + TRUNC/NVL）、Spark（窗口函数派生）、
  ClickHouse（物化视图 + sumMerge）、Trino（approx_distinct/approx_percentile）、
  PostgreSQL（FILTER + GROUP BY 位置序号）、MySQL（DATE_FORMAT）、Doris（CTAS）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExpectedMeasure:
    """期望度量：列名 + 聚合方式（枚举值），可选别名/源表参与签名比对。

    ``agg`` 为 ``None`` 表示派生表达式度量（表达式内含聚合的比率/窗口列，
    规则解析标记 ``derived=True``，聚合占位为空）。
    """

    column: str
    agg: str | None
    alias: str | None = None
    table: str | None = None

    def signature(self) -> str:
        """规范化签名（期望与预测比对键）：列|聚合，别名/源表可选追加。"""
        parts = [self.column, self.agg or "DERIVED"]
        if self.alias:
            parts.append(f"alias:{self.alias}")
        if self.table:
            parts.append(f"table:{self.table}")
        return "|".join(parts)


@dataclass(frozen=True)
class SqlInferCase:
    """单条评测用例。"""

    case_id: str
    dialect: str
    sql: str
    expected_measures: tuple[ExpectedMeasure, ...]
    expected_tables: tuple[str, ...]
    expected_period: str
    note: str = ""


# ----------------------------------------------------------------
# 真实生产 ETL
# ----------------------------------------------------------------

DOCTOR_ACTIVE_MONTH_SQL = """-- set hive.exec.dynamic.partition=true;
set hive.vectorized.execution.enabled=false;
create table if not exists wedw_dws.doctor_active_month_di(
 month_id string comment "统计月,时间格式yyyy-MM",
 current_month_active_doctor_cnt int comment "月活",
 last_month_active_doctor_cnt int comment "上月活跃留存"
) stored as orc;
insert overwrite table wedw_dws.doctor_active_month_di
select a.month_id, a.hosp_code, coalesce(b.org_name,'-99') as hosp_name,
 a.current_month_active_doctor_cnt, a.last_month_active_doctor_cnt
from (
 select t1.month_id, t1.hosp_code, count(distinct t1.doctor_code) as current_month_active_doctor_cnt,
  coalesce(count(distinct case when t2.doctor_code is not null then t2.doctor_code end),0) as last_month_active_doctor_cnt
 from (select substr(create_date,1,7) as month_id, hosp_code, doctor_code from wedw_dw.doctor_visit_agent_info_da) t1
 left join (select substr(last_month_last_visit_date,1,7) as month_id, hosp_code, doctor_code from wedw_dw.doctor_visit_agent_info_da where substr(last_month_last_visit_date,1,4) <> '1700') t2
 on t1.month_id=t2.month_id and t1.hosp_code=t2.hosp_code and t1.doctor_code=t2.doctor_code
 group by t1.month_id, t1.hosp_code
) a
left join (select distinct rel_code, org_name from wedw_dw.disease_care_sys_org_staff_relation_df where rel_code <> '-99') b
on a.hosp_code = b.rel_code;"""

GMV_DAILY_SQL = """select
  substr(create_date, 1, 10) as day_id,
  sum(amount) as gmv,
  count(distinct user_id) as buyer_cnt
from ods.orders
where status = 'paid'
group by substr(create_date, 1, 10);"""

# ----------------------------------------------------------------
# 方言样本
# ----------------------------------------------------------------

ORACLE_ROWNUM_SQL = """SELECT * FROM (
  SELECT TRUNC(paid_at) AS day_id, NVL(SUM(amount),0) AS day_amount, COUNT(*) AS order_cnt
  FROM ods.sales
  GROUP BY TRUNC(paid_at)
) WHERE ROWNUM <= 100;"""

SPARK_WINDOW_SQL = """SELECT
  month_id,
  dept_code,
  SUM(amount) AS month_amount,
  SUM(SUM(amount)) OVER (PARTITION BY month_id) AS month_total
FROM dwd.sales_fact
GROUP BY month_id, dept_code;"""

CLICKHOUSE_SUMMERGE_SQL = """CREATE MATERIALIZED VIEW dws.agg_daily ENGINE = SummingMergeTree() ORDER BY day_id AS
SELECT toDate(created_at) AS day_id, sumMerge(amount_state) AS amount FROM dwd.orders GROUP BY toDate(created_at);"""

TRINO_APPROX_SQL = """SELECT
  day_id,
  approx_distinct(user_id) AS uv,
  approx_percentile(amount, 0.5) AS p50_amount
FROM ods.events
GROUP BY day_id;"""

POSTGRES_FILTER_SQL = """SELECT
  date_trunc('month', created_at) AS month_id,
  count(*) FILTER (WHERE status = 'paid') AS paid_cnt,
  sum(amount) AS total_amount
FROM ods.orders
GROUP BY 1;"""

MYSQL_DATE_FORMAT_SQL = """SELECT
  DATE_FORMAT(create_time, '%Y-%m') AS month_id,
  SUM(price * qty) AS gmv,
  COUNT(DISTINCT user_id) AS buyer_cnt
FROM ods.trade
WHERE status = 1
GROUP BY DATE_FORMAT(create_time, '%Y-%m');"""

DORIS_CTAS_SQL = """CREATE TABLE dws.dept_gmv_di
DISTRIBUTED BY HASH(month_id) BUCKETS 16
AS SELECT month_id, dept_code, SUM(amount) AS gmv FROM dwd.sales GROUP BY month_id, dept_code;"""


GOLDEN: tuple[SqlInferCase, ...] = (
    SqlInferCase(
        case_id="doctor_active_month",
        dialect="hive",
        sql=DOCTOR_ACTIVE_MONTH_SQL,
        expected_measures=(
            ExpectedMeasure(
                column="doctor_code",
                agg="COUNT_DISTINCT",
                alias="current_month_active_doctor_cnt",
                table="wedw_dw.doctor_visit_agent_info_da",
            ),
            ExpectedMeasure(
                column="doctor_code",
                agg="COUNT_DISTINCT",
                alias="last_month_active_doctor_cnt",
                table="wedw_dw.doctor_visit_agent_info_da",
            ),
        ),
        expected_tables=(
            "wedw_dw.doctor_visit_agent_info_da",
            "wedw_dw.disease_care_sys_org_staff_relation_df",
        ),
        expected_period="month",
        note="真实 ETL：COALESCE 包裹 + CASE 内嵌条件去重，双度量同列需别名区分",
    ),
    SqlInferCase(
        case_id="gmv_daily",
        dialect="hive",
        sql=GMV_DAILY_SQL,
        expected_measures=(
            ExpectedMeasure(column="amount", agg="SUM", alias="gmv"),
            ExpectedMeasure(column="user_id", agg="COUNT_DISTINCT", alias="buyer_cnt"),
        ),
        expected_tables=("ods.orders",),
        expected_period="day",
        note="日 GMV + 去重买家数",
    ),
    SqlInferCase(
        case_id="oracle_rownum_pagination",
        dialect="oracle",
        sql=ORACLE_ROWNUM_SQL,
        expected_measures=(
            ExpectedMeasure(column="amount", agg="SUM", alias="day_amount", table="ods.sales"),
            ExpectedMeasure(column="*", agg="COUNT", alias="order_cnt", table="ods.sales"),
        ),
        expected_tables=("ods.sales",),
        expected_period="day",
        note="Oracle ROWNUM 分页 + TRUNC/NVL（下沉子查询度量富集源表）",
    ),
    SqlInferCase(
        case_id="spark_window",
        dialect="spark",
        sql=SPARK_WINDOW_SQL,
        expected_measures=(
            ExpectedMeasure(column="amount", agg="SUM", alias="month_amount"),
            ExpectedMeasure(column="month_id", agg=None, alias="month_total"),
        ),
        expected_tables=("dwd.sales_fact",),
        expected_period="month",
        note="Spark 窗口函数派生列（agg=None 派生度量）",
    ),
    SqlInferCase(
        case_id="clickhouse_summerge",
        dialect="clickhouse",
        sql=CLICKHOUSE_SUMMERGE_SQL,
        expected_measures=(
            ExpectedMeasure(column="amount_state", agg="SUM", alias="amount"),
        ),
        expected_tables=("dwd.orders",),
        expected_period="day",
        note="ClickHouse 物化视图 + sumMerge 合并态聚合",
    ),
    SqlInferCase(
        case_id="trino_approx",
        dialect="trino",
        sql=TRINO_APPROX_SQL,
        expected_measures=(
            ExpectedMeasure(column="user_id", agg="COUNT_DISTINCT", alias="uv"),
            ExpectedMeasure(column="amount", agg="PERCENTILE", alias="p50_amount"),
        ),
        expected_tables=("ods.events",),
        expected_period="day",
        note="Trino approx_distinct→COUNT_DISTINCT / approx_percentile→PERCENTILE 归一",
    ),
    SqlInferCase(
        case_id="postgres_filter_groupby1",
        dialect="postgres",
        sql=POSTGRES_FILTER_SQL,
        expected_measures=(
            ExpectedMeasure(column="*", agg="COUNT", alias="paid_cnt"),
            ExpectedMeasure(column="amount", agg="SUM", alias="total_amount"),
        ),
        expected_tables=("ods.orders",),
        expected_period="month",
        note="PostgreSQL FILTER 条件计数 + GROUP BY 位置序号回映",
    ),
    SqlInferCase(
        case_id="mysql_date_format",
        dialect="mysql",
        sql=MYSQL_DATE_FORMAT_SQL,
        expected_measures=(
            ExpectedMeasure(column="price", agg="SUM", alias="gmv"),
            ExpectedMeasure(column="user_id", agg="COUNT_DISTINCT", alias="buyer_cnt"),
        ),
        expected_tables=("ods.trade",),
        expected_period="month",
        note="MySQL DATE_FORMAT 月粒度",
    ),
    SqlInferCase(
        case_id="doris_ctas",
        dialect="doris",
        sql=DORIS_CTAS_SQL,
        expected_measures=(
            ExpectedMeasure(column="amount", agg="SUM", alias="gmv"),
        ),
        expected_tables=("dwd.sales",),
        expected_period="month",
        note="Doris CTAS + DISTRIBUTED BY 物理属性剥离",
    ),
)


def get_case(case_id: str) -> SqlInferCase | None:
    """按 case_id 取用例（测试/工具用）。"""
    for c in GOLDEN:
        if c.case_id == case_id:
            return c
    return None
