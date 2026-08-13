"""采集领域单元测试（对齐 DEV_GUIDE §8b / gateways unit）。

覆盖：敏感分级规则引擎、SPI 采集器（含外部依赖失败转化为重试型错误）、
服务层（加密/脱敏/幂等/批量废弃/采集编排）、仓储层（upsert 幂等/批量部分失败/
覆盖率重算）。无外部依赖（repo 以 mock 注入；集成测试覆盖真实 MySQL）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import BusinessError, ConflictError, NotFoundError
from app.services.collector.classifier import SensitivityClassifier
from app.services.collector.repository import CollectorRepository
from app.services.collector.schemas import (
    BulkDeprecateItem,
    BulkDeprecateRequest,
    DataSourceCreateRequest,
    DataSourceUpdateRequest,
    DBCatalogCreateRequest,
)
from app.services.collector.service import CollectorService
from app.services.collector.spi import (
    CatalogSpec,
    CollectResult,
    FailedSpec,
    build_collector,
)


def _svc() -> tuple[CollectorService, MagicMock]:
    """构造服务并替换其仓库为 mock，返回 (service, mock_repo_instance)。"""
    with patch("app.services.collector.service.CollectorRepository") as mock_repo:
        db = MagicMock()
        # P0-4: 采集失败路径会 commit unhealthy（service 层），mock 需可 await
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.flush = AsyncMock()
        svc = CollectorService(db=db)
        repo = mock_repo.return_value
        # 确保 US5/US3 新增的异步方法也有默认 AsyncMock
        repo.update_health_status = AsyncMock()
        repo.get_watermark = AsyncMock(return_value=None)
        repo.update_watermark_after_collection = AsyncMock(
            return_value=MagicMock(mode="FULL")
        )
        return svc, repo


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
        self.content_signature = None
        self.schema_incomplete = False


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


async def test_build_collector_unsupported_raises():
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


async def test_update_source_updates_fields_and_reencrypts():
    """更新源：字段覆盖 + 连接配置重新加密 + 健康状态重置为 unknown。"""
    from app.models.data_source import DataSource

    svc, repo = _svc()
    src = DataSource(
        source_id="src1",
        name="旧名",
        source_type="mysql",
        connection_config="old_cipher",
        domain="old_domain",
        cluster_id=None,
        coverage=0.5,
        health_status="healthy",
        last_error="old error",
        quota={},
        created_by=1,
    )
    repo.get_source = AsyncMock(return_value=src)
    resp = await svc.update_source(
        "src1",
        DataSourceUpdateRequest(
            name="新名",
            source_type="spark",
            connection_config={"host": "h2", "port": 10001},
            domain="new_domain",
        ),
        actor_id=1,
    )
    assert resp.name == "新名"
    assert resp.source_type == "spark"
    assert resp.domain == "new_domain"
    assert src.connection_config != '{"host":"h2","port":10001}'
    assert "h2" not in src.connection_config
    # 连接配置变更 → 旧健康状态/错误不再可信
    assert src.health_status == "unknown"
    assert src.last_error is None
    svc._db.flush.assert_called_once()


async def test_update_source_partial_update_keeps_untouched():
    """PATCH 语义：仅更新传入字段，未传字段保持原值。"""
    from app.models.data_source import DataSource

    svc, repo = _svc()
    src = DataSource(
        source_id="src1",
        name="旧名",
        source_type="mysql",
        connection_config="old_cipher",
        domain="old_domain",
        cluster_id="cluster-a",
        coverage=0.5,
        health_status="healthy",
        quota={},
        created_by=1,
    )
    repo.get_source = AsyncMock(return_value=src)
    resp = await svc.update_source(
        "src1",
        DataSourceUpdateRequest(name="仅改名"),
        actor_id=1,
    )
    assert resp.name == "仅改名"
    assert src.source_type == "mysql"  # 未传类型保持不变
    assert src.domain == "old_domain"
    assert src.connection_config == "old_cipher"  # 未传配置不重加密
    assert src.health_status == "healthy"  # 配置未变 → 健康状态保留
    svc._db.flush.assert_called_once()


async def test_update_source_unsupported_type_raises():
    """变更 source_type 为枚举已含但采集器未注册的类型时抛 BusinessError（服务层兜底）。"""
    from app.models.data_source import DataSource
    from app.services.collector.connectors import registry

    svc, repo = _svc()
    src = DataSource(
        source_id="src1",
        name="S",
        source_type="mysql",
        connection_config="cipher",
        domain="d",
        quota={},
        created_by=1,
    )
    repo.get_source = AsyncMock(return_value=src)
    # 模拟枚举新增值但采集器尚未注册（Pydantic 校验通过，服务层防御拦截）
    with (
        patch.object(registry, "list_types", return_value=["mysql", "postgres"]),
        pytest.raises(BusinessError, match="不支持的采集器类型"),
    ):
        await svc.update_source(
            "src1",
            DataSourceUpdateRequest(source_type="spark"),
            actor_id=1,
        )


async def test_update_source_not_found():
    """更新不存在的源抛 NotFoundError。"""
    svc, repo = _svc()
    repo.get_source = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.update_source(
            "nope",
            DataSourceUpdateRequest(name="x"),
            actor_id=1,
        )


async def test_register_catalog_classifies_pii_and_publishes():
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    svc._llm_classify_sensitivity = AsyncMock(return_value=None)
    repo.upsert_catalog = AsyncMock(return_value=(_FakeCatalog("PII"), True, None))
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
    events.publish_batch = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock())
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)

    class StubCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            return CollectResult(
                specs=[
                    CatalogSpec(
                        entity_name="users",
                        entity_type="TABLE",
                        schema_json={"columns": ["user_name"]},
                    )
                ],
                failed_specs=[],
                source_id="s",
            )

    result = await svc.collect_and_register("s", StubCollector(), actor_id=1)
    assert result["scanned"] == 1
    assert result["registered"] == 1
    assert result["coverage"] == 0.5
    assert result["failed_count"] == 0


async def test_collect_and_register_handles_failed_specs():
    """FR-004: collect_and_register 正确报告 failed_specs。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock())
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)

    class FailingCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            return CollectResult(
                specs=[
                    CatalogSpec(
                        entity_name="t1",
                        entity_type="TABLE",
                        schema_json={"columns": ["a"]},
                    ),
                ],
                failed_specs=[
                    FailedSpec(entity_name="t2", error="timeout"),
                ],
                source_id="s",
            )

    result = await svc.collect_and_register("s", FailingCollector(), actor_id=1)
    assert result["failed_count"] == 1
    assert result["failed_specs"][0]["entity_name"] == "t2"


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
    cat, created, drift_info = await repo.upsert_catalog(
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
    assert cat.content_signature is not None
    assert drift_info is None  # 首次采集无 drift


async def test_repo_upsert_updates_when_exists():
    existing = MagicMock()
    existing.content_signature = "old_sig"
    existing.schema_json = {"columns": ["a"]}
    repo = CollectorRepository(_session(scalar_one_or_none=existing))
    cat, created, drift_info = await repo.upsert_catalog(
        source_id="s",
        entity_name="t",
        entity_type="TABLE",
        schema_json={"columns": ["a", "b"]},
        etl_sql=None,
        sensitivity_level="INTERNAL",
        owner_id=None,
    )
    assert created is False
    assert cat.schema_json == {"columns": ["a", "b"]}


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


async def test_repo_recompute_coverage_zero_expected():
    """P2-3: expected<=0（无配额基线）时 coverage=0.0（覆盖率未知，非误导性 1.0）。"""
    src = MagicMock()
    src.quota = {"max_scan_rows": 0}
    repo = CollectorRepository(_session(scalar_one_or_none=src, scalar=5))
    assert await repo.recompute_coverage("s") == 0.0


async def test_repo_list_sources_no_crash_on_filters():
    repo = CollectorRepository(_session(all_rows=[MagicMock()], scalar=1))
    items, total = await repo.list_sources(
        domain="d", source_type=None, keyword="k", page=1, page_size=10
    )
    assert total == 1
    assert items


async def test_repo_list_catalogs_keyword_table_and_field_level():
    """keyword 为表+字段级：entity_name OR CAST(schema_json) 双条件过滤。"""
    s = _session(all_rows=[MagicMock()], scalar=1)
    repo = CollectorRepository(s)
    params = SimpleNamespace(
        source_id=None, entity_type=None, sensitivity_level=None,
        keyword="order_id", page=1, page_size=20,
    )
    items, total = await repo.list_catalogs(params)
    assert total == 1
    assert items
    stmt = s.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "schema_json" in compiled
    # 关键词含 _ 通配符，转义后为 order\_id（防模糊放大）
    assert "order\\_id" in compiled


async def test_repo_list_catalogs_keyword_escapes_wildcards():
    """LIKE 通配符（% / _）须转义，防模糊放大。"""
    s = _session(all_rows=[], scalar=0)
    repo = CollectorRepository(s)
    params = SimpleNamespace(
        source_id=None, entity_type=None, sensitivity_level=None,
        keyword="100%_x", page=1, page_size=20,
    )
    await repo.list_catalogs(params)
    stmt = s.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "100\\%\\_x" in compiled


# ---------- US6: 空 schema 告警 ----------


async def test_collect_empty_schema_warns_and_marks_incomplete():
    """US6: 采集到空 schema 的表时记录 warning + 标记 schema_incomplete=True。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock(source_type="mysql"))
    repo.get_watermark = AsyncMock(return_value=None)
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)
    repo.update_health_status = AsyncMock()
    repo.update_watermark_after_collection = AsyncMock(
        return_value=MagicMock(mode="FULL")
    )

    class EmptySchemaCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            return CollectResult(
                specs=[
                    CatalogSpec(
                        entity_name="empty_tbl",
                        entity_type="TABLE",
                        schema_json={"columns": []},
                    ),
                    CatalogSpec(
                        entity_name="full_tbl",
                        entity_type="TABLE",
                        schema_json={"columns": ["a"]},
                    ),
                ],
                failed_specs=[],
                source_id="s",
            )

    result = await svc.collect_and_register("s", EmptySchemaCollector(), actor_id=1)
    assert result["scanned"] == 2
    assert result["registered"] == 2


# ---------- US6: connection_config 校验 ----------


def test_connection_config_missing_host_raises():
    """FR-020: connection_config 缺少 host 字段时校验拒绝。"""
    with pytest.raises(ValueError, match="host"):
        DataSourceCreateRequest(
            source_id="src1",
            name="S",
            source_type="mysql",
            connection_config={"port": 3306},  # 缺少 host
            domain="d",
        )


def test_connection_config_with_host_passes():
    """FR-020: connection_config 包含 host 字段时校验通过。"""
    req = DataSourceCreateRequest(
        source_id="src1",
        name="S",
        source_type="mysql",
        connection_config={"host": "127.0.0.1"},
        domain="d",
    )
    assert req.connection_config["host"] == "127.0.0.1"


# ---------- US6: batch 事件发布 ----------


async def test_collect_and_register_publishes_batch_not_individual():
    """FR-024: 采集完成后发布1次batch事件而非逐条publish。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock(source_type="mysql"))
    repo.get_watermark = AsyncMock(return_value=None)
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)
    repo.update_health_status = AsyncMock()
    repo.update_watermark_after_collection = AsyncMock(
        return_value=MagicMock(mode="FULL")
    )

    class MultiSpecCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            return CollectResult(
                specs=[
                    CatalogSpec(
                        entity_name=f"t{i}",
                        entity_type="TABLE",
                        schema_json={"columns": ["a"]},
                    )
                    for i in range(3)
                ],
                failed_specs=[],
                source_id="s",
            )

    await svc.collect_and_register("s", MultiSpecCollector(), actor_id=1)
    # publish_batch 应调用1次，publish 不应调用（collect 路径）
    events.publish_batch.assert_awaited_once()
    events.publish.assert_not_awaited()


