"""采集领域真实 MySQL 集成测试（对齐 gateways integration + 工业级修复）。

用真实数据库验证：数据源加密落库、元数据注册敏感分级（PII）、批量废弃部分失败、
覆盖率重算、增量采集、健康状态更新。schema 由 Alembic 迁移（与生产一致）建表；
外部 MySQL 不可用时回退 testcontainers，二者皆不可用则跳过。

增强（工业级修复）：
- US3: 增量采集水位记录
- US5: 健康状态更新
- 多数据源连接器 mock 测试
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import NotFoundError
from app.db.mysql import Base
from app.models.user import Organization, User
from app.services.collector.schemas import (
    BulkDeprecateItem,
    BulkDeprecateRequest,
    DataSourceCreateRequest,
    DBCatalogCreateRequest,
    DBCatalogListParams,
)
from app.services.collector.service import CollectorService
from app.services.collector.spi import CatalogSpec, CollectResult

EXT_DB_URL = os.getenv("UNISENSE_INTEGRATION_DB_URL") or os.getenv("UNISENSE_DB_URL")
_USE_EXT = bool(EXT_DB_URL) and "localhost" in EXT_DB_URL


def _seed(session_factory) -> int:
    """种子：组织 + Owner 用户（data_source.created_by 外键）。"""

    async def _run() -> int:
        async with session_factory() as s:
            org = Organization(name="默认组织", code="default_org", status="active")
            s.add(org)
            await s.flush()
            user = User(
                org_id=org.id,
                username="owner",
                email="owner@example.com",
                password_hash="x",
                display_name="owner",
                role="metric_owner",
                status="active",
            )
            s.add(user)
            await s.flush()
            await s.commit()
            return user.id

    return asyncio.run(_run())


# backend 目录（alembic.ini 所在处），从测试文件推导，任意 cwd 均可运行
_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _reset_via_alembic(url: str) -> None:
    env = {**os.environ, "UNISENSE_DB_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        cwd=_BACKEND_DIR,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="function")
def db_env():
    if _USE_EXT:
        url = EXT_DB_URL.replace("mysql+pymysql", "mysql+aiomysql")
        engine = create_async_engine(url, echo=False, poolclass=NullPool)

        async def _wipe() -> None:
            async with engine.begin() as conn:
                await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
                await conn.run_sync(Base.metadata.drop_all)
                await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
                await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))

        asyncio.run(_wipe())
        _reset_via_alembic(EXT_DB_URL)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        owner_id = _seed(session_factory)
        yield {"engine": engine, "session_factory": session_factory, "owner_id": owner_id}
        asyncio.run(engine.dispose())
    else:
        pytest.importorskip("testcontainers")
        from testcontainers.mysql import MySqlContainer

        container = MySqlContainer("mysql:8.0")
        try:
            container.start()
        except Exception as exc:
            pytest.skip(f"MySQL 容器不可用，跳过集成测试: {exc}")

        url = container.get_connection_url().replace("mysql+pymysql", "mysql+aiomysql")
        engine = create_async_engine(url, echo=False)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async def _create_all() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(_create_all())
        owner_id = _seed(session_factory)
        yield {"engine": engine, "session_factory": session_factory, "owner_id": owner_id}
        container.stop()


async def test_create_source_encrypts_then_register_classifies_pii(db_env):
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    async with session_factory() as session:
        svc = CollectorService(session)
        src = await svc.create_source(
            DataSourceCreateRequest(
                source_id="src1",
                name="主库",
                source_type="mysql",
                connection_config={"host": "127.0.0.1", "password": "secret"},
                domain="db1",
            ),
            actor_id=owner_id,
        )
        await session.commit()
        assert src.connection_config_present is True

        # 注册含 user_name 的实体 -> PII
        cat = await svc.register_catalog(
            DBCatalogCreateRequest(
                source_id="src1",
                entity_name="users",
                schema_def={"columns": ["user_name", "email"]},
            ),
            actor_id=owner_id,
        )
        await session.commit()
        assert cat.sensitivity_level == "PII"

        # 列表可查
        listing = await svc.list_catalogs(
            DBCatalogListParams(source_id="src1", page=1, page_size=10)
        )
        assert listing.total == 1


async def test_bulk_deprecate_partial_on_real_db(db_env):
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    async with session_factory() as session:
        svc = CollectorService(session)
        await svc.create_source(
            DataSourceCreateRequest(
                source_id="src2",
                name="S2",
                source_type="mysql",
                connection_config={"host": "h"},
                domain="db2",
            ),
            actor_id=owner_id,
        )
        await svc.register_catalog(
            DBCatalogCreateRequest(
                source_id="src2", entity_name="orders", schema_def={"columns": ["order_id"]}
            ),
            actor_id=owner_id,
        )
        await session.commit()

        result = await svc.bulk_deprecate(
            BulkDeprecateRequest(
                items=[
                    BulkDeprecateItem(source_id="src2", entity_name="orders"),
                    BulkDeprecateItem(source_id="src2", entity_name="nonexistent"),
                ]
            ),
            actor_id=owner_id,
        )
        await session.commit()
        assert len(result.succeeded) == 1
        assert len(result.failed) == 1


async def test_coverage_recomputed_after_register(db_env):
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    async with session_factory() as session:
        svc = CollectorService(session)
        await svc.create_source(
            DataSourceCreateRequest(
                source_id="src3",
                name="S3",
                source_type="mysql",
                connection_config={"host": "h"},
                domain="db3",
            ),
            actor_id=owner_id,
        )
        await svc.register_catalog(
            DBCatalogCreateRequest(
                source_id="src3", entity_name="a", schema_def={"columns": ["x"]}
            ),
            actor_id=owner_id,
        )
        await svc.register_catalog(
            DBCatalogCreateRequest(
                source_id="src3", entity_name="b", schema_def={"columns": ["y"]}
            ),
            actor_id=owner_id,
        )
        await session.commit()
        src = await svc.get_source("src3")
        # P2-3: 无 quota 基线时 coverage=0.0（覆盖率未知），非误导性 1.0
        assert src.coverage == 0.0


async def test_delete_missing_source_raises(db_env):
    session_factory = db_env["session_factory"]
    async with session_factory() as session:
        svc = CollectorService(session)
        with pytest.raises(NotFoundError):
            await svc.delete_source("ghost")


async def test_health_status_updates_on_collect(db_env):
    """US5: 采集成功后 health_status 更新为 healthy。"""
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    async with session_factory() as session:
        svc = CollectorService(session)
        await svc.create_source(
            DataSourceCreateRequest(
                source_id="src_health",
                name="HealthTest",
                source_type="mysql",
                connection_config={"host": "h"},
                domain="db_health",
            ),
            actor_id=owner_id,
        )
        await session.commit()

        # 模拟采集成功
        class StubCollector:
            def set_incremental_context(self, mode, watermark_ts=None):
                return None

            async def collect(self, source: object) -> CollectResult:
                return CollectResult(
                    specs=[
                        CatalogSpec(
                            entity_name="t1",
                            entity_type="TABLE",
                            schema_json={"columns": ["a"]},
                        )
                    ],
                    failed_specs=[],
                    source_id="src_health",
                )

        await svc.collect_and_register("src_health", StubCollector(), actor_id=owner_id)
        await session.commit()

        # 验证健康状态更新
        health = await svc.get_health("src_health")
        assert health["health_status"] == "healthy"


async def test_watermark_created_after_collection(db_env):
    """US3: 采集完成后创建采集水位记录。"""
    session_factory = db_env["session_factory"]
    owner_id = db_env["owner_id"]
    async with session_factory() as session:
        svc = CollectorService(session)
        await svc.create_source(
            DataSourceCreateRequest(
                source_id="src_watermark",
                name="WatermarkTest",
                source_type="mysql",
                connection_config={"host": "h"},
                domain="db_wm",
            ),
            actor_id=owner_id,
        )
        await session.commit()

        class StubCollector:
            def set_incremental_context(self, mode, watermark_ts=None):
                return None

            async def collect(self, source: object) -> CollectResult:
                return CollectResult(
                    specs=[
                        CatalogSpec(
                            entity_name="t1",
                            entity_type="TABLE",
                            schema_json={"columns": ["a"]},
                        )
                    ],
                    failed_specs=[],
                    source_id="src_watermark",
                )

        await svc.collect_and_register("src_watermark", StubCollector(), actor_id=owner_id)
        await session.commit()

        # 验证水位记录
        watermark = await svc.get_watermark("src_watermark")
        assert watermark is not None
        assert watermark["mode"] == "FULL"
        assert watermark["scanned_count"] == 1


# ---------- 多数据源连接器 Mock 测试 ----------


async def test_postgres_collector_mock():
    """PostgreSQL 连接器 mock 采集测试。"""
    from app.services.collector.connectors.mysql import SqlalchemyConnector
    from app.services.collector.connectors.postgres import PostgresCollector

    mock_connector = MagicMock(spec=SqlalchemyConnector)
    mock_connector.query = AsyncMock(side_effect=[
        [{"table_name": "users"}, {"table_name": "orders"}],
        [
            {"column_name": "id", "data_type": "integer"},
            {"column_name": "name", "data_type": "varchar"},
        ],
        [{"column_name": "order_id", "data_type": "integer"}],
    ])
    mock_connector.dispose = AsyncMock()

    collector = PostgresCollector(mock_connector)
    source = MagicMock(source_id="pg_src", domain="public")
    result = await collector.collect(source)

    assert result.source_id == "pg_src"
    assert len(result.specs) == 2
    assert result.specs[0].entity_name == "users"
    assert result.failed_specs == []


async def test_clickhouse_collector_mock():
    """ClickHouse 连接器 mock 采集测试。"""
    from app.services.collector.connectors.clickhouse import ClickHouseCollector

    collector = ClickHouseCollector(host="ch-host", port=8123, database="analytics")

    # Mock _query method
    collector._query = AsyncMock(side_effect=[
        "events\nsessions\n",  # tables
        "event_id\tString\ntimestamp\tDateTime\n",  # events columns
        "session_id\tUInt64\n",  # sessions columns
    ])

    source = MagicMock(source_id="ch_src", domain="analytics")
    result = await collector.collect(source)

    assert result.source_id == "ch_src"
    assert len(result.specs) == 2
    assert result.specs[0].entity_name == "events"
    assert result.failed_specs == []


async def test_kafka_collector_mock():
    """Kafka 连接器 mock 采集测试。"""
    from app.services.collector.connectors.kafka import KafkaCollector

    collector = KafkaCollector(
        bootstrap_servers="kafka:9092",
        registry_url="http://schema-registry:8081",
    )

    # Mock _get_topics
    collector._get_topics = AsyncMock(return_value=[
        {"name": "user-events", "partition_count": 3, "replication_factor": 2},
        {"name": "order-events", "partition_count": 6, "replication_factor": 3},
    ])
    # Mock _get_subject_schemas
    collector._get_subject_schemas = AsyncMock(return_value={})

    source = MagicMock(source_id="kafka_src")
    result = await collector.collect(source)

    assert result.source_id == "kafka_src"
    assert len(result.specs) == 2
    assert result.specs[0].entity_name == "user-events"


async def test_doris_collector_uses_information_schema():
    """Doris 连接器复用 InformationSchemaCollector。"""
    from app.services.collector.connectors.doris import create_doris_collector

    cfg = {
        "host": "doris-host",
        "port": 9030,
        "user": "root",
        "password": "",
        "database": "test_db",
    }
    # 仅验证工厂函数不报错，返回类型正确
    # 实际连接需要真实 Doris 实例
    with patch("app.services.collector.connectors.doris.SqlalchemyConnector") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.query = AsyncMock(return_value=[])
        mock_instance.dispose = AsyncMock()
        mock_cls.return_value = mock_instance
        collector = create_doris_collector(cfg)
        assert collector is not None


async def test_starrocks_collector_uses_information_schema():
    """StarRocks 连接器复用 InformationSchemaCollector。"""
    from app.services.collector.connectors.starrocks import create_starrocks_collector

    cfg = {"host": "sr-host", "port": 9030, "user": "root", "password": "", "database": "test_db"}
    with patch("app.services.collector.connectors.starrocks.SqlalchemyConnector") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.query = AsyncMock(return_value=[])
        mock_instance.dispose = AsyncMock()
        mock_cls.return_value = mock_instance
        collector = create_starrocks_collector(cfg)
        assert collector is not None
