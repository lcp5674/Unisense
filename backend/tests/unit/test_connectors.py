"""连接器单元测试（对齐 US1 / FR-001）。

覆盖：
1. 每种连接器的 collect() 方法 mock 测试（连接+查询+解析+异常处理）
2. MySQL InformationSchemaCollector 单表容错
3. PostgresCollector 查询+解析
4. HiveCollector pyhive 直连输出解析
5. SparkCollector 复用 pyhive 直连解析（Spark Thrift Server / HiveServer2 协议）
6. DorisCollector/StarRocksCollector MySQL 兼容性
7. ClickHouseCollector HTTP API
8. KafkaCollector Topic+Schema Registry
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import ExternalDependencyError
from app.services.collector.connectors.clickhouse import ClickHouseCollector
from app.services.collector.connectors.hive import HiveCollector
from app.services.collector.connectors.kafka import KafkaCollector
from app.services.collector.connectors.mysql import InformationSchemaCollector
from app.services.collector.connectors.postgres import PostgresCollector
from app.services.collector.connectors.spark import SparkCollector
from app.services.collector.spi import CollectResult


class _FakeConnector:
    """测试用假连接器（适配批量列查询：一次返回 {table_name, column_name} 多行）。"""

    def __init__(self, tables: list[str], columns: dict[str, list[dict]]) -> None:
        self._tables = tables
        self._columns = columns

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        if "information_schema.tables" in sql:
            if "table_schema IN" in sql:
                # 批量枚举（list_tables 单条 IN 查询）：按传入 schemas 返回带 schema 的行
                schemas = (params or {}).get("schemas") or ("db1",)
                rows = []
                for s in schemas:
                    for t in self._tables:
                        rows.append({"table_schema": s, "table_name": t})
                return rows
            return [{"table_name": t} for t in self._tables]
        if "pg_catalog.pg_attribute" in sql:
            # Postgres 注释查询（pg_description 层）→ 按 (table, column) 返回注释
            rows = []
            for tbl, cols in self._columns.items():
                for c in cols:
                    comment = c.get("comment", "")
                    if comment:
                        rows.append(
                            {
                                "table_name": tbl,
                                "column_name": c.get("column_name"),
                                "column_comment": comment,
                            }
                        )
            return rows
        if "information_schema.columns" in sql:
            if "data_type" in sql:
                # Postgres 批量列：SELECT table_name, column_name, data_type ...（P1-2 批量）
                rows: list[dict] = []
                for tbl, cols in self._columns.items():
                    for c in cols:
                        rows.append(
                            {
                                "table_name": tbl,
                                "column_name": c.get("column_name"),
                                "data_type": c.get("data_type"),
                                "column_default": c.get("column_default"),
                                "ordinal_position": c.get("ordinal_position", 1),
                            }
                        )
                return rows
            # MySQL 批量列：SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE（一次全库）
            rows = []
            for tbl, cols in self._columns.items():
                for c in cols:
                    rows.append(
                        {
                            "table_name": tbl,
                            "column_name": c.get("column_name"),
                            "data_type": c.get("data_type"),
                            "is_nullable": c.get("is_nullable", "YES"),
                            "column_comment": c.get("comment", ""),
                            "column_default": c.get("column_default"),
                        }
                    )
            return rows
        return []

    async def dispose(self) -> None:
        pass


class _BoomConnector:
    """模拟连接失败的连接器。"""

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        raise RuntimeError("connection refused")

    async def dispose(self) -> None:
        pass


class _PartialFailConnector:
    """批量列查询部分失败的连接器（模拟第 fail_at 表列缺失）。"""

    def __init__(self, tables: list[str], fail_at: str) -> None:
        self._tables = tables
        self._fail_at = fail_at

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        if "information_schema.tables" in sql:
            return [{"table_name": t} for t in self._tables]
        if "information_schema.columns" in sql:
            rows: list[dict] = []
            for tbl in self._tables:
                if tbl == self._fail_at:
                    continue  # 该表列缺失
                rows.append({"table_name": tbl, "column_name": f"{tbl}_col1"})
            return rows
        return []

    async def dispose(self) -> None:
        pass


# ---------- MySQL InformationSchemaCollector ----------


async def test_mysql_collector_builds_specs():
    """MySQL 采集器正常采集返回 specs。"""
    conn = _FakeConnector(
        ["users", "orders"],
        {"users": [{"column_name": "user_name"}], "orders": [{"column_name": "order_id"}]},
    )
    # FR-030 方案 A：连接库 database 为纯连接凭据；采集范围由 service 注入的
    # 多目标库（set_databases）决定，entity_name 统一为 schema.table
    collector = InformationSchemaCollector(conn, database="db1")
    collector.set_databases(["db1"])  # 模拟 service 层从 DataSource.databases 注入
    result = await collector.collect(MagicMock(source_id="s1", domain="db1"))

    assert isinstance(result, CollectResult)
    assert len(result.specs) == 2
    assert result.specs[0].entity_name == "db1.users"
    # P1-1: schema_json.columns 为 {name,type,nullable} 字典列表
    cols = result.specs[0].schema_json["columns"]
    assert cols[0]["name"] == "user_name"
    assert "type" in cols[0]
    assert "nullable" in cols[0]
    assert len(result.failed_specs) == 0


async def test_mysql_collector_external_failure_raises():
    """MySQL 采集器源库连接失败时抛出 ExternalDependencyError。"""
    collector = InformationSchemaCollector(_BoomConnector())
    with pytest.raises(ExternalDependencyError):
        await collector.collect(MagicMock(source_id="s1", domain="db1"))


async def test_mysql_collector_single_table_failure_skips():
    """批量列查询下某表列缺失 → 该表产出空列，不阻断整批采集（FR-004 容错）。"""
    conn = _PartialFailConnector(["table1", "table2", "table3"], fail_at="table2")
    collector = InformationSchemaCollector(conn, database="db1")
    collector.set_databases(["db1"])  # 模拟 service 层注入多目标库（方案 A）
    result = await collector.collect(MagicMock(source_id="s1", domain="db1"))

    assert len(result.specs) == 3  # 全部表仍产出
    assert len(result.failed_specs) == 0  # 批量查询部分缺失不视为失败
    by_name = {s.entity_name: s for s in result.specs}
    assert by_name["db1.table2"].schema_json["columns"] == []  # fail_at 表列缺失 → 空列
    # P1-1: 成功表列信息为 {name,type,nullable,comment,default} 字典
    assert by_name["db1.table1"].schema_json["columns"] == [
        {
            "name": "table1_col1",
            "type": "unknown",
            "nullable": True,
            "comment": "",
            "default": None,
        }
    ]


async def test_mysql_list_tables_batch_groups_by_db():
    """list_tables 单条 IN 批量查询并按库分组（逐库串行 → 1 次往返）。"""
    conn = _FakeConnector(["users", "orders"], {})
    collector = InformationSchemaCollector(conn, database="db1")
    result = await collector.list_tables(["db1", "db2"])
    assert result == {
        "db1": ["users", "orders"],
        "db2": ["users", "orders"],
    }


async def test_mysql_list_tables_empty_schemas_returns_empty():
    """list_tables 无可用库时返回空字典（不发查询）。"""
    conn = _FakeConnector(["users"], {})
    collector = InformationSchemaCollector(conn, database="db1")
    with patch.object(collector, "_list_schemas", AsyncMock(return_value=[])):
        assert await collector.list_tables() == {}


# ---------- PostgresCollector ----------


class _SamplingPgConnector(_FakeConnector):
    """记录采样 SQL 并返回样本行的 Postgres 假连接器（可模拟采样失败）。"""

    def __init__(
        self,
        tables: list[str],
        columns: dict[str, list[dict]],
        sample_rows: list[dict] | None = None,
        *,
        fail_sampling: bool = False,
    ) -> None:
        super().__init__(tables, columns)
        self._sample_rows = sample_rows if sample_rows is not None else []
        self._fail_sampling = fail_sampling
        self.sample_sql: list[str] = []

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        if " LIMIT " in sql and "SELECT" in sql:
            self.sample_sql.append(sql)
            if self._fail_sampling:
                raise RuntimeError("sampling failed")
            return list(self._sample_rows)
        return await super().query(sql, params)


async def test_postgres_collector_builds_specs():
    """PostgreSQL 采集器正常采集返回 specs（含注释/默认值——P0 列元数据补全）。"""
    conn = _FakeConnector(
        ["users"],
        {
            "users": [
                {
                    "column_name": "user_name",
                    "data_type": "varchar",
                    "column_default": "NULL",
                    "comment": "用户姓名",
                }
            ]
        },
    )
    collector = PostgresCollector(conn)
    result = await collector.collect(MagicMock(source_id="s1", domain="public"))

    assert isinstance(result, CollectResult)
    assert len(result.specs) == 1
    assert result.specs[0].entity_name == "users"
    # PostgresCollector 返回详细列信息
    cols = result.specs[0].schema_json["columns"]
    assert len(cols) == 1
    assert cols[0]["name"] == "user_name"
    assert cols[0]["type"] == "varchar"
    # P0 修复：pg_description 注释被采集并拼入 schema_json（供 PII 分类）
    assert cols[0]["comment"] == "用户姓名"


async def test_postgres_collector_samples_columns_with_pg_dialect():
    """Postgres 采样：双引号标识符方言 + 样本打码写入 columns[].sample。"""
    conn = _SamplingPgConnector(
        ["users"],
        {
            "users": [
                {"column_name": "phone", "data_type": "varchar"},
                {"column_name": "user_name", "data_type": "varchar"},
            ]
        },
        sample_rows=[{"phone": "13812345678", "user_name": None}],
    )
    collector = PostgresCollector(conn)
    collector.set_sampling(5)
    result = await collector.collect(MagicMock(source_id="s1", domain="public"))

    cols = result.specs[0].schema_json["columns"]
    # PG 方言：标识符用双引号；行对齐查询一次取全列、不加非空过滤
    # 第 1 条 = 行对齐全列查询；第 2 条 = user_name 稀疏列单列补采（保 PII 召回）
    assert len(conn.sample_sql) == 2
    assert 'SELECT "phone","user_name" FROM "public"."users"' in conn.sample_sql[0]
    assert "LIMIT 5" in conn.sample_sql[0]
    assert '"user_name" IS NOT NULL' in conn.sample_sql[1]
    # 行视图：一行 = 一条真实记录（user_name 为 NULL → 空串占位，列不错位）
    assert result.specs[0].schema_json["sample_rows"] == [
        {"phone": "138****5678", "user_name": ""}
    ]
    # 列式 sample：手机打码；user_name 为 NULL → 不写 sample
    assert cols[0]["sample"] == ["138****5678"]
    assert "sample" not in cols[1]


async def test_postgres_collector_keeps_multiple_distinct_samples():
    """多值采样：同一列保留多条不同样本（去重、按 sample_rows 截断）。"""
    conn = _SamplingPgConnector(
        ["users"],
        {
            "users": [
                {"column_name": "phone", "data_type": "varchar"},
                {"column_name": "user_name", "data_type": "varchar"},
            ]
        },
        sample_rows=[
            {"phone": "13812345678", "user_name": "张"},
            {"phone": "13987654321", "user_name": "张"},
        ],
    )
    collector = PostgresCollector(conn)
    collector.set_sampling(5)
    result = await collector.collect(MagicMock(source_id="s1", domain="public"))

    cols = result.specs[0].schema_json["columns"]
    # 行视图保留两条真实记录（user_name 两行同值 → 行视图按行保留、不去重）
    assert result.specs[0].schema_json["sample_rows"] == [
        {"phone": "138****5678", "user_name": "张"},
        {"phone": "139****4321", "user_name": "张"},
    ]
    # 列式 sample：phone 两行不同值 → 2 条打码样本；user_name 两行同值 → 去重 1 条
    assert cols[0]["sample"] == ["138****5678", "139****4321"]
    assert cols[1]["sample"] == ["张"]


async def test_postgres_collector_truncates_samples_to_limit():
    """采样行数上限：sample_rows 配置限制每列最多保留的样本条数。"""
    conn = _SamplingPgConnector(
        ["users"],
        {"users": [{"column_name": "phone", "data_type": "varchar"}]},
        sample_rows=[
            {"phone": f"1381234567{i}"} for i in range(8)  # 8 个不同号码
        ],
    )
    collector = PostgresCollector(conn)
    collector.set_sampling(3)  # 每列最多 3 条
    result = await collector.collect(MagicMock(source_id="s1", domain="public"))

    cols = result.specs[0].schema_json["columns"]
    # 行视图同样按采样行数截断（驱动未服从 LIMIT 时由基类防御截断）
    assert len(result.specs[0].schema_json["sample_rows"]) == 3
    assert len(cols[0]["sample"]) == 3


async def test_postgres_collector_skips_sampling_when_disabled():
    """未开启采样（sample_rows=0）→ 不发采样 SQL、字段无 sample 键。"""
    conn = _SamplingPgConnector(
        ["users"], {"users": [{"column_name": "phone", "data_type": "varchar"}]}
    )
    collector = PostgresCollector(conn)
    collector.set_sampling(0)
    result = await collector.collect(MagicMock(source_id="s1", domain="public"))

    assert conn.sample_sql == []
    assert "sample" not in result.specs[0].schema_json["columns"][0]


async def test_postgres_collector_tolerates_sampling_failure():
    """采样查询失败 → 仅告警，不拖垮采集（FR-004 容错）。"""
    conn = _SamplingPgConnector(
        ["users"],
        {"users": [{"column_name": "phone", "data_type": "varchar"}]},
        fail_sampling=True,
    )
    collector = PostgresCollector(conn)
    collector.set_sampling(5)
    result = await collector.collect(MagicMock(source_id="s1", domain="public"))

    assert len(result.specs) == 1
    assert "sample" not in result.specs[0].schema_json["columns"][0]


async def test_postgres_collect_entity_samples_columns():
    """单表刷新路径（collect_entity）同样执行采样。"""
    conn = _SamplingPgConnector(
        ["users"],
        {"users": [{"column_name": "id_card", "data_type": "varchar"}]},
        sample_rows=[{"id_card": "110101199003071234"}],
    )
    collector = PostgresCollector(conn, schema="public")
    collector.set_sampling(5)
    spec = await collector.collect_entity(MagicMock(source_id="s1"), "users")

    assert spec is not None
    assert len(conn.sample_sql) == 1
    assert spec.schema_json["columns"][0]["sample"] == ["110101********1234"]
    assert spec.schema_json["sample_rows"] == [{"id_card": "110101********1234"}]


async def test_postgres_list_databases_returns_all_schemas():
    """list_databases 枚举全部非系统 schema（供创建数据源/维度弹窗选库）。"""
    conn = _FakeConnector([], {})
    collector = PostgresCollector(conn)

    async def fake_query(sql: str, params: dict | None = None) -> list[dict]:
        assert "information_schema.schemata" in sql
        return [
            {"schema_name": "public"},
            {"schema_name": "ods"},
            {"schema_name": "pg_catalog"},
        ]

    conn.query = fake_query  # type: ignore[method-assign]
    assert await collector.list_databases() == ["public", "ods"]


async def test_postgres_list_tables_batch_groups_by_schema():
    """list_tables 单条 IN 批量查询并按 schema 分组（复用连接池、无逐库往返）。"""
    conn = _FakeConnector(["users", "orders"], {})
    collector = PostgresCollector(conn, schema="public")
    result = await collector.list_tables(["public", "ods"])
    assert result == {
        "public": ["users", "orders"],
        "ods": ["users", "orders"],
    }


async def test_postgres_list_tables_falls_back_to_configured_schema():
    """list_tables 未传库时回退连接配置的 schema（单 schema 模式）。"""
    conn = _FakeConnector(["users"], {})
    collector = PostgresCollector(conn, schema="public")
    result = await collector.list_tables()
    assert result == {"public": ["users"]}


# ---------- HiveCollector ----------


async def test_hive_collector_parses_pyhive_rows():
    """Hive 采集器解析 pyhive 查询行（含注释——P0 列元数据补全）。

    原 beeline 版 mock 的即纯数据行（无 table2 表头），与 pyhive fetchall 输出一致，
    故 mock 数据保持不变；此处验证 collect 对 pyhive 行的解析与注释兜底。
    """
    collector = HiveCollector(host="hive-host", database="test_db")

    # Mock _execute 方法
    async def mock_execute(sql: str, conn=None) -> list[list[str]]:
        if "SHOW TABLES" in sql:
            return [["orders"], ["customers"]]
        if "DESCRIBE" in sql:
            if "orders" in sql:
                # DESCRIBE 第三列为注释（可为空）
                return [
                    ["order_id", "bigint", "订单ID"],
                    ["amount", "decimal(10,2)", ""],
                ]
            if "customers" in sql:
                return [["customer_id", "int"], ["name", "string"]]
        return []

    collector._execute = mock_execute  # type: ignore[assignment]
    # 连接复用：collect 每 schema 建一次连接，测试 mock 掉避免真实网络
    collector._connect_managed = AsyncMock(return_value=object())  # type: ignore[assignment]
    result = await collector.collect(MagicMock(source_id="hive1", domain="test_db"))

    assert isinstance(result, CollectResult)
    assert len(result.specs) == 2
    assert result.specs[0].entity_name == "orders"
    cols = result.specs[0].schema_json["columns"]
    assert cols[0]["name"] == "order_id"
    # P0 修复：DESCRIBE 第三列注释被采集
    assert cols[0]["comment"] == "订单ID"
    assert cols[1]["comment"] == ""
    # 仅两列（无注释）向下兼容
    assert result.specs[1].schema_json["columns"][0]["comment"] == ""


async def test_hive_collector_enumerates_all_dbs_when_no_database():
    """未配置连接库时枚举全部库采集（修复默认只扫 default 空库 → 注册 0）。

    前端语义「连接库仅作连接凭据；目标库留空=全部库」：HiveCollector 未显式
    传 database 时 ``_database`` 为 None，collect 走 SHOW DATABASES 枚举全部库，
    entity_name 带库前缀（``schema.table``），不再固定扫 default 单库。
    """
    collector = HiveCollector(host="hive-host")

    async def mock_execute(sql: str, conn=None) -> list[list[str]]:
        if "SHOW DATABASES" in sql:
            return [["ods"], ["dwd"]]
        if "SHOW TABLES" in sql:
            return [["orders"]] if "ods" in sql else [["fee_di"]]
        if "DESCRIBE" in sql:
            return [["order_id", "bigint"]]
        return []

    collector._execute = mock_execute  # type: ignore[assignment]
    collector._connect_managed = AsyncMock(return_value=object())  # type: ignore[assignment]
    result = await collector.collect(MagicMock(source_id="hive1", domain="test_db"))

    assert isinstance(result, CollectResult)
    assert len(result.specs) == 2
    # 枚举全部库 → 实体名带库前缀（跨库不冲突）
    assert result.specs[0].entity_name == "ods.orders"
    assert result.specs[1].entity_name == "dwd.fee_di"


async def test_hive_collector_records_failed_schema_listing():
    """SHOW TABLES IN 失败记入 failed_specs（避免「全部失败却静默 0 表」难排查）。"""
    collector = HiveCollector(host="hive-host")

    async def mock_execute(sql: str, conn=None) -> list[list[str]]:
        if "SHOW DATABASES" in sql:
            return [["ods"], ["locked"]]
        if "SHOW TABLES" in sql:
            raise ExternalDependencyError("Permission denied: locked")
        return []

    collector._execute = mock_execute  # type: ignore[assignment]
    collector._connect_managed = AsyncMock(return_value=object())  # type: ignore[assignment]
    result = await collector.collect(MagicMock(source_id="hive1", domain="test_db"))

    assert len(result.specs) == 0
    assert len(result.failed_specs) == 2
    assert result.failed_specs[0].entity_name == "ods"
    assert "Permission denied" in result.failed_specs[0].error


async def test_hive_and_spark_factory_database_default_none():
    """工厂未填 database 时传 None（不再默认 'default' 空库），会话库用 default 兜底。

    回归：此前 ``cfg.get("database", "default")`` 导致连接库恒为 'default'，
    collect 只扫 default 单库 → 用户未配置任何库时「注册 0」。
    """
    from app.services.collector.connectors.hive import create_hive_collector
    from app.services.collector.connectors.spark import create_spark_collector

    hive = create_hive_collector({"host": "h", "port": 10000})
    assert hive._database is None
    # 无密码 → 认证 NONE（pyhive 的 password 仅限 LDAP/CUSTOM 模式）
    assert hive._auth == "NONE"

    spark = create_spark_collector({"host": "s", "port": 10000})
    assert spark._database is None
    assert spark._auth == "NONE"

    # 显式填连接库仍生效（单库 + 裸表名兼容既有行为）
    hive_explicit = create_hive_collector({"host": "h", "database": "dwd"})
    assert hive_explicit._database == "dwd"

    # 有密码 → 默认 LDAP（HiveServer2 标准密码认证）；显式 auth 可覆盖
    assert create_hive_collector({"host": "h", "password": "p"})._auth == "LDAP"
    assert (
        create_hive_collector({"host": "h", "password": "p", "auth": "CUSTOM"})._auth
        == "CUSTOM"
    )


async def test_hive_list_databases_returns_all_dbs():
    """list_databases 枚举全部库（过滤空行），供创建数据源选择目标库。"""
    collector = HiveCollector(host="hive-host")

    async def mock_execute(sql: str, conn=None) -> list[list[str]]:
        assert "SHOW DATABASES" in sql
        return [["ods"], ["dwd"], [], [""]]

    collector._execute = mock_execute  # type: ignore[assignment]
    assert await collector.list_databases() == ["ods", "dwd"]


async def test_hive_list_tables_groups_by_db():
    """list_tables 逐库 SHOW TABLES 并按库分组；非法库名跳过不拖垮整批。"""
    collector = HiveCollector(host="hive-host")
    seen_sql: list[str] = []

    async def mock_execute(sql: str, conn=None) -> list[list[str]]:
        seen_sql.append(sql)
        if "ods" in sql:
            return [["orders"], ["customers"]]
        if "dwd" in sql:
            return [["fee_di"]]
        return []

    collector._execute = mock_execute  # type: ignore[assignment]
    collector._connect_managed = AsyncMock(return_value=object())  # type: ignore[assignment]
    result = await collector.list_tables(["ods", "bad.name", "dwd"])
    assert result == {"ods": ["orders", "customers"], "dwd": ["fee_di"]}
    # 非法库名跳过，未对其发出 SHOW TABLES
    assert not any("bad.name" in s for s in seen_sql)


async def test_hive_list_tables_reuses_single_conn():
    """list_tables 复用单连接枚举全部库（每库一次握手是级联选表慢的主因）。"""
    collector = HiveCollector(host="hive-host")

    class _FakeConn:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    conn = _FakeConn()
    collector._connect_managed = AsyncMock(return_value=conn)  # type: ignore[assignment]
    collector._execute = AsyncMock(  # type: ignore[assignment]
        side_effect=lambda sql, conn=None: [["t1"]] if "db1" in sql else [["t2"]],
    )
    result = await collector.list_tables(["db1", "db2"])
    assert result == {"db1": ["t1"], "db2": ["t2"]}
    # 单连接复用：仅建立一次连接（修复前每库一次 _connect_managed），且最终关闭
    collector._connect_managed.assert_awaited_once()
    assert conn.closed
    assert collector._execute.await_count == 2


async def test_hive_list_tables_overall_timeout():
    """list_tables 整体超时兜底：库多/HS2 慢时抛 ExternalDependencyError 而非无限空等。"""
    collector = HiveCollector(host="hive-host", query_timeout=1)

    async def _slow_execute(sql: str, conn=None) -> list[list[str]]:
        await asyncio.sleep(60)

    collector._execute = _slow_execute  # type: ignore[assignment]
    collector._connect_managed = AsyncMock(return_value=object())  # type: ignore[assignment]
    with pytest.raises(ExternalDependencyError, match="枚举表超时"):
        await collector.list_tables(["db1"])


async def test_hive_execute_reuses_conn_without_close():
    """连接复用：复用路径不关闭连接（调用方管理），单次路径自建自关。

    回归：此前 _execute 每次自建连接且 _query 内部关闭——每张表一次
    TCP+认证握手导致 wedata_tmp 上千张临时表全量采集耗时数小时。
    """

    class _FakeConn:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    collector = HiveCollector(host="hive-host")
    conn = _FakeConn()
    collector._connect_managed = AsyncMock(return_value=conn)  # type: ignore[assignment]
    collector._query = MagicMock(return_value=[["ok"]])  # type: ignore[assignment]

    # 复用路径：传入 conn 时不关闭连接
    await collector._execute("SELECT 1", conn=conn)
    assert not conn.closed
    collector._query.assert_called_once_with(conn, "SELECT 1")

    # 单次路径：自建连接并在查询后关闭
    collector._query.reset_mock()
    await collector._execute("SELECT 2")
    assert conn.closed
    collector._query.assert_called_once_with(conn, "SELECT 2")


# ---------- SparkCollector ----------


async def test_spark_collector_parses_pyhive_rows():
    """Spark 采集器经 Spark Thrift Server（HiveServer2 协议）解析 pyhive 行。"""
    collector = SparkCollector(host="spark-host", database="test_db")

    # Mock _execute 方法（与 Hive 相同的 SHOW TABLES / DESCRIBE 协议）
    async def mock_execute(sql: str, conn=None) -> list[list[str]]:
        if "SHOW TABLES" in sql:
            return [["orders"], ["customers"]]
        if "DESCRIBE" in sql:
            if "orders" in sql:
                return [["order_id", "bigint"], ["amount", "decimal(10,2)"]]
            if "customers" in sql:
                return [["customer_id", "int"], ["name", "string"]]
        return []

    collector._execute = mock_execute  # type: ignore[assignment]
    collector._connect_managed = AsyncMock(return_value=object())  # type: ignore[assignment]
    result = await collector.collect(MagicMock(source_id="spark1", domain="test_db"))

    assert isinstance(result, CollectResult)
    assert len(result.specs) == 2
    assert result.specs[0].entity_name == "orders"
    assert result.specs[0].schema_json["columns"][1]["name"] == "amount"


async def test_spark_collector_normalizes_placeholder_comment():
    """Spark Thrift 对无注释列返回占位串 "from deserializer"——归一化为空串。

    否则假注释会污染 schema_json，批量字段推断误判「已有注释」而全部跳过
    （描述缺失面板显示缺失、推断却全跳过）。
    """
    collector = SparkCollector(host="spark-host", database="test_db")

    async def mock_execute(sql: str, conn=None) -> list[list[str]]:
        if "SHOW TABLES" in sql:
            return [["orders"]]
        if "DESCRIBE" in sql:
            return [
                ["order_id", "bigint", "from deserializer"],
                ["amount", "decimal(10,2)", "订单金额"],
                ["remark", "string", ""],
            ]
        return []

    collector._execute = mock_execute  # type: ignore[assignment]
    collector._connect_managed = AsyncMock(return_value=object())  # type: ignore[assignment]
    result = await collector.collect(MagicMock(source_id="spark1", domain="test_db"))

    cols = result.specs[0].schema_json["columns"]
    # 占位串 → 空串（恢复「无注释」语义）
    assert cols[0]["comment"] == ""
    # 真实注释保留
    assert cols[1]["comment"] == "订单金额"
    # 空串保持空串
    assert cols[2]["comment"] == ""


async def test_spark_collector_default_port_and_register():
    """Spark 采集器默认端口 10000（Spark Thrift Server 官方默认）且已注册到全局 registry。"""
    collector = SparkCollector(host="spark-host")
    assert collector._port == 10000
    assert collector._auth == "NONE"

    from app.services.collector.connectors import registry

    assert "spark" in registry.list_types()
    # SSRF 校验：mock DNS 解析为公网 IP（探活/枚举严格模式放行）
    with patch("app.core.ssrf.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 0))]):
        built = registry.build_from_cfg("spark", {"host": "spark-host", "port": 10001})
    assert isinstance(built, SparkCollector)
    assert built._port == 10001


# ---------- ClickHouseCollector ----------


async def test_clickhouse_collector_parses_http_response():
    """ClickHouse 采集器解析 HTTP API 响应（含默认值/注释——P0 列元数据补全）。"""
    collector = ClickHouseCollector(host="ch-host", database="test_db")

    # Mock _query 方法
    async def mock_query(sql: str) -> str:
        if "system.tables" in sql:
            return "events\nlogs\n"
        if "system.columns" in sql:
            if "events" in sql:
                # name \t type \t default_kind \t default_expression \t comment
                return "event_id\tUInt64\tDEFAULT\t0\t事件ID\nevent_name\tString\n"
            if "logs" in sql:
                return "log_id\tUInt64\nmessage\tString\n"
        return ""

    collector._query = mock_query  # type: ignore[assignment]
    result = await collector.collect(MagicMock(source_id="ch1", domain="test_db"))

    assert isinstance(result, CollectResult)
    assert len(result.specs) == 2
    assert result.specs[0].entity_name == "events"
    cols = result.specs[0].schema_json["columns"]
    assert cols[0]["name"] == "event_id"
    assert cols[0]["type"] == "UInt64"
    # P0 修复：默认值与注释被采集（仅 DEFAULT 类型认作可写默认值）
    assert cols[0]["default"] == "0"
    assert cols[0]["comment"] == "事件ID"
    # 旧版 2 列 TabSeparated 兼容：type 兜底、comment/default 为空
    assert cols[1]["type"] == "String"
    assert cols[1]["comment"] == ""


async def test_clickhouse_collector_samples_columns_via_http():
    """ClickHouse 采样：TabSeparated 文本解析 + 样本打码写入 columns[].sample。"""
    collector = ClickHouseCollector(host="ch-host", database="test_db")
    sample_sql: list[str] = []

    async def mock_query(sql: str) -> str:
        if "system.tables" in sql:
            return "events\n"
        if "system.columns" in sql:
            return "phone\tString\nemail\tString\nnickname\tString\n"
        if "SELECT" in sql:
            sample_sql.append(sql)
            # TabSeparatedWithNames：首行列名 + 数据行（列序 = phone,email,nickname）
            return (
                "phone\temail\tnickname\n"
                "13812345678\t\\N\t\\N\n"
                "13812345678\ta@b.com\t\\N\n"
            )
        return ""

    collector._query = mock_query  # type: ignore[assignment]
    collector.set_sampling(5)
    result = await collector.collect(MagicMock(source_id="ch1", domain="test_db"))

    cols = result.specs[0].schema_json["columns"]
    # 第 1 条 = 行对齐全列查询；第 2 条 = nickname 全 NULL 稀疏列单列补采
    assert len(sample_sql) == 2
    assert "FROM `test_db`.`events`" in sample_sql[0]
    assert "LIMIT 5 FORMAT TabSeparatedWithNames" in sample_sql[0]
    # 行视图：一行 = 一条真实记录（email 首行 \N、nickname 全 NULL → 空串占位）
    assert result.specs[0].schema_json["sample_rows"] == [
        {"phone": "138****5678", "email": "", "nickname": ""},
        {"phone": "138****5678", "email": "a***@b.com", "nickname": ""},
    ]
    # 列式 sample：手机打码（两行同值去重）；email 跳过首行 \N 取次行值；
    # nickname 全 NULL → 不写 sample
    assert cols[0]["sample"] == ["138****5678"]
    assert cols[1]["sample"] == ["a***@b.com"]
    assert "sample" not in cols[2]


async def test_clickhouse_list_databases_excludes_system_dbs():
    """list_databases 枚举非系统库（system/information_schema/default 排除）。"""
    collector = ClickHouseCollector(host="ch-host")

    async def mock_query(sql: str) -> str:
        assert "system.databases" in sql
        return "system\ninformation_schema\ndefault\nods\ndwd\n"

    collector._query = mock_query  # type: ignore[assignment]
    assert await collector.list_databases() == ["ods", "dwd"]


async def test_clickhouse_list_tables_groups_by_db():
    """list_tables 逐库 system.tables 查询并按库分组（复用 HTTP 单连接）。"""
    collector = ClickHouseCollector(host="ch-host")
    seen_sql: list[str] = []

    async def mock_query(sql: str) -> str:
        seen_sql.append(sql)
        if "ods" in sql:
            return "orders\ncustomers\n"
        if "dwd" in sql:
            return "fee_di\n"
        return ""

    collector._query = mock_query  # type: ignore[assignment]
    result = await collector.list_tables(["ods", "bad.db", "dwd"])
    assert result == {"ods": ["orders", "customers"], "dwd": ["fee_di"]}
    # 非法库名跳过，未对其发出查询
    assert not any("bad.db" in s for s in seen_sql)


async def test_clickhouse_list_tables_falls_back_to_configured_database():
    """list_tables 未传库时回退连接库（单库模式）。"""
    collector = ClickHouseCollector(host="ch-host", database="test_db")

    async def mock_query(sql: str) -> str:
        return "events\nlogs\n"

    collector._query = mock_query  # type: ignore[assignment]
    assert await collector.list_tables() == {"test_db": ["events", "logs"]}


async def test_clickhouse_collector_skips_sampling_when_disabled():
    """未开启采样 → 不发采样 SQL、字段无 sample 键。"""
    collector = ClickHouseCollector(host="ch-host", database="test_db")
    sample_sql: list[str] = []

    async def mock_query(sql: str) -> str:
        if "system.tables" in sql:
            return "events\n"
        if "system.columns" in sql:
            return "phone\tString\n"
        sample_sql.append(sql)
        return "13812345678\n"

    collector._query = mock_query  # type: ignore[assignment]
    collector.set_sampling(0)
    result = await collector.collect(MagicMock(source_id="ch1", domain="test_db"))

    assert sample_sql == []
    assert "sample" not in result.specs[0].schema_json["columns"][0]


async def test_clickhouse_collector_tolerates_sampling_failure():
    """采样查询失败 → 仅告警，不拖垮采集（FR-004 容错）。"""
    collector = ClickHouseCollector(host="ch-host", database="test_db")

    async def mock_query(sql: str) -> str:
        if "system.tables" in sql:
            return "events\n"
        if "system.columns" in sql:
            return "phone\tString\n"
        raise RuntimeError("sampling failed")

    collector._query = mock_query  # type: ignore[assignment]
    collector.set_sampling(5)
    result = await collector.collect(MagicMock(source_id="ch1", domain="test_db"))

    assert len(result.specs) == 1
    assert "sample" not in result.specs[0].schema_json["columns"][0]


async def test_clickhouse_collect_entity_samples_columns():
    """单表刷新路径（collect_entity）同样执行采样。"""
    collector = ClickHouseCollector(host="ch-host", database="test_db")
    sample_sql: list[str] = []

    async def mock_query(sql: str) -> str:
        if "system.columns" in sql:
            return "id_card\tString\n"
        sample_sql.append(sql)
        return "id_card\n110101199003071234\n"

    collector._query = mock_query  # type: ignore[assignment]
    collector.set_sampling(5)
    spec = await collector.collect_entity(MagicMock(source_id="ch1"), "users")

    assert spec is not None
    assert len(sample_sql) == 1
    assert "FROM `test_db`.`users`" in sample_sql[0]
    assert spec.schema_json["columns"][0]["sample"] == ["110101********1234"]
    assert spec.schema_json["sample_rows"] == [{"id_card": "110101********1234"}]


# ---------- KafkaCollector ----------


async def test_kafka_collector_with_no_topics():
    """Kafka 采集器无 Topic 时返回空结果。"""
    collector = KafkaCollector(bootstrap_servers="kafka-host:9092")

    # Mock _get_topics 返回空列表
    collector._get_topics = AsyncMock(return_value=[])  # type: ignore[assignment]
    collector._registry_url = None

    result = await collector.collect(MagicMock(source_id="kafka1"))

    assert isinstance(result, CollectResult)
    assert len(result.specs) == 0


async def test_kafka_collector_with_topics():
    """Kafka 采集器采集 Topic 列表。"""
    collector = KafkaCollector(
        bootstrap_servers="kafka-host:9092",
        registry_url=None,
    )

    topics = [
        {"name": "user-events", "partition_count": 3, "replication_factor": 2},
        {"name": "order-events", "partition_count": 6, "replication_factor": 3},
    ]
    collector._get_topics = AsyncMock(return_value=topics)  # type: ignore[assignment]
    collector._registry_url = None

    result = await collector.collect(MagicMock(source_id="kafka1"))

    assert isinstance(result, CollectResult)
    assert len(result.specs) == 2
    assert result.specs[0].entity_name == "user-events"
    assert result.specs[0].schema_json["partition_count"] == 3


# ---------- DorisCollector / StarRocksCollector ----------


async def test_doris_collector_uses_mysql_protocol():
    """Doris 采集器使用 MySQL 协议兼容。"""
    from app.services.collector.connectors.doris import create_doris_collector

    with patch("app.services.collector.connectors.doris.SqlalchemyConnector") as mock_conn_cls:
        mock_conn = MagicMock()
        mock_conn_cls.return_value = mock_conn

        collector = create_doris_collector({"host": "doris-host", "port": 9030})
        assert isinstance(collector, InformationSchemaCollector)
        mock_conn_cls.assert_called_once()


async def test_starrocks_collector_uses_mysql_protocol():
    """StarRocks 采集器使用 MySQL 协议兼容。"""
    from app.services.collector.connectors.starrocks import create_starrocks_collector

    with patch("app.services.collector.connectors.starrocks.SqlalchemyConnector") as mock_conn_cls:
        mock_conn = MagicMock()
        mock_conn_cls.return_value = mock_conn

        collector = create_starrocks_collector({"host": "sr-host", "port": 9030})
        assert isinstance(collector, InformationSchemaCollector)
        mock_conn_cls.assert_called_once()


# ---------- URL 构建 ----------


def test_mysql_url_build_uses_url_create():
    """MySQL URL 构建使用 SQLAlchemy URL.create()。"""
    from app.services.collector.connectors.mysql import _build_mysql_url

    url = _build_mysql_url(
        {
            "user": "root",
            "password": "secret",
            "host": "db-host",
            "port": 3306,
            "database": "mydb",
        }
    )
    # URL.create 不暴露密码在 __repr__ 中
    url_str = str(url)
    assert "db-host" in url_str
    assert "3306" in url_str
    assert "mydb" in url_str


def test_postgres_url_build():
    """PostgreSQL URL 构建使用 postgresql+asyncpg 驱动。"""
    from app.services.collector.connectors.postgres import _build_postgres_url

    url = _build_postgres_url(
        {
            "user": "pguser",
            "password": "pgpass",
            "host": "pg-host",
            "port": 5432,
            "database": "pgdb",
        }
    )
    url_str = str(url)
    assert "postgresql+asyncpg" in url_str
    assert "pg-host" in url_str


def test_doris_url_build():
    """Doris URL 构建使用 mysql+aiomysql 驱动，默认端口 9030。"""
    from app.services.collector.connectors.doris import _build_doris_url

    url = _build_doris_url({"host": "doris-host", "port": 9030})
    url_str = str(url)
    assert "mysql+aiomysql" in url_str
    assert "9030" in url_str


def test_starrocks_url_build():
    """StarRocks URL 构建使用 mysql+aiomysql 驱动，默认端口 9030。"""
    from app.services.collector.connectors.starrocks import _build_starrocks_url

    url = _build_starrocks_url({"host": "sr-host", "port": 9030})
    url_str = str(url)
    assert "mysql+aiomysql" in url_str
    assert "9030" in url_str


async def test_hive_connect_timeout_raises():
    """P2-15: pyhive 连接超时（asyncio.wait_for connect_timeout=10s）→ ExternalDependencyError。"""
    from app.services.collector.connectors import hive as hive_mod

    collector = HiveCollector(host="hive-host", database="test_db")
    pending = asyncio.get_event_loop().create_future()
    with (
        # to_thread 是 async def，patch 默认会建 AsyncMock → 须 new_callable=MagicMock
        # 强制同步 mock（返回 pending future），避免未 await coroutine 泄漏
        patch.object(
            hive_mod.asyncio,
            "to_thread",
            new_callable=MagicMock,
            return_value=pending,
        ),
        patch.object(
            hive_mod.asyncio,
            "wait_for",
            side_effect=TimeoutError("timeout"),
        ),
        pytest.raises(hive_mod.ExternalDependencyError, match="连接超时"),
    ):
        await collector._execute("SELECT 1")


async def test_hive_query_timeout_raises():
    """P2-15: pyhive 查询超时（asyncio.wait_for query_timeout=120s）→ ExternalDependencyError。"""
    from app.services.collector.connectors import hive as hive_mod

    class _FakeCursor:
        def execute(self, sql: str) -> None:
            return None

        def fetchall(self) -> list:
            return []

    class _FakeConn:
        def __init__(self) -> None:
            self.closed = False

        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

        def close(self) -> None:
            self.closed = True

    collector = HiveCollector(host="hive-host", database="test_db")
    done = asyncio.get_event_loop().create_future()
    done.set_result(_FakeConn())
    pending = asyncio.get_event_loop().create_future()
    # close 阶段 to_thread 需返回已完成 future——否则 _execute 的
    # ``finally: await asyncio.to_thread(self._close, conn)`` 会 await 永不
    # 完成的 pending future 卡死（新 _execute 连接复用重构后单次路径自关连接）。
    done_close = asyncio.get_event_loop().create_future()
    done_close.set_result(None)
    with (
        patch.object(
            hive_mod.asyncio,
            "to_thread",
            new_callable=MagicMock,
            side_effect=[pending, pending, done_close],
        ),
        # 连接 wait_for 成功（返回已完成的 conn future），查询 wait_for 抛超时
        patch.object(
            hive_mod.asyncio,
            "wait_for",
            side_effect=[done, TimeoutError("timeout")],
        ),
        pytest.raises(hive_mod.ExternalDependencyError, match="查询超时"),
    ):
        await collector._execute("SELECT 1")


async def test_hive_connect_passes_no_timeout_kwarg():
    """回归：pyhive 0.7.0 的 Connection 不接受 timeout 参数——_connect 不得传。

    此前误传 ``timeout=self._connect_timeout`` 导致真实连接
    ``TypeError: Connection.__init__() got an unexpected keyword argument 'timeout'``
    （采集/测试连接全部失败）。断言 connect 调用 kwargs 精确匹配，无 timeout。
    """
    from pyhive import hive as pyhive_hive

    collector = HiveCollector(host="hive-host", port=10000, user="u", password="p")
    with patch.object(pyhive_hive, "connect", return_value=MagicMock()) as mock_connect:
        collector._connect()
    kwargs = mock_connect.call_args.kwargs
    assert "timeout" not in kwargs
    assert kwargs["host"] == "hive-host"
    assert kwargs["port"] == 10000
    assert kwargs["username"] == "u"
    assert kwargs["password"] == "p"
    assert kwargs["auth"] == "LDAP"
    assert kwargs["database"] == "default"


async def test_hive_sync_query_returns_string_rows():
    """pyhive 查询行转字符串列表（None → 空串，与 beeline 空输出一致）。"""
    from pyhive import hive as pyhive_hive

    class _FakeCursor:
        def __init__(self, rows) -> None:
            self._rows = rows

        def execute(self, sql: str) -> None:
            return None

        def fetchall(self) -> list:
            return self._rows

    class _FakeConn:
        def __init__(self, rows) -> None:
            self._cursor = _FakeCursor(rows)
            self.closed = False

        def cursor(self) -> _FakeCursor:
            return self._cursor

        def close(self) -> None:
            self.closed = True

    conn = _FakeConn([("a", 1), (None, "b")])
    collector = HiveCollector(host="hive-host", database="test_db")
    with patch.object(pyhive_hive, "connect", return_value=conn):
        rows = collector._sync_query("SHOW TABLES")
    assert rows == [["a", "1"], ["", "b"]]
    assert conn.closed is True


async def test_hive_sync_query_connect_failure():
    """连接失败（网络/认证）统一转 ExternalDependencyError（503 可重试）。"""
    from pyhive import hive as pyhive_hive

    collector = HiveCollector(host="hive-host", user="u", password="p")
    assert collector._auth == "LDAP"
    with (
        patch.object(pyhive_hive, "connect", side_effect=RuntimeError("no route")),
        pytest.raises(ExternalDependencyError, match="Hive 连接失败"),
    ):
        collector._sync_query("SELECT 1")


async def test_hive_sync_query_execute_failure():
    """查询失败（语法/权限）统一转 ExternalDependencyError，连接仍被关闭。"""
    from pyhive import hive as pyhive_hive

    class _FailingCursor:
        def execute(self, sql: str) -> None:
            raise RuntimeError("ParseException: failed")

        def fetchall(self) -> list:
            return []

    class _FakeConn:
        def __init__(self) -> None:
            self.closed = False

        def cursor(self) -> _FailingCursor:
            return _FailingCursor()

        def close(self) -> None:
            self.closed = True

    conn = _FakeConn()
    collector = HiveCollector(host="hive-host", database="test_db")
    with (
        patch.object(pyhive_hive, "connect", return_value=conn),
        pytest.raises(ExternalDependencyError, match="Hive 查询失败"),
    ):
        collector._sync_query("SELECT bad")
    assert conn.closed is True


async def test_hive_collector_samples_columns():
    """Hive 采集：开启采样后批量列 SELECT 采样，值打码写入 sample。"""
    collector = HiveCollector(host="hive-host")

    async def mock_execute(sql: str, conn=None) -> list[list[str]]:
        if "SHOW DATABASES" in sql:
            return [["ods"]]
        if "SHOW TABLES" in sql:
            return [["orders"]]
        if "DESCRIBE" in sql:
            return [["order_id", "bigint", ""], ["phone", "string", ""]]
        if sql.lstrip().upper().startswith("SELECT"):
            # 采样查询：order_id=1, phone=手机号（打码前原始值）
            return [["1", "13812341234"]]
        return []

    collector._execute = mock_execute  # type: ignore[assignment]
    collector._connect_managed = AsyncMock(return_value=object())  # type: ignore[assignment]
    collector.set_sampling(5)
    result = await collector.collect(MagicMock(source_id="hive1", domain="d"))
    cols = {c["name"]: c for c in result.specs[0].schema_json["columns"]}
    assert cols["order_id"]["sample"] == ["1"]  # 非敏感值原样
    assert cols["phone"]["sample"] == ["138****1234"]  # 手机号打码


async def test_hive_collector_no_sampling_when_disabled():
    """Hive 采集：未开启采样（sample_rows=0）时不做采样查询。"""
    collector = HiveCollector(host="hive-host")

    async def mock_execute(sql: str, conn=None) -> list[list[str]]:
        if "SHOW DATABASES" in sql:
            return [["ods"]]
        if "SHOW TABLES" in sql:
            return [["orders"]]
        if "DESCRIBE" in sql:
            return [["order_id", "bigint"]]
        raise AssertionError(f"不应执行采样查询: {sql}")

    collector._execute = mock_execute  # type: ignore[assignment]
    collector._connect_managed = AsyncMock(return_value=object())  # type: ignore[assignment]
    result = await collector.collect(MagicMock(source_id="hive1", domain="d"))
    assert "sample" not in result.specs[0].schema_json["columns"][0]


# ---------- 采样修复回归（跨连接器审查发现）----------


async def test_clickhouse_parse_tsv_named_unescapes_escaped_fields():
    """ClickHouse TabSeparated 字段内转义（\\t/\\n/\\\\）必须反转义，否则样本值损坏。"""
    collector = ClickHouseCollector(host="ch-host", database="test_db")
    # 列名行用真实 tab 分隔；数据行内字段按 ClickHouse 转义规则：
    # 真实反斜杠 → \\\\（两个反斜杠）；NULL → \\N
    text = (
        "addr\tcode\n"
        "北京\\\\朝阳区\tA-1\n"
        "\\N\tB-2\n"
    )
    rows = collector._parse_tsv_named(text)
    assert rows == [
        {"addr": "北京\\朝阳区", "code": "A-1"},
        {"addr": None, "code": "B-2"},
    ]


async def test_clickhouse_collector_emits_sampling_progress():
    """ClickHouse 采集：采样阶段发 sampling 进度事件（对齐 MySQL，避免前端卡 0%）。"""
    collector = ClickHouseCollector(host="ch-host", database="test_db")
    events: list[dict] = []

    async def mock_query(sql: str) -> str:
        if "system.tables" in sql:
            return "events\nlogs\n"
        if "system.columns" in sql:
            return "phone\tString\n"
        if "SELECT" in sql:
            return "phone\n13812345678\n"
        return ""

    async def cb(event: dict) -> None:
        events.append(event)

    collector._query = mock_query  # type: ignore[assignment]
    collector.set_sampling(5)
    collector.set_progress_cb(cb)
    await collector.collect(MagicMock(source_id="ch1", domain="test_db"))
    sampling = [e for e in events if e["phase"] == "sampling"]
    assert len(sampling) == 2
    assert sampling[0]["index"] == 1
    assert sampling[0]["total"] == 2
    assert sampling[0]["entity_name"] == "events"
    assert sampling[1]["index"] == 2
    assert sampling[1]["entity_name"] == "logs"


async def test_postgres_collector_emits_sampling_progress():
    """Postgres 采集：采样阶段发 sampling 进度事件。"""
    conn = _SamplingPgConnector(
        ["users", "orders"],
        {
            "users": [{"column_name": "phone", "data_type": "varchar"}],
            "orders": [{"column_name": "order_id", "data_type": "bigint"}],
        },
        sample_rows=[{"phone": "13812345678"}],
    )
    collector = PostgresCollector(conn)
    events: list[dict] = []

    async def cb(event: dict) -> None:
        events.append(event)

    collector.set_sampling(5)
    collector.set_progress_cb(cb)
    await collector.collect(MagicMock(source_id="s1", domain="public"))
    sampling = [e for e in events if e["phase"] == "sampling"]
    assert len(sampling) == 2
    assert sampling[0]["entity_name"] == "users"
    assert sampling[1]["entity_name"] == "orders"
    assert sampling[0]["total"] == 2


async def test_hive_collector_writes_sample_rows():
    """Hive 采集：行视图 sample_rows 必须落库（此前只写列式 sample、丢行视图）。"""
    collector = HiveCollector(host="hive-host")

    async def mock_execute(sql: str, conn=None) -> list[list[str]]:
        if "SHOW DATABASES" in sql:
            return [["ods"]]
        if "SHOW TABLES" in sql:
            return [["orders"]]
        if "DESCRIBE" in sql:
            return [["order_id", "bigint", ""], ["phone", "string", ""]]
        if sql.lstrip().upper().startswith("SELECT"):
            return [["1", "13812341234"]]
        return []

    collector._execute = mock_execute  # type: ignore[assignment]
    collector._connect_managed = AsyncMock(return_value=object())  # type: ignore[assignment]
    collector.set_sampling(5)
    result = await collector.collect(MagicMock(source_id="hive1", domain="d"))
    assert result.specs[0].schema_json["sample_rows"] == [
        {"order_id": "1", "phone": "138****1234"}
    ]


async def test_mysql_collector_accepts_hyphen_identifier():
    """MySQL 系标识符允许连字符：含 - 的列名应进入采样（反引号包裹安全）。"""
    import app.services.collector.connectors.mysql as mysql_mod

    for name in ("user-name", "phone", "order_id"):
        assert mysql_mod._IDENT_RE.match(name), f"{name} 应通过标识符校验"
    # 反引号包裹的含 - 标识符拼入 SQL 无歧义
    assert f"`{'user-name'}`" == "`user-name`"


async def test_postgres_collector_accepts_hyphen_identifier():
    """Postgres 标识符允许连字符：含 - 的列名可采样（双引号包裹安全）。"""
    collector = PostgresCollector(MagicMock())
    for name in ("user-name", "phone", "order_id"):
        assert collector._IDENT_RE.match(name), f"{name} 应通过标识符校验"


async def test_sample_value_truncated_for_large_fields():
    """大字段（BLOB/TEXT/JSON）样本值截断，防止 schema_json 膨胀。"""
    conn = _SamplingPgConnector(
        ["logs"],
        {
            "logs": [
                {"column_name": "payload", "data_type": "jsonb"},
                {"column_name": "phone", "data_type": "varchar"},
            ]
        },
        sample_rows=[{"payload": "x" * 5000, "phone": "13812345678"}],
    )
    collector = PostgresCollector(conn)
    collector.set_sampling(5)
    result = await collector.collect(MagicMock(source_id="s1", domain="public"))
    rows = result.specs[0].schema_json["sample_rows"]
    assert len(rows[0]["payload"]) <= 201  # 200 截断 + … 标记
    assert rows[0]["payload"].endswith("…")
    # 手机号等短敏感值不受截断影响（仍完整打码）
    assert rows[0]["phone"] == "138****5678"
    # 列式 sample 同样截断
    by_name = {c["name"]: c for c in result.specs[0].schema_json["columns"]}
    assert len(by_name["payload"]["sample"][0]) <= 201