# ---------- US6: LLM metric 计数 ----------


def test_llm_classify_error_metric_increments():
    """FR-023: LLM 分类异常记录 llm_classify_error_total metric。"""
    from app.services.collector.service import (
        _llm_classify_error_counts,
        _record_llm_error_metric,
        get_llm_classify_error_total,
    )

    # 重置计数器
    _llm_classify_error_counts.clear()
    _record_llm_error_metric("timeout")
    _record_llm_error_metric("timeout")
    _record_llm_error_metric("format_error")

    counts = get_llm_classify_error_total()
    assert counts.get("timeout") == 2
    assert counts.get("format_error") == 1


# ---------- FR-030: 自动生成 source_id / 连接测试 ----------


async def test_create_source_auto_generates_source_id():
    """source_id 未传时按 类型_库 自动生成。"""
    svc, repo = _svc()
    repo.get_source = AsyncMock(return_value=None)
    repo.create_source = AsyncMock()
    resp = await svc.create_source(
        DataSourceCreateRequest(
            source_id=None,
            name="财务库",
            source_type="mysql",
            connection_config={"host": "h", "database": "finance"},
            domain="sales",
        ),
        actor_id=1,
    )
    assert resp.source_id == "mysql_finance"
    assert repo.create_source.call_args.args[0].source_id == "mysql_finance"


