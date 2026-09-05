"""dp 调度 SQL 节点解析器单元测试。

覆盖 plan.md §4 解析管线三态判定、USE 库前缀补全、临时表排除、复杂度分级。
"""

from __future__ import annotations

from app.services.lineage.dp_sync_parser import (
    DEFAULT_COMPLEXITY_RULES,
    detect_complexity_features,
    parse_dp_step,
)


def test_simple_ctas_is_ok_without_features() -> None:
    sql = (
        "create table wedw_dwd.dp_out as "
        "select a.col1, b.col2 from wedw_ods.a a join wedw_ods.b b on a.id=b.id"
    )
    r = parse_dp_step(sql)
    assert r.status == "ok"
    assert r.is_complex is False
    assert {e.source for e in r.table_edges} == {"wedw_ods.a", "wedw_ods.b"}
    assert {e.target for e in r.table_edges} == {"wedw_dwd.dp_out"}
    assert len(r.field_edges) == 2


def test_hive_full_script_with_use_and_drop() -> None:
    sql = """set hive.exec.dynamic.partition=true;
use wedw_dwd;
drop table if exists dp_dq_measure_df;
create table dp_dq_measure_df as
select department_id, count(1) as cnt from wedw_ods.visit_d
where date_id='2026-08-18' group by department_id"""
    r = parse_dp_step(sql)
    assert r.status == "ok"
    # 无前缀产出表应补 use 库前缀，与任务 out_table（wedw_dwd.dp_dq_measure_df）对齐
    assert [e.target for e in r.table_edges] == ["wedw_dwd.dp_dq_measure_df"]
    assert r.used_db == "wedw_dwd"


def test_use_db_switch_scopes_statements_correctly() -> None:
    sql = (
        "use wedw_ods; create table t1 as select * from src1; "
        "use wedw_dwd; create table t2 as select * from t1"
    )
    r = parse_dp_step(sql)
    assert r.status == "ok"
    edges = {(e.source, e.target) for e in r.table_edges}
    assert ("wedw_ods.src1", "wedw_ods.t1") in edges  # 第一个 use 域
    assert ("wedw_dwd.t1", "wedw_dwd.t2") in edges  # 第二个 use 域
    assert r.used_db == "wedw_dwd"


def test_pure_ddl_no_flow_is_skipped() -> None:
    sql = "create table wedw_dwd.dp_tmp_init (id bigint, name string)"
    r = parse_dp_step(sql)
    assert r.status == "no_flow"
    assert r.table_edges == []


def test_complex_window_flagged() -> None:
    sql = """create table t as select dept_id,
  row_number() over (partition by dept_id order by cnt desc) as rn
from (select dept_id, count(1) as cnt from wedw_ods.x group by dept_id) a"""
    r = parse_dp_step(sql)
    assert r.status == "ok"
    assert r.is_complex is True
    assert "window" in r.features


def test_parse_error_garbage_is_failed() -> None:
    r = parse_dp_step("this is not sql at all {{{")
    assert r.status == "failed"
    assert "parse_error" in r.features


def test_macro_pure_ddl_is_no_flow() -> None:
    """含 ${DATA_DATE} 宏的纯 DDL 建表（无数据流）→ no_flow，不堆 unparseable 单。

    调度宏展开后建表可解析，但无数据搬移——仍 no_flow 跳过。
    """
    sql = (
        "use wedw_ods;\ndrop table if exists wedw_ods.t_${DATA_DATE};\n"
        "create table wedw_ods.t_${DATA_DATE}\n"
        "    (id string COMMENT '主键'\n"
        "    ,amt double COMMENT '金额')\n"
    )
    r = parse_dp_step(sql)
    assert r.status == "no_flow"


def test_schedule_macro_with_dataflow_is_ok() -> None:
    """宏 + as select/insert（有真实数据搬移）→ 宏展开后自动解析为 ok。

    回归：dp 大量带 ${DATA_DATE} 的加工脚本此前 sqlglot 失败落 unparseable
    （实测 201 单中 195 含调度宏、92% 可借此自动解析），不再需要 LLM/人工。
    """
    sql = (
        "use wedw_ods;\ndrop table if exists wedw_ods.t_${DATA_DATE};\n"
        "create table wedw_ods.t_${DATA_DATE} as select * from wedw_ods.src_${DATA_DATE};\n"
    )
    r = parse_dp_step(sql)
    assert r.status == "ok"
    assert {e.source for e in r.table_edges} == {"wedw_ods.src_20260101"}
    assert {e.target for e in r.table_edges} == {"wedw_ods.t_20260101"}


