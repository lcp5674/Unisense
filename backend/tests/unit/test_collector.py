"""采集领域单元测试（对齐 DEV_GUIDE §8b / gateways unit）。

覆盖：敏感分级规则引擎、SPI 采集器（含外部依赖失败转化为重试型错误）、
服务层（加密/脱敏/幂等/批量废弃/采集编排）、仓储层（upsert 幂等/批量部分失败/
覆盖率重算）。无外部依赖（repo 以 mock 注入；集成测试覆盖真实 MySQL）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import BusinessError, ConflictError, ExternalDependencyError
from app.services.collector.classifier import SensitivityClassifier
from app.services.collector.repository import CollectorRepository
from app.services.collector.schemas import (
    BulkDeprecateItem,
    BulkDeprecateRequest,
    DataSourceCreateRequest,
    DBCatalogCreateRequest,
)
from app.services.collector.service import CollectorService
from app.services.collector.spi import (
    CatalogSpec,
    InformationSchemaCollector,
    build_collector,
)


def _svc() -> tuple[CollectorService, MagicMock]:
    """构造服务并替换其仓库为 mock，返回 (service, mock_repo_instance)。"""
    with patch("app.services.collector.service.CollectorRepository") as mock_repo:
        svc = CollectorService(db=MagicMock())
        return svc, mock_repo.return_value


class _FakeCatalog:
    """用于 DBCatalogResponse.model_validate 的轻量替身（含 schema_json 列名）。"""

    def __init__(self, sensitivity: str) -> None:
        self.source_id = "src1"
        self.entity_name = "users"
        self.entity_type = "TABLE"
        self.schema_json = {"columns": ["user_name"]}
        self.etl_sql = None
        self.sensitivity_level = sensitivity
        self.owner_id = None
        self.upstream_signature = "sig"


class _FakeConnector:
    """SPI 测试用假连接器（记录查询）。"""

    def __init__(self, tables: list[str], columns: dict[str, list[str]]) -> None:
        self._tables = tables
        self._columns = columns
        self.queries: list[tuple[str, object]] = []

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        self.queries.append((sql, params))
        if "information_schema.tables" in sql:
            return [{"table_name": t} for t in self._tables]
        return [{"column_name": c} for c in self._columns.get(params.get("tbl"), [])]


# ---------- 敏感分级 ----------


def test_classifier_pii_by_column_name():
    assert (
        SensitivityClassifier().classify("user_profile", {"columns": ["user_name", "email"]})
        == "PII"
    )


def test_classifier_pii_by_token_pattern():
    assert SensitivityClassifier().classify("t", {"columns": ["id_no"]}) == "PII"


def test_classifier_confidential():
    assert (
        SensitivityClassifier().classify("payroll", {"columns": ["salary", "emp_id"]})
        == "CONFIDENTIAL"
    )


def test_classifier_internal_default():
    assert (
        SensitivityClassifier().classify("order", {"columns": ["order_id", "amount"]}) == "INTERNAL"
    )


# ---------- SPI ----------


async def test_info_schema_collector_builds_specs():
    conn = _FakeConnector(["users", "orders"], {"users": ["user_name"], "orders": ["order_id"]})
    collector = InformationSchemaCollector(conn)
    specs = await collector.collect(MagicMock(source_id="s1", domain="db1"))
    assert len(specs) == 2
    assert specs[0].entity_name == "users"
    assert "user_name" in specs[0].schema_json["columns"]
    # 查询参数化（无字符串拼接）
    assert all(params for _, params in conn.queries)


async def test_info_schema_collector_external_failure_is_retryable():
    class BoomConnector:
        async def query(self, sql: str, params: dict | None = None) -> list[dict]:
            raise RuntimeError("connection refused")

    collector = InformationSchemaCollector(BoomConnector())
    with pytest.raises(ExternalDependencyError):
        await collector.collect(MagicMock(source_id="s1", domain="db1"))


def test_build_collector_unsupported_raises():
    with pytest.raises(BusinessError):
        build_collector("unknown_type", "enc")


# ---------- 服务层 ----------


async def test_create_source_encrypts_and_redacts():
    svc, repo = _svc()
    repo.get_source = AsyncMock(return_value=None)
    repo.create_source = AsyncMock()
    resp = await svc.create_source(
        DataSourceCreateRequest(
            source_id="src1",
            name="S",
            source_type="mysql",
            connection_config={"host": "h", "password": "p"},
            domain="d",
        ),
        actor_id=1,
    )
    assert resp.connection_config_present is True
    created = repo.create_source.call_args.args[0]
    # 落库为密文，不含明文键名
    assert created.connection_config != '{"host":"h","password":"p"}'
    assert "password" not in created.connection_config


async def test_create_source_conflict():
    svc, repo = _svc()
    repo.get_source = AsyncMock(return_value=MagicMock())
    with pytest.raises(ConflictError):
        await svc.create_source(
            DataSourceCreateRequest(
                source_id="src1",
                name="S",
                source_type="mysql",
                connection_config={"host": "h"},
                domain="d",
            ),
            actor_id=1,
        )


async def test_register_catalog_classifies_pii_and_publishes():
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    svc._events = events
    repo.upsert_catalog = AsyncMock(return_value=(_FakeCatalog("PII"), True))
    repo.recompute_coverage = AsyncMock(return_value=1.0)
    resp = await svc.register_catalog(
        DBCatalogCreateRequest(
            source_id="s", entity_name="users", schema_def={"columns": ["user_name"]}
        ),
        actor_id=1,
    )
    assert resp.sensitivity_level == "PII"
    events.publish.assert_awaited()


async def test_bulk_deprecate_delegates():
    svc, repo = _svc()
    item = BulkDeprecateItem(source_id="s", entity_name="t")
    repo.bulk_deprecate = AsyncMock(return_value=([item], [{"item": {}, "reason": "x"}]))
    result = await svc.bulk_deprecate(BulkDeprecateRequest(items=[item]), actor_id=1)
    assert result.succeeded == [item]
    assert len(result.failed) == 1


async def test_collect_and_register_uses_classifier_and_counts():
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock())
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True))
    repo.recompute_coverage = AsyncMock(return_value=0.5)

    class StubCollector:
        async def collect(self, source: object) -> list[CatalogSpec]:
            return [
                CatalogSpec(
                    entity_name="users", entity_type="TABLE", schema_json={"columns": ["user_name"]}
                )
            ]

    result = await svc.collect_and_register("s", StubCollector(), actor_id=1)
    assert result["scanned"] == 1
    assert result["registered"] == 1
    assert result["coverage"] == 0.5


# ---------- 仓储层（mock session） ----------


def _session(scalar_one_or_none=None, all_rows=None, scalar=None) -> MagicMock:
    s = MagicMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = scalar_one_or_none
    res.scalars.return_value.all.return_value = all_rows or []
    res.scalar.return_value = scalar
    s.execute = AsyncMock(return_value=res)
    s.scalar = AsyncMock(return_value=scalar)
    s.add = MagicMock()
    s.flush = AsyncMock()
    return s


async def test_repo_get_source_none():
    repo = CollectorRepository(_session(scalar_one_or_none=None))
    assert await repo.get_source("x") is None


async def test_repo_upsert_creates_when_missing():
    repo = CollectorRepository(_session(scalar_one_or_none=None))
    cat, created = await repo.upsert_catalog(
        source_id="s",
        entity_name="t",
        entity_type="TABLE",
        schema_json={"columns": ["a"]},
        etl_sql=None,
        sensitivity_level="INTERNAL",
        owner_id=None,
    )
    assert created is True
    assert cat.upstream_signature


async def test_repo_upsert_updates_when_exists():
    existing = MagicMock()
    existing.row_version = 1
    repo = CollectorRepository(_session(scalar_one_or_none=existing))
    cat, created = await repo.upsert_catalog(
        source_id="s",
        entity_name="t",
        entity_type="TABLE",
        schema_json={"columns": ["a"]},
        etl_sql=None,
        sensitivity_level="INTERNAL",
        owner_id=None,
    )
    assert created is False
    assert cat.schema_json == {"columns": ["a"]}


async def test_repo_bulk_deprecate_partial():
    repo = CollectorRepository(_session())
    repo.get_catalog = AsyncMock(side_effect=[MagicMock(), None])
    items = [
        BulkDeprecateItem(source_id="s", entity_name="a"),
        BulkDeprecateItem(source_id="s", entity_name="b"),
    ]
    succeeded, failed = await repo.bulk_deprecate(items)
    assert len(succeeded) == 1
    assert len(failed) == 1


async def test_repo_recompute_coverage_dict_quota():
    src = MagicMock()
    src.quota = {"max_scan_rows": 2}
    repo = CollectorRepository(_session(scalar_one_or_none=src, scalar=3))
    assert await repo.recompute_coverage("s") == 1.0


async def test_repo_list_sources_no_crash_on_filters():
    repo = CollectorRepository(_session(all_rows=[MagicMock()], scalar=1))
    items, total = await repo.list_sources(
        domain="d", source_type=None, keyword="k", page=1, page_size=10
    )
    assert total == 1
    assert items