async def test_create_source_auto_source_id_conflict_increments():
    """自动生成的 source_id 冲突时追加 _2 后缀。"""
    svc, repo = _svc()
    # 第一次 get_source 命中（mysql_finance 已存在），第二次返回 None（mysql_finance_2 空闲）
    repo.get_source = AsyncMock(side_effect=[MagicMock(), None])
    repo.create_source = AsyncMock()
    resp = await svc.create_source(
        DataSourceCreateRequest(
            source_id=None,
            name="财务库",
            source_type="mysql",
            connection_config={"host": "h", "database": "finance"},
            domain="sales",
        ),
        actor_id=1,
    )
    assert resp.source_id == "mysql_finance_2"


async def test_create_source_manual_source_id_conflict_still_raises():
    """显式传 source_id 且已存在时仍应冲突报错。"""
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


async def test_test_connection_ok():
    """连接预检成功返回 ok=True + 延迟。"""
    svc, repo = _svc()

    class ProbeOkCollector:
        async def probe(self):
            from app.services.collector.spi import ProbeResult

            return ProbeResult(ok=True, latency_ms=12)

        async def dispose(self):
            return None

    with patch(
        "app.services.collector.connectors.registry.build_from_cfg",
        return_value=ProbeOkCollector(),
    ) as mock_build:
        result = await svc.test_connection("mysql", {"host": "h"})
    mock_build.assert_called_once()
    assert result.ok is True
    assert result.latency_ms == 12
    assert result.error is None


