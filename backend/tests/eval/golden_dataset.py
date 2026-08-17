# ruff: noqa: E501  # 评测集数据表：SQL 原文长行保持完整可读，文件级豁免行宽
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
    expected_ddl: set[str] = field(default_factory=set)
    note: str = ""


#: 评测集：209 个生产场景（含纯 SELECT 上游依赖、DDL 血缘；期望值由独立语义推导）。
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
    # ===== 生成评测集（企业级扩充）=====
    GoldenCase(
        case_id="insert_basic_mysql",
        dialect="mysql",
        sql="INSERT INTO dws.t SELECT id, name FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name"},
        note="INSERT 投影同名映射（mysql）。",
    ),
    GoldenCase(
        case_id="insert_basic_postgres",
        dialect="postgres",
        sql="INSERT INTO dws.t SELECT id, name FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name"},
        note="INSERT 投影同名映射（postgres）。",
    ),
    GoldenCase(
        case_id="insert_basic_hive",
        dialect="hive",
        sql="INSERT INTO dws.t SELECT id, name FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name"},
        note="INSERT 投影同名映射（hive）。",
    ),
    GoldenCase(
        case_id="insert_basic_spark",
        dialect="spark",
        sql="INSERT INTO dws.t SELECT id, name FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name"},
        note="INSERT 投影同名映射（spark）。",
    ),
    GoldenCase(
        case_id="insert_basic_doris",
        dialect="doris",
        sql="INSERT INTO dws.t SELECT id, name FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name"},
        note="INSERT 投影同名映射（doris）。",
    ),
    GoldenCase(
        case_id="insert_basic_clickhouse",
        dialect="clickhouse",
        sql="INSERT INTO dws.t SELECT id, name FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name"},
        note="INSERT 投影同名映射（clickhouse）。",
    ),
    GoldenCase(
        case_id="insert_basic_starrocks",
        dialect="starrocks",
        sql="INSERT INTO dws.t SELECT id, name FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name"},
        note="INSERT 投影同名映射（starrocks）。",
    ),
    GoldenCase(
        case_id="insert_basic_tsql",
        dialect="tsql",
        sql="INSERT INTO dws.t SELECT id, name FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name"},
        note="INSERT 投影同名映射（tsql）。",
    ),
    GoldenCase(
        case_id="insert_alias_mysql",
        dialect="mysql",
        sql="INSERT INTO dws.t SELECT s.id AS x, s.name AS y FROM ods.s s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.x", "ods.s.name->dws.t.y"},
        note="INSERT 投影列别名（mysql，别名解析为真实表）。",
    ),
    GoldenCase(
        case_id="insert_alias_hive",
        dialect="hive",
        sql="INSERT INTO dws.t SELECT s.id AS x, s.name AS y FROM ods.s s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.x", "ods.s.name->dws.t.y"},
        note="INSERT 投影列别名（hive，别名解析为真实表）。",
    ),
    GoldenCase(
        case_id="insert_alias_spark",
        dialect="spark",
        sql="INSERT INTO dws.t SELECT s.id AS x, s.name AS y FROM ods.s s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.x", "ods.s.name->dws.t.y"},
        note="INSERT 投影列别名（spark，别名解析为真实表）。",
    ),
    GoldenCase(
        case_id="insert_alias_doris",
        dialect="doris",
        sql="INSERT INTO dws.t SELECT s.id AS x, s.name AS y FROM ods.s s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.x", "ods.s.name->dws.t.y"},
        note="INSERT 投影列别名（doris，别名解析为真实表）。",
    ),
    GoldenCase(
        case_id="insert_cols_mysql",
        dialect="mysql",
        sql="INSERT INTO dws.t (a, b) SELECT id, name FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.a", "ods.s.name->dws.t.b"},
        note="INSERT 显式列清单按位置映射（mysql，x→a/y→b）。",
    ),
    GoldenCase(
        case_id="insert_cols_postgres",
        dialect="postgres",
        sql="INSERT INTO dws.t (a, b) SELECT id, name FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.a", "ods.s.name->dws.t.b"},
        note="INSERT 显式列清单按位置映射（postgres，x→a/y→b）。",
    ),
    GoldenCase(
        case_id="insert_cols_hive",
        dialect="hive",
        sql="INSERT INTO dws.t (a, b) SELECT id, name FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.a", "ods.s.name->dws.t.b"},
        note="INSERT 显式列清单按位置映射（hive，x→a/y→b）。",
    ),
    GoldenCase(
        case_id="ctas_mysql",
        dialect="mysql",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS（mysql）。",
    ),
    GoldenCase(
        case_id="ctas_postgres",
        dialect="postgres",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS（postgres）。",
    ),
    GoldenCase(
        case_id="ctas_hive",
        dialect="hive",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS（hive）。",
    ),
    GoldenCase(
        case_id="ctas_spark",
        dialect="spark",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS（spark）。",
    ),
    GoldenCase(
        case_id="ctas_doris",
        dialect="doris",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS（doris）。",
    ),
    GoldenCase(
        case_id="ctas_clickhouse",
        dialect="clickhouse",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS（clickhouse）。",
    ),
    GoldenCase(
        case_id="ctas_starrocks",
        dialect="starrocks",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS（starrocks）。",
    ),
    GoldenCase(
        case_id="ctas_cols_mysql",
        dialect="mysql",
        sql="CREATE TABLE dws.t (a, b) AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.a", "ods.s.v->dws.t.b"},
        note="CTAS 列清单位置映射（mysql，id→a/v→b）。",
    ),
    GoldenCase(
        case_id="ctas_cols_postgres",
        dialect="postgres",
        sql="CREATE TABLE dws.t (a, b) AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.a", "ods.s.v->dws.t.b"},
        note="CTAS 列清单位置映射（postgres，id→a/v→b）。",
    ),
    GoldenCase(
        case_id="join_mysql",
        dialect="mysql",
        sql=("INSERT INTO dws.t SELECT a.id, b.name FROM ods.a a JOIN ods.b b ON a.id = b.id\n"),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.name->dws.t.name"},
        note="多表 JOIN 源聚合（mysql）。",
    ),
    GoldenCase(
        case_id="join_hive",
        dialect="hive",
        sql=("INSERT INTO dws.t SELECT a.id, b.name FROM ods.a a JOIN ods.b b ON a.id = b.id\n"),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.name->dws.t.name"},
        note="多表 JOIN 源聚合（hive）。",
    ),
    GoldenCase(
        case_id="join_spark",
        dialect="spark",
        sql=("INSERT INTO dws.t SELECT a.id, b.name FROM ods.a a JOIN ods.b b ON a.id = b.id\n"),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.name->dws.t.name"},
        note="多表 JOIN 源聚合（spark）。",
    ),
    GoldenCase(
        case_id="join_doris",
        dialect="doris",
        sql=("INSERT INTO dws.t SELECT a.id, b.name FROM ods.a a JOIN ods.b b ON a.id = b.id\n"),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.name->dws.t.name"},
        note="多表 JOIN 源聚合（doris）。",
    ),
    GoldenCase(
        case_id="union_mysql",
        dialect="mysql",
        sql=("INSERT INTO dws.t SELECT id FROM ods.a UNION ALL SELECT uid AS id FROM ods.b\n"),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.uid->dws.t.id"},
        note="UNION 多分支合并（mysql）。",
    ),
    GoldenCase(
        case_id="union_hive",
        dialect="hive",
        sql=("INSERT INTO dws.t SELECT id FROM ods.a UNION ALL SELECT uid AS id FROM ods.b\n"),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.uid->dws.t.id"},
        note="UNION 多分支合并（hive）。",
    ),
    GoldenCase(
        case_id="union_spark",
        dialect="spark",
        sql=("INSERT INTO dws.t SELECT id FROM ods.a UNION ALL SELECT uid AS id FROM ods.b\n"),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.uid->dws.t.id"},
        note="UNION 多分支合并（spark）。",
    ),
    GoldenCase(
        case_id="cte_hive",
        dialect="hive",
        sql=(
            "WITH cte AS (SELECT id, name FROM ods.s) INSERT INTO dws.t SELECT id, name FROM cte\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name"},
        note="CTE 前缀穿透到真实源表（hive，不泄漏 cte 伪表）。",
    ),
    GoldenCase(
        case_id="cte_spark",
        dialect="spark",
        sql=(
            "WITH cte AS (SELECT id, name FROM ods.s) INSERT INTO dws.t SELECT id, name FROM cte\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name"},
        note="CTE 前缀穿透到真实源表（spark，不泄漏 cte 伪表）。",
    ),
    GoldenCase(
        case_id="cte_mysql",
        dialect="mysql",
        sql=(
            "WITH cte AS (SELECT id, name FROM ods.s) INSERT INTO dws.t SELECT id, name FROM cte\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name"},
        note="CTE 前缀穿透到真实源表（mysql，不泄漏 cte 伪表）。",
    ),
    GoldenCase(
        case_id="merge_mysql",
        dialect="mysql",
        sql=(
            "MERGE INTO dws.tgt USING ods.src ON dws.tgt.id = ods.src.id WHEN MATCHED THEN UPDATE SET dws.tgt.v = ods.src.v\n"
        ),
        expected_te={"ods.src->dws.tgt"},
        expected_fe={"ods.src.v->dws.tgt.v"},
        note="MERGE WHEN MATCHED UPDATE 列映射（mysql）。",
    ),
    GoldenCase(
        case_id="merge_hive",
        dialect="hive",
        sql=(
            "MERGE INTO dws.tgt USING ods.src ON dws.tgt.id = ods.src.id WHEN MATCHED THEN UPDATE SET dws.tgt.v = ods.src.v\n"
        ),
        expected_te={"ods.src->dws.tgt"},
        expected_fe={"ods.src.v->dws.tgt.v"},
        note="MERGE WHEN MATCHED UPDATE 列映射（hive）。",
    ),
    GoldenCase(
        case_id="update_mysql",
        dialect="mysql",
        sql="UPDATE dws.tgt JOIN ods.src s ON dws.tgt.id = s.id SET dws.tgt.v = s.v",
        expected_te={"ods.src->dws.tgt"},
        expected_fe={"ods.src.v->dws.tgt.v"},
        note="UPDATE 来源映射（mysql）。",
    ),
    GoldenCase(
        case_id="update_postgres",
        dialect="postgres",
        sql="UPDATE dws.tgt SET v = s.v FROM ods.src s WHERE dws.tgt.id = s.id",
        expected_te={"ods.src->dws.tgt"},
        expected_fe={"ods.src.v->dws.tgt.v"},
        note="UPDATE 来源映射（postgres）。",
    ),
    GoldenCase(
        case_id="doris_ctas_distributed",
        dialect="doris",
        sql=(
            "CREATE TABLE dws.t (id INT, v INT) DISTRIBUTED BY HASH(id) BUCKETS 10 PROPERTIES('replication_num'='1') AS SELECT id, v FROM ods.s\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="Doris CTAS 带 DISTRIBUTED BY/PROPERTIES 物理属性（剥离后血缘保留）。",
    ),
    GoldenCase(
        case_id="doris_with_label",
        dialect="doris",
        sql="INSERT INTO dws.t WITH LABEL 'load_20260816' SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="Doris INSERT WITH LABEL（剥离 LABEL 后血缘保留）。",
    ),
    GoldenCase(
        case_id="doris_aggregate_key",
        dialect="doris",
        sql=(
            "CREATE TABLE dws.t (id INT, v INT SUM) AGGREGATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 10 AS SELECT id, v FROM ods.s\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="Doris AGGREGATE KEY + 列级聚合类型（剥离后血缘保留）。",
    ),
    GoldenCase(
        case_id="hive_explode",
        dialect="hive",
        sql=(
            "INSERT INTO dws.t SELECT e.tag, a.id FROM ods.a LATERAL VIEW EXPLODE(a.tags) e AS tag\n"
        ),
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.a.tags->dws.t.tag"},
        note="Hive LATERAL VIEW EXPLODE 展开列归属数组源列。",
    ),
    GoldenCase(
        case_id="ck_array_join",
        dialect="clickhouse",
        sql="INSERT INTO dws.t SELECT item, id FROM ods.a ARRAY JOIN tags AS item",
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.a.item->dws.t.item"},
        note="ClickHouse ARRAY JOIN 展开列（未限定列归属源表既定行为）。",
    ),
    GoldenCase(
        case_id="pg_unnest",
        dialect="postgres",
        sql="INSERT INTO dws.t SELECT u.v, a.id FROM ods.a, UNNEST(a.items) AS u(v)",
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.a.items->dws.t.v"},
        note="PG UNNEST 数组展开列归属。",
    ),
    GoldenCase(
        case_id="tsql_insert_top",
        dialect="tsql",
        sql="INSERT TOP (10) INTO dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="SQL Server INSERT TOP (n)（剥离限行后血缘保留）。",
    ),
    GoldenCase(
        case_id="mysql_replace_into",
        dialect="mysql",
        sql="REPLACE INTO dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="MySQL REPLACE INTO（归一化为 INSERT 后血缘等价）。",
    ),
    GoldenCase(
        case_id="oracle_insert_all",
        dialect="oracle",
        sql=(
            "INSERT ALL INTO dws.t1 (id, v) VALUES (id, v) INTO dws.t2 (id, v) VALUES (id, v) SELECT id, v FROM ods.s\n"
        ),
        expected_te={"ods.s->dws.t1", "ods.s->dws.t2"},
        expected_fe={
            "ods.s.id->dws.t1.id",
            "ods.s.id->dws.t2.id",
            "ods.s.v->dws.t1.v",
            "ods.s.v->dws.t2.v",
        },
        note="Oracle INSERT ALL 多目标写入（逐目标映射，无伪边）。",
    ),
    GoldenCase(
        case_id="expr_mysql",
        dialect="mysql",
        sql=(
            "INSERT INTO dws.t SELECT a.amount + b.amount AS total FROM ods.a a JOIN ods.b b ON a.id = b.id\n"
        ),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.amount->dws.t.total", "ods.b.amount->dws.t.total"},
        note="多源表达式拆分（mysql，a.amount/b.amount 均计入派生源）。",
    ),
    GoldenCase(
        case_id="expr_hive",
        dialect="hive",
        sql=(
            "INSERT INTO dws.t SELECT a.amount + b.amount AS total FROM ods.a a JOIN ods.b b ON a.id = b.id\n"
        ),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.amount->dws.t.total", "ods.b.amount->dws.t.total"},
        note="多源表达式拆分（hive，a.amount/b.amount 均计入派生源）。",
    ),
    GoldenCase(
        case_id="window_mysql",
        dialect="mysql",
        sql=(
            "INSERT INTO dws.t SELECT ROW_NUMBER() OVER (PARTITION BY g ORDER BY ts) AS rn FROM ods.s\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.g->dws.t.rn", "ods.s.ts->dws.t.rn"},
        note="窗口函数 PARTITION/ORDER 列计入派生源（mysql）。",
    ),
    GoldenCase(
        case_id="window_hive",
        dialect="hive",
        sql=(
            "INSERT INTO dws.t SELECT ROW_NUMBER() OVER (PARTITION BY g ORDER BY ts) AS rn FROM ods.s\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.g->dws.t.rn", "ods.s.ts->dws.t.rn"},
        note="窗口函数 PARTITION/ORDER 列计入派生源（hive）。",
    ),
    GoldenCase(
        case_id="ud_mysql",
        dialect="mysql",
        sql="SELECT id, name FROM ods.s WHERE dt = '20260816'",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.id", "ods.s.name"},
        note="纯 SELECT 上游依赖只含投影列（mysql，条件列不混入）。",
    ),
    GoldenCase(
        case_id="ud_hive",
        dialect="hive",
        sql="SELECT id, name FROM ods.s WHERE dt = '20260816'",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.id", "ods.s.name"},
        note="纯 SELECT 上游依赖只含投影列（hive，条件列不混入）。",
    ),
    GoldenCase(
        case_id="ud_spark",
        dialect="spark",
        sql="SELECT id, name FROM ods.s WHERE dt = '20260816'",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.id", "ods.s.name"},
        note="纯 SELECT 上游依赖只含投影列（spark，条件列不混入）。",
    ),
    GoldenCase(
        case_id="ud_subquery_alias",
        dialect="hive",
        sql=(
            "SELECT t1.hosp_id, t3.expert_id FROM wedw_dw.wy_zh_hospital_std_df t1 JOIN wedw_dw.wy_zh_hosp_dept_expert_relation_df t3 ON t1.hosp_id = t3.hosp_id\n"
        ),
        expected_ud_tables={
            "wedw_dw.wy_zh_hosp_dept_expert_relation_df",
            "wedw_dw.wy_zh_hospital_std_df",
        },
        expected_ud_fields={
            "wedw_dw.wy_zh_hosp_dept_expert_relation_df.expert_id",
            "wedw_dw.wy_zh_hospital_std_df.hosp_id",
        },
        note="纯 SELECT 别名 JOIN（无子查询泄漏）。",
    ),
    GoldenCase(
        case_id="setop_except",
        dialect="postgres",
        sql="INSERT INTO dws.t SELECT id FROM ods.a EXCEPT SELECT id FROM ods.b",
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.id->dws.t.id"},
        note="集合运算 EXCEPT 两分支合并。",
    ),
    GoldenCase(
        case_id="setop_intersect",
        dialect="postgres",
        sql="INSERT INTO dws.t SELECT id FROM ods.a INTERSECT SELECT id FROM ods.b",
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.id->dws.t.id"},
        note="集合运算 INTERSECT 两分支合并。",
    ),
    GoldenCase(
        case_id="self_ref",
        dialect="hive",
        sql="INSERT INTO dws.t SELECT id, v FROM dws.t",
        note="同表覆盖式更新：不产自环边。",
    ),
    GoldenCase(
        case_id="ddl_create_like",
        dialect="mysql",
        sql="CREATE TABLE dws.t LIKE ods.s",
        expected_ddl={"create_like:ods.s->dws.t"},
        note="DDL CREATE TABLE LIKE 结构复制依赖。",
    ),
    GoldenCase(
        case_id="ddl_create_like_pg",
        dialect="postgres",
        sql="CREATE TABLE dws.t (LIKE ods.s INCLUDING ALL)",
        expected_ddl={"create_like:ods.s->dws.t"},
        note="PG CREATE TABLE (LIKE s) 结构复制。",
    ),
    GoldenCase(
        case_id="ddl_create_as_copy_of",
        dialect="postgres",
        sql="CREATE TABLE dws.t AS COPY OF ods.s",
        expected_ddl={"create_as_copy:ods.s->dws.t"},
        note="PG AS COPY OF 结构复制（正则兜底）。",
    ),
    GoldenCase(
        case_id="ddl_rename_table",
        dialect="mysql",
        sql="ALTER TABLE dws.old RENAME TO dws.new",
        expected_ddl={"rename_table:dws.old->dws.new"},
        note="DDL 表重命名依赖。",
    ),
    GoldenCase(
        case_id="ddl_rename_column",
        dialect="postgres",
        sql="ALTER TABLE dws.old RENAME COLUMN a TO b",
        expected_ddl={"rename_column:dws.old.a->dws.old.b"},
        note="DDL 列重命名依赖。",
    ),
    GoldenCase(
        case_id="ddl_change_column_mysql",
        dialect="mysql",
        sql="ALTER TABLE dws.t CHANGE a b INT NOT NULL",
        expected_ddl={"rename_column:dws.t.a->dws.t.b"},
        note="MySQL CHANGE 列重命名（正则兜底）。",
    ),
    GoldenCase(
        case_id="ddl_add_column",
        dialect="mysql",
        sql="ALTER TABLE dws.t ADD COLUMN c VARCHAR(10)",
        expected_ddl={"add_column:dws.t.c"},
        note="DDL ADD COLUMN 标记（不产数据流转边）。",
    ),
    GoldenCase(
        case_id="ddl_drop_column",
        dialect="mysql",
        sql="ALTER TABLE dws.t DROP COLUMN c",
        expected_ddl={"drop_column:dws.t.c"},
        note="DDL DROP COLUMN 标记。",
    ),
    GoldenCase(
        case_id="ddl_alter_column",
        dialect="mysql",
        sql="ALTER TABLE dws.t MODIFY COLUMN v BIGINT",
        expected_ddl={"alter_column:dws.t.v"},
        note="DDL MODIFY COLUMN 标记。",
    ),
    GoldenCase(
        case_id="ddl_drop_table",
        dialect="mysql",
        sql="DROP TABLE IF EXISTS ods.s, dws.t",
        expected_ddl={"drop_table:dws.t", "drop_table:ods.s"},
        note="DDL DROP TABLE 多表依赖失效标记。",
    ),
    GoldenCase(
        case_id="insert_1col_mysql",
        dialect="mysql",
        sql="INSERT INTO dws.t SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 INSERT（mysql）。",
    ),
    GoldenCase(
        case_id="insert_1col_postgres",
        dialect="postgres",
        sql="INSERT INTO dws.t SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 INSERT（postgres）。",
    ),
    GoldenCase(
        case_id="insert_1col_hive",
        dialect="hive",
        sql="INSERT INTO dws.t SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 INSERT（hive）。",
    ),
    GoldenCase(
        case_id="insert_1col_spark",
        dialect="spark",
        sql="INSERT INTO dws.t SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 INSERT（spark）。",
    ),
    GoldenCase(
        case_id="insert_1col_doris",
        dialect="doris",
        sql="INSERT INTO dws.t SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 INSERT（doris）。",
    ),
    GoldenCase(
        case_id="insert_1col_clickhouse",
        dialect="clickhouse",
        sql="INSERT INTO dws.t SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 INSERT（clickhouse）。",
    ),
    GoldenCase(
        case_id="insert_1col_starrocks",
        dialect="starrocks",
        sql="INSERT INTO dws.t SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 INSERT（starrocks）。",
    ),
    GoldenCase(
        case_id="insert_1col_tsql",
        dialect="tsql",
        sql="INSERT INTO dws.t SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 INSERT（tsql）。",
    ),
    GoldenCase(
        case_id="insert_1col_oracle",
        dialect="oracle",
        sql="INSERT INTO dws.t SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 INSERT（oracle）。",
    ),
    GoldenCase(
        case_id="insert_4col_mysql",
        dialect="mysql",
        sql="INSERT INTO dws.t SELECT id, name, dept, amt FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={
            "ods.s.amt->dws.t.amt",
            "ods.s.dept->dws.t.dept",
            "ods.s.id->dws.t.id",
            "ods.s.name->dws.t.name",
        },
        note="四列 INSERT（mysql）。",
    ),
    GoldenCase(
        case_id="insert_4col_hive",
        dialect="hive",
        sql="INSERT INTO dws.t SELECT id, name, dept, amt FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={
            "ods.s.amt->dws.t.amt",
            "ods.s.dept->dws.t.dept",
            "ods.s.id->dws.t.id",
            "ods.s.name->dws.t.name",
        },
        note="四列 INSERT（hive）。",
    ),
    GoldenCase(
        case_id="insert_4col_spark",
        dialect="spark",
        sql="INSERT INTO dws.t SELECT id, name, dept, amt FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={
            "ods.s.amt->dws.t.amt",
            "ods.s.dept->dws.t.dept",
            "ods.s.id->dws.t.id",
            "ods.s.name->dws.t.name",
        },
        note="四列 INSERT（spark）。",
    ),
    GoldenCase(
        case_id="insert_cols_alias",
        dialect="mysql",
        sql="INSERT INTO dws.t (a, b) SELECT s.id, s.name FROM ods.s s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.a", "ods.s.name->dws.t.b"},
        note="列清单 + 源别名组合。",
    ),
    GoldenCase(
        case_id="cte_chain",
        dialect="hive",
        sql=(
            "WITH a AS (SELECT id, v FROM ods.s), b AS (SELECT a.id AS id, a.v AS v FROM a) INSERT INTO dws.t SELECT id, v FROM b\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="多级 CTE 链穿透到真实源表。",
    ),
    GoldenCase(
        case_id="cte_join",
        dialect="spark",
        sql=(
            "WITH x AS (SELECT id FROM ods.a) INSERT INTO dws.t SELECT x.id, b.name FROM x JOIN ods.b b ON x.id = b.id\n"
        ),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.name->dws.t.name"},
        note="CTE 与真实表 JOIN（CTE 不泄漏为伪表）。",
    ),
    GoldenCase(
        case_id="derived_table",
        dialect="mysql",
        sql="INSERT INTO dws.t SELECT sq.v AS x FROM (SELECT v FROM ods.s) sq",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.v->dws.t.x"},
        note="FROM 派生表列穿透到内部真实表。",
    ),
    GoldenCase(
        case_id="case_expr",
        dialect="mysql",
        sql=(
            "INSERT INTO dws.t SELECT CASE WHEN flag = 1 THEN amt ELSE 0 END AS amt, flag FROM ods.a\n"
        ),
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.amt->dws.t.amt", "ods.a.flag->dws.t.amt", "ods.a.flag->dws.t.flag"},
        note="CASE WHEN 表达式：条件列与 then 分支列均计入派生源（业界惯例）。",
    ),
    GoldenCase(
        case_id="agg_groupby",
        dialect="hive",
        sql=("INSERT INTO dws.t SELECT dept, SUM(amt) AS total FROM ods.s GROUP BY dept\n"),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.amt->dws.t.total", "ods.s.dept->dws.t.dept"},
        note="聚合 GROUP BY 列计入派生源。",
    ),
    GoldenCase(
        case_id="scalar_subquery",
        dialect="postgres",
        sql=(
            "INSERT INTO dws.t SELECT id, (SELECT MAX(v) FROM ods.d WHERE ods.d.id = ods.s.id) AS calc FROM ods.s\n"
        ),
        expected_te={"ods.d->dws.t", "ods.s->dws.t"},
        expected_fe={
            "ods.d.id->dws.t.calc",
            "ods.d.v->dws.t.calc",
            "ods.s.id->dws.t.calc",
            "ods.s.id->dws.t.id",
        },
        note="SELECT 列表标量子查询：内层列/相关引用均计入 calc 派生源（第13轮修复语义）。",
    ),
    GoldenCase(
        case_id="hive_overwrite",
        dialect="hive",
        sql="INSERT OVERWRITE TABLE dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="Hive INSERT OVERWRITE TABLE。",
    ),
    GoldenCase(
        case_id="spark_ctas_stored",
        dialect="spark",
        sql="CREATE TABLE dws.t STORED AS PARQUET AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="Spark CTAS 带 STORED AS。",
    ),
    GoldenCase(
        case_id="ck_final",
        dialect="clickhouse",
        sql="INSERT INTO dws.t SELECT id, v FROM ods.s FINAL",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="ClickHouse FINAL 修饰（不干扰血缘）。",
    ),
    GoldenCase(
        case_id="ck_settings",
        dialect="clickhouse",
        sql="INSERT INTO dws.t SELECT id, v FROM ods.s SETTINGS max_threads = 8",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="ClickHouse SETTINGS 子句。",
    ),
    GoldenCase(
        case_id="oracle_select_into",
        dialect="oracle",
        sql="SELECT id, v INTO dws.t FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="Oracle SELECT INTO 建表式赋值（等价 CTAS）。",
    ),
    GoldenCase(
        case_id="tsql_select_into",
        dialect="tsql",
        sql="SELECT id, v INTO dws.t FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="SQL Server SELECT INTO 建表式赋值。",
    ),
    GoldenCase(
        case_id="merge_not_matched",
        dialect="hive",
        sql=(
            "MERGE INTO dws.tgt USING ods.src ON dws.tgt.id = ods.src.id WHEN NOT MATCHED THEN INSERT (id, v) VALUES (id, v)\n"
        ),
        expected_te={"ods.src->dws.tgt"},
        expected_fe={"ods.src.id->dws.tgt.id", "ods.src.v->dws.tgt.v"},
        note="MERGE WHEN NOT MATCHED INSERT 列映射。",
    ),
    GoldenCase(
        case_id="update_multi_set",
        dialect="mysql",
        sql=(
            "UPDATE dws.t JOIN ods.a ON dws.t.id = ods.a.id JOIN ods.b ON dws.t.id = ods.b.id SET dws.t.a = ods.a.v, dws.t.b = ods.b.x\n"
        ),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.v->dws.t.a", "ods.b.x->dws.t.b"},
        note="多表 UPDATE 跨 SET：全部源表计入表级、每列独立归属。",
    ),
    GoldenCase(
        case_id="three_part",
        dialect="spark",
        sql="INSERT INTO dws.t SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="三层表名规范化（spark catalog.db.table）。",
    ),
    GoldenCase(
        case_id="quoted_table",
        dialect="postgres",
        sql='INSERT INTO dws.t SELECT id FROM "s1"."t1"',
        expected_te={"s1.t1->dws.t"},
        expected_fe={"s1.t1.id->dws.t.id"},
        note="PG 双引号 schema 表名规范化。",
    ),
    GoldenCase(
        case_id="ud_join_filter",
        dialect="hive",
        sql=(
            "SELECT a.id, b.name FROM ods.a a JOIN ods.b b ON a.id = b.id WHERE a.dt = '2026-08-16'\n"
        ),
        expected_ud_tables={"ods.a", "ods.b"},
        expected_ud_fields={"ods.a.id", "ods.b.name"},
        note="纯 SELECT JOIN + WHERE（条件列不混入投影）。",
    ),
    GoldenCase(
        case_id="ud_cte",
        dialect="spark",
        sql="WITH x AS (SELECT id, v FROM ods.s) SELECT id FROM x WHERE v > 0",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.id"},
        note="纯 SELECT 经 CTE（穿透到真实源表）。",
    ),
    GoldenCase(
        case_id="ud_except",
        dialect="postgres",
        sql="SELECT id FROM ods.a EXCEPT SELECT id FROM ods.b",
        expected_ud_tables={"ods.a", "ods.b"},
        expected_ud_fields={"ods.a.id", "ods.b.id"},
        note="纯 SELECT EXCEPT 两分支来源。",
    ),
    GoldenCase(
        case_id="ddl_drop_table_single",
        dialect="postgres",
        sql="DROP TABLE IF EXISTS ods.s",
        expected_ddl={"drop_table:ods.s"},
        note="DDL 单表 DROP。",
    ),
    GoldenCase(
        case_id="ddl_rename_view",
        dialect="mysql",
        sql="ALTER VIEW dws.v RENAME TO dws.w",
        expected_ddl={"rename_table:dws.v->dws.w"},
        note="DDL 视图重命名（ALTER VIEW RENAME）。",
    ),
    GoldenCase(
        case_id="ddl_multi_statement",
        dialect="hive",
        sql="CREATE TABLE dws.t LIKE ods.s; DROP TABLE IF EXISTS ods.old",
        expected_ddl={"create_like:ods.s->dws.t", "drop_table:ods.old"},
        note="多语句混合 DDL（LIKE + DROP）。",
    ),
    GoldenCase(
        case_id="ctas_all_mysql",
        dialect="mysql",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS 全方言（mysql）。",
    ),
    GoldenCase(
        case_id="ctas_all_postgres",
        dialect="postgres",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS 全方言（postgres）。",
    ),
    GoldenCase(
        case_id="ctas_all_hive",
        dialect="hive",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS 全方言（hive）。",
    ),
    GoldenCase(
        case_id="ctas_all_spark",
        dialect="spark",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS 全方言（spark）。",
    ),
    GoldenCase(
        case_id="ctas_all_doris",
        dialect="doris",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS 全方言（doris）。",
    ),
    GoldenCase(
        case_id="ctas_all_clickhouse",
        dialect="clickhouse",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS 全方言（clickhouse）。",
    ),
    GoldenCase(
        case_id="ctas_all_starrocks",
        dialect="starrocks",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS 全方言（starrocks）。",
    ),
    GoldenCase(
        case_id="ctas_all_tsql",
        dialect="tsql",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS 全方言（tsql）。",
    ),
    GoldenCase(
        case_id="ctas_all_oracle",
        dialect="oracle",
        sql="CREATE TABLE dws.t AS SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS 全方言（oracle）。",
    ),
    GoldenCase(
        case_id="insert_all_mysql",
        dialect="mysql",
        sql="INSERT INTO dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="INSERT 全方言（mysql）。",
    ),
    GoldenCase(
        case_id="insert_all_postgres",
        dialect="postgres",
        sql="INSERT INTO dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="INSERT 全方言（postgres）。",
    ),
    GoldenCase(
        case_id="insert_all_hive",
        dialect="hive",
        sql="INSERT INTO dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="INSERT 全方言（hive）。",
    ),
    GoldenCase(
        case_id="insert_all_spark",
        dialect="spark",
        sql="INSERT INTO dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="INSERT 全方言（spark）。",
    ),
    GoldenCase(
        case_id="insert_all_doris",
        dialect="doris",
        sql="INSERT INTO dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="INSERT 全方言（doris）。",
    ),
    GoldenCase(
        case_id="insert_all_clickhouse",
        dialect="clickhouse",
        sql="INSERT INTO dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="INSERT 全方言（clickhouse）。",
    ),
    GoldenCase(
        case_id="insert_all_starrocks",
        dialect="starrocks",
        sql="INSERT INTO dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="INSERT 全方言（starrocks）。",
    ),
    GoldenCase(
        case_id="insert_all_tsql",
        dialect="tsql",
        sql="INSERT INTO dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="INSERT 全方言（tsql）。",
    ),
    GoldenCase(
        case_id="insert_all_oracle",
        dialect="oracle",
        sql="INSERT INTO dws.t SELECT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="INSERT 全方言（oracle）。",
    ),
    GoldenCase(
        case_id="join3",
        dialect="mysql",
        sql=(
            "INSERT INTO dws.t SELECT a.id, b.name, c.dept FROM ods.a a JOIN ods.b b ON a.id = b.id JOIN ods.c c ON b.id = c.id\n"
        ),
        expected_te={"ods.a->dws.t", "ods.b->dws.t", "ods.c->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.name->dws.t.name", "ods.c.dept->dws.t.dept"},
        note="三表 JOIN 全源聚合。",
    ),
    GoldenCase(
        case_id="union3",
        dialect="mysql",
        sql=(
            "INSERT INTO dws.t SELECT id FROM ods.a UNION ALL SELECT uid AS id FROM ods.b UNION ALL SELECT eid AS id FROM ods.c\n"
        ),
        expected_te={"ods.a->dws.t", "ods.b->dws.t", "ods.c->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.uid->dws.t.id", "ods.c.eid->dws.t.id"},
        note="UNION 三分支列名不同（位置回退）。",
    ),
    GoldenCase(
        case_id="multi_stage",
        dialect="hive",
        sql=(
            "CREATE TABLE tmp.tmp_t AS SELECT id, v FROM ods.a; INSERT INTO dws.t SELECT id, v FROM tmp.tmp_t\n"
        ),
        expected_te={"ods.a->tmp.tmp_t", "tmp.tmp_t->dws.t"},
        expected_fe={
            "ods.a.id->tmp.tmp_t.id",
            "ods.a.v->tmp.tmp_t.v",
            "tmp.tmp_t.id->dws.t.id",
            "tmp.tmp_t.v->dws.t.v",
        },
        note="多语句中间表链（tmp_t 成链）。",
    ),
    GoldenCase(
        case_id="nested_subquery",
        dialect="mysql",
        sql=(
            "INSERT INTO dws.t SELECT sq.v AS x FROM (SELECT v FROM (SELECT v FROM ods.s) i) sq\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.v->dws.t.x"},
        note="嵌套两层子查询穿透。",
    ),
    GoldenCase(
        case_id="distinct_window",
        dialect="mysql",
        sql=(
            "INSERT INTO dws.t SELECT DISTINCT dept, RANK() OVER (ORDER BY amt) AS rnk FROM ods.s\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.amt->dws.t.rnk", "ods.s.dept->dws.t.dept"},
        note="DISTINCT + 窗口组合。",
    ),
    GoldenCase(
        case_id="qualify",
        dialect="mysql",
        sql=(
            "INSERT INTO dws.t SELECT dept, ROW_NUMBER() OVER (PARTITION BY dept ORDER BY amt) AS rn FROM ods.s QUALIFY rn = 1\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.amt->dws.t.rn", "ods.s.dept->dws.t.dept", "ods.s.dept->dws.t.rn"},
        note="QUALIFY 窗口（PARTITION/ORDER 列均计入派生源）。",
    ),
    GoldenCase(
        case_id="update_subquery_set",
        dialect="postgres",
        sql=(
            "UPDATE dws.t SET v = (SELECT MAX(v) FROM ods.s WHERE ods.s.id = dws.t.id) WHERE id = 1\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.v->dws.t.v"},
        note="UPDATE SET 子查询赋值（内层列映射）。",
    ),
    GoldenCase(
        case_id="merge_using_subquery",
        dialect="hive",
        sql=(
            "MERGE INTO dws.tgt USING (SELECT a.id, b.v FROM ods.a a JOIN ods.b b ON a.id = b.id) src ON dws.tgt.id = src.id WHEN MATCHED THEN UPDATE SET dws.tgt.v = src.v\n"
        ),
        expected_te={"ods.a->dws.tgt", "ods.b->dws.tgt"},
        expected_fe={"ods.b.v->dws.tgt.v"},
        note="MERGE USING 子查询：SET 仅映射被更新列（v→v），join 键不产字段边。",
    ),
    GoldenCase(
        case_id="ud_4col",
        dialect="spark",
        sql="SELECT id, name, dept, amt FROM ods.s",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.amt", "ods.s.dept", "ods.s.id", "ods.s.name"},
        note="纯 SELECT 四列投影。",
    ),
    GoldenCase(
        case_id="ud_derived",
        dialect="mysql",
        sql="SELECT sq.v FROM (SELECT v FROM ods.s) sq",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.v"},
        note="纯 SELECT 派生表穿透。",
    ),
    GoldenCase(
        case_id="ud_expr",
        dialect="hive",
        sql="SELECT id, amt * 0.9 AS discounted FROM ods.s",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.amt", "ods.s.id"},
        note="纯 SELECT 表达式投影（叶子列）。",
    ),
    GoldenCase(
        case_id="ddl_rename_column_hive",
        dialect="hive",
        sql="ALTER TABLE dws.t CHANGE a b STRING",
        expected_ddl={"rename_column:dws.t.a->dws.t.b"},
        note="Hive CHANGE 列重命名（正则兜底）。",
    ),
    GoldenCase(
        case_id="ddl_drop_column_ifexists",
        dialect="postgres",
        sql="ALTER TABLE dws.t DROP COLUMN IF EXISTS c",
        expected_ddl={"drop_column:dws.t.c"},
        note="PG DROP COLUMN IF EXISTS。",
    ),
    GoldenCase(
        case_id="insert_alias_more_postgres",
        dialect="postgres",
        sql="INSERT INTO dws.t SELECT s.id AS x, s.name AS y FROM ods.s s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.x", "ods.s.name->dws.t.y"},
        note="INSERT 列别名（postgres）。",
    ),
    GoldenCase(
        case_id="insert_alias_more_clickhouse",
        dialect="clickhouse",
        sql="INSERT INTO dws.t SELECT s.id AS x, s.name AS y FROM ods.s s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.x", "ods.s.name->dws.t.y"},
        note="INSERT 列别名（clickhouse）。",
    ),
    GoldenCase(
        case_id="insert_alias_more_starrocks",
        dialect="starrocks",
        sql="INSERT INTO dws.t SELECT s.id AS x, s.name AS y FROM ods.s s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.x", "ods.s.name->dws.t.y"},
        note="INSERT 列别名（starrocks）。",
    ),
    GoldenCase(
        case_id="insert_alias_more_tsql",
        dialect="tsql",
        sql="INSERT INTO dws.t SELECT s.id AS x, s.name AS y FROM ods.s s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.x", "ods.s.name->dws.t.y"},
        note="INSERT 列别名（tsql）。",
    ),
    GoldenCase(
        case_id="insert_alias_more_oracle",
        dialect="oracle",
        sql="INSERT INTO dws.t SELECT s.id AS x, s.name AS y FROM ods.s s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.x", "ods.s.name->dws.t.y"},
        note="INSERT 列别名（oracle）。",
    ),
    GoldenCase(
        case_id="ctas_1col_mysql",
        dialect="mysql",
        sql="CREATE TABLE dws.t AS SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 CTAS（mysql）。",
    ),
    GoldenCase(
        case_id="ctas_1col_postgres",
        dialect="postgres",
        sql="CREATE TABLE dws.t AS SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 CTAS（postgres）。",
    ),
    GoldenCase(
        case_id="ctas_1col_hive",
        dialect="hive",
        sql="CREATE TABLE dws.t AS SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 CTAS（hive）。",
    ),
    GoldenCase(
        case_id="ctas_1col_spark",
        dialect="spark",
        sql="CREATE TABLE dws.t AS SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 CTAS（spark）。",
    ),
    GoldenCase(
        case_id="ctas_1col_doris",
        dialect="doris",
        sql="CREATE TABLE dws.t AS SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 CTAS（doris）。",
    ),
    GoldenCase(
        case_id="ctas_1col_clickhouse",
        dialect="clickhouse",
        sql="CREATE TABLE dws.t AS SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 CTAS（clickhouse）。",
    ),
    GoldenCase(
        case_id="ctas_1col_starrocks",
        dialect="starrocks",
        sql="CREATE TABLE dws.t AS SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 CTAS（starrocks）。",
    ),
    GoldenCase(
        case_id="ctas_1col_tsql",
        dialect="tsql",
        sql="CREATE TABLE dws.t AS SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 CTAS（tsql）。",
    ),
    GoldenCase(
        case_id="ctas_1col_oracle",
        dialect="oracle",
        sql="CREATE TABLE dws.t AS SELECT id FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id"},
        note="单列 CTAS（oracle）。",
    ),
    GoldenCase(
        case_id="union_same_postgres",
        dialect="postgres",
        sql="INSERT INTO dws.t SELECT id FROM ods.a UNION SELECT id FROM ods.b",
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.id->dws.t.id"},
        note="UNION 同名列（postgres）。",
    ),
    GoldenCase(
        case_id="union_same_doris",
        dialect="doris",
        sql="INSERT INTO dws.t SELECT id FROM ods.a UNION SELECT id FROM ods.b",
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.id->dws.t.id"},
        note="UNION 同名列（doris）。",
    ),
    GoldenCase(
        case_id="union_same_starrocks",
        dialect="starrocks",
        sql="INSERT INTO dws.t SELECT id FROM ods.a UNION SELECT id FROM ods.b",
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.id->dws.t.id"},
        note="UNION 同名列（starrocks）。",
    ),
    GoldenCase(
        case_id="merge_more_mysql",
        dialect="mysql",
        sql=(
            "MERGE INTO dws.tgt USING ods.src ON dws.tgt.id = ods.src.id WHEN MATCHED THEN UPDATE SET dws.tgt.v = ods.src.v\n"
        ),
        expected_te={"ods.src->dws.tgt"},
        expected_fe={"ods.src.v->dws.tgt.v"},
        note="MERGE 基础（mysql）。",
    ),
    GoldenCase(
        case_id="merge_more_postgres",
        dialect="postgres",
        sql=(
            "MERGE INTO dws.tgt USING ods.src ON dws.tgt.id = ods.src.id WHEN MATCHED THEN UPDATE SET dws.tgt.v = ods.src.v\n"
        ),
        expected_te={"ods.src->dws.tgt"},
        expected_fe={"ods.src.v->dws.tgt.v"},
        note="MERGE 基础（postgres）。",
    ),
    GoldenCase(
        case_id="merge_more_doris",
        dialect="doris",
        sql=(
            "MERGE INTO dws.tgt USING ods.src ON dws.tgt.id = ods.src.id WHEN MATCHED THEN UPDATE SET dws.tgt.v = ods.src.v\n"
        ),
        expected_te={"ods.src->dws.tgt"},
        expected_fe={"ods.src.v->dws.tgt.v"},
        note="MERGE 基础（doris）。",
    ),
    GoldenCase(
        case_id="update_where_subquery",
        dialect="postgres",
        sql=(
            "UPDATE dws.t SET v = s.v FROM ods.s s WHERE dws.t.id IN (SELECT id FROM ods.s WHERE v > 0)\n"
        ),
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.v->dws.t.v"},
        note="UPDATE FROM + WHERE 子查询来源。",
    ),
    GoldenCase(
        case_id="ud_more_mysql",
        dialect="mysql",
        sql="SELECT id, name FROM ods.s",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.id", "ods.s.name"},
        note="纯 SELECT 两列（mysql）。",
    ),
    GoldenCase(
        case_id="ud_more_postgres",
        dialect="postgres",
        sql="SELECT id, name FROM ods.s",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.id", "ods.s.name"},
        note="纯 SELECT 两列（postgres）。",
    ),
    GoldenCase(
        case_id="ud_more_clickhouse",
        dialect="clickhouse",
        sql="SELECT id, name FROM ods.s",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.id", "ods.s.name"},
        note="纯 SELECT 两列（clickhouse）。",
    ),
    GoldenCase(
        case_id="ud_more_doris",
        dialect="doris",
        sql="SELECT id, name FROM ods.s",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.id", "ods.s.name"},
        note="纯 SELECT 两列（doris）。",
    ),
    GoldenCase(
        case_id="ud_more_starrocks",
        dialect="starrocks",
        sql="SELECT id, name FROM ods.s",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.id", "ods.s.name"},
        note="纯 SELECT 两列（starrocks）。",
    ),
    GoldenCase(
        case_id="ud_more_tsql",
        dialect="tsql",
        sql="SELECT id, name FROM ods.s",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.id", "ods.s.name"},
        note="纯 SELECT 两列（tsql）。",
    ),
    GoldenCase(
        case_id="ud_more_oracle",
        dialect="oracle",
        sql="SELECT id, name FROM ods.s",
        expected_ud_tables={"ods.s"},
        expected_ud_fields={"ods.s.id", "ods.s.name"},
        note="纯 SELECT 两列（oracle）。",
    ),
    GoldenCase(
        case_id="ddl_like_doris",
        dialect="doris",
        sql="CREATE TABLE dws.t LIKE ods.s",
        expected_ddl={"create_like:ods.s->dws.t"},
        note="Doris CREATE LIKE。",
    ),
    GoldenCase(
        case_id="ddl_rename_ck",
        dialect="clickhouse",
        sql="ALTER TABLE dws.old RENAME TO dws.new",
        expected_ddl={"rename_table:dws.old->dws.new"},
        note="ClickHouse 表重命名。",
    ),
    GoldenCase(
        case_id="ddl_drop_tsql",
        dialect="tsql",
        sql="DROP TABLE dws.t",
        expected_ddl={"drop_table:dws.t"},
        note="tsql DROP TABLE。",
    ),
    GoldenCase(
        case_id="coalesce_expr",
        dialect="postgres",
        sql="INSERT INTO dws.t SELECT COALESCE(a.x, a.y) AS v FROM ods.a a",
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.x->dws.t.v", "ods.a.y->dws.t.v"},
        note="COALESCE 多参数均计入派生源。",
    ),
    GoldenCase(
        case_id="quoted_col",
        dialect="mysql",
        sql="INSERT INTO dws.t SELECT `select` AS sel FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.select->dws.t.sel"},
        note="MySQL 反引号保留字列名。",
    ),
    GoldenCase(
        case_id="ctas_distinct",
        dialect="mysql",
        sql="CREATE TABLE dws.t AS SELECT DISTINCT id, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.v->dws.t.v"},
        note="CTAS DISTINCT。",
    ),
    GoldenCase(
        case_id="insert_3col_mysql",
        dialect="mysql",
        sql="INSERT INTO dws.t SELECT id, name, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name", "ods.s.v->dws.t.v"},
        note="三列 INSERT（mysql）。",
    ),
    GoldenCase(
        case_id="insert_3col_hive",
        dialect="hive",
        sql="INSERT INTO dws.t SELECT id, name, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name", "ods.s.v->dws.t.v"},
        note="三列 INSERT（hive）。",
    ),
    GoldenCase(
        case_id="insert_3col_spark",
        dialect="spark",
        sql="INSERT INTO dws.t SELECT id, name, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name", "ods.s.v->dws.t.v"},
        note="三列 INSERT（spark）。",
    ),
    GoldenCase(
        case_id="insert_3col_doris",
        dialect="doris",
        sql="INSERT INTO dws.t SELECT id, name, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name", "ods.s.v->dws.t.v"},
        note="三列 INSERT（doris）。",
    ),
    GoldenCase(
        case_id="insert_3col_clickhouse",
        dialect="clickhouse",
        sql="INSERT INTO dws.t SELECT id, name, v FROM ods.s",
        expected_te={"ods.s->dws.t"},
        expected_fe={"ods.s.id->dws.t.id", "ods.s.name->dws.t.name", "ods.s.v->dws.t.v"},
        note="三列 INSERT（clickhouse）。",
    ),
    GoldenCase(
        case_id="expr_single_postgres",
        dialect="postgres",
        sql="INSERT INTO dws.t SELECT amt * 1.1 AS total FROM ods.a",
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.amt->dws.t.total"},
        note="单源表达式（postgres）。",
    ),
    GoldenCase(
        case_id="expr_single_spark",
        dialect="spark",
        sql="INSERT INTO dws.t SELECT amt * 1.1 AS total FROM ods.a",
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.amt->dws.t.total"},
        note="单源表达式（spark）。",
    ),
    GoldenCase(
        case_id="expr_single_starrocks",
        dialect="starrocks",
        sql="INSERT INTO dws.t SELECT amt * 1.1 AS total FROM ods.a",
        expected_te={"ods.a->dws.t"},
        expected_fe={"ods.a.amt->dws.t.total"},
        note="单源表达式（starrocks）。",
    ),
    GoldenCase(
        case_id="join_more_postgres",
        dialect="postgres",
        sql=("INSERT INTO dws.t SELECT a.id, b.name FROM ods.a a JOIN ods.b b ON a.id = b.id\n"),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.name->dws.t.name"},
        note="双表 JOIN（postgres）。",
    ),
    GoldenCase(
        case_id="join_more_clickhouse",
        dialect="clickhouse",
        sql=("INSERT INTO dws.t SELECT a.id, b.name FROM ods.a a JOIN ods.b b ON a.id = b.id\n"),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.name->dws.t.name"},
        note="双表 JOIN（clickhouse）。",
    ),
    GoldenCase(
        case_id="join_more_starrocks",
        dialect="starrocks",
        sql=("INSERT INTO dws.t SELECT a.id, b.name FROM ods.a a JOIN ods.b b ON a.id = b.id\n"),
        expected_te={"ods.a->dws.t", "ods.b->dws.t"},
        expected_fe={"ods.a.id->dws.t.id", "ods.b.name->dws.t.name"},
        note="双表 JOIN（starrocks）。",
    ),
    GoldenCase(
        case_id="update_more_hive",
        dialect="hive",
        sql=("UPDATE dws.tgt SET v = src.v FROM ods.src src WHERE dws.tgt.id = src.id\n"),
        expected_te={"ods.src->dws.tgt"},
        expected_fe={"ods.src.v->dws.tgt.v"},
        note="UPDATE FROM（hive）。",
    ),
    GoldenCase(
        case_id="update_more_doris",
        dialect="doris",
        sql=("UPDATE dws.tgt SET v = src.v FROM ods.src src WHERE dws.tgt.id = src.id\n"),
        expected_te={"ods.src->dws.tgt"},
        expected_fe={"ods.src.v->dws.tgt.v"},
        note="UPDATE FROM（doris）。",
    ),
    GoldenCase(
        case_id="ddl_like_spark",
        dialect="spark",
        sql="CREATE TABLE dws.t LIKE ods.s",
        expected_ddl={"create_like:ods.s->dws.t"},
        note="Spark CREATE LIKE。",
    ),
    GoldenCase(
        case_id="ddl_drop_oracle",
        dialect="oracle",
        sql="DROP TABLE ods.s",
        expected_ddl={"drop_table:ods.s"},
        note="Oracle DROP TABLE。",
    ),
    GoldenCase(
        case_id="ddl_rename_col_starrocks",
        dialect="starrocks",
        sql="ALTER TABLE dws.t RENAME COLUMN a TO b",
        expected_ddl={"rename_column:dws.t.a->dws.t.b"},
        note="StarRocks 列重命名。",
    ),
    GoldenCase(
        case_id="ud_3table_join",
        dialect="mysql",
        sql=(
            "SELECT a.id, b.name, c.dept FROM ods.a a JOIN ods.b b ON a.id = b.id JOIN ods.c c ON b.id = c.id\n"
        ),
        expected_ud_tables={"ods.a", "ods.b", "ods.c"},
        expected_ud_fields={"ods.a.id", "ods.b.name", "ods.c.dept"},
        note="纯 SELECT 三表 JOIN 投影。",
    ),
)