def test_schedule_macro_date_offset_are_distinct() -> None:
    """日期宏 ±N 偏移展开为互异日期（防同名表碰撞）。"""
    sql = (
        "create table wedw_ods.t_${DATA_DATE} as "
        "select * from wedw_ods.src_${DATA_DATE} a "
        "left join wedw_ods.src_${DATA_DATE-1} b on a.id=b.id "
        "left join wedw_ods.src_${DATA_DATE-7} c on a.id=c.id"
    )
    r = parse_dp_step(sql)
    assert r.status == "ok"
    targets = {e.target for e in r.table_edges}
    sources = {e.source for e in r.table_edges}
    assert targets == {"wedw_ods.t_20260101"}
    assert sources == {
        "wedw_ods.src_20260101",
        "wedw_ods.src_20251231",
        "wedw_ods.src_20251225",
    }


def test_schedule_macro_unknown_and_tmp() -> None:
    """未知宏兜底字母占位、tmp_tabname 稳定展开、EXEC_TIME 稳定时间戳。"""
    sql = (
        "create table wedw_ods.t_${tmp_tabname} as select * "
        "from wedw_ods.src where dt='${DATA_DATE}' and hh='${HH}' and y=${YYYY}"
    )
    r = parse_dp_step(sql)
    assert r.status == "ok"
    assert {e.target for e in r.table_edges} == {"wedw_ods.t_tmp_dp_parse"}
    assert {e.source for e in r.table_edges} == {"wedw_ods.src"}


def test_macro_create_external_no_flow() -> None:
    """宏 + create external table（无 select/insert）→ no_flow。"""
    sql = "create external table if not exists wedw_dw.y_${D} (id string) stored as parquet;\n"
    r = parse_dp_step(sql)
    assert r.status == "no_flow"


def test_tmp_table_excluded_by_default() -> None:
    sql = "create table wedw_tmp.tmp_clean_1 as select * from wedw_ods.src"
    r = parse_dp_step(sql)
    assert r.status == "no_flow"  # 目标被排除后无可用流转
    assert r.table_edges == []


def test_custom_exclude_patterns() -> None:
    sql = "create table wedw_dwd.his_archive_2026 as select * from wedw_ods.src"
    r = parse_dp_step(sql, exclude_patterns=[r"^wedw_dwd\.his_", r"_archive"])
    assert r.status == "no_flow"


def test_detect_complexity_subquery_depth() -> None:
    sql = (
        "create table t as select * from (select * from (select * from (select * from s) a) b) c"
    )
    features = detect_complexity_features(sql, "hive")
    assert "subquery_depth" in features


def test_detect_complexity_cte_and_join() -> None:
    sql = """with a as (select 1 as id), b as (select 2 as id), c as (select 3 as id),
d as (select 4 as id)
select * from a join b on a.id=b.id join c on b.id=c.id join d on c.id=d.id join s on d.id=s.id
join t on s.id=t.id join u on t.id=u.id"""
    features = detect_complexity_features(sql, "hive")
    assert "cte_count" in features
    assert "join_count" in features


def test_simple_sql_no_complexity_features() -> None:
    sql = "create table wedw_dwd.t as select a, b from wedw_ods.s"
    assert detect_complexity_features(sql, "hive") == []


def test_default_rules_are_json_serializable() -> None:
    import json

    json.dumps(DEFAULT_COMPLEXITY_RULES)  # 不抛异常即视为可配置承载


def test_split_statements_filters_lone_semicolons() -> None:
    """_split_statements 不再返回纯分号/空元素（否则逐语句 parse_one 误判失败）。

    sqlparse.split 对 ``";\\n"`` join 的多语句偶发产生独立 ``";"`` 元素——
    此前导致可解析的数据流脚本被 _has_parse_error 误判 failed（dp 16 张
    unparseable 中 14 张根因）。
    """
    from app.services.lineage.parser import _split_statements

    # 模拟 _qualify_sql_text 的 ";\\n" join 产物（sqlparse 曾从中拆出 ; 元素）
    sql = (
        "drop table wedw_dw.a;\n"
        "create table if not exists wedw_dw.b (\n"
        "etl_time string comment '数据加工时间'\n"
        ") comment '南平医院情况'\n"
        "partitioned by (date_id string comment '数据日期')\n"
        "STORED AS orcfile;\n"
        "insert overwrite table wedw_dw.b partition(date_id='20260101')\n"
        "select 'x', 'y';\n"
    )
    stmts = _split_statements(sql)
    assert stmts, "应拆出语句"
    assert all(s and s.strip("; \t\r\n") for s in stmts), "不应含纯分号/空元素"
    # 全部语句可独立解析（不会被空分号误伤）
    import sqlglot

    for st in stmts:
        sqlglot.parse_one(st, dialect="hive")  # 不抛即通过