async def test_test_connection_failure_normalized():
    """连接失败归一为 ok=False 结果而非抛出。"""
    svc, repo = _svc()

    class ProbeFailCollector:
        async def probe(self):
            from app.services.collector.spi import ProbeResult

            return ProbeResult(ok=False, latency_ms=5, error="连接被拒绝")

        async def dispose(self):
            return None

    with patch(
        "app.services.collector.connectors.registry.build_from_cfg",
        return_value=ProbeFailCollector(),
    ):
        result = await svc.test_connection("mysql", {"host": "h"})
    assert result.ok is False
    assert "连接被拒绝" in (result.error or "")


async def test_test_connection_unsupported_type_normalized():
    """未注册类型构建失败归一为 ok=False（不抛出 BusinessError）。"""
    svc, repo = _svc()
    with patch(
        "app.services.collector.connectors.registry.build_from_cfg",
        side_effect=BusinessError("不支持的采集器类型: oracle"),
    ):
        result = await svc.test_connection("oracle", {"host": "h"})
    assert result.ok is False
    assert "不支持的采集器类型" in (result.error or "")


async def test_check_connection_updates_health():
    """存量探活成功更新健康状态为 healthy。"""
    svc, repo = _svc()
    src = MagicMock(source_id="s1", source_type="mysql", connection_config="enc")
    repo.get_source = AsyncMock(return_value=src)
    repo.update_health_status = AsyncMock()

    class ProbeOkCollector:
        async def probe(self):
            from app.services.collector.spi import ProbeResult

            return ProbeResult(ok=True, latency_ms=8, detail={"version": "8.0"})

        async def dispose(self):
            return None

    with patch(
        "app.services.collector.connectors.registry.build",
        return_value=ProbeOkCollector(),
    ) as mock_build:
        result = await svc.check_connection("s1")
    mock_build.assert_called_once_with("mysql", "enc")
    repo.update_health_status.assert_awaited_once_with("s1", "healthy", error=None)
    assert result.ok is True
    assert result.detail == {"version": "8.0"}


async def test_check_connection_failure_marks_unhealthy():
    """存量探活失败更新健康状态为 unhealthy。"""
    svc, repo = _svc()
    src = MagicMock(source_id="s1", source_type="mysql", connection_config="enc")
    repo.get_source = AsyncMock(return_value=src)
    repo.update_health_status = AsyncMock()

    class ProbeFailCollector:
        async def probe(self):
            from app.services.collector.spi import ProbeResult

            return ProbeResult(ok=False, latency_ms=0, error="timeout")

        async def dispose(self):
            return None

    with patch(
        "app.services.collector.connectors.registry.build",
        return_value=ProbeFailCollector(),
    ):
        result = await svc.check_connection("s1")
    # P1-3: 失败时回填错误信息到健康状态
    repo.update_health_status.assert_awaited_once_with("s1", "unhealthy", error="timeout")
    assert result.ok is False


async def test_list_source_types_returns_metadata():
    """类型元信息列表覆盖全部注册类型且含中文标签。"""
    svc, repo = _svc()
    info = await svc.list_source_types()
    types = {t.source_type: t for t in info}
    assert set(types.keys()) == {
        "clickhouse", "doris", "hive", "kafka", "mysql", "postgres", "spark", "starrocks",
    }
    assert types["mysql"].label == "MySQL"
    assert types["kafka"].supports_database is False
    assert types["mysql"].default_port == 3306
    assert types["spark"].label == "Spark"
    assert types["spark"].default_port == 10000


# ---------- FR-030: 采集器数据库语义（全库 / 单库） ----------


