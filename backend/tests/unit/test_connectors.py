"""连接器单元测试（对齐 US1 / FR-001）。

覆盖：
1. 每种连接器的 collect() 方法 mock 测试（连接+查询+解析+异常处理）
2. MySQL InformationSchemaCollector 单表容错
3. PostgresCollector 查询+解析
4. HiveCollector beeline 输出解析
5. SparkCollector 复用 beeline 输出解析（Spark Thrift Server / HiveServer2 协议）
6. DorisCollector/StarRocksCollector MySQL 兼容性
7. ClickHouseCollector HTTP API
8. KafkaCollector Topic+Schema Registry
"""

from __future__ import annotations

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


# ---------- PostgresCollector ----------


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


# ---------- HiveCollector ----------


async def test_hive_collector_parses_beeline_output():
    """Hive 采集器解析 beeline 输出（含注释——P0 列元数据补全）。"""
    collector = HiveCollector(host="hive-host", database="test_db")

    # Mock _execute 方法
    async def mock_execute(sql: str) -> list[list[str]]:
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


# ---------- SparkCollector ----------


async def test_spark_collector_parses_beeline_output():
    """Spark 采集器经 Spark Thrift Server（HiveServer2 协议）解析 beeline 输出。"""
    collector = SparkCollector(host="spark-host", database="test_db")

    # Mock _execute 方法（与 Hive 相同的 SHOW TABLES / DESCRIBE 协议）
    async def mock_execute(sql: str) -> list[list[str]]:
        if "SHOW TABLES" in sql:
            return [["orders"], ["customers"]]
        if "DESCRIBE" in sql:
            if "orders" in sql:
                return [["order_id", "bigint"], ["amount", "decimal(10,2)"]]
            if "customers" in sql:
                return [["customer_id", "int"], ["name", "string"]]
        return []

    collector._execute = mock_execute  # type: ignore[assignment]
    result = await collector.collect(MagicMock(source_id="spark1", domain="test_db"))

    assert isinstance(result, CollectResult)
    assert len(result.specs) == 2
    assert result.specs[0].entity_name == "orders"
    assert result.specs[0].schema_json["columns"][1]["name"] == "amount"


async def test_spark_collector_default_port_and_register():
    """Spark 采集器默认端口 10000（Spark Thrift Server 官方默认）且已注册到全局 registry。"""
    collector = SparkCollector(host="spark-host")
    assert collector._port == 10000
    assert collector._jdbc_url == "jdbc:hive2://spark-host:10000/default"

    from app.services.collector.connectors import registry

    assert "spark" in registry.list_types()
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
