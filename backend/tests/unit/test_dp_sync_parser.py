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