class _MultiSchemaConnector:
    """模拟多库 information_schema 的假连接器。"""

    def __init__(
        self,
        schemas: list[str],
        tables_by_schema: dict[str, list[str]],
        columns: dict[str, list[str]],
    ) -> None:
        self._schemas = schemas
        self._tables_by_schema = tables_by_schema
        self._columns = columns
        self.queries: list[tuple[str, object]] = []

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        self.queries.append((sql, params))
        if "information_schema.schemata" in sql:
            return [{"schema_name": s} for s in self._schemas]
        if "information_schema.tables" in sql:
            return [{"table_name": t} for t in self._tables_by_schema.get(params.get("schema"), [])]
        if "information_schema.columns" in sql:
            # P1-7: 批量列查询返回 {table_name, column_name} 行（消除 N+1）
            rows = []
            for tbl, cols in self._columns.items():
                for c in cols:
                    rows.append({"table_name": tbl, "column_name": c})
            return rows
        return [{"column_name": c} for c in self._columns.get(params.get("tbl"), [])]

    async def dispose(self):
        return None


async def test_info_schema_collector_all_databases_enumerates():
    """未指定 database 时枚举全部非系统库，entity_name 以 库.表 命名。"""
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    connector = _MultiSchemaConnector(
        schemas=["finance", "sales", "information_schema", "mysql"],
        tables_by_schema={
            "finance": ["orders"],
            "sales": ["gmv"],
            "information_schema": ["TABLES"],
            "mysql": ["user"],
        },
        columns={"orders": ["order_id"], "gmv": ["amount"]},
    )
    collector = InformationSchemaCollector(connector)  # database=None
    result = await collector.collect(MagicMock(source_id="s1"))
    entity_names = {s.entity_name for s in result.specs}
    # 系统库被排除，库.表 命名避免冲突
    assert entity_names == {"finance.orders", "sales.gmv"}
    assert len(result.specs) == 2
    assert result.specs[0].schema_json["columns"]  # 列已采集


async def test_info_schema_collector_single_database_keeps_plain_name():
    """指定 database 时只采该库，entity_name 保持表名（向后兼容）。"""
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    connector = _MultiSchemaConnector(
        schemas=["finance", "sales"],
        tables_by_schema={"finance": ["orders"]},
        columns={"orders": ["order_id"]},
    )
    collector = InformationSchemaCollector(connector, database="finance")
    result = await collector.collect(MagicMock(source_id="s1"))
    assert [s.entity_name for s in result.specs] == ["orders"]


async def test_mysql_probe_returns_ok():
    """MySQL 探活 SELECT 1 成功返回 ok=True。"""
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    class ProbeConnector:
        async def query(self, sql, params=None):
            return [{"1": 1}]

        async def dispose(self):
            return None

    collector = InformationSchemaCollector(ProbeConnector())
    result = await collector.probe()
    assert result.ok is True
    assert result.latency_ms >= 0


async def test_sqlalchemy_connector_normalizes_uppercase_keys():
    """MySQL information_schema 大写列标签被规范化为小写（FR-030 回归防护）。"""
    from app.services.collector.connectors.mysql import SqlalchemyConnector

    class FakeRow:
        _mapping = {"SCHEMA_NAME": "unisense", "TABLE_NAME": "orders"}

    class FakeResult:
        def __init__(self) -> None:
            self._rows = [FakeRow()]

        def __iter__(self):
            return iter(self._rows)

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, params=None):
            return FakeResult()

    class FakeEngine:
        def connect(self):
            return FakeConn()

    connector = SqlalchemyConnector.__new__(SqlalchemyConnector)
    connector._engine = FakeEngine()  # type: ignore[attr-defined]
    connector._query_timeout = 60  # type: ignore[attr-defined]
    rows = await connector.query("SELECT schema_name, table_name FROM information_schema.tables")
    assert rows == [{"schema_name": "unisense", "table_name": "orders"}]


async def test_registry_build_from_cfg_and_type_info():
    """registry.build_from_cfg 支持明文构建；list_type_info 兜底插件类型。"""
    from app.services.collector.connectors import registry

    collector = registry.build_from_cfg("mysql", {"host": "h", "user": "u"})
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    assert isinstance(collector, InformationSchemaCollector)

    info = registry.list_type_info()
    by_type = {t.source_type: t for t in info}
    assert by_type["postgres"].supports_schema is True
    assert by_type["postgres"].default_port == 5432


# ============================================================
# 工业级补齐回归测试（P0-1/2/3/5/6、P1-2/3/5、P2-3/4/5/6/7）
# ============================================================


# ---------- P0-1: 分类器 dict 列格式不误判 PII ----------