def test_semicolon_noise_no_longer_marks_failed() -> None:
    """多语句脚本含空分号噪音不再被误判 failed。

    直接构造 sqlparse 易拆出 ``;`` 的多行 DDL + insert 脚本，验证 parse_dp_step
    给出 no_flow/ok 而非 failed（此前 `;` 元素使 _has_parse_error 误判）。
    """
    sql = (
        "-- 注释行\n"
        "drop table wedw_dw.np_hosp_list_info_df_df;\n"
        "create table if not exists wedw_dw.np_hosp_list_info_df_df (\n"
        "etl_time string comment '数据加工时间',\n"
        "org_code string comment '医院id'\n"
        ") comment '南平医院情况'\n"
        "partitioned by (date_id string comment '数据日期')\n"
        "STORED AS orcfile;\n"
        "insert overwrite table wedw_dw.np_hosp_list_info_df_df partition(date_id='20260101')\n"
        "select from_unixtime(unix_timestamp(), 'yyyy-MM-dd HH:mm:ss'), 'xxx';\n"
    )
    r = parse_dp_step(sql, dialect="hive")
    # insert 是常量 select（无源表）→ 无数据流边 → no_flow（而非 failed 堆工作台）
    assert r.status == "no_flow", r.status


def test_reserved_column_word_lock_quoted_and_ok() -> None:
    """裸保留字列名（lock）反引号保护后可解析并出真实边。

    dp 真实脚本 `select ..., lock, ... from src`（精神科锁档字段）未加引号，
    sqlglot 把 lock 当 LOCK 语法关键字致整条 CTAS 失败 → 此前误判 failed。
    """
    sql = (
        "create table wedw_dwd.out_t as\n"
        "select id, lock, hospitalization, lastDischargeDate\n"
        "from wedw_ods.src_t"
    )
    r = parse_dp_step(sql, dialect="hive")
    assert r.status == "ok", r.status
    assert r.table_edges, "应提取到源→目标表级边"
    assert any(e.source == "wedw_ods.src_t" for e in r.table_edges)
    assert any(e.target == "wedw_dwd.out_t" for e in r.table_edges)


def test_reserved_quote_does_not_break_order_by() -> None:
    """保留字保护不破坏 order by/partition by 语法结构（替换失败自动回退）。"""
    from app.services.lineage.dp_sync_parser import _quote_reserved_column_words

    sql = "select id, lock from wedw_ods.s order by id limit 10"
    fixed = _quote_reserved_column_words(sql, "hive")
    # 只应包 lock（order 后跟 by 属语法，若被包则 parse 失败回退不采用）
    assert fixed == "select id, `lock` from wedw_ods.s order by id limit 10", fixed
    import sqlglot

    sqlglot.parse_one(fixed, dialect="hive")  # 不抛即通过


def test_quote_reserved_does_not_rewrite_string_literals() -> None:
    """T2：引号保护不连带改写字符串常量里的保留字（'lock' 不被包反引号）。

    dp 真实形态 `concat('lock','-x') as tag` / `where kind='lock'`——替换正则无
    字符串上下文感知，若不掩码会把字面量改写为 '`lock`'，污染派生列表达式与
    谓词语义（实验确证）。
    """
    from app.services.lineage.dp_sync_parser import _quote_reserved_column_words

    sql = (
        "create table wedw_dwd.out_t as\n"
        "select id, lock, concat('lock', '-x') as tag\n"
        "from wedw_ods.src_t\n"
        "where kind = 'lock'"
    )
    fixed = _quote_reserved_column_words(sql, "hive")
    # 裸列 lock 被包；字符串字面量 'lock' / '-x' 与谓词 'lock' 保持原样
    assert "`lock`" in fixed, fixed
    assert "concat('lock', '-x')" in fixed, fixed
    assert "kind = 'lock'" in fixed, fixed
    import sqlglot

    sqlglot.parse_one(fixed, dialect="hive")  # 不抛即通过


def test_detect_complexity_multi_statement_no_false_parse_error() -> None:
    """T3：多语句（';\\n' join）脚本逐句全可解析时不再误报 parse_error。

    此前整段 sqlglot.parse 对 join 形态插入 None 空语句 → parse_error 假阳性 →
    引号保护/宏展开修复的脚本仍每轮判复杂喂 LLM/建 diverged 单。
    """
    sql = (
        "use wedw_dwd;\n"
        "create table if not exists wedw_dwd.t as select id, `lock` from wedw_ods.s;\n"
        "insert overwrite table wedw_dwd.t partition(date_id='20260101')\n"
        "select id, `lock` from wedw_ods.s;\n"
    )
    features = detect_complexity_features(sql, "hive")
    assert "parse_error" not in features, features


def test_partial_parse_failure_leaves_parse_error_feature() -> None:
    """T4：多语句一条出边、另一条仍解析失败 → 显式补 parse_error 特征留痕。

    不再让失败语句血缘静默丢失（有边 ok 掩盖）——节点判复杂走 LLM confirm /
    LLM 关则建 diverged 单。
    """
    sql = (
        "create table wedw_dwd.out_t as\n"
        "select id, `lock` from wedw_ods.src_t;\n"
        # 第二条含方言/宏形态令 sqlglot 无法解析（保留字引号保护也救不回）
        "create table wedw_dwd.bad_t as select cast(a as weird_type_udf(x)) from wedw_ods.o;\n"
    )
    r = parse_dp_step(sql, dialect="hive")
    assert r.status == "ok", r.status
    assert r.table_edges, "应有第一条语句的流转边"
    assert "parse_error" in r.features, r.features
