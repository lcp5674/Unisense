"""血缘评测集（golden dataset）——企业级血缘平台的精确率/召回率量化基准。

每条用例是**人工核对过语义**的真实生产场景（非 parser 输出的自证循环）：
标准答案由 SQL 语义人工推导——源表/目标表/列映射是否符合该 SQL 的加工含义。
覆盖 9 种方言（mysql/postgres/hive/spark/doris/clickhouse/starrocks/tsql/oracle）
× 生产高频形态（数仓分层链、CTAS 物理属性、集合运算、CTE 链、多表 UPDATE、
MERGE 多 WHEN、UNNEST/EXPLODE 数组展开、多目标 INSERT、分区写入、UPSERT 等），
其中 ``hive_user_production`` 为线上真实 Hive 生产 SQL（用户反馈过的子查询别名
泄漏场景）。

期望值均经真实 sqlglot 25.34.1（对齐生产 requirements pin）验证，语义人工核对。

用法：
- 回归保护：``pytest backend/tests/eval`` 断言全部用例精确匹配（100% 精确率+召回率）
- 量化报告：``python -m tests.eval.lineage_eval`` 输出各场景精确率/召回率/综合准确率
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GoldenCase:
    """一条血缘评测用例。

    Attributes:
        case_id: 唯一标识。
        dialect: sqlglot 方言名。
        sql: 生产 SQL 原文。
        target_table: 纯 SELECT 时的可选落点（方案 A+B），无则 None。
        expected_te: 期望表级血缘 ``{src->tgt}`` 集合（空=不产表级边）。
        expected_fe: 期望字段级血缘 ``{src.col->tgt.col}`` 集合（空=不产字段级边）。
        expected_ud_tables: 期望上游依赖表集合（仅纯 SELECT 无落点场景）。
        expected_ud_fields: 期望上游依赖字段集合（仅纯 SELECT 无落点场景）。
        note: 场景来源/语义说明。
    """

    case_id: str
    dialect: str
    sql: str
    target_table: str | None = None
    expected_te: set[str] = field(default_factory=set)
    expected_fe: set[str] = field(default_factory=set)
    expected_ud_tables: set[str] = field(default_factory=set)
    expected_ud_fields: set[str] = field(default_factory=set)
    note: str = ""


#: 评测集：24 个生产场景（含 1 个纯 SELECT 上游依赖场景）。
GOLDEN: tuple[GoldenCase, ...] = (
    # ---- 线上真实生产场景 ----
    GoldenCase(
        case_id="hive_user_production",
        dialect="hive",
        sql=(
            "select \n"
            "t1.hosp_id,\n"
            "t1.hosp_name,\n"
            "t1.province_name,\n"
            "t3.expert_id,\n"
            "t3.expert_name\n"
            "from wedw_dw.wy_zh_hospital_std_df t1 \n"
            "join (\n"
            "select tag_id,hospital_id \n"
            "from wedw_dwd.hospital_tag_df \n"
            "where date_id = '2026-08-13' and tag_id = 1151  and state = 0 \n"
            "group by tag_id,hospital_id\n"
            ") t2 \n"
            "on t1.hosp_id = t2.hospital_id \n"
            "join wedw_dw.wy_zh_hosp_dept_expert_relation_df t3 \n"
            "on t1.hosp_id = t3.hosp_id\n"
            " and t3.status_id=1 \n"
            " and t1.status_id=1"
        ),
        expected_ud_tables={
            "wedw_dw.wy_zh_hospital_std_df",
            "wedw_dwd.hospital_tag_df",
            "wedw_dw.wy_zh_hosp_dept_expert_relation_df",
        },
        expected_ud_fields={
            "wedw_dw.wy_zh_hospital_std_df.hosp_id",
            "wedw_dw.wy_zh_hospital_std_df.hosp_name",
            "wedw_dw.wy_zh_hospital_std_df.province_name",
            "wedw_dw.wy_zh_hosp_dept_expert_relation_df.expert_id",
            "wedw_dw.wy_zh_hosp_dept_expert_relation_df.expert_name",
        },
        note="线上真实 Hive 生产 SQL：纯 SELECT 多表 JOIN + 子查询别名，"
        "子查询别名 t2 不得泄漏进血缘（历轮 2340ac2 修复场景）。",
    ),
    # ---- 数仓分层加工链 ----
    GoldenCase(
        case_id="hive_layer_chain",
        dialect="hive",
        sql=(
            "INSERT INTO dwd.dim_user SELECT user_id, user_name, dept_id FROM ods.ods_user; "
            "INSERT INTO dws.dws_user_stat SELECT dept_id, COUNT(DISTINCT user_id) AS user_cnt "
            "FROM dwd.dim_user GROUP BY dept_id"
        ),
        expected_te={
            "ods.ods_user->dwd.dim_user",
            "dwd.dim_user->dws.dws_user_stat",
        },
        expected_fe={
            "ods.ods_user.user_id->dwd.dim_user.user_id",
            "ods.ods_user.user_name->dwd.dim_user.user_name",
            "ods.ods_user.dept_id->dwd.dim_user.dept_id",
            "dwd.dim_user.dept_id->dws.dws_user_stat.dept_id",
            "dwd.dim_user.user_id->dws.dws_user_stat.user_cnt",
        },
        note="典型数仓分层：ODS→DWD→DWS 两跳 INSERT，多语句拆分 + 聚合列映射。",
    ),
    # ---- 方言专属 CTAS 物理属性 ----
    GoldenCase(
        case_id="doris_ctas_agg",
        dialect="doris",
        sql=(
            "CREATE TABLE dws.t (id INT, v INT SUM, name VARCHAR(50) REPLACE) "
            "AGGREGATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 10 "
            'PROPERTIES("replication_num" = "1") AS SELECT id, v, name FROM ods.s'
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={
            "ods.s.id->dws.t.id",
            "ods.s.v->dws.t.v",
            "ods.s.name->dws.t.name",
        },
        note="Doris 聚合 CTAS：AGGREGATE KEY + 列级聚合类型 + 物理属性剥离（2d1e1e8 修复）。",
    ),
    # ---- UNION + 列清单 ----
    GoldenCase(
        case_id="mysql_union_column_list",
        dialect="mysql",
        sql=(
            "INSERT INTO dws.t (id, total) SELECT a.id, a.amount FROM ods.a "
            "UNION ALL SELECT b.uid, b.amt FROM ods.b"
        ),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={
            "ods.a.id->dws.t.id",
            "ods.a.amount->dws.t.total",
            "ods.b.uid->dws.t.id",
            "ods.b.amt->dws.t.total",
        },
        note="UNION ALL + 显式列清单：两分支列按位置映射到列清单（b66c51d 修复）。",
    ),
    # ---- PG 增量回刷 UPDATE...FROM ----
    GoldenCase(
        case_id="pg_update_from",
        dialect="postgres",
        sql="UPDATE dws.t SET v = s.v, updated_at = NOW() FROM ods.s WHERE dws.t.id = s.id",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.v->dws.t.v"},
        note="PG UPDATE...FROM 增量回刷：静态值 NOW() 不产边（c71b79a 修复）。",
    ),
    # ---- PG LATERAL 相关子查询 ----
    GoldenCase(
        case_id="pg_lateral",
        dialect="postgres",
        sql=(
            "INSERT INTO dws.t SELECT s.id, x.v FROM ods.s, "
            "LATERAL (SELECT v FROM ods.d WHERE d.id = s.id) x"
        ),
        expected_te={"ods.s->dws.t", "ods.d->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.d.v->dws.t.v"},
        note="PG LATERAL 相关子查询：x.v 归属内层 ods.d.v 而非外层（2d1e1e8 修复）。",
    ),
    # ---- PG UNNEST 数组展开 ----
    GoldenCase(
        case_id="pg_unnest",
        dialect="postgres",
        sql="INSERT INTO dws.t SELECT a.id, u.v FROM ods.a, UNNEST(a.items) AS u(v)",
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.a.items->dws.t.v"},
        note="PG UNNEST 带列别名：展开列 u.v 归属数组来源列 a.items（99a7b9b 修复）。",
    ),
    # ---- ClickHouse ARRAY JOIN + FINAL ----
    GoldenCase(
        case_id="ck_array_final",
        dialect="clickhouse",
        sql="INSERT INTO dws.t SELECT e.id, a.item FROM ods.a ARRAY JOIN a.items AS item FINAL",
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.a.item->dws.t.item"},
        note="CK ARRAY JOIN + FINAL：展开列映射 + 引擎修饰符。",
    ),
    # ---- Spark CTAS STORED AS ----
    GoldenCase(
        case_id="spark_ctas_stored",
        dialect="spark",
        sql="CREATE TABLE dws.t STORED AS PARQUET AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="Spark CTAS 带 STORED AS 物理属性。",
    ),
    # ---- Oracle 多目标 INSERT ALL ----
    GoldenCase(
        case_id="oracle_insert_all",
        dialect="oracle",
        sql=(
            "INSERT ALL INTO t1 (id) VALUES (s.id) INTO t2 (id, v) VALUES (s.id, s.v) "
            "SELECT id, v FROM ods.s"
        ),
        expected_te={"ods.s->t1", "ods.s->t2"},
        expected_fe={
            "ods.s.id->t1.id",
            "ods.s.id->t2.id",
            "ods.s.v->t2.v",
        },
        note="Oracle INSERT ALL 多目标：无 t1/t2 伪边、逐目标映射（2d1e1e8 修复）。",
    ),
    # ---- Hive MERGE 多 WHEN ----
    GoldenCase(
        case_id="hive_merge_multi_when",
        dialect="hive",
        sql=(
            "MERGE INTO dws.t USING ods.s ON dws.t.id = s.id "
            "WHEN MATCHED AND s.flag = 1 THEN UPDATE SET dws.t.v = s.v "
            "WHEN MATCHED THEN DELETE "
            "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v)"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="Hive MERGE 多 WHEN：UPDATE/INSERT 分支列映射，DELETE 不产字段边（c71b79a 兼容）。",
    ),
    # ---- Hive LATERAL VIEW EXPLODE ----
    GoldenCase(
        case_id="hive_explode",
        dialect="hive",
        sql=(
            "INSERT INTO dws.t SELECT e.tag, a.id FROM ods.a LATERAL VIEW EXPLODE(a.tags) e AS tag"
        ),
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.a.tags->dws.t.tag"},
        note="Hive LATERAL VIEW EXPLODE：展开列 e.tag 归属数组来源 a.tags（9266371 修复）。",
    ),
    # ---- CTE 深链穿透 ----
    GoldenCase(
        case_id="cte_deep_chain",
        dialect="mysql",
        sql=(
            "WITH c1 AS (SELECT id, v FROM ods.a), "
            "c2 AS (SELECT c1.id, c1.v * 2 AS v2 FROM c1) "
            "INSERT INTO dws.t SELECT c2.id, c2.v2 FROM c2"
        ),
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.a.v->dws.t.v2"},
        note="多层 CTE 链：c2 列穿透到真实源表 ods.a（b09cf81/f727e51 修复）。",
    ),
    # ---- EXCEPT 集合运算 ----
    GoldenCase(
        case_id="except_intersect",
        dialect="mysql",
        sql=("INSERT INTO dws.t SELECT id, v FROM ods.a EXCEPT SELECT id, v FROM ods.b"),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={
            "ods.a.id->dws.t.id",
            "ods.a.v->dws.t.v",
            "ods.b.id->dws.t.id",
            "ods.b.v->dws.t.v",
        },
        note="EXCEPT 集合运算：两分支表级/字段级均合并（55b29e4 修复）。",
    ),
    # ---- 多语句中间表链 ----
    GoldenCase(
        case_id="multi_statement",
        dialect="mysql",
        sql=(
            "CREATE TABLE dws.tmp AS SELECT id FROM ods.a; INSERT INTO dws.t SELECT id FROM dws.tmp"
        ),
        expected_te={"ods.a->dws.tmp", "dws.tmp->dws.t"},
        expected_fe={"ods.a.id->dws.tmp.id", "dws.tmp.id->dws.t.id"},
        note="多语句 ETL：中间表 dws.tmp 正确成链。",
    ),
    # ---- 子查询别名不泄漏 ----
    GoldenCase(
        case_id="subquery_no_leak",
        dialect="mysql",
        sql=(
            "INSERT INTO dws.t SELECT t1.id, t1.name FROM ods.orders t1 "
            "JOIN (SELECT order_id FROM ods.detail WHERE status = 0) t2 ON t1.id = t2.order_id"
        ),
        expected_te={"ods.orders->dws.t", "ods.detail->dws.t"},
        expected_fe={"ods.orders.id->dws.t.id", "ods.orders.name->dws.t.name"},
        note="子查询别名 t2 不泄漏为字段来源（2340ac2 修复）；WHERE 条件列不计入字段血缘。",
    ),
    # ---- StarRocks PRIMARY KEY CTAS ----
    GoldenCase(
        case_id="starrocks_primary",
        dialect="starrocks",
        sql=(
            "CREATE TABLE dws.t (id INT, v INT) PRIMARY KEY(id) "
            "DISTRIBUTED BY HASH(id) AS SELECT id, v FROM ods.s"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="StarRocks 主键模型 CTAS：PRIMARY KEY + DISTRIBUTED BY 支持（2d1e1e8 修复）。",
    ),
    # ---- SQL Server MERGE OUTPUT ----
    GoldenCase(
        case_id="tsql_merge_output",
        dialect="tsql",
        sql=(
            "MERGE INTO dws.t USING ods.s ON dws.t.id = s.id "
            "WHEN MATCHED THEN UPDATE SET dws.t.v = s.v OUTPUT $action, inserted.id"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.v->dws.t.v"},
        note="SQL Server MERGE OUTPUT：OUTPUT 子句不影响血缘映射。",
    ),
    # ---- MySQL REPLACE INTO ----
    GoldenCase(
        case_id="mysql_replace",
        dialect="mysql",
        sql="REPLACE INTO dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="MySQL REPLACE INTO：预处理等价 INSERT（b66c51d 修复）。",
    ),
    # ---- Hive 静态分区写入 ----
    GoldenCase(
        case_id="hive_insert_overwrite_partition",
        dialect="hive",
        sql=("INSERT OVERWRITE TABLE dws.t PARTITION (dt = '2026-08-16') SELECT id, v FROM ods.s"),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="Hive INSERT OVERWRITE 静态分区：分区列常量不产边。",
    ),
    # ---- ClickHouse GROUP BY WITH TOTALS ----
    GoldenCase(
        case_id="ck_totals_group",
        dialect="clickhouse",
        sql="INSERT INTO dws.t SELECT g, SUM(v) AS sv FROM ods.s GROUP BY g WITH TOTALS",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.g->dws.t.g", "ods.s.v->dws.t.sv"},
        note="CK GROUP BY WITH TOTALS：聚合列 v 映射到别名 sv。",
    ),
    # ---- PG UPSERT ON CONFLICT ----
    GoldenCase(
        case_id="pg_upsert_conflict",
        dialect="postgres",
        sql=(
            "INSERT INTO dws.t (id, v) SELECT id, v FROM ods.s "
            "ON CONFLICT (id) DO UPDATE SET v = EXCLUDED.v"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="PG UPSERT：INSERT 侧列映射为主，ON CONFLICT 更新侧不重复产边。",
    ),
    # ---- Hive 动态分区写入 ----
    GoldenCase(
        case_id="hive_dynamic_partition",
        dialect="hive",
        sql=("INSERT OVERWRITE TABLE dws.t PARTITION (dt) SELECT id, v, dt FROM ods.s"),
        expected_te={"ods.s->dws.t"},
        expected_fe={
            "ods.s.id->dws.t.id",
            "ods.s.v->dws.t.v",
            "ods.s.dt->dws.t.dt",
        },
        note="Hive 动态分区：分区列 dt 来自投影列，正常产边。",
    ),
    # ---- Spark EXPLODE(split(...)) 函数包裹 ----
    GoldenCase(
        case_id="spark_lateral_view",
        dialect="spark",
        sql=(
            "INSERT INTO dws.t SELECT e.tag, a.id FROM ods.a "
            "LATERAL VIEW EXPLODE(split(a.tags, ',')) e AS tag"
        ),
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.a.tags->dws.t.tag"},
        note="Spark EXPLODE(split()) 函数包裹：穿透解析到 a.tags（9266371 修复）。",
    ),
)
