"""采集领域单元测试（对齐 DEV_GUIDE §8b / gateways unit）。

覆盖：敏感分级规则引擎、SPI 采集器（含外部依赖失败转化为重试型错误）、
服务层（加密/脱敏/幂等/批量废弃/采集编排）、仓储层（upsert 幂等/批量部分失败/
覆盖率重算）。无外部依赖（repo 以 mock 注入；集成测试覆盖真实 MySQL）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import BusinessError, ConflictError, NotFoundError
from app.core.secrets import SecretManager
from app.services.collector.classifier import SensitivityClassifier
from app.services.collector.drift_detector import compute_content_signature
from app.services.collector.repository import CollectorRepository
from app.services.collector.schemas import (
    BulkDeprecateItem,
    BulkDeprecateRequest,
    DataSourceCreateRequest,
    DataSourceUpdateRequest,
    DBCatalogCreateRequest,
    TestConnectionRequest,
)
from app.services.collector.service import CollectorService
from app.services.collector.spi import (
    BaseCollector,
    CatalogSpec,
    CollectResult,
    FailedSpec,
    build_collector,
)

# 以 Test 开头的 Pydantic 模型名会被 pytest 误判为测试类收集，显式排除
TestConnectionRequest.__test__ = False


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
        repo.update_watermark_after_collection = AsyncMock(return_value=MagicMock(mode="FULL"))
        # P1-5: 对账相关方法默认 mock（空存活实体 → 不过期任何表）
        repo.list_active_entity_names = AsyncMock(return_value=[])
        repo.deprecate_catalog = AsyncMock(return_value=False)
        # PII 合规增强：字段级命中明细落库方法默认 AsyncMock
        repo.upsert_classification = AsyncMock()
        return svc, repo


class _FakeCatalog:
    """用于 DBCatalogResponse.model_validate 的轻量替身（含 schema_json 列名）。"""

    def __init__(self, sensitivity: str) -> None:
        self.id = 1
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
        self.updated_at = None


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


def test_classifier_pii_by_column_comment():
    """P0 修复：列注释含敏感词即触发 PII——修复前注释未被采集，此类列被误判 INTERNAL。

    典型生产场景：字段名是通用编码（c1/user_info），业务含义写在注释里
    （「用户手机号」「客户邮箱」）。
    """
    assert (
        SensitivityClassifier().classify(
            "user_profile",
            {"columns": [{"name": "c1", "type": "varchar", "comment": "用户手机号"}]},
        )
        == "PII"
    )
    assert (
        SensitivityClassifier().classify(
            "t",
            {"columns": [{"name": "field_a", "type": "varchar", "comment": "客户邮箱地址"}]},
        )
        == "PII"
    )


def test_classifier_comment_does_not_pollute_generic_names():
    """注释匹配不应把通用注释（如「创建时间」）误判为 PII/CONFIDENTIAL。"""
    assert (
        SensitivityClassifier().classify(
            "orders",
            {"columns": [{"name": "created_at", "type": "datetime", "comment": "创建时间"}]},
        )
        == "INTERNAL"
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
    # 落库为 Fernet 密文（token 前缀 gAAAA），不含明文
    assert created.connection_config != '{"host":"h","password":"p"}'
    assert created.connection_config.startswith("gAAAA")


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
    assert src.connection_config.startswith("gAAAA")  # Fernet 密文格式
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


async def test_collect_and_register_quota_truncates_specs():
    """P1-4: max_scan_rows 配额按表数截断注册清单（防超大源端拖垮注册）。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    src = MagicMock(source_type="mysql", quota={"max_scan_rows": 2})
    src.include_patterns = None
    src.exclude_patterns = None
    src.databases = None
    src.health_metrics = None
    repo.get_source = AsyncMock(return_value=src)
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True, None))
    repo.recompute_coverage = AsyncMock(return_value=1.0)
    repo.update_watermark_after_collection = AsyncMock(return_value=MagicMock(mode="FULL"))
    repo.list_active_entity_names = AsyncMock(return_value=[])
    repo.update_health_status = AsyncMock()

    class ThreeTablesCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            return CollectResult(
                specs=[
                    CatalogSpec(
                        entity_name="t1",
                        entity_type="TABLE",
                        schema_json={"columns": ["x"]},
                    ),
                    CatalogSpec(
                        entity_name="t2",
                        entity_type="TABLE",
                        schema_json={"columns": ["x"]},
                    ),
                    CatalogSpec(
                        entity_name="t3",
                        entity_type="TABLE",
                        schema_json={"columns": ["x"]},
                    ),
                ],
                failed_specs=[],
                source_id="s",
            )

    result = await svc.collect_and_register("s", ThreeTablesCollector(), actor_id=1)

    # 源端 3 个实体，配额 2 → 截断 1 个，仅注册前 2 个
    assert result["quota_truncated"] == 1
    assert result["scanned"] == 2
    assert result["registered"] == 2
    assert result["failed_count"] == 0


async def test_collect_and_register_no_quota_no_truncation():
    """P1-4: 未配置配额（quota={}）不截断——全部注册。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    src = MagicMock(source_type="mysql", quota={})
    src.include_patterns = None
    src.exclude_patterns = None
    src.databases = None
    src.health_metrics = None
    repo.get_source = AsyncMock(return_value=src)
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True, None))
    repo.recompute_coverage = AsyncMock(return_value=1.0)
    repo.update_watermark_after_collection = AsyncMock(return_value=MagicMock(mode="FULL"))
    repo.list_active_entity_names = AsyncMock(return_value=[])
    repo.update_health_status = AsyncMock()

    class TwoTablesCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            return CollectResult(
                specs=[
                    CatalogSpec(
                        entity_name="t1",
                        entity_type="TABLE",
                        schema_json={"columns": ["x"]},
                    ),
                    CatalogSpec(
                        entity_name="t2",
                        entity_type="TABLE",
                        schema_json={"columns": ["x"]},
                    ),
                ],
                failed_specs=[],
                source_id="s",
            )

    result = await svc.collect_and_register("s", TwoTablesCollector(), actor_id=1)

    assert result["quota_truncated"] == 0
    assert result["scanned"] == 2
    assert result["registered"] == 2


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


async def test_collect_and_register_deprecates_dropped_tables_full_mode():
    """P1-5: 全量采集后，catalog 中源端已 drop 的实体被标记为 DEPRECATED。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock())
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(content_signature="sig"), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)
    # 源端现有 users，但 catalog 中还残留已删除的 legacy_table
    repo.list_active_entity_names = AsyncMock(return_value=["users", "legacy_table"])
    repo.deprecate_catalog = AsyncMock(return_value=True)

    class OnlyUsersCollector:
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

    result = await svc.collect_and_register("s", OnlyUsersCollector(), actor_id=1, mode="FULL")
    assert result["deprecated_count"] == 1
    # 仅已 drop 的 legacy_table 被废弃，存活的 users 不被触碰
    repo.deprecate_catalog.assert_awaited_once_with("s", "legacy_table")


async def test_collect_and_register_refreshes_coverage_baseline():
    """采集完成后 coverage 基线刷新为本次扫描实体数（TD §2051 分母）。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock())
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(content_signature="sig"), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)
    repo.list_active_entity_names = AsyncMock(return_value=["users"])
    repo.deprecate_catalog = AsyncMock(return_value=False)

    class TwoTablesCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            return CollectResult(
                specs=[
                    CatalogSpec(
                        entity_name="t1", entity_type="TABLE", schema_json={"columns": ["a"]}
                    ),
                    CatalogSpec(
                        entity_name="t2", entity_type="TABLE", schema_json={"columns": ["b"]}
                    ),
                ],
                failed_specs=[],
                source_id="s",
            )

    await svc.collect_and_register("s", TwoTablesCollector(), actor_id=1, mode="FULL")
    repo.recompute_coverage.assert_awaited_once_with("s", total_entities=2)


async def test_collect_and_register_triggers_dsd_on_dropped_tables():
    """P1-4: 全量采集检测到源表 DROP → 沿血缘把下游指标置 DSD（PRD R3-04④ 接线）。

    断言以管理角色、精确到 drop 表名调用 mark_source_dropped，且 dsd_count 进结果。
    """
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock())
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(content_signature="sig"), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)
    # 源端现有 users，但 catalog 中还残留已删除的 legacy_table
    repo.list_active_entity_names = AsyncMock(return_value=["users", "legacy_table"])
    repo.deprecate_catalog = AsyncMock(return_value=True)

    class OnlyUsersCollector:
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

    with patch("app.services.semantic.service.MetricService") as mock_metric_svc:
        mock_metric_svc.return_value.mark_source_dropped = AsyncMock(return_value=2)
        result = await svc.collect_and_register(
            "s", OnlyUsersCollector(), actor_id=7, mode="FULL"
        )

    assert result["dsd_count"] == 2
    # 以管理角色、仅精确到本次 drop 的表名触发（不误伤同源未 drop 表的下游）
    mock_metric_svc.return_value.mark_source_dropped.assert_awaited_once_with(
        ["s"], actor_id=7, role="platform_admin", entity_names=["legacy_table"]
    )


async def test_collect_reconcile_excludes_filtered_and_truncated_tables():
    """HIGH-1: include/exclude 过滤 + 配额截断的表不被误判为源端已 DROP。

    仅真正从源端消失的表进入 dropped_names（DSD + 目录废弃），
    避免假 DROP 批量废弃目录与下游指标置 DATA_SOURCE_DROPPED（数据事故）。
    """
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    src = MagicMock(source_type="mysql", quota={"max_scan_rows": 1})
    src.include_patterns = None
    src.exclude_patterns = None
    src.databases = None
    src.health_metrics = None
    repo.get_source = AsyncMock(return_value=src)
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(content_signature="sig"), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)
    # 源端活跃实体：t1(本次保留) t2/t3(配额截断) t4(include/exclude 过滤) t5(真正消失)
    repo.list_active_entity_names = AsyncMock(return_value=["t1", "t2", "t3", "t4", "t5"])
    repo.deprecate_catalog = AsyncMock(return_value=True)

    class ReconcileCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            return CollectResult(
                specs=[
                    CatalogSpec(
                            entity_name="t1",
                            entity_type="TABLE",
                            schema_json={"columns": ["x"]},
                        ),
                    CatalogSpec(
                            entity_name="t2",
                            entity_type="TABLE",
                            schema_json={"columns": ["x"]},
                        ),
                    CatalogSpec(
                            entity_name="t3",
                            entity_type="TABLE",
                            schema_json={"columns": ["x"]},
                        ),
                ],
                filtered_names=["t4"],
                failed_specs=[],
                source_id="s",
            )

    with patch("app.services.semantic.service.MetricService") as mock_metric_svc:
        mock_metric_svc.return_value.mark_source_dropped = AsyncMock(return_value=1)
        result = await svc.collect_and_register("s", ReconcileCollector(), actor_id=7, mode="FULL")

    # 仅 t5（真正消失）触发 DSD + 目录废弃；t2/t3(截断) t4(过滤) 不被误判
    mock_metric_svc.return_value.mark_source_dropped.assert_awaited_once_with(
        ["s"], actor_id=7, role="platform_admin", entity_names=["t5"]
    )
    repo.deprecate_catalog.assert_awaited_once_with("s", "t5")
    assert result["quota_truncated"] == 2
    assert result["dsd_count"] == 1


async def test_collect_incremental_does_not_override_coverage_baseline():
    """HIGH-2: 增量模式不覆盖 coverage 基线（total_entities=None），仅 FULL 刷新。

    增量只采变更实体，若用变更数覆盖基线会把 coverage 压缩失真为 1.0。
    """
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    src = MagicMock(source_type="clickhouse", quota={})
    src.include_patterns = None
    src.exclude_patterns = None
    src.databases = None
    src.health_metrics = None
    repo.get_source = AsyncMock(return_value=src)
    repo.get_watermark = AsyncMock(
        return_value=MagicMock(last_collected_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(content_signature="sig"), True, None))
    repo.recompute_coverage = AsyncMock(return_value=1.0)

    class SingleTableCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            return CollectResult(
                specs=[
                    CatalogSpec(
                            entity_name="t1",
                            entity_type="TABLE",
                            schema_json={"columns": ["x"]},
                        ),
                ],
                failed_specs=[],
                source_id="s",
            )

    await svc.collect_and_register("s", SingleTableCollector(), actor_id=1, mode="INCREMENTAL")
    # 增量：total_entities=None → 沿用存量基线，不覆盖
    repo.recompute_coverage.assert_awaited_once_with("s", total_entities=None)


async def test_collect_and_register_dsd_failure_does_not_break_collect():
    """P1-4: DSD 标记失败（血缘不可用/服务异常）不阻断采集主流程，dsd_count 归 0。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock())
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(content_signature="sig"), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)
    repo.list_active_entity_names = AsyncMock(return_value=["users", "legacy_table"])
    repo.deprecate_catalog = AsyncMock(return_value=True)

    class OnlyUsersCollector:
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

    with patch("app.services.semantic.service.MetricService") as mock_metric_svc:
        mock_metric_svc.return_value.mark_source_dropped = AsyncMock(
            side_effect=RuntimeError("lineage down")
        )
        result = await svc.collect_and_register(
            "s", OnlyUsersCollector(), actor_id=1, mode="FULL"
        )

    # 采集主流程不中断：废弃照常、DSD 计数归 0
    assert result["dsd_count"] == 0
    assert result["deprecated_count"] == 1
    repo.deprecate_catalog.assert_awaited_once_with("s", "legacy_table")