def test_classifier_dict_columns_not_misclassified_as_pii():
    """postgres/clickhouse/hive 的 dict 列格式（含字面量 'name' 键）不应误判 PII。"""
    svc = SensitivityClassifier()
    # 列只有 order_id/amount，无任何敏感字段 → 不应因 dict 键 'name' 命中 PII
    assert (
        svc.classify(
            "orders",
            {
                "columns": [
                    {"name": "order_id", "type": "integer"},
                    {"name": "amount", "type": "decimal"},
                ]
            },
        )
        == "INTERNAL"
    )
    # 真实 PII 列（dict 格式含 user_name）仍应判定 PII
    assert (
        svc.classify(
            "users", {"columns": [{"name": "user_name", "type": "varchar"}]}
        )
        == "PII"
    )
    # 混合格式（字符串列 + dict 列）兼容
    assert (
        svc.classify("t", {"columns": ["order_id", {"name": "email", "type": "varchar"}]})
        == "PII"
    )


# ---------- P0-2: LLM content 使用 + NEEDS_REVIEW 大写 ----------


async def test_register_catalog_llm_high_confidence_uses_content():
    """LLM 高置信度（>=0.7）时采用其判定的 content（原实现忽略 content）。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    svc._llm_classify_sensitivity = AsyncMock(
        return_value={"content": "CONFIDENTIAL", "confidence": 0.95}
    )
    repo.upsert_catalog = AsyncMock(return_value=(_FakeCatalog("INTERNAL"), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.0)
    await svc.register_catalog(
        DBCatalogCreateRequest(
            source_id="s", entity_name="orders", schema_def={"columns": ["amount"]}
        ),
        actor_id=1,
    )
    kwargs = repo.upsert_catalog.call_args.kwargs
    assert kwargs["sensitivity_level"] == "CONFIDENTIAL"


async def test_register_catalog_llm_low_confidence_marks_needs_review_uppercase():
    """LLM 低置信度时标记 NEEDS_REVIEW（大写，与 DB ENUM 一致）。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    svc._events = events
    svc._llm_classify_sensitivity = AsyncMock(
        return_value={"content": "PII", "confidence": 0.5}
    )
    repo.upsert_catalog = AsyncMock(return_value=(_FakeCatalog("INTERNAL"), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.0)
    await svc.register_catalog(
        DBCatalogCreateRequest(
            source_id="s", entity_name="orders", schema_def={"columns": ["amount"]}
        ),
        actor_id=1,
    )
    kwargs = repo.upsert_catalog.call_args.kwargs
    assert kwargs["sensitivity_level"] == "NEEDS_REVIEW"


async def test_llm_classify_fallback_client_returns_none():
    """P0-2 回归防护：LLM 降级客户端（content 空 + confidence=0）→ 返回 None，不参与分流。"""
    from app.services.llm.client import DeterministicFallbackLlmClient

    svc, repo = _svc()
    with patch(
        "app.services.llm.client.build_llm_client",
        return_value=DeterministicFallbackLlmClient(),
    ):
        result = await svc._llm_classify_sensitivity("orders", {"columns": ["amount"]})
    assert result is None


async def test_llm_classify_llm_error_returns_none():
    """LlmError（如模型不存在 404）→ 返回 None 不抛异常，登记实体不 500。

    回归：LLM 分类是辅助能力，网关/模型错误必须降级而非阻断主流程。
    """
    from app.services.llm.client import LlmError

    class _FailingClient:
        enabled = True

        async def chat(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise LlmError("LLM 请求失败: 404")

        async def close(self) -> None:
            return None

    svc, _repo = _svc()
    with patch(
        "app.services.llm.client.build_llm_client",
        return_value=_FailingClient(),
    ):
        result = await svc._llm_classify_sensitivity("orders", {"columns": ["amount"]})
    assert result is None


# ---------- P0-3: 软删释放 source_id + IntegrityError 转 409 ----------


async def test_soft_delete_source_releases_id_and_cleans_children():
    """软删时清理子表并把 source_id 改名，释放唯一约束供重建同名源。"""
    src = MagicMock()
    src.source_id = "src1"
    s = _session(scalar_one_or_none=src)
    repo = CollectorRepository(s)
    assert await repo.soft_delete_source("src1") is True
    assert src.source_id != "src1"  # 已改名（__del_{ts} 后缀）
    assert src.deleted_at is not None
    # update(db_catalog) + delete(watermark) + delete(drift_log) 均被调用
    assert s.execute.await_count >= 3


async def test_create_source_integrity_error_returns_conflict():
    """检查-插入竞态下唯一约束冲突归一为 ConflictError（非 500）。"""
    from sqlalchemy.exc import IntegrityError

    svc, repo = _svc()
    repo.get_source = AsyncMock(return_value=None)
    repo.create_source = AsyncMock(
        side_effect=IntegrityError("stmt", {}, Exception("dup"))
    )
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


# ---------- P0-5: Kafka 依赖缺失时 probe 诚实失败 ----------


async def test_kafka_probe_fails_when_kafka_python_missing():
    """kafka-python 未安装时 probe 返回 ok=False（原实现恒假成功）。"""
    from app.services.collector.connectors.kafka import KafkaCollector

    collector = KafkaCollector(bootstrap_servers="kafka:9092")
    with patch.dict("sys.modules", {"kafka": None}):
        result = await collector.probe()
    assert result.ok is False
    assert "kafka-python" in (result.error or "")


# ---------- P0-6: 增量采集真实接入 ----------


async def test_mysql_incremental_collect_uses_change_query():
    """增量模式下 MySQL 采集器使用带 UPDATE_TIME 条件的查询（原实现从未调用）。"""
    from datetime import UTC, datetime

    from app.services.collector.connectors.mysql import InformationSchemaCollector

    connector = _MultiSchemaConnector(
        schemas=["finance"],
        tables_by_schema={"finance": ["orders", "legacy"]},
        columns={"orders": ["order_id"], "legacy": ["id"]},
    )
    collector = InformationSchemaCollector(connector, database="finance")
    collector.set_incremental_context("INCREMENTAL", datetime(2026, 1, 1, tzinfo=UTC))
    result = await collector.collect(MagicMock(source_id="s1"))
    # 增量表查询 SQL 应含 UPDATE_TIME 水位条件
    inc_sqls = [sql for sql, _ in connector.queries if "UPDATE_TIME" in sql]
    assert inc_sqls, "增量模式必须生成带 UPDATE_TIME 条件的表查询"
    assert result.source_id == "s1"


# ---------- P1-2: PostgreSQL 空 schema 全库枚举 ----------


async def test_postgres_collector_all_schemas_enumerates():
    """PostgreSQL schema 为空时枚举全部非系统 schema，entity_name 用 schema.表 命名。"""
    from app.services.collector.connectors.postgres import PostgresCollector

    mock_connector = MagicMock()

    async def fake_query(sql, params=None):
        if "information_schema.schemata" in sql:
            return [
                {"schema_name": "finance"},
                {"schema_name": "sales"},
                {"schema_name": "pg_catalog"},
            ]
        if "information_schema.tables" in sql:
            return [{"table_name": "orders"}]
        return [{"column_name": "id", "data_type": "integer"}]

    mock_connector.query = fake_query
    mock_connector.dispose = AsyncMock()
    collector = PostgresCollector(mock_connector, schema=None)  # 全库枚举
    result = await collector.collect(MagicMock(source_id="pg"))
    assert {s.entity_name for s in result.specs} == {"finance.orders", "sales.orders"}
    # pg_catalog 系统 schema 被排除
    assert len(result.specs) == 2


# ---------- P1-3: 健康端点真实字段 ----------


async def test_get_health_returns_last_error_and_check_time():
    """健康端点返回真实 last_error / last_health_check（原为恒 None / 假 uptime）。"""
    from datetime import UTC, datetime

    svc, repo = _svc()
    src = MagicMock(
        source_id="s1",
        health_status="unhealthy",
        last_error="boom",
        last_health_check=datetime(2026, 1, 1, tzinfo=UTC),
    )
    repo.get_source = AsyncMock(return_value=src)
    repo.get_watermark = AsyncMock(return_value=None)
    health = await svc.get_health("s1")
    assert health["last_error"] == "boom"
    assert health["last_health_check"] is not None
    assert health["health_status"] == "unhealthy"


# ---------- FR-014: 采集水位语义（存在但未采集 ≠ 404） ----------


async def test_get_watermark_returns_empty_for_never_collected():
    """数据源存在但从未采集：返回空水位而非 404（与 get_health 语义一致）。"""
    svc, repo = _svc()
    src = MagicMock(source_id="doris_default", collection_mode="FULL")
    repo.get_source = AsyncMock(return_value=src)
    repo.get_watermark = AsyncMock(return_value=None)
    wm = await svc.get_watermark("doris_default")
    assert wm is not None
    assert wm["source_id"] == "doris_default"
    assert wm["last_collected_at"] is None
    assert wm["mode"] == "FULL"
    assert wm["scanned_count"] == 0
    assert wm["failed_count"] == 0


async def test_get_watermark_raises_for_missing_source():
    """数据源不存在：404（NotFoundError）。"""
    from app.core.exceptions import NotFoundError

    svc, repo = _svc()
    repo.get_source = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.get_watermark("ghost")


async def test_get_watermark_returns_record_when_collected():
    """数据源已采集：返回真实水位记录。"""
    from datetime import UTC, datetime

    svc, repo = _svc()
    src = MagicMock(source_id="s1", collection_mode="FULL")
    repo.get_source = AsyncMock(return_value=src)
    wm = MagicMock(
        source_id="s1",
        last_collected_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
        mode="INCREMENTAL",
        scanned_count=10,
        failed_count=1,
    )
    repo.get_watermark = AsyncMock(return_value=wm)
    out = await svc.get_watermark("s1")
    assert out["last_collected_at"] == "2026-08-01T12:00:00+00:00"
    assert out["mode"] == "INCREMENTAL"
    assert out["scanned_count"] == 10
    assert out["failed_count"] == 1


# ---------- P1-5: ClickHouse 密码经 Basic Auth ----------


async def test_clickhouse_query_uses_basic_auth_not_query_param():
    """ClickHouse 密码经 HTTP Basic Auth 传递，不进入 URL query 参数。"""
    from app.services.collector.connectors.clickhouse import ClickHouseCollector

    collector = ClickHouseCollector(host="ch", port=8123, user="u", password="secret")
    captured: dict = {}

    async def fake_get(url, params=None, auth=None):
        captured["params"] = dict(params or {})
        captured["auth"] = auth

        class _R:
            text = "1"

            def raise_for_status(self):
                return None

        return _R()

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=fake_get
        )
        await collector._query("SELECT 1")
    assert "password" not in captured["params"]
    assert "user" not in captured["params"]
    assert captured["auth"] == ("u", "secret")


# ---------- P2-3: coverage 无配额语义 ----------


async def test_repo_recompute_coverage_no_quota_is_unknown():
    """无 quota 基线时 coverage=0.0（覆盖率未知），非误导性 1.0。"""
    src = MagicMock()
    src.quota = {}
    repo = CollectorRepository(_session(scalar_one_or_none=src, scalar=5))
    assert await repo.recompute_coverage("s") == 0.0


# ---------- P2-4: watermark fingerprints 写入 ----------


async def test_watermark_persists_content_fingerprints():
    """采集水位持久化实体级内容指纹（P2-4：此前声明但从不写入）。"""
    repo = CollectorRepository(_session(scalar_one_or_none=None))
    wm = await repo.update_watermark_after_collection(
        "s", "FULL", 2, 0, content_fingerprints={"t1": "sig1", "t2": "sig2"}
    )
    assert wm.content_fingerprints == {"t1": "sig1", "t2": "sig2"}


# ---------- P2-5: Kafka SASL/SSL 连接参数 ----------


def test_kafka_admin_kwargs_include_sasl():
    """Kafka 采集器透传 SASL/SSL 连接参数（P2-5 生产安全连接）。"""
    from app.services.collector.connectors.kafka import KafkaCollector

    c = KafkaCollector(
        bootstrap_servers="k:9092",
        security_protocol="SASL_SSL",
        sasl_mechanism="PLAIN",
        sasl_username="u",
        sasl_password="p",
        ssl_cafile="/ca.pem",
    )
    kwargs = c._admin_kwargs()
    assert kwargs["security_protocol"] == "SASL_SSL"
    assert kwargs["sasl_mechanism"] == "PLAIN"
    assert kwargs["sasl_plain_username"] == "u"
    assert kwargs["sasl_plain_password"] == "p"
    assert kwargs["ssl_cafile"] == "/ca.pem"


# ---------- P2-6: _safe_ident 允许连字符 ----------


def test_clickhouse_safe_ident_allows_dash():
    from app.core.exceptions import ExternalDependencyError
    from app.services.collector.connectors.clickhouse import ClickHouseCollector

    assert ClickHouseCollector._safe_ident("orders-2026") == "orders-2026"
    with pytest.raises(ExternalDependencyError):
        ClickHouseCollector._safe_ident("db.table")  # 不允许 '.'（分隔符）


def test_hive_safe_ident_allows_dash():
    from app.services.collector.connectors.hive import HiveCollector

    assert HiveCollector._safe_ident("orders-2026") == "orders-2026"


# ---------- P2-7: kafka 连接配置按类型校验 ----------


def test_kafka_connection_config_requires_bootstrap_or_host():
    """kafka 类型必须提供 bootstrap_servers 或 host（语义错位修复）。"""
    with pytest.raises(ValueError, match="bootstrap_servers"):
        DataSourceCreateRequest(
            source_id="k1",
            name="K",
            source_type="kafka",
            connection_config={"user": "u"},
            domain="d",
        )


def test_kafka_connection_config_with_host_passes():
    req = DataSourceCreateRequest(
        source_id="k1",
        name="K",
        source_type="kafka",
        connection_config={"host": "kafka:9092"},
        domain="d",
    )
    assert req.connection_config["host"] == "kafka:9092"
