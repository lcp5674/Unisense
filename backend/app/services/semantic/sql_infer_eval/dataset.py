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

# ----------------------------------------------------------------
# 生产模式补充（2026-08-28 覆盖审查）：真实数仓高频形态
# ----------------------------------------------------------------

# MERGE 增量 upsert（DWS 指标表回写）——USING 子查询聚合须识别，写目标表不混入
MERGE_UPSERT_SQL = """merge into wedw_dws.dept_fee_mi t
using (select substr(fee_date,1,7) as month_id, dept_code, sum(real_amount) as fee_amt
       from wedw_dwd.fee_bill_di group by substr(fee_date,1,7), dept_code) s
on t.month_id = s.month_id and t.dept_code = s.dept_code
when matched then update set t.fee_amt = s.fee_amt
when not matched then insert values (s.month_id, s.dept_code, s.fee_amt)"""

# count(1) 常量计数（Hive 生产最普遍写法）→ COUNT(*)
COUNT_ONE_SQL = """select month_id, hosp_code, count(1) as visit_cnt
from wedw_dwd.visit_detail
group by month_id, hosp_code"""

# row_number 取最新再聚合（拉链/快照去重的标准形态）——CTE + 窗口 + 条件过滤
ROWNUM_DEDUP_SQL = """with ranked as (
  select doctor_code, hosp_code, dept_code, visit_date,
         row_number() over (partition by doctor_code order by visit_date desc) as rn
  from wedw_dwd.visit_detail
)
select substr(visit_date,1,7) as month_id, hosp_code, count(distinct doctor_code) as active_doctor_cnt
from ranked where rn = 1 group by substr(visit_date,1,7), hosp_code"""

# LATERAL VIEW explode（Hive UDTF 明细展开）——数组列不混入源表
LATERAL_VIEW_SQL = """select month_id, hosp_code, drug_code, sum(qty) as drug_qty
from wedw_dwd.prescription_detail
lateral view explode(split(drug_list, ',')) t as drug_code
group by month_id, hosp_code, drug_code"""

# 多级 CTE 链（base → roll 逐级汇总）——度量 table 须穿透 CTE 名解析到物理表
CHAINED_CTE_SQL = """with base as (
  select dt, hosp_code, dept_code, sum(amount) as day_amt from wedw_dwd.fee_di group by dt, hosp_code, dept_code
), roll as (
  select substr(dt,1,7) as month_id, hosp_code, sum(day_amt) as month_amt from base group by substr(dt,1,7), hosp_code
)
select month_id, hosp_code, month_amt from roll"""

# Hive 条件 IF 求和（西药/中药费拆分）——同列双语义靠别名区分
HIVE_IF_SUM_SQL = """select month_id,
  sum(if(fee_type = '01', real_amount, 0)) as west_med_fee,
  sum(if(fee_type = '02', real_amount, 0)) as cn_med_fee
from wedw_dwd.fee_detail group by month_id"""

# 下沉透传改名（外层 a.cnt as active_doctor_cnt）——度量 alias 须升级为外层别名
OUTER_RENAME_SQL = """insert overwrite table wedw_dws.doctor_active_mi
select a.month_id, a.hosp_code, a.cnt as active_doctor_cnt
from (select substr(visit_date,1,7) as month_id, hosp_code, count(distinct doctor_code) as cnt
      from wedw_dwd.visit_detail group by substr(visit_date,1,7), hosp_code) a"""

