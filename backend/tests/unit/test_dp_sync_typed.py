"""dp 调度非 SQL/跨方言节点类型解析器（dp_sync_typed）单元测试。

覆盖 plan/观测四类非 SQL 形态（DataX/Shell/上报配置/接口同步）的真实样本
（2026-09 连 dp 元库观测 script_info），以及 SQL 类方言分发与边界。
"""

from __future__ import annotations

from app.services.lineage.dp_sync_typed import (
    _extract_shell_sqls,
    _hdfs_plugin_table,
    parse_dp_step_typed,
)

#: DataX mysqlreader(table) → doriswriter(table)，两侧均带/可推库。
_DATAX_MYSQL_TO_DORIS = """{
  "job": {"content": [{
    "reader": {"name": "mysqlreader", "parameter": {
      "connection": [{
        "jdbcUrl": "jdbc:mysql://192.168.94.38:3306/dp_dev",
        "table": ["dp_dev.t_test"]
      }]
    }},
    "writer": {"name": "doriswriter", "parameter": {
      "connection": [{
        "jdbcUrl": "jdbc:mysql://192.168.1.144:9030/demo",
        "selectedDatabase": "demo",
        "table": ["ods_t_test"]
      }]
    }}
  }]}
}"""

#: DataX hdfsreader(path) → mysqlwriter(table)：hdfs 端按 warehouse 两级库反推。
_DATAX_HDFS_TO_MYSQL = """{
  "job": {"content": [{
    "reader": {"name": "hdfsreader", "parameter": {
      "path": "/data/hive/warehouse/wedw/dw/wy_vip_company_health_assessment_df"
    }},
    "writer": {"name": "mysqlwriter", "parameter": {
      "connection": [{
        "jdbcUrl": "jdbc:mysql://192.168.94.38:3306/st",
        "table": ["st.wy_vip_company_health_assessment"]
      }]
    }}
  }]}
}"""

#: DataX mysqlreader(querySql) → hdfswriter(path)。
_DATAX_QUERYSQL_TO_HDFS = """{
  "job": {"content": [{
    "reader": {"name": "mysqlreader", "parameter": {
      "connection": [{
        "jdbcUrl": "jdbc:mysql://192.168.1.144:9030/ssb10",
        "querySql": ["select c_custkey from customer"]
      }]
    }},
    "writer": {"name": "hdfswriter", "parameter": {
      "path": "/data/hive/warehouse/wedw/ods/ssb10_customer_ful_d"
    }}
  }]}
}"""

#: 接口同步（type=15）mysql → hive 上传真实样本。
_UPLOAD_UP_REAL = (
    '{"hiveDbName":"wedw_dw","isCamel":1,"upNumber":1,'
    '"mysqlDbName":"bhc_to_pingjiang","upMode":0,"upType":1,'
    '"hiveTableName":"jwy_zh_pingjiang_visit_hypertension_df",'
    '"url":"https://bhc-open.guahao-test.com/phc/health-record/hypertension/save",'
    '"isSerial":1,"mysqlPkId":"source_unique_key","isError":1,'
    '"mysqlTableName":"visit_hypertension","taskName":"sync.jwy_zh_visit_hypertension_df"}'
)


class TestDataX:
    def test_mysql_to_doris_table_edges(self) -> None:
        r = parse_dp_step_typed(_DATAX_MYSQL_TO_DORIS, step_type=2)
        assert r.status == "ok"
        assert {(e.source, e.target) for e in r.table_edges} == {
            ("dp_dev.t_test", "demo.ods_t_test")
        }

    def test_hdfs_to_mysql_reverse_warehouse_db(self) -> None:
        """hdfs path 两级库目录 wedw/dw → wedw_dw（健康仓惯例）。"""
        r = parse_dp_step_typed(_DATAX_HDFS_TO_MYSQL, step_type=2)
        assert r.status == "ok"
        assert {(e.source, e.target) for e in r.table_edges} == {
            (
                "wedw_dw.wy_vip_company_health_assessment_df",
                "st.wy_vip_company_health_assessment",
            )
        }

    def test_query_sql_reader_with_jdbc_db(self) -> None:
        """querySql 纯 SELECT 读表 + jdbc 库拼接。"""
        r = parse_dp_step_typed(_DATAX_QUERYSQL_TO_HDFS, step_type=2)
        assert r.status == "ok"
        assert {(e.source, e.target) for e in r.table_edges} == {
            ("ssb10.customer", "wedw_ods.ssb10_customer_ful_d")
        }

    def test_invalid_json_is_failed(self) -> None:
        r = parse_dp_step_typed("{not json", step_type=2)
        assert r.status == "failed"
        assert "JSON" in (r.error or "")

    def test_empty_or_no_content_is_no_flow(self) -> None:
        assert parse_dp_step_typed("", step_type=2).status == "no_flow"
        assert (
            parse_dp_step_typed('{"job": {}}', step_type=2).status == "no_flow"
        )

    def test_hdfs_db_plugin_without_connection_is_no_flow(self) -> None:
        """两侧插件在但表均抽不出（如 hdfs→hdfs 文件搬移）→ no_flow 不堆单。"""
        script = (
            '{"job":{"content":[{"reader":{"name":"hdfsreader","parameter":{'
            '"path":"/data/tmp/x"}},'
            '"writer":{"name":"hdfswriter","parameter":{"path":"/data/tmp/y"}}}]}}'
        )
        r = parse_dp_step_typed(script, step_type=2)
        assert r.status == "no_flow"


