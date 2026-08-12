"""连接器单元测试（对齐 US1 / FR-001）。

覆盖：
1. 每种连接器的 collect() 方法 mock 测试（连接+查询+解析+异常处理）
2. MySQL InformationSchemaCollector 单表容错
3. PostgresCollector 查询+解析
4. HiveCollector beeline 输出解析
5. DorisCollector/StarRocksCollector MySQL 兼容性
6. ClickHouseCollector HTTP API
7. KafkaCollector Topic+Schema Registry
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
from app.services.collector.spi import CollectResult


class _FakeConnector:
    """测试用假连接器。"""

    def __init__(self, tables: list[str], columns: dict[str, list[dict]]) -> None:
        self._tables = tables
        self._columns = columns

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        if "information_schema.tables" in sql:
            return [{"table_name": t} for t in self._tables]
        if "information_schema.columns" in sql:
            tbl = params.get("tbl", "") if params else ""
            return self._columns.get(tbl, [])
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
    """部分表查询失败的连接器（模拟第500表超时）。"""

    def __init__(self, tables: list[str], fail_at: str) -> None:
        self._tables = tables
        self._fail_at = fail_at

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        if "information_schema.tables" in sql:
            return [{"table_name": t} for t in self._tables]
        if "information_schema.columns" in sql:
            tbl = params.get("tbl", "") if params else ""
            if tbl == self._fail_at:
                raise RuntimeError(f"查询表 {tbl} 超时")
            return [{"column_name": f"{tbl}_col1"}]
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
    collector = InformationSchemaCollector(conn)
    result = await collector.collect(MagicMock(source_id="s1", domain="db1"))

    assert isinstance(result, CollectResult)
    assert len(result.specs) == 2
    assert result.specs[0].entity_name == "users"
    assert "user_name" in result.specs[0].schema_json["columns"]
    assert len(result.failed_specs) == 0


async def test_mysql_collector_external_failure_raises():
    """MySQL 采集器源库连接失败时抛出 ExternalDependencyError。"""
    collector = InformationSchemaCollector(_BoomConnector())
    with pytest.raises(ExternalDependencyError):
        await collector.collect(MagicMock(source_id="s1", domain="db1"))


async def test_mysql_collector_single_table_failure_skips():
    """FR-004: 单表失败跳过继续，不中断全批采集。"""
    conn = _PartialFailConnector(["table1", "table2", "table3"], fail_at="table2")
    collector = InformationSchemaCollector(conn)
    result = await collector.collect(MagicMock(source_id="s1", domain="db1"))

    assert len(result.specs) == 2  # table1, table3 成功
    assert len(result.failed_specs) == 1  # table2 失败
    assert result.failed_specs[0].entity_name == "table2"
    assert "超时" in result.failed_specs[0].error


# ---------- PostgresCollector ----------


async def test_postgres_collector_builds_specs():
    """PostgreSQL 采集器正常采集返回 specs。"""
    conn = _FakeConnector(
        ["users"],
        {"users": [{"column_name": "user_name", "data_type": "varchar"}]},
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


# ---------- HiveCollector ----------


async def test_hive_collector_parses_beeline_output():
    """Hive 采集器解析 beeline 输出。"""
    collector = HiveCollector(host="hive-host", database="test_db")

    # Mock _execute 方法
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
    result = await collector.collect(MagicMock(source_id="hive1", domain="test_db"))

    assert isinstance(result, CollectResult)
    assert len(result.specs) == 2
    assert result.specs[0].entity_name == "orders"
    assert result.specs[0].schema_json["columns"][0]["name"] == "order_id"


# ---------- ClickHouseCollector ----------


async def test_clickhouse_collector_parses_http_response():
    """ClickHouse 采集器解析 HTTP API 响应。"""
    collector = ClickHouseCollector(host="ch-host", database="test_db")

    # Mock _query 方法
    async def mock_query(sql: str) -> str:
        if "system.tables" in sql:
            return "events\nlogs\n"
        if "system.columns" in sql:
            if "events" in sql:
                return "event_id\tUInt64\nevent_name\tString\n"
            if "logs" in sql:
                return "log_id\tUInt64\nmessage\tString\n"
        return ""

    collector._query = mock_query  # type: ignore[assignment]
    result = await collector.collect(MagicMock(source_id="ch1", domain="test_db"))

    assert isinstance(result, CollectResult)
    assert len(result.specs) == 2
    assert result.specs[0].entity_name == "events"
    assert result.specs[0].schema_json["columns"][0]["name"] == "event_id"
    assert result.specs[0].schema_json["columns"][0]["type"] == "UInt64"


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

    url = _build_mysql_url({
        "user": "root",
        "password": "secret",
        "host": "db-host",
        "port": 3306,
        "database": "mydb",
    })
    # URL.create 不暴露密码在 __repr__ 中
    url_str = str(url)
    assert "db-host" in url_str
    assert "3306" in url_str
    assert "mydb" in url_str


def test_postgres_url_build():
    """PostgreSQL URL 构建使用 postgresql+asyncpg 驱动。"""
    from app.services.collector.connectors.postgres import _build_postgres_url

    url = _build_postgres_url({
        "user": "pguser",
        "password": "pgpass",
        "host": "pg-host",
        "port": 5432,
        "database": "pgdb",
    })
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