async def test_collect_and_register_no_deprecate_in_incremental_mode():
    """P1-5: 增量模式不触发对账废弃（仅覆盖变更实体，避免误废未扫描实体）。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock(source_type="mysql"))
    repo.get_watermark = AsyncMock(
        return_value=MagicMock(last_collected_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(content_signature="sig"), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)
    repo.list_active_entity_names = AsyncMock(return_value=["users", "legacy_table"])
    repo.deprecate_catalog = AsyncMock(return_value=True)

    class OnlyUsersCollector:
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

    result = await svc.collect_and_register(
        "s", OnlyUsersCollector(), actor_id=1, mode="INCREMENTAL"
    )
    assert result["deprecated_count"] == 0
    repo.deprecate_catalog.assert_not_awaited()


# ---------- 仓储层（mock session） ----------


class _AsyncCM:
    """异步上下文管理器桩（begin_nested 成功路径：__aexit__ 不抛异常）。"""

    async def __aenter__(self) -> _AsyncCM:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


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
    s.begin_nested = MagicMock(return_value=_AsyncCM())
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


async def test_repo_upsert_concurrent_duplicate_falls_back_to_update():
    """并发采集竞态：两个事务都读到 existing=None，INSERT 撞唯一键 uk_db_catalog_entity。

    SAVEPOINT 回滚后应重查走更新语义（created=False），而不是抛 IntegrityError
    污染会话致整批采集失败（回归：Duplicate entry ... for key 'uk_db_catalog_entity'）。
    """
    s = _session()
    # INSERT flush 撞唯一键（仅首次；savepoint 回滚后更新路径 flush 正常）
    s.flush = AsyncMock(
        side_effect=[IntegrityError("INSERT", {}, Exception("duplicate key")), None]
    )
    repo = CollectorRepository(s)
    existing = MagicMock()
    existing.content_signature = "old_sig"
    existing.schema_json = {"columns": ["a"]}
    # 首次活跃查询返回 None；含软删重查首次也 None（MVCC 旧快照未见并发事务），
    # 撞键后再次重查返回并发事务已提交的行
    repo.get_catalog = AsyncMock(return_value=None)
    repo.get_catalog_any_status = AsyncMock(side_effect=[None, existing])
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
    assert repo.get_catalog_any_status.await_count == 2
    # 更新路径生效：schema 与内容指纹被刷新
    assert existing.schema_json == {"columns": ["a", "b"]}
    assert existing.content_signature == compute_content_signature(
        {"columns": ["a", "b"]}
    )
    # SAVEPOINT 已回滚且会话未被污染（后续仍可正常 flush）
    assert s.begin_nested.call_count == 1


async def test_repo_upsert_reactivates_soft_deleted():
    """源端表重现：活跃行缺失但软删行占着幂等键 uk_db_catalog_entity。

    应复用软删行（清 deleted_at 重新激活）走更新语义，而不是 INSERT 撞软删行
    的唯一键抛 IntegrityError（回归：Duplicate entry ... for key 'uk_db_catalog_entity'）。
    """
    s = _session()
    soft = MagicMock()
    soft.deleted_at = datetime(2026, 8, 14, 10, 1, 40, tzinfo=UTC)
    soft.content_signature = "old_sig"
    soft.schema_json = {"columns": ["a"]}
    repo = CollectorRepository(s)
    # 主路径活跃查询返回 None；含软删查询返回软删行
    repo.get_catalog = AsyncMock(return_value=None)
    repo.get_catalog_any_status = AsyncMock(return_value=soft)
    cat, created, drift_info = await repo.upsert_catalog(
        source_id="s",
        entity_name="t",
        entity_type="TABLE",
        schema_json={"columns": ["a", "b"]},
        etl_sql=None,
        sensitivity_level="INTERNAL",
        owner_id=None,
    )
    assert created is False  # 复用而非新建
    assert soft.deleted_at is None  # 软删标记已清除（重新激活）
    assert cat.schema_json == {"columns": ["a", "b"]}
    # 未走 INSERT 路径（无 SAVEPOINT），软删行直接复用
    assert s.begin_nested.call_count == 0


async def test_repo_upsert_creates_with_description():
    """HMS 直连：首次采集带表描述 → 写入 description + description_source=schema。"""
    repo = CollectorRepository(_session(scalar_one_or_none=None))
    cat, created, _drift = await repo.upsert_catalog(
        source_id="s",
        entity_name="t",
        entity_type="TABLE",
        schema_json={"columns": ["a"]},
        etl_sql=None,
        sensitivity_level="INTERNAL",
        owner_id=None,
        description="订单明细表",
    )
    assert created is True
    assert cat.description == "订单明细表"
    assert cat.description_source == "schema"


async def test_repo_upsert_updates_description_when_changed():
    """表描述变化（非人工）→ 更新 description 并置 description_source=schema。"""
    existing = MagicMock()
    existing.content_signature = "old_sig"
    existing.schema_json = {"columns": ["a"]}
    existing.description = "旧描述"
    existing.description_source = "schema"
    repo = CollectorRepository(_session(scalar_one_or_none=existing))
    cat, created, drift_info = await repo.upsert_catalog(
        source_id="s",
        entity_name="t",
        entity_type="TABLE",
        schema_json={"columns": ["a"]},
        etl_sql=None,
        sensitivity_level="INTERNAL",
        owner_id=None,
        description="新描述",
    )
    assert created is False
    assert cat.description == "新描述"
    assert cat.description_source == "schema"


async def test_repo_upsert_does_not_overwrite_manual_description():
    """人工编辑的表描述不被采集覆盖（desc_changed 判定 + 更新路径保护）。"""
    existing = MagicMock()
    existing.content_signature = "old_sig"
    existing.schema_json = {"columns": ["a"]}
    existing.description = "人工填写的描述"
    existing.description_source = "manual"
    repo = CollectorRepository(_session(scalar_one_or_none=existing))
    cat, created, drift_info = await repo.upsert_catalog(
        source_id="s",
        entity_name="t",
        entity_type="TABLE",
        schema_json={"columns": ["a"]},
        etl_sql=None,
        sensitivity_level="INTERNAL",
        owner_id=None,
        description="采集到的描述",
    )
    # desc_changed=False（manual 保护）→ 短路，不写库、不覆盖
    assert cat.description == "人工填写的描述"
    assert cat.description_source == "manual"
    assert created is False


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


async def test_repo_set_source_enabled():
    src = MagicMock()
    src.enabled = False
    repo = CollectorRepository(_session(scalar_one_or_none=src))
    result = await repo.set_source_enabled("s", True)
    assert result is src
    assert src.enabled is True
    repo._db.flush.assert_awaited()


async def test_repo_set_source_enabled_not_found():
    repo = CollectorRepository(_session(scalar_one_or_none=None))
    assert await repo.set_source_enabled("s", True) is None


async def test_repo_recompute_coverage_uses_total_entities():
    """TD §2051: coverage = 已采集实体 / 源端实体总数基线。"""
    src = MagicMock()
    src.source_total_entities = 4
    repo = CollectorRepository(_session(scalar_one_or_none=src, scalar=3))
    assert await repo.recompute_coverage("s") == 0.75


async def test_repo_recompute_coverage_refreshes_baseline():
    """提供 total_entities 时（采集完成）刷新基线并计算。"""
    src = MagicMock()
    src.source_total_entities = 4
    repo = CollectorRepository(_session(scalar_one_or_none=src, scalar=3))
    assert await repo.recompute_coverage("s", total_entities=10) == 0.3
    assert src.source_total_entities == 10
    assert src.coverage == 0.3


async def test_repo_recompute_coverage_zero_baseline():
    """基线<=0（从未采集/源端扫描数为 0）时 coverage=0.0（覆盖率未知）。"""
    src = MagicMock()
    src.source_total_entities = 0
    repo = CollectorRepository(_session(scalar_one_or_none=src, scalar=5))
    assert await repo.recompute_coverage("s") == 0.0


async def test_repo_list_sources_no_crash_on_filters():
    repo = CollectorRepository(_session(all_rows=[MagicMock()], scalar=1))
    items, total = await repo.list_sources(
        domain="d", source_type=None, keyword="k", page=1, page_size=10
    )
    assert total == 1
    assert items


async def test_repo_list_sources_filters_by_source_status():
    """source_status=deleted 时查已软删源，默认仅活跃源（deleted_at IS NULL）。"""
    captured: dict[str, object] = {}
    s = _session(all_rows=[MagicMock()], scalar=1)
    res = s.execute.return_value

    async def _capture(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        return res

    s.execute = _capture
    repo = CollectorRepository(s)

    # deleted → 查已软删源
    await repo.list_sources(
        domain=None, source_type=None, keyword=None,
        source_status="deleted", page=1, page_size=10,
    )
    sql = str(captured["stmt"].compile(compile_kwargs={"literal_binds": True}))
    assert "deleted_at IS NOT NULL" in sql

    # 默认（None）→ 仅活跃源（保持既有行为）
    await repo.list_sources(
        domain=None, source_type=None, keyword=None,
        page=1, page_size=10,
    )
    sql = str(captured["stmt"].compile(compile_kwargs={"literal_binds": True}))
    assert "deleted_at IS NULL" in sql


async def test_repo_list_scheduled_sources_filters_disabled() -> None:
    """list_scheduled_sources 仅返回启用中（enabled=True）的源，停用源不进定时调度。"""
    captured: dict[str, object] = {}
    s = _session(all_rows=[MagicMock()])
    res = s.execute.return_value  # _session 生成的 AsyncMock 返回值

    async def _capture(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        return res

    s.execute = _capture
    repo = CollectorRepository(s)

    await repo.list_scheduled_sources()

    stmt = captured["stmt"]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "enabled" in sql.lower()


async def test_repo_list_catalogs_keyword_table_and_field_level():
    """keyword 为表+字段级：entity_name OR CAST(schema_json) 双条件过滤。"""
    s = _session(all_rows=[MagicMock()], scalar=1)
    repo = CollectorRepository(s)
    params = SimpleNamespace(
        source_id=None,
        entity_type=None,
        sensitivity_level=None,
        keyword="order_id",
        page=1,
        page_size=20,
    )
    items, total = await repo.list_catalogs(params)
    assert total == 1
    assert items
    stmt = s.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "schema_json" in compiled
    # 关键词含 _ 通配符，转义后为 order/_id（防模糊放大，escape="/" 生成 ESCAPE '/'）
    assert "order/_id" in compiled


async def test_repo_list_catalogs_keyword_escapes_wildcards():
    """LIKE 通配符（% / _）须转义，防模糊放大。"""
    s = _session(all_rows=[], scalar=0)
    repo = CollectorRepository(s)
    params = SimpleNamespace(
        source_id=None,
        entity_type=None,
        sensitivity_level=None,
        keyword="100%_x",
        page=1,
        page_size=20,
    )
    await repo.list_catalogs(params)
    stmt = s.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "100/%/_x" in compiled


async def test_repo_list_catalogs_filters_by_database_prefix():
    """database 参数按 entity_name 前缀（库.表）过滤，且通配符转义。"""
    s = _session(all_rows=[], scalar=0)
    repo = CollectorRepository(s)
    params = SimpleNamespace(
        source_id=None,
        entity_type=None,
        sensitivity_level=None,
        database="sales_%",
        keyword=None,
        page=1,
        page_size=20,
    )
    await repo.list_catalogs(params)
    stmt = s.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    # 库名转义后为 sales/_/%，并带前缀匹配 sales/_/%.%（防模糊放大，escape="/"）
    assert "sales/_/%" in compiled
    assert ".%" in compiled


async def test_repo_list_catalogs_filters_by_domain():
    """domain 参数经数据源继承过滤（JOIN data_source + WHERE domain）。"""
    s = _session(all_rows=[], scalar=0)
    repo = CollectorRepository(s)
    params = SimpleNamespace(
        source_id=None,
        entity_type=None,
        sensitivity_level=None,
        domain="sales",
        database=None,
        keyword=None,
        page=1,
        page_size=20,
    )
    await repo.list_catalogs(params)
    stmt = s.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    # db_catalog 无 domain 列，须 JOIN data_source 并按 domain 过滤
    assert "JOIN" in compiled.upper()
    assert "data_source" in compiled.lower()
    assert "'sales'" in compiled


async def test_repo_list_catalogs_filters_pending_review():
    """pending_review 过滤：sensitivity IN (PII,CONFIDENTIAL) 且未合规复核。"""
    s = _session(all_rows=[], scalar=0)
    repo = CollectorRepository(s)
    params = SimpleNamespace(
        source_id=None,
        entity_type=None,
        sensitivity_level=None,
        keyword=None,
        database=None,
        pending_review=True,
        page=1,
        page_size=20,
    )
    await repo.list_catalogs(params)
    stmt = s.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "PII" in compiled and "CONFIDENTIAL" in compiled
    assert "compliance_reviewed" in compiled and "0" in compiled
    # 缺省（False/None）不加过滤
    s2 = _session(all_rows=[], scalar=0)
    repo2 = CollectorRepository(s2)
    await repo2.list_catalogs(
        SimpleNamespace(
            source_id=None,
            entity_type=None,
            sensitivity_level=None,
            keyword=None,
            database=None,
            page=1,
            page_size=20,
        )
    )
    stmt2 = s2.execute.call_args_list[0].args[0]
    compiled2 = str(stmt2.compile(compile_kwargs={"literal_binds": True}))
    # 缺省不加过滤：WHERE 不含 PII/CONFIDENTIAL 常量（列名在 SELECT 中恒存在，不作断言）
    assert "PII" not in compiled2 and "CONFIDENTIAL" not in compiled2


async def test_repo_list_catalog_databases_returns_distinct_prefix():
    """list_catalog_databases 返回去重库名（entity_name 前缀），可随 source_id 过滤。"""
    s = MagicMock()
    res = MagicMock()
    res.all.return_value = [("unisense.a",), ("unisense.b",), ("sales.c",), ("kafka_topic",)]
    s.execute = AsyncMock(return_value=res)
    repo = CollectorRepository(s)
    dbs = await repo.list_catalog_databases()
    assert dbs == ["sales", "unisense"]
    # 无 "." 前缀的实体（Kafka topic）不计入
    assert "kafka_topic" not in dbs


async def test_repo_list_catalog_databases_filters_by_source():
    """指定 source_id 时仅统计该源下的库名。"""
    s = MagicMock()
    res = MagicMock()
    res.all.return_value = [("sales.c",)]
    s.execute = AsyncMock(return_value=res)
    repo = CollectorRepository(s)
    dbs = await repo.list_catalog_databases(source_id="mysql_sales")
    assert dbs == ["sales"]
    stmt = s.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "mysql_sales" in compiled


async def test_repo_list_catalog_databases_filters_by_source_status():
    """source_status=active/deleted 时按源删除状态过滤库名（与列表默认「活跃源」对齐）。"""
    s = MagicMock()
    res = MagicMock()
    res.all.return_value = [("sales.c",)]
    s.execute = AsyncMock(return_value=res)
    repo = CollectorRepository(s)
    # active：outerjoin DataSource 且仅活跃源（deleted_at IS NULL）
    dbs = await repo.list_catalog_databases(source_status="active")
    assert dbs == ["sales"]
    stmt = s.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "data_source" in compiled.lower()
    assert "deleted_at" in compiled.lower()
    # deleted：仅已删源（deleted_at IS NOT NULL）
    s.execute.reset_mock()
    await repo.list_catalog_databases(source_status="deleted")
    deleted_stmt = s.execute.call_args_list[0].args[0].compile(
        compile_kwargs={"literal_binds": True}
    )
    assert "is not null" in str(deleted_stmt).lower()


async def test_repo_list_catalog_databases_source_status_none_keeps_plain_query():
    """source_status 缺省/None 时不过滤源状态（保持既有行为，不 join DataSource）。"""
    s = MagicMock()
    res = MagicMock()
    res.all.return_value = [("sales.c",)]
    s.execute = AsyncMock(return_value=res)
    repo = CollectorRepository(s)
    await repo.list_catalog_databases()
    stmt = s.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "data_source" not in compiled


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
    repo.update_watermark_after_collection = AsyncMock(return_value=MagicMock(mode="FULL"))

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


def test_hive_metastore_type_accepted_by_schemas():
    """回归：SourceTypeEnum 曾缺 hive_metastore，连接预检/创建/枚举请求全部 422。

    Hive Metastore 采集器与前端类型列表均已支持，但共享枚举遗漏导致
    ``TestConnectionRequest.source_type`` 等用 ``SourceType`` 校验的请求直接 422
    （修复见 enums.py + 迁移 0085）。本测试锁定该类型在三类请求 schema 中均可解析。
    """
    cfg = {"host": "10.0.0.5", "port": 3306, "database": "metastore"}
    # 连接预检（test-connection）
    probe_req = TestConnectionRequest(source_type="hive_metastore", connection_config=cfg)
    assert probe_req.source_type == "hive_metastore"
    # 创建数据源
    create_req = DataSourceCreateRequest(
        source_id="hms1",
        name="HMS",
        source_type="hive_metastore",
        connection_config=cfg,
        domain="d",
    )
    assert create_req.source_type == "hive_metastore"
    # 更新数据源（PATCH 语义下 source_type 可选）
    update_req = DataSourceUpdateRequest(source_type="hive_metastore")
    assert update_req.source_type == "hive_metastore"


def test_hive_metastore_database_defaults_to_hive():
    """回归：hive_metastore 的 database（HMS 元数据库名）缺省按 hive 填充。

    曾因 database 被当作"可选"，漏填时采集直连 HMS backend 库报裸
    ``pymysql 1046 'No database selected'``，用户只有等报错才知道缺库名。
    现创建/更新/连接预检三层 schema 均默认填 hive（用户可覆盖），
    非该类型不改变任何字段。
    """
    # 创建：缺 database → 自动填 hive
    create_req = DataSourceCreateRequest(
        source_id="hms1",
        name="HMS",
        source_type="hive_metastore",
        connection_config={"host": "10.0.0.5", "port": 3306},
        domain="d",
    )
    assert create_req.connection_config["database"] == "hive"
    # 创建：显式提供 database → 保持原值不被覆盖
    create_req2 = DataSourceCreateRequest(
        source_id="hms2",
        name="HMS2",
        source_type="hive_metastore",
        connection_config={"host": "10.0.0.5", "port": 3306, "database": "metastore_db"},
        domain="d",
    )
    assert create_req2.connection_config["database"] == "metastore_db"
    # 更新：传入 connection_config 缺 database → 自动填 hive
    update_req = DataSourceUpdateRequest(
        source_type="hive_metastore",
        connection_config={"host": "10.0.0.5"},
    )
    assert update_req.connection_config["database"] == "hive"
    # 连接预检：缺 database → 自动填 hive
    probe_req = TestConnectionRequest(
        source_type="hive_metastore",
        connection_config={"host": "10.0.0.5", "port": 3306},
    )
    assert probe_req.connection_config["database"] == "hive"
    # 非该类型（mysql）不受影响：缺 database 仍保持为空
    mysql_req = DataSourceCreateRequest(
        source_id="mysql1",
        name="MySQL",
        source_type="mysql",
        connection_config={"host": "10.0.0.5", "port": 3306},
        domain="d",
    )
    assert "database" not in mysql_req.connection_config


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
    repo.update_watermark_after_collection = AsyncMock(return_value=MagicMock(mode="FULL"))

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
    mock_build.assert_called_once_with("mysql", "enc", allow_private=True)
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
        "clickhouse",
        "doris",
        "hive",
        "hive_metastore",
        "kafka",
        "mysql",
        "postgres",
        "spark",
        "starrocks",
    }
    assert types["mysql"].label == "MySQL"
    assert types["kafka"].supports_database is False
    assert types["mysql"].default_port == 3306
    assert types["spark"].label == "Spark"
    assert types["hive_metastore"].label == "Hive Metastore"
    assert types["hive_metastore"].default_port == 3306
    assert types["spark"].default_port == 10000


# ---------- FR-030: 采集器数据库语义（全库 / 单库） ----------


class _MultiSchemaConnector:
    """模拟多库 information_schema 的假连接器。"""

    def __init__(
        self,
        schemas: list[str],
        tables_by_schema: dict[str, list[str]],
        columns: dict[str, list[dict[str, str]]] | dict[str, list[str]],
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
            # P1-1/P1-2: 批量列查询返回 {table_name, column_name, data_type, is_nullable}
            rows = []
            for tbl, cols in self._columns.items():
                for c in cols:
                    if isinstance(c, dict):
                        rows.append(
                            {
                                "table_name": tbl,
                                "column_name": c["name"],
                                "data_type": c.get("type", "varchar"),
                                "is_nullable": c.get("nullable", "YES"),
                            }
                        )
                    else:
                        rows.append(
                            {
                                "table_name": tbl,
                                "column_name": c,
                                "data_type": "varchar",
                                "is_nullable": "YES",
                            }
                        )
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
        columns={
            "orders": [{"name": "order_id", "type": "bigint", "nullable": "NO"}],
            "gmv": [{"name": "amount", "type": "decimal", "nullable": "YES"}],
        },
    )
    collector = InformationSchemaCollector(connector)  # database=None
    result = await collector.collect(MagicMock(source_id="s1"))
    entity_names = {s.entity_name for s in result.specs}
    # 系统库被排除，库.表 命名避免冲突
    assert entity_names == {"finance.orders", "sales.gmv"}
    assert len(result.specs) == 2
    assert result.specs[0].schema_json["columns"]  # 列已采集
    # P1-1: schema_json 格式为 [{"name": ..., "type": ..., "nullable": ...}]
    col = result.specs[0].schema_json["columns"][0]
    assert "name" in col and "type" in col and "nullable" in col
    assert col["name"] == "order_id"
    assert col["type"] == "bigint"
    assert col["nullable"] is False


async def test_info_schema_collector_connection_db_is_not_collection_scope():
    """连接库为纯凭据：指定 database 不再限定采集范围，仍枚举全部非系统库。"""
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
    collector = InformationSchemaCollector(connector, database="finance")
    result = await collector.collect(MagicMock(source_id="s1"))
    # 连接库不影响采集范围：仍枚举全部非系统库，entity_name 带库前缀
    assert {s.entity_name for s in result.specs} == {"finance.orders", "sales.gmv"}
    assert len(result.specs) == 2


async def test_info_schema_collector_list_tables_grouped_by_db():
    """list_tables 按库分组返回 BASE TABLE（仅指定库，不含系统库查询）。"""
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    connector = _MultiSchemaConnector(
        schemas=["finance", "sales", "information_schema"],
        tables_by_schema={"finance": ["orders", "users"], "sales": ["gmv"]},
        columns={},
    )
    collector = InformationSchemaCollector(connector)
    tables = await collector.list_tables(["finance", "sales"])
    assert tables == {"finance": ["orders", "users"], "sales": ["gmv"]}


async def test_info_schema_collector_list_tables_empty_db_fallback_all():
    """list_tables 未指定库时回退枚举全部非系统库（系统库被排除）。"""
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    connector = _MultiSchemaConnector(
        schemas=["finance", "information_schema", "mysql"],
        tables_by_schema={"finance": ["orders"], "information_schema": ["TABLES"]},
        columns={},
    )
    collector = InformationSchemaCollector(connector)
    tables = await collector.list_tables()
    assert tables == {"finance": ["orders"]}


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


def test_sqlalchemy_connector_connect_args_by_driver():
    """HIGH-3: asyncpg 用 timeout，aiomysql 用 connect_timeout（驱动参数兼容）。

    对 asyncpg 透传 connect_timeout 会 TypeError（asyncpg.connect 无此参数），
    Postgres 采集/探活整体不可用——按 drivername 分支注入。
    """
    from sqlalchemy.engine import URL

    from app.services.collector.connectors.mysql import SqlalchemyConnector

    with patch("app.services.collector.connectors.mysql.create_async_engine") as mock_create:
        SqlalchemyConnector(
            URL.create("postgresql+asyncpg", host="h", username="u"), connect_timeout=10
        )
        assert mock_create.call_args.kwargs["connect_args"] == {"timeout": 10}

    with patch("app.services.collector.connectors.mysql.create_async_engine") as mock_create:
        SqlalchemyConnector(
            URL.create("mysql+aiomysql", host="h", username="u"), connect_timeout=10
        )
        assert mock_create.call_args.kwargs["connect_args"] == {"connect_timeout": 10}


async def test_registry_build_from_cfg_and_type_info():
    """registry.build_from_cfg 支持明文构建；list_type_info 兜底插件类型。"""
    from app.services.collector.connectors import registry

    # SSRF 校验：mock DNS 解析为公网 IP，探活/枚举严格模式放行
    with patch("app.core.ssrf.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 0))]):
        collector = registry.build_from_cfg("mysql", {"host": "db.pub", "user": "u"})
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
    assert svc.classify("users", {"columns": [{"name": "user_name", "type": "varchar"}]}) == "PII"
    # 混合格式（字符串列 + dict 列）兼容
    assert (
        svc.classify("t", {"columns": ["order_id", {"name": "email", "type": "varchar"}]}) == "PII"
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
    svc._llm_classify_sensitivity = AsyncMock(return_value={"content": "PII", "confidence": 0.5})
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
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=DeterministicFallbackLlmClient()),
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
        "app.services.llm.config_service.LlmConfigService.build_client",
        new=AsyncMock(return_value=_FailingClient()),
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
    # P0-1: FOREIGN_KEY_CHECKS=0（先关）→ 子表 update ×3 + 父表 flush → =1（后开）
    stmts = [str(c.args[0]) if c.args else "" for c in s.execute.await_args_list]
    fk_off = [i for i, st in enumerate(stmts) if "FOREIGN_KEY_CHECKS=0" in st]
    fk_on = [i for i, st in enumerate(stmts) if "FOREIGN_KEY_CHECKS=1" in st]
    assert fk_off and fk_on, f"FK 开关缺失: {stmts}"
    assert fk_off[0] < fk_on[0], "FK 检查应先关闭后开启"
    # update(db_catalog) + update(watermark) + update(drift_log) 均被调用（改名保留审计）
    update_calls = [
        st
        for st in stmts
        if "UPDATE db_catalog" in st
        or "UPDATE collection_watermark" in st
        or "UPDATE schema_drift_log" in st
    ]
    assert len(update_calls) == 3, f"子表级联改名调用数异常: {stmts}"


async def test_create_source_integrity_error_returns_conflict():
    """检查-插入竞态下唯一约束冲突归一为 ConflictError（非 500）。"""
    from sqlalchemy.exc import IntegrityError

    svc, repo = _svc()
    repo.get_source = AsyncMock(return_value=None)
    repo.create_source = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dup")))
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


async def test_postgres_collector_batch_column_query_no_n_plus_1():
    """列信息一次性批量查出（按 schema 单次查询），而非每张表各查一次（消除 N+1）。"""
    from app.services.collector.connectors.postgres import PostgresCollector

    mock_connector = MagicMock()
    recorded: list = []

    async def fake_query(sql, params=None):
        recorded.append((sql, params))
        if "information_schema.schemata" in sql:
            return [{"schema_name": "finance"}]
        if "information_schema.tables" in sql:
            # 多张表，验证列查询不会逐表触发
            return [
                {"table_name": "orders"},
                {"table_name": "customers"},
                {"table_name": "invoices"},
            ]
        if "information_schema.columns" in sql:
            return [
                {"table_name": "orders", "column_name": "id", "data_type": "integer"},
                {"table_name": "customers", "column_name": "id", "data_type": "integer"},
                {"table_name": "invoices", "column_name": "id", "data_type": "integer"},
            ]
        return []

    mock_connector.query = fake_query
    mock_connector.dispose = AsyncMock()
    collector = PostgresCollector(mock_connector, schema="finance")
    await collector.collect(MagicMock(source_id="pg"))

    column_queries = [
        (sql, params)
        for sql, params in recorded
        if isinstance(sql, str) and "information_schema.columns" in sql
    ]
    # 批量：整个 schema 仅一次列查询，而非 N 张表各一次
    assert len(column_queries) == 1
    # 批量查询必须携带 schema 过滤参数
    assert "table_schema" in column_queries[0][0].lower()


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


# ---------- P1-4: Drift 日志暴露 ----------


async def test_list_drift_logs_returns_paged_items():
    """P1-4: list_drift_logs 返回分页的 drift 记录（含 total）。"""
    from datetime import UTC, datetime

    svc, repo = _svc()
    src = MagicMock(source_id="s1")
    repo.get_source = AsyncMock(return_value=src)

    log = MagicMock(
        source_id="s1",
        entity_name="users",
        change_type="ADD_COLUMN",
        before_signature=None,
        after_signature="sig2",
        before_schema=None,
        after_schema={"columns": [{"name": "age", "type": "int"}]},
        diff_json={"added": ["age"], "removed": [], "changed": []},
        detected_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    repo.list_drift_logs = AsyncMock(return_value=([log], 1))

    result = await svc.list_drift_logs("s1", page=1, page_size=20)
    assert result["total"] == 1
    assert result["items"][0]["entity_name"] == "users"
    assert result["items"][0]["change_type"] == "ADD_COLUMN"
    assert result["items"][0]["detected_at"] is not None


async def test_list_drift_logs_raises_not_found_for_missing_source():
    """P1-4: 数据源不存在时 list_drift_logs 抛 NotFoundError（非静默空列表）。"""
    svc, repo = _svc()
    repo.get_source = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.list_drift_logs("ghost")


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
        mock_client.return_value.get = AsyncMock(side_effect=fake_get)
        await collector._query("SELECT 1")
    assert "password" not in captured["params"]
    assert "user" not in captured["params"]
    assert captured["auth"] == ("u", "secret")


async def test_clickhouse_reuses_client_across_multiple_queries():
    """P1-3: 多次 _query 复用同一个 httpx.AsyncClient 实例（单例），不重复创建。"""
    from app.services.collector.connectors.clickhouse import ClickHouseCollector

    collector = ClickHouseCollector(host="ch", port=8123, user="u", password="secret")

    async def fake_get(url, params=None, auth=None):
        class _R:
            text = "1"

            def raise_for_status(self):
                return None

        return _R()

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.get = AsyncMock(side_effect=fake_get)
        mock_client.return_value.aclose = AsyncMock()
        await collector._query("SELECT 1")
        await collector._query("SELECT 2")
        await collector._query("SELECT 3")
        # 仅首次创建一次 AsyncClient，后续复用实例
        assert mock_client.call_count == 1
        assert mock_client.return_value.get.await_count == 3
        # 关闭后 client 置空，下次查询再次创建新实例
        await collector.close()
        assert collector._client is None


async def test_clickhouse_async_context_manager_closes_client():
    """P1-3: ClickHouseCollector 支持 async with，退出时关闭连接。"""
    from app.services.collector.connectors.clickhouse import ClickHouseCollector

    collector = ClickHouseCollector(host="ch")
    async with collector as ctx:
        assert ctx is collector
        assert ctx._client is not None
    # 退出上下文后 client 已释放
    assert collector._client is None


# ---------- P2-3: coverage 无配额语义 ----------


async def test_repo_recompute_coverage_no_baseline_is_unknown():
    """无基线（source_total_entities=0）时 coverage=0.0（覆盖率未知），非误导性 1.0。"""
    src = MagicMock()
    src.source_total_entities = 0
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


# ---------- 全链路工业级修复回归（列元数据 / 单实体刷新 / 签名短路 / 降级事件） ----------


async def test_repo_upsert_short_circuits_when_signature_unchanged():
    """元数据增量短路：内容签名未变、无漂移 → 不写库不更新实体。

    这是 PostgreSQL 等无源端时间戳类型的核心增量机制——全量扫描廉价，
    真正代价是逐实体 UPDATE；短路后仅变更落库。
    """
    existing = MagicMock()
    existing.content_signature = "sig"
    existing.schema_json = {"columns": ["old"]}
    with (
        patch("app.services.collector.repository.compute_content_signature", return_value="sig"),
        patch(
            "app.services.collector.repository.DriftDetector.detect", return_value=None
        ) as mock_detect,
    ):
        repo = CollectorRepository(_session(scalar_one_or_none=existing))
        cat, created, drift_info = await repo.upsert_catalog(
            source_id="s",
            entity_name="t",
            entity_type="TABLE",
            schema_json={"columns": ["new"]},
            etl_sql=None,
            sensitivity_level="PII",
            owner_id=None,
        )
    assert created is False
    assert cat is existing
    # 短路：实体未被原地更新（旧 schema_json 保持不变）
    assert existing.schema_json == {"columns": ["old"]}
    mock_detect.assert_called_once()


class _EntityConnector:
    """模拟按 (schema, table) 过滤列元数据的假连接器（collect_entity 专用）。"""

    def __init__(self, columns: dict[tuple[str, str], list[dict]]) -> None:
        self._columns = columns

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        return self._columns.get((params.get("schema", ""), params.get("tbl", "")), [])

    async def dispose(self) -> None:
        return None


async def test_collect_entity_mysql_connection_db_not_scope():
    """连接库为纯凭据：collect_entity 需显式 库.表 定位（不再按连接库隐式解析）。"""
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    connector = _EntityConnector(
        {
            ("finance", "orders"): [
                {
                    "table_name": "orders",
                    "column_name": "order_id",
                    "data_type": "bigint",
                    "is_nullable": "NO",
                    "column_comment": "订单ID",
                    "column_default": "0",
                }
            ],
        }
    )
    collector = InformationSchemaCollector(connector, database="finance")
    spec = await collector.collect_entity(MagicMock(source_id="s1"), "finance.orders")
    assert spec is not None
    assert spec.entity_name == "finance.orders"
    col = spec.schema_json["columns"][0]
    assert col["name"] == "order_id"
    assert col["comment"] == "订单ID"
    assert col["default"] == "0"
    # 裸表名无法定位 schema → None（回退全量）；连接库不参与隐式解析
    assert await collector.collect_entity(MagicMock(source_id="s1"), "orders") is None
    assert await collector.collect_entity(MagicMock(source_id="s1"), "other.orders") is None


async def test_collect_entity_mysql_multi_database_missing():
    """MySQL 多库模式：entity_name 需带 schema；源端无此表返回 None。"""
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    connector = _EntityConnector(
        {
            ("finance", "orders"): [
                {
                    "table_name": "orders",
                    "column_name": "order_id",
                    "data_type": "bigint",
                    "is_nullable": "NO",
                    "column_comment": "",
                    "column_default": None,
                }
            ],
        }
    )
    collector = InformationSchemaCollector(connector)  # database=None 多库模式
    spec = await collector.collect_entity(MagicMock(source_id="s1"), "finance.orders")
    assert spec is not None
    assert spec.entity_name == "finance.orders"
    # 多库模式纯表名无法定位 schema → None
    assert await collector.collect_entity(MagicMock(source_id="s1"), "orders") is None
    # 源端表不存在 → None
    assert await collector.collect_entity(MagicMock(source_id="s1"), "finance.ghost") is None


async def test_refresh_entity_uses_collect_entity():
    """refresh_entity 走连接器单实体采集，不触发全源扫描；注释命中 PII 分级。"""
    svc, repo = _svc()
    repo.get_source = AsyncMock(return_value=MagicMock(source_id="s"))
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), False, None))
    repo.update_health_status = AsyncMock()

    class SingleCollector:
        async def collect_entity(self, source: object, entity_name: str):
            return CatalogSpec(
                entity_name=entity_name,
                entity_type="TABLE",
                schema_json={
                    "columns": [{"name": "c1", "type": "varchar", "comment": "用户手机号"}]
                },
            )

        async def collect(self, source: object) -> CollectResult:
            raise AssertionError("单实体刷新不应触发全源采集")

    result = await svc.refresh_entity("s", "orders", 1, SingleCollector())
    assert result["entity_name"] == "orders"
    # 列注释含「手机号」→ PII（修复前注释未采集，此列会被误判 INTERNAL）
    assert result["sensitivity_level"] == "PII"
    assert result["columns"] == 1
    assert result["drifted"] is False
    repo.upsert_catalog.assert_awaited_once()
    repo.update_health_status.assert_awaited_once_with("s", "healthy")


async def test_refresh_entity_fallback_to_full_when_unsupported():
    """连接器未实现 collect_entity（继承默认）→ 回退全量采集后仅取目标实体。"""
    svc, repo = _svc()
    repo.get_source = AsyncMock(return_value=MagicMock(source_id="s"))
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), False, None))
    repo.update_health_status = AsyncMock()

    class FullOnlyCollector(BaseCollector):
        async def collect(self, source: object) -> CollectResult:
            return CollectResult(
                specs=[
                    CatalogSpec(
                        entity_name="orders",
                        entity_type="TABLE",
                        schema_json={"columns": [{"name": "a", "type": "varchar"}]},
                    )
                ],
                failed_specs=[],
                source_id="s",
            )

    result = await svc.refresh_entity("s", "orders", 1, FullOnlyCollector())
    assert result["entity_name"] == "orders"


async def test_refresh_entity_raises_when_source_entity_missing():
    """目标实体在源端不存在 → NotFoundError（非静默成功）。"""
    svc, repo = _svc()
    repo.get_source = AsyncMock(return_value=MagicMock(source_id="s"))

    class MissingCollector:
        async def collect_entity(self, source: object, entity_name: str):
            return None

        async def collect(self, source: object) -> CollectResult:
            raise AssertionError("不应回退全量")

    with pytest.raises(NotFoundError, match="源端不存在实体"):
        await svc.refresh_entity("s", "ghost", 1, MissingCollector())


async def test_collect_and_register_emits_degrade_event():
    """可观测性：增量请求被降级为全量时发布 collect_degraded 事件（不静默失效）。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock(source_type="postgres"))
    repo.get_watermark = AsyncMock(
        return_value=MagicMock(last_collected_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(content_signature="sig"), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)

    class StubCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            return CollectResult(specs=[], failed_specs=[], source_id="s")

    await svc.collect_and_register("s", StubCollector(), actor_id=1, mode="INCREMENTAL")
    # postgres 不支持增量 → 降级 FULL → 发布 collect_degraded
    events.publish.assert_awaited_once()
    event_type = events.publish.await_args.args[0]
    payload = events.publish.await_args.args[1]
    assert event_type == "collect_degraded"
    assert payload["reason"] == "source_type_not_supported:postgres"


# ---------- 采集进度回调 + 明细（SSE 实时推送） ----------


async def test_collect_and_register_emits_progress_and_entities():
    """progress_cb 逐阶段回调 + 结果含 entities 明细（供前端展示本次采集到哪些表）。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish = AsyncMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock())
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(content_signature="sig"), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)

    class StubCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            return CollectResult(
                specs=[
                    CatalogSpec("t1", "TABLE", {"columns": ["a"]}),
                    CatalogSpec("t2", "TABLE", {"columns": ["b"]}),
                ],
                failed_specs=[],
                source_id="s",
            )

    emitted: list[dict] = []

    async def progress_cb(event: dict) -> None:
        emitted.append(event)

    result = await svc.collect_and_register(
        "s", StubCollector(), actor_id=1, progress_cb=progress_cb
    )
    # 阶段：start → scanning → registering×2
    phases = [e["phase"] for e in emitted]
    assert phases == ["start", "scanning", "registering", "registering"]
    assert emitted[-1]["index"] == 2
    assert emitted[-1]["total"] == 2
    # entities 明细
    assert [e["entity_name"] for e in result["entities"]] == ["t1", "t2"]
    assert result["entities"][0]["sensitivity_level"] in (
        "PII",
        "INTERNAL",
        "CONFIDENTIAL",
        "PUBLIC",
    )


async def test_collect_and_register_progress_cb_failure_does_not_break():
    """进度回调抛异常只告警，不阻断采集主流程。"""
    svc, repo = _svc()
    events = MagicMock()
    events.publish_batch = AsyncMock()
    svc._events = events
    repo.get_source = AsyncMock(return_value=MagicMock())
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(content_signature="sig"), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)

    class StubCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            return CollectResult(specs=[], failed_specs=[], source_id="s")

    async def broken_cb(event: dict) -> None:
        raise RuntimeError("store down")

    result = await svc.collect_and_register("s", StubCollector(), actor_id=1, progress_cb=broken_cb)
    assert result["scanned"] == 0
    assert result["registered"] == 0


async def test_list_databases_delegates_to_connector():
    """list_databases 用明文配置构建采集器并委托 list_databases。"""
    svc, _repo = _svc()
    fake_collector = MagicMock()
    fake_collector.list_databases = AsyncMock(return_value=["db1", "db2"])
    fake_collector.dispose = AsyncMock()
    with patch(
        "app.services.collector.connectors.registry.build_from_cfg",
        return_value=fake_collector,
    ):
        dbs = await svc.list_databases("mysql", {"host": "h", "database": "d"})
    assert dbs == ["db1", "db2"]
    fake_collector.dispose.assert_awaited_once()


async def test_list_databases_failure_raises_clear_error():
    """连接器构建/枚举失败（2026-08-28 起）抛出明确错误，不再静默返回空。

    此前静默返回 [] 令前端无法区分「实例无库」与「连接失败」——连接器不支持
    枚举（Kafka 等）正常返回空，真实连接失败须让用户看到可排查的错误。
    """
    from app.core.exceptions import UnisenseError

    svc, _repo = _svc()
    with patch(
        "app.services.collector.connectors.registry.build_from_cfg",
        side_effect=RuntimeError("down"),
    ), pytest.raises(UnisenseError) as exc:
        await svc.list_databases("mysql", {"host": "h"})
    assert exc.value.error_code == "LIST_DATABASES_FAILED"
    assert "枚举数据库失败" in exc.value.message


async def test_list_tables_delegates_to_connector():
    """list_tables 用明文配置构建采集器并委托 list_tables（透传库列表）。"""
    svc, _repo = _svc()
    fake_collector = MagicMock()
    fake_collector.list_tables = AsyncMock(
        return_value={"finance": ["orders", "users"], "sales": ["gmv"]}
    )
    fake_collector.dispose = AsyncMock()
    with patch(
        "app.services.collector.connectors.registry.build_from_cfg",
        return_value=fake_collector,
    ):
        tables = await svc.list_tables("mysql", {"host": "h"}, ["finance", "sales"])
    assert tables == {"finance": ["orders", "users"], "sales": ["gmv"]}
    fake_collector.list_tables.assert_awaited_once_with(["finance", "sales"])
    fake_collector.dispose.assert_awaited_once()


async def test_list_tables_failure_returns_empty():
    """连接器构建/枚举失败时返回空字典（不抛出，前端隐藏表级选择区）。"""
    svc, _repo = _svc()
    with patch(
        "app.services.collector.connectors.registry.build_from_cfg",
        side_effect=RuntimeError("down"),
    ):
        tables = await svc.list_tables("mysql", {"host": "h"}, ["finance"])
    assert tables == {}


async def test_list_databases_passes_allow_private_when_enabled():
    """内网部署开关 collector_allow_private=true 时，预检路径放行私网（枚举库/测试连接）。

    回归：此前 test_connection/list_databases/list_tables 走 build_from_cfg
    默认 allow_private=False，Hive 192.168.x.x 私网被 SSRF 拦截 → 枚举库为空。
    """
    svc, _repo = _svc()
    fake_collector = MagicMock()
    fake_collector.list_databases = AsyncMock(return_value=["db1"])
    fake_collector.dispose = AsyncMock()
    with patch.object(svc._settings, "collector_allow_private", True), patch(
        "app.services.collector.connectors.registry.build_from_cfg",
        return_value=fake_collector,
    ) as mock_build:
        dbs = await svc.list_databases("mysql", {"host": "192.168.1.10"})
    assert dbs == ["db1"]
    mock_build.assert_called_once_with(
        "mysql", {"host": "192.168.1.10"}, allow_private=True
    )


async def test_test_connection_allow_private_default_false():
    """默认（公网部署）不放开私网：预检路径保持 SSRF 严格模式。"""
    svc, _repo = _svc()

    class ProbeOkCollector:
        async def probe(self):
            from app.services.collector.spi import ProbeResult

            return ProbeResult(ok=True, latency_ms=1)

        async def dispose(self):
            return None

    with patch(
        "app.services.collector.connectors.registry.build_from_cfg",
        return_value=ProbeOkCollector(),
    ) as mock_build:
        await svc.test_connection("mysql", {"host": "h"})
    assert mock_build.call_args.kwargs.get("allow_private") is False


async def test_repo_list_catalogs_source_status_active_uses_join():
    """source_status=active 时外连接 DataSource 过滤仅活跃源，并挂瞬态属性。"""
    s = MagicMock()
    res = MagicMock()
    cat = SimpleNamespace(source_id="mysql_unisense", entity_name="t1")
    res.all.return_value = [(cat, None, "MySQL 主库", "sales")]
    res.scalar.return_value = 1
    s.execute = AsyncMock(return_value=res)
    s.scalar = AsyncMock(return_value=1)
    repo = CollectorRepository(s)
    params = SimpleNamespace(
        source_id=None,
        entity_type=None,
        sensitivity_level=None,
        keyword=None,
        source_status="active",
        page=1,
        page_size=20,
    )
    items, total = await repo.list_catalogs(params)
    assert total == 1
    assert items[0]._src_deleted is False
    assert items[0]._src_name == "MySQL 主库"
    assert items[0]._src_domain == "sales"
    compiled = str(
        s.execute.call_args_list[0].args[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "data_source" in compiled  # 已 join DataSource


async def test_repo_get_sources_meta():
    """批量取数据源名称与删除状态。"""
    s = MagicMock()
    res = MagicMock()
    res.all.return_value = [
        ("mysql_a", "A", None),
        ("mysql_b", "B", datetime(2026, 1, 1, tzinfo=UTC)),
    ]
    s.execute = AsyncMock(return_value=res)
    repo = CollectorRepository(s)
    meta = await repo.get_sources_meta(["mysql_a", "mysql_b", "missing"])
    assert meta["mysql_a"] == ("A", False)
    assert meta["mysql_b"] == ("B", True)
    assert "missing" not in meta


# ---------- 连接配置明文回显（编辑场景） ----------


def _make_src_with_config(cfg: dict[str, Any]) -> Any:
    """构造带真实加密配置的 DataSource ORM（模拟落库形态）。"""
    from app.core.secrets import SecretManager
    from app.models.data_source import DataSource

    return DataSource(
        source_id="s1",
        name="S",
        source_type="mysql",
        connection_config=SecretManager.encrypt(cfg),
        domain="d",
        coverage=0.0,
        quota={},
        health_status="unknown",
        cluster_id="default",
        created_by=1,
    )


async def test_get_source_redacts_config_by_default() -> None:
    """P0-1: get_source 默认脱敏（非平台管理员路径）——connection_config=None。"""
    svc, repo = _svc()
    repo.get_source = AsyncMock(
        return_value=_make_src_with_config(
            {"host": "h", "port": 3306, "user": "u", "password": "p"}
        )
    )

    resp = await svc.get_source("s1")

    assert resp.connection_config is None
    assert resp.connection_config_present is True


async def test_get_source_include_config_returns_plaintext() -> None:
    """P0-1: include_config=True（平台管理员路径）返回解密后的明文配置。"""
    svc, repo = _svc()
    repo.get_source = AsyncMock(
        return_value=_make_src_with_config(
            {"host": "h", "port": 3306, "user": "u", "password": "p"}
        )
    )

    resp = await svc.get_source("s1", include_config=True)

    assert resp.connection_config == {"host": "h", "port": 3306, "user": "u", "password": "p"}
    assert resp.connection_config_present is True


async def test_update_source_preserves_password_when_omitted() -> None:
    """P0-1: 编辑态「二次确认」——新配置未提交密码时保留旧密码。"""
    svc, repo = _svc()
    repo.get_source = AsyncMock(
        return_value=_make_src_with_config({"host": "h", "password": "old_pw"})
    )

    await svc.update_source(
        "s1",
        DataSourceUpdateRequest(connection_config={"host": "h2"}),
        actor_id=1,
    )

    stored = SecretManager.decrypt(repo.get_source.return_value.connection_config)
    assert stored["host"] == "h2"
    assert stored["password"] == "old_pw"


async def test_update_source_overrides_password_when_provided() -> None:
    """P0-1: 新配置显式提交密码时覆盖旧密码。"""
    svc, repo = _svc()
    repo.get_source = AsyncMock(
        return_value=_make_src_with_config({"host": "h", "password": "old_pw"})
    )

    await svc.update_source(
        "s1",
        DataSourceUpdateRequest(connection_config={"host": "h2", "password": "new_pw"}),
        actor_id=1,
    )

    stored = SecretManager.decrypt(repo.get_source.return_value.connection_config)
    assert stored["password"] == "new_pw"


async def test_update_source_ignores_empty_host() -> None:
    """Med 9: 非 admin 编辑提交空 host 时保留真实 host（防覆盖致源不可用）。"""
    svc, repo = _svc()
    repo.get_source = AsyncMock(
        return_value=_make_src_with_config({"host": "real-host", "password": "old_pw"})
    )

    await svc.update_source(
        "s1",
        DataSourceUpdateRequest(connection_config={"host": "", "password": "new_pw"}),
        actor_id=1,
    )

    stored = SecretManager.decrypt(repo.get_source.return_value.connection_config)
    assert stored["host"] == "real-host"  # 空 host 被剔除，保留原值
    assert stored["password"] == "new_pw"



async def test_list_data_sources_redacts_config() -> None:
    """列表接口保持脱敏：connection_config 一律为 None（安全边界不扩大）。"""
    svc, repo = _svc()
    repo.list_sources = AsyncMock(
        return_value=([_make_src_with_config({"host": "h", "password": "p"})], 1)
    )
    repo.list_sources_signals = AsyncMock(return_value={})

    items, total = await svc.list_sources(page=1, page_size=20)

    assert total == 1
    assert items[0].connection_config is None
    assert items[0].connection_config_present is True


async def test_get_source_decrypt_failure_degrades_to_none() -> None:
    """密钥漂移导致解密失败时，详情返回 connection_config=None 而不抛 500。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.connection_config = "invalid-token"
    repo.get_source = AsyncMock(return_value=src)

    resp = await svc.get_source("s1")

    assert resp.connection_config is None
    assert resp.connection_config_present is True


# ---- 停用/启用（enabled）----


async def test_collect_and_register_rejects_disabled_source() -> None:
    """停用（enabled=False）的数据源被拒采集，返回 SOURCE_DISABLED。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.enabled = False
    repo.get_source = AsyncMock(return_value=src)

    # enabled 校验先于 collector.collect，传 MagicMock 即可
    with pytest.raises(BusinessError) as ei:
        await svc.collect_and_register("s1", MagicMock(), actor_id=1)

    assert ei.value.error_code == "SOURCE_DISABLED"


class _EnabledStubCollector:
    """模块级最小采集器（enabled 回归测试用）：单表 users。"""

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
            source_id=source.source_id,
        )


async def test_collect_and_register_allows_enabled_source() -> None:
    """启用状态（enabled=True）的数据源正常采集（回归）。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.enabled = True
    repo.get_source = AsyncMock(return_value=src)
    repo.upsert_catalog = AsyncMock(return_value=(MagicMock(), True, None))
    repo.recompute_coverage = AsyncMock(return_value=0.5)

    result = await svc.collect_and_register("s1", _EnabledStubCollector(), actor_id=1)

    assert result["registered"] == 1


async def test_refresh_entity_rejects_disabled_source() -> None:
    """停用源的单实体刷新同样被拒。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.enabled = False
    repo.get_source = AsyncMock(return_value=src)

    with pytest.raises(BusinessError) as ei:
        await svc.refresh_entity("s1", "users", 1, MagicMock())

    assert ei.value.error_code == "SOURCE_DISABLED"


async def test_schedule_collection_rejects_disabled_source() -> None:
    """停用源的异步入队（collect-async/collect-now）同样被拒。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.enabled = False
    repo.get_source = AsyncMock(return_value=src)

    with pytest.raises(BusinessError) as ei:
        await svc.schedule_collection("s1", 1)

    assert ei.value.error_code == "SOURCE_DISABLED"


async def test_schedule_collection_passes_mode_to_queue() -> None:
    """M4: collect-now 选择的 mode 透传到队列（不静默降级为全量）。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.enabled = True
    repo.get_source = AsyncMock(return_value=src)

    queue = MagicMock()
    queue.enqueue = AsyncMock(return_value="job-1")

    job_id = await svc.schedule_collection("s1", 1, queue=queue, mode="INCREMENTAL")

    assert job_id == "job-1"
    queue.enqueue.assert_awaited_once_with(
        "s1", 1, mode="INCREMENTAL", include_patterns=None, exclude_patterns=None
    )


async def test_schedule_collection_passes_temp_filters_to_queue() -> None:
    """A 方案：collect-now 临时白/黑名单透传到队列（仅本次生效）。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.enabled = True
    repo.get_source = AsyncMock(return_value=src)

    queue = MagicMock()
    queue.enqueue = AsyncMock(return_value="job-2")

    await svc.schedule_collection(
        "s1",
        1,
        queue=queue,
        mode="FULL",
        include_patterns=["ods_*"],
        exclude_patterns=["tmp_*"],
    )

    queue.enqueue.assert_awaited_once_with(
        "s1", 1, mode="FULL", include_patterns=["ods_*"], exclude_patterns=["tmp_*"]
    )


async def test_update_source_toggles_enabled() -> None:
    """update_source 支持停用/启用（enabled 字段）。"""
    from app.services.collector.schemas import DataSourceUpdateRequest

    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.enabled = True
    repo.get_source = AsyncMock(return_value=src)

    resp = await svc.update_source("s1", DataSourceUpdateRequest(enabled=False), 1)

    assert src.enabled is False
    assert resp.enabled is False

    resp = await svc.update_source("s1", DataSourceUpdateRequest(enabled=True), 1)

    assert src.enabled is True
    assert resp.enabled is True


async def test_update_source_enabled_none_keeps_unchanged() -> None:
    """enabled 未传（None）时不修改（PATCH 语义）。"""
    from app.services.collector.schemas import DataSourceUpdateRequest

    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.enabled = True
    repo.get_source = AsyncMock(return_value=src)

    resp = await svc.update_source("s1", DataSourceUpdateRequest(name="改名"), 1)

    assert src.enabled is True
    assert resp.enabled is True


async def test_batch_toggle_sources_all_success() -> None:
    """批量启停：全部成功时 succeeded 全量、failed 为空，逐条携带目标状态。"""
    svc, repo = _svc()
    src_a = _make_src_with_config({"host": "h"})
    src_a.name = "源A"
    src_b = _make_src_with_config({"host": "h"})
    src_b.name = "源B"
    repo.set_source_enabled = AsyncMock(side_effect=[src_a, src_b])

    result = await svc.batch_toggle_sources(["a", "b"], False, 1)

    assert len(result.succeeded) == 2
    assert len(result.failed) == 0
    assert result.succeeded[0].name == "源A"
    # repository 逐条以目标状态调用（enabled=False 表示停用）
    assert [c.args for c in repo.set_source_enabled.call_args_list] == [
        ("a", False),
        ("b", False),
    ]


async def test_batch_toggle_sources_partial_failure() -> None:
    """批量启停：不存在的源记为 NOT_FOUND 失败项，其余仍成功（207）。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.name = "源A"
    repo.set_source_enabled = AsyncMock(side_effect=[src, None])

    result = await svc.batch_toggle_sources(["a", "missing"], True, 1)

    assert len(result.succeeded) == 1
    assert len(result.failed) == 1
    assert result.failed[0].source_id == "missing"
    assert result.failed[0].error_code == "NOT_FOUND"


async def test_batch_toggle_sources_exception_isolated() -> None:
    """批量启停：单条抛异常记为 INTERNAL 失败项，不阻断其余（207）。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.name = "源A"
    repo.set_source_enabled = AsyncMock(side_effect=[RuntimeError("boom"), src])

    result = await svc.batch_toggle_sources(["bad", "ok"], True, 1)

    assert len(result.succeeded) == 1
    assert result.succeeded[0].source_id == "ok"
    assert len(result.failed) == 1
    assert result.failed[0].error_code == "INTERNAL"


async def test_batch_delete_sources_all_success() -> None:
    """批量删除：全部成功时 succeeded 全量、failed 为空。"""
    svc, repo = _svc()
    src_a = _make_src_with_config({"host": "h"})
    src_a.name = "源A"
    src_b = _make_src_with_config({"host": "h"})
    src_b.name = "源B"
    repo.get_source = AsyncMock(side_effect=[src_a, src_b])
    repo.soft_delete_source = AsyncMock(return_value=True)

    result = await svc.batch_delete_sources(["a", "b"], 1)

    assert len(result.succeeded) == 2
    assert len(result.failed) == 0
    assert result.succeeded[0].name == "源A"


async def test_batch_delete_sources_partial_failure() -> None:
    """批量删除：不存在的源记为 NOT_FOUND 失败项，其余仍成功（207）。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.name = "源A"
    repo.get_source = AsyncMock(side_effect=[src, None])
    repo.soft_delete_source = AsyncMock(return_value=True)

    result = await svc.batch_delete_sources(["a", "missing"], 1)

    assert len(result.succeeded) == 1
    assert len(result.failed) == 1
    assert result.failed[0].error_code == "NOT_FOUND"


async def test_response_includes_enabled() -> None:
    """DataSourceResponse 携带 enabled 字段（列表/详情均可见）。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.enabled = False
    repo.get_source = AsyncMock(return_value=src)

    resp = await svc.get_source("s1")

    assert resp.enabled is False



# ---------- 目录实体详情（血缘图谱表节点下钻） ----------


async def test_repo_get_catalog_by_id() -> None:
    """按主键取目录实体（含删除过滤）。"""
    s = MagicMock()
    cat = MagicMock()
    s.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=cat)))
    repo = CollectorRepository(s)
    got = await repo.get_catalog_by_id(42)
    assert got is cat
    compiled = str(
        s.execute.call_args_list[0].args[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "db_catalog" in compiled and "deleted_at" in compiled and "42" in compiled


async def test_service_get_catalog_detail_enriches_source_meta() -> None:
    """详情返回目录实体并富集源名称/删除状态。"""
    from app.services.collector.schemas import DBCatalogResponse

    svc, repo = _svc()
    repo.get_catalog_by_id = AsyncMock(return_value=_FakeCatalog("PII-HIGH"))
    repo.get_sources_meta = AsyncMock(return_value={"src1": ("MySQL 主库", False)})
    repo.get_descriptions = AsyncMock(return_value=[])

    resp = await svc.get_catalog_detail(42)

    assert isinstance(resp, DBCatalogResponse)
    assert resp.entity_name == "users"
    assert resp.source_name == "MySQL 主库"
    assert resp.source_deleted is False
    assert resp.sensitivity_level == "PII-HIGH"


async def test_service_get_catalog_detail_merges_column_descriptions() -> None:
    """字段详情合并 column_descriptions：LLM 推断/人工编辑的描述随 schema_def.columns[] 返回。

    回归：描述存 column_descriptions 表（不回写 schema_json.comment），详情接口必须
    合并注入 ``description``/``description_source``，否则采集目录字段详情抽屉看不到。
    """
    from app.models.data_source import ColumnDescription

    fake = _FakeCatalog("INTERNAL")
    fake.schema_json = {
        "columns": [
            {"name": "id", "type": "bigint", "comment": ""},
            {"name": "user_name", "type": "varchar", "comment": ""},
        ]
    }

    svc, repo = _svc()
    repo.get_catalog_by_id = AsyncMock(return_value=fake)
    repo.get_sources_meta = AsyncMock(return_value={"src1": ("MySQL 主库", False)})
    repo.get_descriptions = AsyncMock(
        return_value=[
            ColumnDescription(
                catalog_id=1,
                column_name="user_name",
                description="用户登录名",
                source="llm",
            )
        ]
    )

    resp = await svc.get_catalog_detail(42)

    cols = resp.schema_def["columns"]
    by_name = {c["name"]: c for c in cols}
    # 有 column_descriptions 记录 → 合并描述
    assert by_name["user_name"]["description"] == "用户登录名"
    assert by_name["user_name"]["description_source"] == "llm"
    # 无记录 → 不注入 description（前端回退 comment）
    assert "description" not in by_name["id"]


async def test_dbcatalog_response_serializes_schema_def_not_schema_json() -> None:
    """响应序列化契约回归：FastAPI 以 by_alias=True 序列化时，schema 字段必须输出
    ``schema_def``（而非 alias 的 ``schema_json``），否则前端字段详情读不到列。

    修复前用 ``Field(alias=...)``，FastAPI by_alias 序列化输出 ``schema_json``，
    与前端 ``DBCatalog.schema_def`` 契约不一致，导致采集目录字段详情抽屉恒为空。
    """
    from app.services.collector.schemas import DBCatalogResponse

    fake = _FakeCatalog("INTERNAL")
    from datetime import UTC, datetime

    fake.updated_at = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
    resp = DBCatalogResponse.model_validate(fake)
    dump = resp.model_dump(by_alias=True)  # 模拟 FastAPI 响应序列化
    assert "schema_def" in dump
    assert "schema_json" not in dump
    assert dump["schema_def"] == {"columns": ["user_name"]}
    # 最近更新时间随响应透出（前端「最近更新」列数据源；FastAPI 序列化时转 ISO 字符串）
    assert dump["updated_at"] == datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


async def test_service_get_catalog_detail_not_found() -> None:
    """目录实体不存在（或已删除）时抛 NotFoundError。"""
    from app.core.exceptions import NotFoundError

    svc, repo = _svc()
    repo.get_catalog_by_id = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError):
        await svc.get_catalog_detail(999)


# ---- 表级业务描述 + 描述缺失统计（TD §12.1） ----


async def test_repo_update_table_description_ok() -> None:
    """人工/LLM 更新表级描述：写 4 列并返回更新后的实体。"""
    cat = MagicMock()
    cat.description = None
    cat.description_source = None
    cat.description_updated_by = None
    cat.description_updated_at = None
    repo = CollectorRepository(_session(scalar_one_or_none=cat))

    result = await repo.update_table_description(
        catalog_id=1, description="订单明细表", source="manual", updated_by=7
    )

    assert result is cat
    assert cat.description == "订单明细表"
    assert cat.description_source == "manual"
    assert cat.description_updated_by == 7
    assert cat.description_updated_at is not None


async def test_repo_update_table_description_not_found() -> None:
    """目录不存在：返回 None（由 API 层抛 404）。"""
    repo = CollectorRepository(_session(scalar_one_or_none=None))
    result = await repo.update_table_description(
        catalog_id=999, description="x", source="manual"
    )
    assert result is None


def _coverage_session() -> MagicMock:
    """构造 coverage 统计用 session：SQL 聚合（scalar）+ execute 返回分页明细。"""
    s = MagicMock()
    # scalar 顺序：total_tables / tables_with_desc / fields_with_desc / per_table_total
    s.scalar = AsyncMock(side_effect=[2, 1, 1, 2])

    cat1 = MagicMock()
    cat1.id = 1
    cat1.entity_name = "ods_order"
    cat1.source_id = "s1"
    cat1.entity_type = "TABLE"
    cat1.sensitivity_level = "INTERNAL"
    cat1.schema_json = {
        "columns": [
            {"name": "id", "type": "bigint", "comment": ""},
            {"name": "name", "type": "varchar", "comment": "用户名"},
        ]
    }
    cat1.description = None
    cat1.description_source = None
    cat1.owner_id = None
    cat1.updated_at = None

    cat2 = MagicMock()
    cat2.id = 2
    cat2.entity_name = "dwd_user"
    cat2.source_id = "s2"
    cat2.entity_type = "TABLE"
    cat2.sensitivity_level = "CONFIDENTIAL"
    cat2.schema_json = {
        "columns": [
            {"name": "email", "type": "varchar", "comment": ""},
            {"name": "phone", "type": "varchar", "comment": ""},
        ]
    }
    cat2.description = "用户明细表"
    cat2.description_source = "manual"
    cat2.owner_id = 5
    cat2.updated_at = None

    desc = MagicMock()
    desc.catalog_id = 2
    desc.column_name = "email"
    desc.source = "manual"

    res_cats = MagicMock()
    res_cats.scalars.return_value.all.return_value = [cat1, cat2]
    res_descs = MagicMock()
    res_descs.scalars.return_value.all.return_value = [desc]
    res_srcs = MagicMock()
    res_srcs.all.return_value = [
        SimpleNamespace(source_id="s1", domain="sales", name="Sales MySQL"),
        SimpleNamespace(source_id="s2", domain="platform", name="Platform MySQL"),
    ]
    res_users = MagicMock()
    res_users.all.return_value = [
        SimpleNamespace(id=5, display_name="张三", username="zhangsan"),
    ]

    res_fields = MagicMock()
    res_fields.scalar.return_value = 4
    s.execute = AsyncMock(
        side_effect=[res_fields, res_cats, res_descs, res_srcs, res_users]
    )
    return s


async def test_repo_get_description_coverage_stats() -> None:
    """覆盖统计：表/字段覆盖率、按表列缺失字段数、domain join。"""
    repo = CollectorRepository(_coverage_session())
    cov = await repo.get_description_coverage()

    assert cov["total_tables"] == 2
    assert cov["tables_with_desc"] == 1
    assert cov["tables_missing_desc"] == 1
    assert cov["total_fields"] == 4
    # 仅 manual/llm 记录计覆盖（cat2.email）；cat1.name 的 schema comment 不计
    assert cov["fields_with_desc"] == 1
    assert cov["fields_missing_desc"] == 3
    # P1-8: 分页元信息（默认 page_size=None 全量）
    assert cov["per_table_total"] == 2
    assert cov["page"] == 1
    assert cov["page_size"] is None

    by_name = {t["entity_name"]: t for t in cov["per_table"]}
    assert by_name["ods_order"]["missing_fields"] == 2
    assert by_name["ods_order"]["domain"] == "sales"
    assert by_name["ods_order"]["table_desc"] is False
    assert by_name["ods_order"]["sensitivity_level"] == "INTERNAL"
    assert by_name["ods_order"]["missing_field_names"] == ["id", "name"]
    assert by_name["ods_order"]["source_name"] == "Sales MySQL"
    assert by_name["ods_order"]["owner_name"] is None
    assert by_name["dwd_user"]["missing_fields"] == 1
    assert by_name["dwd_user"]["domain"] == "platform"
    assert by_name["dwd_user"]["table_desc"] is True
    assert by_name["dwd_user"]["sensitivity_level"] == "CONFIDENTIAL"
    assert by_name["dwd_user"]["missing_field_names"] == ["phone"]
    assert by_name["dwd_user"]["source_name"] == "Platform MySQL"
    assert by_name["dwd_user"]["owner_name"] == "张三"
    assert by_name["dwd_user"]["description"] == "用户明细表"
    assert by_name["dwd_user"]["description_source"] == "manual"


async def test_count_jobs_by_status_aggregates() -> None:
    """总览仪表资产卡片：按任务状态聚合计数（运行时 JobStore 数据）。"""
    svc, _repo = _svc()
    svc.list_jobs = AsyncMock(
        return_value=[
            {"job_id": "j1", "status": "COMPLETED"},
            {"job_id": "j2", "status": "COMPLETED"},
            {"job_id": "j3", "status": "RUNNING"},
            {"job_id": "j4", "status": "QUEUED"},
            {"job_id": "j5", "status": None},
        ]
    )

    counts = await svc.count_jobs_by_status()

    assert counts == {"COMPLETED": 2, "RUNNING": 1, "QUEUED": 1, "UNKNOWN": 1}
    svc.list_jobs.assert_awaited_once_with(limit=100000, offset=0)


async def test_count_jobs_by_status_queue_unsupported_returns_empty() -> None:
    """队列不支持 list 时返回空分布，不阻断仪表盘。"""
    svc, _repo = _svc()
    svc.list_jobs = AsyncMock(return_value=[])

    counts = await svc.count_jobs_by_status()

    assert counts == {}


async def test_service_list_catalogs_enriches_domain_and_owner_name() -> None:
    """list_catalogs 生产化补充：批量回填业务域（经数据源继承）与责任人展示名。"""
    svc, repo = _svc()
    cat = SimpleNamespace(
        id=1,
        source_id="s1",
        owner_id=5,
        entity_name="dw.t",
        entity_type="table",
        schema_json={"fields": []},
        etl_sql=None,
        sensitivity_level="INTERNAL",
        upstream_signature="sig",
        content_signature=None,
        schema_incomplete=False,
        _src_deleted=None,
        _src_name=None,
        _src_domain=None,
    )
    repo.list_catalogs = AsyncMock(return_value=([cat], 1))
    repo.get_sources_meta = AsyncMock(return_value={"s1": ("MySQL 主库", False)})
    repo.get_sources_domain = AsyncMock(return_value={"s1": "sales"})
    repo.get_owner_names = AsyncMock(return_value={5: "张三"})
    repo.get_descriptions_for_catalogs = AsyncMock(return_value={})
    params = SimpleNamespace(
        source_id=None,
        entity_type=None,
        sensitivity_level=None,
        keyword=None,
        domain=None,
        source_status=None,
        page=1,
        page_size=20,
    )

    resp = await svc.list_catalogs(params)

    assert resp.total == 1
    item = resp.items[0]
    assert item.source_name == "MySQL 主库"
    assert item.domain == "sales"
    assert item.owner_name == "张三"
    assert item.owner_id == 5
    repo.get_sources_domain.assert_awaited_once_with(["s1"])
    repo.get_owner_names.assert_awaited_once_with([5])



# ---- 采集运行历史（CollectionRun）repository 测试 ----

async def test_repo_create_collection_run_defaults_running():
    s = _session()
    repo = CollectorRepository(s)
    run = await repo.create_collection_run(
        source_id="src1", trigger="manual", mode="FULL", actor_id=5
    )
    assert run.status == "RUNNING"
    assert run.trigger == "manual"
    assert run.mode == "FULL"
    assert run.actor_id == 5
    assert run.job_id is None
    assert run.started_at is not None
    s.add.assert_called_once()


async def test_repo_complete_collection_run_fills_metrics():
    fake = SimpleNamespace(
        status="RUNNING",
        finished_at=None,
        effective_mode=None,
        scanned=0,
        registered=0,
        pii_registered=0,
        failed_count=0,
        drift_count=0,
        deprecated_count=0,
        coverage=None,
        detail_json=None,
    )
    repo = CollectorRepository(_session(scalar_one_or_none=fake))
    result = {
        "mode": "FULL",
        "scanned": 10,
        "registered": 8,
        "pii_registered": 2,
        "failed_count": 1,
        "drift_count": 3,
        "deprecated_count": 4,
        "coverage": 0.8,
        "failed_specs": [{"entity_name": "t1", "error": "e"}],
        "drift_events": [{"entity_name": "t2", "change_type": "ADD_COLUMN"}],
    }
    out = await repo.complete_collection_run(1, result)
    assert out.status == "COMPLETED"
    assert out.scanned == 10
    assert out.drift_count == 3
    assert out.coverage == 0.8
    assert out.finished_at is not None
    assert out.detail_json["failed_specs"] == result["failed_specs"]


async def test_repo_fail_collection_run_records_error():
    fake = SimpleNamespace(status="RUNNING", finished_at=None, error=None)
    repo = CollectorRepository(_session(scalar_one_or_none=fake))
    out = await repo.fail_collection_run(1, "boom " * 200)
    assert out.status == "FAILED"
    assert len(out.error) <= 512
    assert out.finished_at is not None


async def test_repo_list_collection_runs_paginated():
    runs = [MagicMock(id=i) for i in range(2)]
    repo = CollectorRepository(_session(all_rows=runs, scalar=7))
    out, total = await repo.list_collection_runs(
        source_id="src1", status="COMPLETED", trigger=None, page=1, page_size=10
    )
    assert len(out) == 2
    assert total == 7


# ---- 采集运行历史（CollectionRun）service 测试 ----

async def test_svc_collection_run_lifecycle_commit():
    svc, repo = _svc()
    repo.create_collection_run = AsyncMock(return_value=SimpleNamespace(id=42))
    repo.complete_collection_run = AsyncMock()
    repo.fail_collection_run = AsyncMock()
    run_id = await svc.start_collection_run(source_id="s", trigger="scheduled", job_id="j1")
    assert run_id == 42
    svc._db.commit.assert_awaited()

    await svc.complete_collection_run(42, {"scanned": 5})
    repo.complete_collection_run.assert_awaited_once_with(42, {"scanned": 5})

    await svc.fail_collection_run(42, "err")
    repo.fail_collection_run.assert_awaited_once_with(42, "err")


async def test_svc_list_collection_runs_enriched():
    svc, repo = _svc()
    fake_run = SimpleNamespace(
        id=1,
        source_id="s1",
        job_id="j1",
        trigger="manual",
        mode="FULL",
        effective_mode="FULL",
        status="COMPLETED",
        actor_id=5,
        started_at=datetime(2026, 8, 1, tzinfo=UTC),
        finished_at=datetime(2026, 8, 1, 0, 1, tzinfo=UTC),
        scanned=10,
        registered=8,
        pii_registered=1,
        failed_count=0,
        drift_count=2,
        deprecated_count=0,
        coverage=0.5,
        error=None,
        detail_json={"failed_specs": []},
    )
    repo.list_collection_runs = AsyncMock(return_value=([fake_run], 1))
    repo.get_sources_meta = AsyncMock(return_value={"s1": ("MySQL", False)})
    repo.get_owner_names = AsyncMock(return_value={5: "张三"})
    result = await svc.list_collection_runs(page=1, page_size=10)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["source_name"] == "MySQL"
    assert item["actor_name"] == "张三"
    assert item["duration_seconds"] == 60.0
    assert item["detail"] is None  # 列表不携带 detail
    repo.get_sources_meta.assert_awaited_once_with(["s1"])


async def test_svc_get_collection_run_detail_includes_detail():
    svc, repo = _svc()
    fake_run = SimpleNamespace(
        id=1,
        source_id="s1",
        job_id=None,
        trigger="manual",
        mode="FULL",
        effective_mode=None,
        status="FAILED",
        actor_id=None,
        started_at=datetime(2026, 8, 1, tzinfo=UTC),
        finished_at=None,
        scanned=0,
        registered=0,
        pii_registered=0,
        failed_count=0,
        drift_count=0,
        deprecated_count=0,
        coverage=None,
        error="boom",
        detail_json={"failed_specs": [{"entity_name": "t", "error": "e"}]},
    )
    repo.get_collection_run = AsyncMock(return_value=fake_run)
    repo.get_sources_meta = AsyncMock(return_value={"s1": ("MySQL", False)})
    repo.get_owner_names = AsyncMock(return_value={})
    result = await svc.get_collection_run_detail(1)
    assert result["status"] == "FAILED"
    assert result["detail"] == fake_run.detail_json
    assert result["source_name"] == "MySQL"


# ============================================================
# 三期数据地基：DataSource 治理字段 + 采集 include/exclude 过滤
# ============================================================


def test_data_source_model_governance_columns():
    """模型字段映射：新增 6 个治理列且类型/外键正确。"""
    from sqlalchemy.dialects.mysql import JSON

    from app.models.data_source import DataSource

    cols = DataSource.__table__.columns
    for col in (
        "owner_id",
        "description",
        "include_patterns",
        "exclude_patterns",
        "health_metrics",
        "degraded_since",
    ):
        assert col in cols, f"缺失治理列: {col}"
    # owner_id 为 FK → user.id
    fks = list(cols["owner_id"].foreign_keys)
    assert fks and "user" in fks[0].target_fullname
    # include/exclude/health_metrics 为 JSON 类型
    assert isinstance(cols["include_patterns"].type, JSON)
    assert isinstance(cols["exclude_patterns"].type, JSON)
    assert isinstance(cols["health_metrics"].type, JSON)


def test_migration_0052_importable():
    """迁移可导入且 revision/down_revision/upgrade/downgrade 齐备。"""
    import importlib.util
    import os

    path = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "alembic",
            "versions",
            "0052_data_source_governance.py",
        )
    )
    spec = importlib.util.spec_from_file_location("m0052_governance", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0052_data_source_governance"
    # 当前 alembic 链头为 0051，故本迁移前驱指向 0051（保持线性）
    assert mod.down_revision == "0051_metric_template_owner"
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


# ---------- include/exclude 过滤（fnmatch） ----------


def _make_filter_collector(include=None, exclude=None):
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    c = InformationSchemaCollector.__new__(InformationSchemaCollector)
    c._connector = None
    c._database = None
    c._include_patterns = include
    c._exclude_patterns = exclude
    return c


def test_include_exclude_filter_fnmatch_match():
    """fnmatch 风格匹配：orders* 命中 orders/orders_x，不命中 users。"""
    c = _make_filter_collector(include=["orders*"], exclude=None)
    assert c._keep_table("orders") is True
    assert c._keep_table("orders_2026") is True
    assert c._keep_table("users") is False


def test_include_exclude_filter_schema_dot_naming():
    """多库实体名 finance.orders 受 include=finance.* 命中。"""
    c = _make_filter_collector(include=["finance.*"], exclude=None)
    assert c._keep_table("finance.orders") is True
    assert c._keep_table("sales.orders") is False


def test_include_exclude_filter_empty_no_filter():
    """空 pattern（None）不过滤：全部保留。"""
    c = _make_filter_collector(include=None, exclude=None)
    assert c._keep_table("any_table") is True


def test_exclude_pattern_drops_match_without_include():
    """未设 include 时，exclude 黑名单命中即丢弃。"""
    c = _make_filter_collector(include=None, exclude=["*_tmp"])
    assert c._keep_table("orders_tmp") is False
    assert c._keep_table("orders") is True


def test_include_priority_overrides_exclude():
    """include 优先：同时命中 include 与 exclude 时白名单胜出（保留）。"""
    c = _make_filter_collector(include=["orders", "users"], exclude=["users"])
    assert c._keep_table("users") is True  # include 优先于 exclude
    assert c._keep_table("orders") is True
    assert c._keep_table("legacy") is False  # 未命中 include 直接拒绝


class _IncludeExcludeConnector:
    """按 schema 返回表清单的假连接器（多库采集端到端过滤验证）。"""

    def __init__(self, tables_by_schema: dict[str, list[str]]) -> None:
        self._tables_by_schema = tables_by_schema

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        if "information_schema.tables" in sql:
            schema = (params or {}).get("schema")
            return [{"table_name": t} for t in self._tables_by_schema.get(schema, [])]
        return []

    async def dispose(self) -> None:
        return None


async def test_collector_collect_applies_include_exclude_patterns():
    """采集端到端：仅 include 命中的表进入 specs（exclude 冗余亦无碍）。"""
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    connector = _IncludeExcludeConnector({"finance": ["orders", "users", "legacy_table"]})
    collector = InformationSchemaCollector(
        connector,
        include_patterns=["finance.orders"],
        exclude_patterns=["finance.users"],
    )
    collector.set_databases(["finance"])
    result = await collector.collect(MagicMock(source_id="s"))
    assert [s.entity_name for s in result.specs] == ["finance.orders"]


async def test_collector_collect_include_priority_in_collect():
    """采集端到端验证 include 优先：users 虽命中黑名单仍被保留。"""
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    connector = _IncludeExcludeConnector({"finance": ["orders", "users"]})
    collector = InformationSchemaCollector(
        connector,
        include_patterns=["finance.orders", "finance.users"],
        exclude_patterns=["finance.users"],
    )
    collector.set_databases(["finance"])
    result = await collector.collect(MagicMock(source_id="s"))
    assert sorted(s.entity_name for s in result.specs) == ["finance.orders", "finance.users"]


async def test_collector_collect_empty_patterns_no_filter():
    """采集端到端：未传 patterns 时不过滤（全量）。"""
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    connector = _IncludeExcludeConnector({"finance": ["orders", "users"]})
    collector = InformationSchemaCollector(connector)
    collector.set_databases(["finance"])
    result = await collector.collect(MagicMock(source_id="s"))
    assert sorted(s.entity_name for s in result.specs) == ["finance.orders", "finance.users"]


async def test_collector_collect_multi_db_via_set_databases():
    """多目标库：set_databases 注入后逐库扫描，实体以 库.表 命名避免跨库冲突。"""
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    connector = _IncludeExcludeConnector(
        {"finance": ["orders", "users"], "sales": ["orders", "users"]}
    )
    collector = InformationSchemaCollector(connector)
    collector.set_databases(["finance", "sales"])
    result = await collector.collect(MagicMock(source_id="s"))
    assert sorted(s.entity_name for s in result.specs) == [
        "finance.orders",
        "finance.users",
        "sales.orders",
        "sales.users",
    ]


async def test_collector_collect_filtered_stats_and_multi_db():
    """方案 B：表级过滤统计——filtered_count / filtered_names 在 CollectResult 透出。"""
    from app.services.collector.connectors.mysql import InformationSchemaCollector

    connector = _IncludeExcludeConnector(
        {"finance": ["orders", "tmp_backup"], "sales": ["orders", "tmp_backup"]}
    )
    collector = InformationSchemaCollector(connector)
    collector.set_databases(["finance", "sales"])
    collector.set_table_filter(include_patterns=["*orders"], exclude_patterns=["tmp_*"])
    result = await collector.collect(MagicMock(source_id="s"))
    # 仅 include 命中的表进入 specs；tmp_backup 跨两个库都被过滤
    assert sorted(s.entity_name for s in result.specs) == ["finance.orders", "sales.orders"]
    assert result.filtered_count == 2
    assert sorted(result.filtered_names) == ["finance.tmp_backup", "sales.tmp_backup"]


# ---------- repository.set_source_governance ----------


async def test_repo_set_source_governance_updates_fields():
    src = MagicMock()
    src.owner_id = None
    src.description = None
    src.include_patterns = None
    src.exclude_patterns = None
    repo = CollectorRepository(_session(scalar_one_or_none=src))
    result = await repo.set_source_governance(
        "s",
        owner_id=7,
        description="财务库",
        include_patterns=["finance.*"],
        exclude_patterns=["*_tmp"],
    )
    assert result is src
    assert src.owner_id == 7
    assert src.description == "财务库"
    assert src.include_patterns == ["finance.*"]
    assert src.exclude_patterns == ["*_tmp"]


async def test_repo_set_source_governance_partial_keeps_untouched():
    """PATCH 语义：仅更新传入字段，未传保持原值。"""
    src = MagicMock()
    src.owner_id = 1
    src.description = "原描述"
    src.include_patterns = ["a"]
    src.exclude_patterns = ["b"]
    repo = CollectorRepository(_session(scalar_one_or_none=src))
    await repo.set_source_governance("s", description="新描述")
    assert src.description == "新描述"
    assert src.owner_id == 1
    assert src.include_patterns == ["a"]


async def test_repo_set_source_governance_not_found():
    repo = CollectorRepository(_session(scalar_one_or_none=None))
    assert await repo.set_source_governance("s", owner_id=1) is None


# ---------- service.update_source 治理字段 ----------


def _apply_governance(src, **kwargs):
    for key, value in kwargs.items():
        if value is not None:
            setattr(src, key, value)
    return src


async def test_update_source_sets_governance_fields():
    """update_source 支持 owner_id/description/patterns（经 set_source_governance）。"""
    from app.models.data_source import DataSource

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
    repo.set_source_governance = AsyncMock(
        side_effect=lambda source_id, **kw: _apply_governance(src, **kw)
    )
    resp = await svc.update_source(
        "src1",
        DataSourceUpdateRequest(
            owner_id=9,
            description="用途说明",
            include_patterns=["t*"],
            exclude_patterns=["tmp*"],
        ),
        actor_id=1,
    )
    assert resp.owner_id == 9
    assert resp.description == "用途说明"
    assert resp.include_patterns == ["t*"]
    assert resp.exclude_patterns == ["tmp*"]


async def test_update_source_governance_partial_keeps_untouched():
    """PATCH 语义：仅传 description 时 owner/patterns 保持原值。"""
    from app.models.data_source import DataSource

    svc, repo = _svc()
    src = DataSource(
        source_id="src1",
        name="S",
        source_type="mysql",
        connection_config="cipher",
        domain="d",
        quota={},
        created_by=1,
        owner_id=3,
        description="原",
        include_patterns=["a"],
        exclude_patterns=["b"],
    )
    repo.get_source = AsyncMock(return_value=src)
    repo.set_source_governance = AsyncMock(
        side_effect=lambda source_id, **kw: _apply_governance(src, **kw)
    )
    await svc.update_source("src1", DataSourceUpdateRequest(description="新"), actor_id=1)
    assert src.description == "新"
    assert src.owner_id == 3
    assert src.include_patterns == ["a"]


# ---- 三期：健康状态机 / 列表信号 / 资产概览 / 批量探活 / 批量调度 ----


async def test_evaluate_health_all_success_healthy() -> None:
    """全部成功 → healthy，degraded_since 为 None。"""
    status, metrics, degraded = CollectorService._evaluate_health_after_collect(
        None, attempted=10, failed=0
    )
    assert status == "healthy"
    assert metrics["success_rate"] == 1.0
    assert degraded is None


async def test_evaluate_health_high_failure_degraded() -> None:
    """失败率 ≥5% → DEGRADED，记录 degraded_since。"""
    status, metrics, degraded = CollectorService._evaluate_health_after_collect(
        None, attempted=10, failed=2
    )
    assert status == "degraded"
    assert metrics["fail_count"] == 2
    assert degraded is not None


async def test_evaluate_health_recovery_clears_degraded() -> None:
    """此前降级（90%）后连续成功 → healthy，degraded_since 清空。"""
    prev = {"ok_count": 9, "fail_count": 1, "sample_count": 10}
    status, metrics, degraded = CollectorService._evaluate_health_after_collect(
        prev, attempted=30, failed=0
    )
    assert status == "healthy"
    assert metrics["ok_count"] == 19  # (9+30)//2 窗口减半后
    assert degraded is None


async def test_evaluate_health_window_decay() -> None:
    """滑动窗口超限整体减半，稀释历史噪声。"""
    prev = {"ok_count": 100, "fail_count": 100, "sample_count": 200}
    status, metrics, _degraded = CollectorService._evaluate_health_after_collect(
        prev, attempted=1, failed=1
    )
    assert metrics["ok_count"] <= 51  # (100+0)//2
    assert metrics["fail_count"] <= 51  # (100+1)//2
    assert metrics["sample_count"] <= 101
    assert status in ("healthy", "degraded")


async def test_list_sources_backfills_signals() -> None:
    """列表接口批量回填表数/PII/最近采集/漂移信号。"""
    svc, repo = _svc()
    repo.list_sources = AsyncMock(
        return_value=([_make_src_with_config({"host": "h"})], 1)
    )
    repo.list_sources_signals = AsyncMock(
        return_value={
            "s1": {
                "table_count": 5,
                "pii_count": 2,
                "last_collected_at": datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
                "drift_count": 3,
            }
        }
    )
    items, total = await svc.list_sources(page=1, page_size=20)
    assert total == 1
    assert items[0].table_count == 5
    assert items[0].pii_count == 2
    assert items[0].last_collected_at == "2026-08-01T12:00:00+00:00"
    assert items[0].drift_count == 3


async def test_list_sources_passes_source_status_to_repo() -> None:
    """service.list_sources 透传 source_status 给 repository（数据源筛选下拉联动已删源）。"""
    svc, repo = _svc()
    repo.list_sources = AsyncMock(return_value=([_make_src_with_config({"host": "h"})], 1))
    repo.list_sources_signals = AsyncMock(return_value={})
    await svc.list_sources(page=1, page_size=20, source_status="deleted")
    assert repo.list_sources.call_args.kwargs["source_status"] == "deleted"


async def test_get_health_includes_degraded_info() -> None:
    """健康端点返回 health_metrics 与 degraded_since（黄态展示依据）。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    src.health_status = "degraded"
    src.health_metrics = {"success_rate": 0.8, "ok_count": 8, "fail_count": 2}
    src.degraded_since = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    repo.get_source = AsyncMock(return_value=src)
    health = await svc.get_health("s1")
    assert health["health_status"] == "degraded"
    assert health["health_metrics"]["success_rate"] == 0.8
    assert health["degraded_since"] == "2026-08-01T12:00:00+00:00"
    assert health["uptime_check"] is False


def test_health_status_enum_includes_degraded() -> None:
    """模型 health_status Enum 含 degraded——与 0065 迁移一致。

    防止未来删值导致健康状态机（_evaluate_health_after_collect 产出 degraded）
    在 ORM/DB 层写崩（DataError 1265 Data truncated，采集整批失败）。
    """
    from app.models.data_source import DataSource

    enums = DataSource.__table__.c.health_status.type.enums
    assert "degraded" in enums
    # 既有三值保留（存储序号不变，存量行语义不漂移）
    assert set(enums) == {"healthy", "unhealthy", "unknown", "degraded"}


async def test_collect_failure_path_rollbacks_then_writes_unhealthy() -> None:
    """采集抛异常时先 rollback（清 PendingRollback）再写 unhealthy，不被掩盖。

    回归背景：collect 异常可能已让 session 进入 PendingRollback，若直接
    update_health_status + commit 会抛 PendingRollbackError——掩盖原始异常、
    健康状态永不落库。
    """
    svc, repo = _svc()
    repo.get_source = AsyncMock(
        return_value=MagicMock(enabled=True, source_type="mysql", connection_config="enc")
    )
    repo.update_health_status = AsyncMock()

    class BoomCollector:
        def set_incremental_context(self, mode, watermark_ts=None):
            return None

        async def collect(self, source: object) -> CollectResult:
            raise RuntimeError("collect boom")

    with pytest.raises(RuntimeError, match="collect boom"):
        await svc.collect_and_register("s", BoomCollector(), actor_id=1)

    # 关键顺序：先 rollback 释放会话，再写 unhealthy，最后 commit
    svc._db.rollback.assert_awaited_once()
    repo.update_health_status.assert_awaited_once_with("s", "unhealthy", error="collect boom")
    svc._db.commit.assert_awaited_once()


async def test_get_source_overview_aggregates() -> None:
    """资产概览聚合返回实体/PII 分布、字段数、漂移、水位。"""
    svc, repo = _svc()
    repo.get_source_overview = AsyncMock(
        return_value={
            "source_id": "s1",
            "entity_types": {"TABLE": 5, "VIEW": 1},
            "by_sensitivity": {"INTERNAL": 4, "PII": 2},
            "total_fields": 42,
            "drift_count": 3,
            "coverage": 0.8,
            "last_collected_at": "2026-08-01T12:00:00+00:00",
            "scanned_count": 6,
            "failed_count": 0,
        }
    )
    overview = await svc.get_source_overview("s1")
    assert overview["total_fields"] == 42
    assert overview["by_sensitivity"]["PII"] == 2
    assert overview["drift_count"] == 3


async def test_get_source_overview_not_found() -> None:
    """概览不存在的数据源 → 404。"""
    svc, repo = _svc()
    repo.get_source_overview = AsyncMock(return_value={})
    with pytest.raises(NotFoundError):
        await svc.get_source_overview("ghost")


async def test_batch_test_sources_probe_ok() -> None:
    """批量探活：probe 成功 → succeeded + healthy。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    repo.get_source = AsyncMock(return_value=src)
    repo.update_health_status = AsyncMock()
    fake_probe = SimpleNamespace(ok=True, error=None, latency_ms=5)
    with patch(
        "app.services.collector.connectors.registry.build_from_cfg"
    ) as build:
        collector = MagicMock()
        collector.probe = AsyncMock(return_value=fake_probe)
        collector.dispose = AsyncMock()
        build.return_value = collector
        result = await svc.batch_test_sources(["s1"], actor_id=1)
    assert len(result.succeeded) == 1
    assert len(result.failed) == 0
    repo.update_health_status.assert_awaited_with("s1", "healthy")


async def test_batch_test_sources_probe_failed_and_not_found() -> None:
    """批量探活：probe 失败 → unhealthy + PROBE_FAILED；不存在 → NOT_FOUND。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    repo.get_source = AsyncMock(side_effect=[src, None])
    repo.update_health_status = AsyncMock()
    fake_probe = SimpleNamespace(ok=False, error="connect refused", latency_ms=0)
    with patch(
        "app.services.collector.connectors.registry.build_from_cfg"
    ) as build:
        collector = MagicMock()
        collector.probe = AsyncMock(return_value=fake_probe)
        collector.dispose = AsyncMock()
        build.return_value = collector
        result = await svc.batch_test_sources(["s1", "ghost"], actor_id=1)
    assert len(result.succeeded) == 0
    assert len(result.failed) == 2
    codes = {f.error_code for f in result.failed}
    assert codes == {"PROBE_FAILED", "NOT_FOUND"}
    repo.update_health_status.assert_awaited_with(
        "s1", "unhealthy", error="connect refused"
    )


async def test_batch_schedule_sources_success() -> None:
    """批量调度：统一设置 schedule_cron 并逐条回执。"""
    svc, repo = _svc()
    src = _make_src_with_config({"host": "h"})
    repo.get_source = AsyncMock(return_value=src)
    result = await svc.batch_schedule_sources(["s1"], "0 2 * * *", actor_id=1)
    assert len(result.succeeded) == 1
    assert src.schedule_cron == "0 2 * * *"


async def test_repo_get_description_coverage_pagination() -> None:
    """P1-8: per_table 服务端分页——page_size 构造 limit/offset，元信息透传。"""
    s = _coverage_session()
    repo = CollectorRepository(s)
    cov = await repo.get_description_coverage(page=2, page_size=1)

    assert cov["per_table_total"] == 2
    assert cov["page"] == 2
    assert cov["page_size"] == 1
    # 分页明细语句带 offset/limit（第二条 execute 为分页表查询）
    page_stmt = s.execute.call_args_list[1].args[0]
    assert page_stmt._limit == 1
    assert page_stmt._offset == 1
    # 汇总指标不受分页影响（SQL 端聚合，全量口径；字段覆盖仅计 manual/llm）
    assert cov["total_tables"] == 2
    assert cov["fields_with_desc"] == 1


async def test_repo_get_description_coverage_filters() -> None:
    """治理筛选：source_id/keyword 过滤应用到汇总与明细，字段覆盖统计 join db_catalog 收窄。"""
    s = _coverage_session()
    repo = CollectorRepository(s)
    cov = await repo.get_description_coverage(source_id="s1", keyword="order")

    # 4 次 scalar（total_tables/tables_with_desc/fields_with_desc/per_table_total）
    # 的语句均携带 source_id 精确匹配与 entity_name LIKE（治理筛选口径）
    for call in s.scalar.call_args_list:
        text = str(call.args[0])
        assert "source_id" in text
        assert "LIKE" in text
    # fields_with_desc（第 3 次 scalar）必须 join db_catalog——否则字段覆盖统计不随
    # 数据源/表筛选收窄（修复点：原查询仅扫 column_descriptions，筛选会失真）
    fields_stmt = str(s.scalar.call_args_list[2].args[0])
    assert "column_descriptions" in fields_stmt
    assert "JOIN db_catalog" in fields_stmt
    # 分页明细语句同样过滤
    page_text = str(s.execute.call_args_list[1].args[0])
    assert "source_id" in page_text and "LIKE" in page_text
    # 汇总结果仍按 mock 口径返回（filter 只影响 SQL 构造，不影响 mock 返回值）
    assert cov["total_tables"] == 2
    assert cov["fields_with_desc"] == 1


async def test_repo_get_description_coverage_database_filter() -> None:
    """治理筛选：database 库名过滤——entity_name 前缀精确匹配「库.表」应用到汇总与明细。"""
    s = _coverage_session()
    repo = CollectorRepository(s)
    cov = await repo.get_description_coverage(database="ods")

    # 4 次 scalar + 分页明细语句均携带 entity_name LIKE 'ods.%'（前缀匹配，防库名串库）
    for call in s.scalar.call_args_list:
        text = str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "entity_name" in text
        assert "LIKE" in text
        assert "ods.%" in text
    page_text = str(
        s.execute.call_args_list[1].args[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "ods.%" in page_text
    # 汇总结果不受 filter 影响（mock 口径）
    assert cov["total_tables"] == 2
    assert cov["fields_with_desc"] == 1


async def test_repo_get_description_coverage_database_escape() -> None:
    """库名过滤通配符转义：库名含 % / _ 时按字面匹配（对齐 list_catalogs 防模糊放大）。"""
    s = _coverage_session()
    repo = CollectorRepository(s)
    await repo.get_description_coverage(database="a_b%c")

    text = str(s.scalar.call_args_list[0].args[0].compile(compile_kwargs={"literal_binds": True}))
    # 转义后模式：a/_b/%c.% —— % 与 _ 均被 / 转义，只保留字面前缀
    assert "a/_b/%c.%" in text
    assert "ESCAPE" in text


# ---------- P2-10/12/13 ----------


def test_sanitize_conn_error_redacts_credentials():
    """P2-10: 连接错误脱敏——DSN/URL 内嵌凭据与 password= 值被掩码。"""
    from app.services.collector.service import _sanitize_conn_error

    raw = "Access denied for user 'root'@'10.0.0.5' (using password: YES) mysql://root:secret123@db.internal:3306/mydb"
    out = _sanitize_conn_error(raw)
    assert "secret123" not in out
    assert "***:***@" in out
    # 保留主机与错误语义
    assert "db.internal" in out

    raw2 = "connection failed password=abc123 user=admin; timeout"
    out2 = _sanitize_conn_error(raw2)
    assert "abc123" not in out2
    assert "password=***" in out2

    assert _sanitize_conn_error("") == ""
    assert _sanitize_conn_error("普通错误") == "普通错误"


def test_batch_schedule_cron_validation_rejects_invalid():
    """P2-12: 非法 cron 表达式在写入口 422 拒绝（防调度静默失效）。"""
    from pydantic import ValidationError

    from app.services.collector.schemas import BatchScheduleRequest, ScheduleRequest

    with pytest.raises(ValidationError):
        BatchScheduleRequest(source_ids=["s1"], schedule_cron="not-a-cron")
    with pytest.raises(ValidationError):
        ScheduleRequest(cron="99 99 * * *")
    # 合法 cron 通过
    ok = BatchScheduleRequest(source_ids=["s1"], schedule_cron="0 3 * * *")
    assert ok.schedule_cron == "0 3 * * *"


async def test_repo_purge_collection_runs_terminal_only():
    """P2-13: 清理仅删终态（COMPLETED/FAILED）且早于保留期的记录。"""
    s = MagicMock()
    result = MagicMock()
    result.rowcount = 5
    s.execute = AsyncMock(return_value=result)
    repo = CollectorRepository(s)

    before = datetime.now(UTC) - timedelta(days=90)
    purged = await repo.purge_collection_runs(before)

    assert purged == 5
    # 删除条件包含终态过滤（RUNNING 永不清理）
    stmt = s.execute.call_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "COMPLETED" in sql and "FAILED" in sql


async def test_repo_summarize_collection_runs_single_query():
    """#23: 采集记录 summary 服务端聚合——单次 SQL 聚合 total/completed/failed/扫描/注册。

    前端不再用 page_size=200 拉全量客户端聚合（总数>200 时口径矛盾）。
    """
    s = MagicMock()
    row = MagicMock()
    row.total = 120
    row.completed = 100
    row.failed = 15
    row.scanned = 4000
    row.registered = 3990
    result = MagicMock()
    result.one.return_value = row
    s.execute = AsyncMock(return_value=result)
    repo = CollectorRepository(s)

    summary = await repo.summarize_collection_runs(
        source_id="s1", status=None, trigger=None
    )

    assert summary == {
        "total": 120,
        "completed": 100,
        "failed": 15,
        "scanned": 4000,
        "registered": 3990,
    }
    # 单次查询（非逐条拉取）
    assert s.execute.await_count == 1
    sql = str(s.execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "COMPLETED" in sql and "FAILED" in sql


# ---------- 采集运行日志（collection_run_log 表） ----------


async def test_repo_append_run_logs_bulk_adds_and_flushes():
    s = _session()
    s.add_all = MagicMock()
    repo = CollectorRepository(s)

    await repo.append_run_logs(
        42,
        [
            {"ts": "2026-08-27T10:00:00", "level": "INFO", "phase": "start", "message": "开始采集"},
            {
                "ts": "2026-08-27T10:00:01",
                "level": "ERROR",
                "phase": "registering",
                "entity_name": "t",
                "message": "注册失败：t",
            },
        ],
    )

    s.add_all.assert_called_once()
    added = s.add_all.call_args.args[0]
    assert len(added) == 2
    assert added[0].run_id == 42
    assert added[0].level == "INFO"
    assert added[1].level == "ERROR"
    assert added[1].entity_name == "t"
    s.flush.assert_awaited_once()


async def test_repo_append_run_logs_skips_empty():
    s = _session()
    s.add_all = MagicMock()
    repo = CollectorRepository(s)

    await repo.append_run_logs(42, [])

    s.add_all.assert_not_called()
    s.flush.assert_not_awaited()


async def test_repo_has_run_logs_true_when_count_nonzero():
    s = _session(scalar=3)
    repo = CollectorRepository(s)

    assert await repo.has_run_logs(42) is True


async def test_repo_has_run_logs_false_when_zero():
    s = _session(scalar=0)
    repo = CollectorRepository(s)

    assert await repo.has_run_logs(42) is False


async def test_repo_list_run_logs_paginates_and_orders():
    s = _session(scalar=2)
    row1 = MagicMock(
        ts=datetime(2026, 8, 27, tzinfo=UTC),
        level="INFO",
        phase="start",
        entity_name=None,
        message="开始采集",
    )
    row2 = MagicMock(
        ts=datetime(2026, 8, 27, tzinfo=UTC),
        level="INFO",
        phase="registering",
        entity_name="t",
        message="注册 1/2：t",
    )
    s.execute = AsyncMock(
        return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: [row1, row2]))
    )
    repo = CollectorRepository(s)

    rows, total = await repo.list_run_logs(42, 0, 10)

    assert total == 2
    assert rows == [row1, row2]
    # 查询按 run_id 过滤
    sql = str(s.execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "collection_run_log" in sql
    assert "42" in sql


async def test_repo_purge_collection_runs_deletes_logs_before_runs():
    s = _session()
    res_log = MagicMock()
    res_log.rowcount = 12  # 级联删除的日志行数
    res_run = MagicMock()
    res_run.rowcount = 5  # 删除的运行记录数
    s.execute = AsyncMock(side_effect=[res_log, res_run])
    repo = CollectorRepository(s)

    purged = await repo.purge_collection_runs(datetime(2026, 1, 1, tzinfo=UTC))

    assert purged == 5
    # 先删日志子表、再删主表（外键约束顺序）
    assert s.execute.await_count == 2
    sql1 = str(s.execute.call_args_list[0].args[0].compile(compile_kwargs={"literal_binds": True}))
    sql2 = str(s.execute.call_args_list[1].args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "collection_run_log" in sql1
    assert "collection_run" in sql2