class TestShell:
    def test_wait_command_only_is_no_flow(self) -> None:
        """真实样本：sleep/注释等待节点 → no_flow（无内嵌 SQL）。"""
        r = parse_dp_step_typed("# 等待6分钟\nsleep 360", step_type=3)
        assert r.status == "no_flow"

    def test_embedded_hive_sql_is_ok(self) -> None:
        script = (
            'hive -e "insert overwrite table wedw_dwd.t select * '
            'from wedw_ods.s where dt=\'20260101\'"'
        )
        r = parse_dp_step_typed(script, step_type=3)
        assert r.status == "ok"
        assert {(e.source, e.target) for e in r.table_edges} == {
            ("wedw_ods.s", "wedw_dwd.t")
        }

    def test_extract_shell_sqls_multi(self) -> None:
        sqls = _extract_shell_sqls(
            'beeline -u jdbc:hive2://h:10000 -e "select 1"\n'
            'mysql -h x -e \'select 2\''
        )
        assert len(sqls) == 2


class TestReportConfig:
    def test_numeric_config_id_is_no_flow(self) -> None:
        """真实样本：纯数字配置 ID（189-228）→ no_flow，不编造表。"""
        for script in ("228", "189", " 220 "):
            r = parse_dp_step_typed(script, step_type=9)
            assert r.status == "no_flow"
            assert "配置 ID" in (r.error or "")


class TestUploadConfig:
    def test_up_type_mysql_to_hive(self) -> None:
        """真实样本：upType=1 上传 → mysql 业务表 → hive 表。"""
        r = parse_dp_step_typed(_UPLOAD_UP_REAL, step_type=15)
        assert r.status == "ok"
        assert {(e.source, e.target) for e in r.table_edges} == {
            (
                "bhc_to_pingjiang.visit_hypertension",
                "wedw_dw.jwy_zh_pingjiang_visit_hypertension_df",
            )
        }

    def test_down_type_hive_to_mysql(self) -> None:
        c = (
            '{"hiveDbName":"wedw_dw","mysqlDbName":"bhc_to_pingjiang",'
            '"downType":1,"hiveTableName":"jwy_zh_pingjiang_visit_hypertension_df",'
            '"mysqlTableName":"visit_hypertension"}'
        )
        r = parse_dp_step_typed(c, step_type=15)
        assert r.status == "ok"
        assert {(e.source, e.target) for e in r.table_edges} == {
            (
                "wedw_dw.jwy_zh_pingjiang_visit_hypertension_df",
                "bhc_to_pingjiang.visit_hypertension",
            )
        }

    def test_invalid_json_is_failed(self) -> None:
        assert parse_dp_step_typed("{bad", step_type=15).status == "failed"


class TestSqlDialectDispatch:
    def test_type4_mysql_dml_no_flow(self) -> None:
        """type=4 直连库 DML（Airflow 元库维护 update）mysql 方言 → 无源自然 no_flow。"""
        r = parse_dp_step_typed(
            "update dag_run set state='success' where dag_id='x'", step_type=4
        )
        assert r.status == "no_flow"

    def test_type6_oracle_plsql_no_flow(self) -> None:
        """type=6 Oracle PL/SQL 系统表操作 → 无业务流转 no_flow。"""
        r = parse_dp_step_typed(
            "declare num number; begin select count(1) into num "
            "from user_tables where table_name='X'; end;",
            step_type=6,
        )
        assert r.status in ("no_flow", "failed")

    def test_type7_sql_ok(self) -> None:
        r = parse_dp_step_typed(
            "create table wedw_dwd.t as select * from wedw_ods.s", step_type=7
        )
        assert r.status == "ok"
        assert {(e.source, e.target) for e in r.table_edges} == {
            ("wedw_ods.s", "wedw_dwd.t")
        }

    def test_unknown_type_falls_back_to_sql(self) -> None:
        r = parse_dp_step_typed("select 1", step_type=None)
        assert r.status == "no_flow"


class TestHdfsPathHelper:
    def test_two_level_db(self) -> None:
        plugin = {"name": "hdfsreader", "parameter": {
            "path": "/data/hive/warehouse/wedw/dw/xxx_df"}}
        assert _hdfs_plugin_table(plugin) == "wedw_dw.xxx_df"

    def test_db_dir_style(self) -> None:
        plugin = {"name": "hdfswriter", "parameter": {
            "path": "/user/hive/warehouse/wedw_ods.db/yyy"}}
        assert _hdfs_plugin_table(plugin) == "wedw_ods.yyy"

    def test_partition_segments_stripped(self) -> None:
        plugin = {"name": "hdfsreader", "parameter": {
            "path": "/data/hive/warehouse/wedw/dw/zzz/dt=20260101"}}
        assert _hdfs_plugin_table(plugin) == "wedw_dw.zzz"

    def test_unmappable_path_is_none(self) -> None:
        plugin = {"name": "hdfsreader", "parameter": {"path": "/data/tmp/x"}}
        assert _hdfs_plugin_table(plugin) is None