# 分区过滤单表（dt 分区 + date_sub/current_date 谓词）
PARTITION_FILTER_SQL = """select dt, org_code, count(distinct doctor_id) as doc_cnt
from wedw_dwd.doctor_active_da
where dt >= date_sub(current_date, 7) and dt <= current_date
group by dt, org_code"""


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
    SqlInferCase(
        case_id="merge_upsert",
        dialect="hive",
        sql=MERGE_UPSERT_SQL,
        expected_measures=(
            ExpectedMeasure(column="real_amount", agg="SUM", alias="fee_amt"),
        ),
        expected_tables=("wedw_dwd.fee_bill_di",),
        expected_period="month",
        note="MERGE 增量 upsert：USING 子查询聚合识别，写目标表不混入源表",
    ),
    SqlInferCase(
        case_id="count_one_literal",
        dialect="hive",
        sql=COUNT_ONE_SQL,
        expected_measures=(
            ExpectedMeasure(column="*", agg="COUNT", alias="visit_cnt"),
        ),
        expected_tables=("wedw_dwd.visit_detail",),
        expected_period="month",
        note="count(1) 常量计数（生产最普遍写法）→ COUNT(*)",
    ),
    SqlInferCase(
        case_id="row_number_dedup",
        dialect="hive",
        sql=ROWNUM_DEDUP_SQL,
        expected_measures=(
            ExpectedMeasure(column="doctor_code", agg="COUNT_DISTINCT", alias="active_doctor_cnt"),
        ),
        expected_tables=("wedw_dwd.visit_detail",),
        expected_period="month",
        note="row_number 取最新再聚合：CTE + 窗口 + 条件过滤（rn=1）",
    ),
    SqlInferCase(
        case_id="lateral_view_explode",
        dialect="hive",
        sql=LATERAL_VIEW_SQL,
        expected_measures=(
            ExpectedMeasure(column="qty", agg="SUM", alias="drug_qty"),
        ),
        expected_tables=("wedw_dwd.prescription_detail",),
        expected_period="month",
        note="LATERAL VIEW explode：UDTF 数组列不混入源表",
    ),
    SqlInferCase(
        case_id="chained_cte",
        dialect="hive",
        sql=CHAINED_CTE_SQL,
        expected_measures=(
            ExpectedMeasure(column="amount", agg="SUM", alias="day_amt", table="wedw_dwd.fee_di"),
            ExpectedMeasure(column="amount", agg="SUM", alias="month_amt", table="wedw_dwd.fee_di"),
        ),
        expected_tables=("wedw_dwd.fee_di",),
        expected_period="month",
        note="多级 CTE 链：度量 table 穿透 CTE 名（base）解析到物理表",
    ),
    SqlInferCase(
        case_id="hive_if_conditional_sum",
        dialect="hive",
        sql=HIVE_IF_SUM_SQL,
        expected_measures=(
            ExpectedMeasure(column="real_amount", agg="SUM", alias="west_med_fee"),
            ExpectedMeasure(column="real_amount", agg="SUM", alias="cn_med_fee"),
        ),
        expected_tables=("wedw_dwd.fee_detail",),
        expected_period="month",
        note="Hive 条件 IF 求和：同列双语义靠别名区分",
    ),
    SqlInferCase(
        case_id="outer_rename_sunk",
        dialect="hive",
        sql=OUTER_RENAME_SQL,
        expected_measures=(
            ExpectedMeasure(
                column="doctor_code",
                agg="COUNT_DISTINCT",
                alias="active_doctor_cnt",
                table="wedw_dwd.visit_detail",
            ),
        ),
        expected_tables=("wedw_dwd.visit_detail",),
        expected_period="month",
        note="下沉透传改名：度量 alias 升级为外层别名（a.cnt as active_doctor_cnt）",
    ),
    SqlInferCase(
        case_id="partition_filter",
        dialect="hive",
        sql=PARTITION_FILTER_SQL,
        expected_measures=(
            ExpectedMeasure(column="doctor_id", agg="COUNT_DISTINCT", alias="doc_cnt"),
        ),
        expected_tables=("wedw_dwd.doctor_active_da",),
        expected_period="day",
        note="dt 分区过滤单表（date_sub/current_date 谓词）→ 日粒度",
    ),
)


def get_case(case_id: str) -> SqlInferCase | None:
    """按 case_id 取用例（测试/工具用）。"""
    for c in GOLDEN:
        if c.case_id == case_id:
            return c
    return None
