"""Hive Metastore 直连连接器单测（方案 B：HMS backend 为 MySQL）。

覆盖：collect 组装（库.表/字段/表描述/VIEW 识别/_meta）、多库范围、
include/exclude 过滤、collect_entity 单表、库/表枚举、TBL_COMMENT/INTEGER_IDX
旧版降级、probe、registry 注册。
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.exc import OperationalError

from app.services.collector.connectors.collector_registry import registry
from app.services.collector.connectors.hive_metastore import HiveMetastoreCollector


class _FakeHmsConnector:
    """HMS 测试用假连接器：按 SQL 分支返回可配置数据。"""

    def __init__(
        self,
        tables: list[dict],
        columns: list[dict],
        dbs: list[str] | None = None,
        *,
        fail_first_tables: bool = False,
        fail_first_columns: bool = False,
    ) -> None:
        self._tables = tables
        self._columns = columns
        self._dbs = dbs or ["ods", "dwd"]
        self._fail_first_tables = fail_first_tables
        self._fail_first_columns = fail_first_columns
        self._tables_calls = 0
        self._columns_calls = 0
        self.queries: list[tuple[str, object]] = []

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        self.queries.append((sql, params))
        if "SELECT NAME FROM DBS" in sql:
            return [{"name": d} for d in self._dbs]
        if "FROM COLUMNS_V2 c" in sql:
            self._columns_calls += 1
            if self._fail_first_columns and self._columns_calls == 1:
                raise OperationalError("", {}, Exception("Unknown column 'INTEGER_IDX'"))
            if params and params.get("tbl"):
                return [c for c in self._columns if c["tbl_name"] == params["tbl"]]
            return self._columns
        if "SELECT 1" in sql:
            return []
        if "FROM TBLS t" in sql:
            self._tables_calls += 1
            if self._fail_first_tables and self._tables_calls == 1:
                raise OperationalError("", {}, Exception("Unknown column 'TBL_COMMENT'"))
            if params and params.get("tbl"):
                return [t for t in self._tables if t["tbl_name"] == params["tbl"]]
            return self._tables
        return []

    async def dispose(self) -> None:
        return None


def _source() -> SimpleNamespace:
    return SimpleNamespace(source_id="hms_test")


def _tables_rows() -> list[dict]:
    return [
        {
            "db_name": "ods",
            "tbl_name": "orders",
            "tbl_type": "MANAGED_TABLE",
            "owner": "dba",
            "tbl_comment": "订单明细表",
            "location": "hdfs://ns/warehouse/ods/orders",
        },
        {
            "db_name": "ods",
            "tbl_name": "orders_v",
            "tbl_type": "VIRTUAL_VIEW",
            "owner": "dba",
            "tbl_comment": "订单视图",
            "location": None,
        },
    ]


def _columns_rows() -> list[dict]:
    return [
        {
            "db_name": "ods",
            "tbl_name": "orders",
            "column_name": "id",
            "type_name": "bigint",
            "comment": "订单ID",
        },
        {
            "db_name": "ods",
            "tbl_name": "orders",
            "column_name": "amount",
            "type_name": "decimal(10,2)",
            "comment": "金额",
        },
        {
            "db_name": "ods",
            "tbl_name": "orders_v",
            "column_name": "id",
            "type_name": "bigint",
            "comment": "订单ID",
        },
    ]


def _collector(
    tables: list[dict] | None = None,
    columns: list[dict] | None = None,
    dbs: list[str] | None = None,
    **kw,
) -> HiveMetastoreCollector:
    conn = _FakeHmsConnector(tables or _tables_rows(), columns or _columns_rows(), dbs, **kw)
    return HiveMetastoreCollector(conn, database="hive_metastore"), conn


# ---------- collect 主流程 ----------


async def test_hms_collect_full_assembly():
    col, conn = _collector()
    result = await col.collect(_source())

    assert len(result.specs) == 2
    orders = result.specs[0]
    assert orders.entity_name == "ods.orders"
    assert orders.entity_type == "TABLE"
    # 表描述关联
    assert orders.description == "订单明细表"
    # 字段含名称/类型/描述
    assert orders.schema_json["columns"][0] == {
        "name": "id",
        "type": "bigint",
        "comment": "订单ID",
    }
    # _meta 补充元数据
    assert orders.schema_json["_meta"]["owner"] == "dba"
    assert orders.schema_json["_meta"]["location"].startswith("hdfs://")
    # 视图识别
    assert result.specs[1].entity_type == "VIEW"
    # 枚举 DBS + 表 JOIN + 列 JOIN（3 次查询）
    assert len(conn.queries) == 3


async def test_hms_collect_multi_db_via_databases():
    col, conn = _collector()
    col.set_databases(["ods"])
    result = await col.collect(_source())
    assert len(result.specs) == 2
    # 未走枚举（_databases 非空），表查询 IN 占位符参数只含 ods
    table_query, table_params = next(q for q in conn.queries if "FROM TBLS t" in q[0])
    assert table_params == {"d0": "ods"}
    # 未发枚举查询（SQL 无 SELECT NAME FROM DBS）
    assert not any("SELECT NAME FROM DBS" in q for q, _ in conn.queries)


async def test_hms_collect_include_filter():
    col, conn = _collector()
    col.set_table_filter(include_patterns=["ods.orders"])
    result = await col.collect(_source())
    assert [s.entity_name for s in result.specs] == ["ods.orders"]


async def test_hms_collect_exclude_filter():
    col, conn = _collector()
    col.set_table_filter(exclude_patterns=["*.orders_v"])
    result = await col.collect(_source())
    assert [s.entity_name for s in result.specs] == ["ods.orders"]


# ---------- 单表刷新 ----------


async def test_hms_collect_entity():
    col, _conn = _collector()
    spec = await col.collect_entity(_source(), "ods.orders")
    assert spec is not None
    assert spec.entity_name == "ods.orders"
    assert spec.description == "订单明细表"
    assert len(spec.schema_json["columns"]) == 2


async def test_hms_collect_entity_missing_table_returns_none():
    col, conn = _collector(tables=[], columns=[])
    assert await col.collect_entity(_source(), "ods.missing") is None


# ---------- 枚举 ----------


async def test_hms_list_databases():
    col, _conn = _collector(dbs=["ods", "dwd", "information_schema"])
    dbs = await col.list_databases()
    assert dbs == ["ods", "dwd"]


async def test_hms_list_tables():
    col, _conn = _collector()
    tables = await col.list_tables(["ods"])
    assert tables == {"ods": ["orders", "orders_v"]}


# ---------- 旧版降级 ----------


async def test_hms_collect_tbl_comment_missing_downgrades():
    col, conn = _collector(fail_first_tables=True)
    result = await col.collect(_source())
    assert len(result.specs) == 2
    # 第二次表查询成功（降级为不含 TBL_COMMENT）
    assert conn._tables_calls == 2
    assert len(conn.queries) == 4  # 枚举 + 表(失败) + 表(降级) + 列


async def test_hms_collect_integer_idx_missing_downgrades():
    col, conn = _collector(fail_first_columns=True)
    result = await col.collect(_source())
    assert len(result.specs) == 2
    assert conn._columns_calls == 2


# ---------- 探活 / 注册 ----------


async def test_hms_probe_ok():
    col, _conn = _collector()
    probe = await col.probe()
    assert probe.ok is True
    assert probe.latency_ms >= 0


async def test_hms_registered_in_registry():
    assert "hive_metastore" in registry.list_types()
    info = {i.source_type: i for i in registry.list_type_info()}
    assert info["hive_metastore"].label == "Hive Metastore"
    assert info["hive_metastore"].default_port == 3306
    assert info["hive_metastore"].supports_database is True


def test_hms_factory_defaults_database_to_hive():
    """回归：factory 缺 database 时按 hive 填充（与 schemas 层默认值一致）。

    覆盖直接 build_from_cfg 的入口（连接预检/枚举），避免 URL 无库导致
    ``1046 No database selected``；显式提供 database 时保持原值。
    """
    col = registry.build_from_cfg(
        "hive_metastore",
        {"host": "8.8.8.8", "port": 3306, "user": "u", "password": "p"},
    )
    assert col._database == "hive"
    col2 = registry.build_from_cfg(
        "hive_metastore",
        {"host": "8.8.8.8", "port": 3306, "database": "metastore_db"},
    )
    assert col2._database == "metastore_db"


# ---------- 样本采样（PII 精度增强：sample_connection 直连 HiveServer2）----------


class _FakeSampler:
    """模拟 HiveCollector 采样器（记录连接/采样调用，可注入样本行）。"""

    def __init__(self, rows: list[list[str]] | None = None) -> None:
        self._rows = rows or [["13812341234"]]
        self.sampled: list[tuple[str, list[dict]]] = []
        self.closed = 0
        self.max_rows = 0

    def set_sampling(self, max_rows: int = 0) -> None:
        self.max_rows = max_rows

    async def _connect_managed(self) -> _FakeSampler:
        return self

    async def _sample_columns(self, entity_name, columns, conn) -> list[dict]:
        self.sampled.append((entity_name, list(columns)))
        # 模拟 Hive 采样：每列注入打码样本（复用 classifier.mask_sample）
        from app.services.collector.classifier import SensitivityClassifier

        clf = SensitivityClassifier()
        for i, col in enumerate(columns):
            if i < len(self._rows[0]):
                col["sample"] = clf.mask_sample(self._rows[0][i])
        return columns

    async def _close(self, conn) -> None:
        self.closed += 1


def test_hms_factory_parses_sample_connection():
    """工厂：配置 sample_connection 时构造 HiveCollector 采样器；未配置则无。"""
    col = registry.build_from_cfg(
        "hive_metastore",
        {
            "host": "8.8.8.8",
            "port": 3306,
            "database": "hive",
            "sample_connection": {"host": "9.9.9.9", "port": 10000, "user": "hive"},
        },
    )
    assert col._sampler is not None
    assert col._sampler._host == "9.9.9.9"
    assert col._sampler._port == 10000
    # 未配置 sample_connection → 无采样器
    col2 = registry.build_from_cfg(
        "hive_metastore", {"host": "8.8.8.8", "port": 3306, "database": "hive"}
    )
    assert col2._sampler is None


async def test_hms_collect_samples_when_configured():
    """collect：配置采样器且开启采样时，逐表采样并写入 schema_json.columns[].sample。"""
    col, _conn = _collector()
    sampler = _FakeSampler(rows=[["13812341234", "北京"]])
    col._sampler = sampler
    col.set_sampling(5)
    result = await col.collect(_source())
    # 两张表都被采样（orders 2 列、orders_v 1 列）
    assert len(sampler.sampled) == 2
    assert sampler.sampled[0][0] == "ods.orders"
    specs = {s.entity_name: s for s in result.specs}
    orders_cols = specs["ods.orders"].schema_json["columns"]
    by_name = {c["name"]: c for c in orders_cols}
    # fake 行 [["13812341234", "北京"]]：id→手机号打码、amount→普通值原样
    assert by_name["id"]["sample"] == "138****1234"
    assert by_name["amount"]["sample"] == "北京"
    assert sampler.closed == 1  # 采样连接复用并关闭


async def test_hms_collect_no_sampling_without_configure():
    """collect：未配置采样器时不做采样（元数据采集不受影响）。"""
    col, _conn = _collector()
    col.set_sampling(5)
    result = await col.collect(_source())
    for s in result.specs:
        for c in s.schema_json.get("columns", []):
            assert "sample" not in c
