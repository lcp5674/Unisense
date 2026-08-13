"""测试基础设施（对齐 DEV_GUIDE §8b 单元测试标准）。

必须在导入 app 之前设置环境变量（settings 在导入时即读取）。
提供：
- make_metric / make_create_payload：构造内存中 ORM 对象与请求体
- client：ASGI 测试客户端（覆盖 DB session / 当前用户依赖）
"""

from __future__ import annotations

import os


# 必须在导入 app 之前设置（settings 在导入时即读取环境变量）
def _default_db_url() -> str:
    """确定测试默认数据库 URL（本地 / CI 自适应）。

    优先级：
    1. 已显式设置 UNISENSE_DB_URL（开发者 / CI 覆盖）→ 尊重；
    2. 本地 .env 存在（docker-compose 起栈脚本写入，偏移端口 3307）
       → 复用其 host:port，用户改用 root（拥有建库权限）、库改用**专用测试库**
       ``unisense_it``——避免集成测试 DROP/CREATE 开发库 ``unisense``；
    3. CI（无 .env，gateways.yml 的 MySQL 服务 root:test@3306）→ 直接用默认。

    注意：``mysql+aiomysql`` 是 async 引擎驱动；alembic env.py 会自动转
    ``mysql+pymysql`` 供迁移子进程使用，两者均可。
    """
    env_url = os.environ.get("UNISENSE_DB_URL")
    if env_url:
        return env_url
    env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
    local_url: str | None = None
    try:
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("UNISENSE_DB_URL="):
                    local_url = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except OSError:
        pass
    if local_url and "@" in local_url and "/" in local_url:
        host_port = local_url.split("@", 1)[-1].split("/", 1)[0]
        return f"mysql+aiomysql://root:test@{host_port}/unisense_it?charset=utf8mb4"
    return "mysql+aiomysql://root:test@localhost:3306/unisense?charset=utf8mb4"


os.environ.setdefault("UNISENSE_DB_URL", _default_db_url())
os.environ.setdefault("UNISENSE_JWT_SECRET", "test-secret-key-for-unit-tests")
os.environ.setdefault("UNISENSE_ENVIRONMENT", "test")

# 以下导入依赖上面已设置的环境变量（settings 在导入时即读取），
# 因此位于 env 初始化之后，属 conftest 合法模式，忽略 E402。
from datetime import UTC, datetime  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from app.api import deps  # noqa: E402
from app.core import resilience  # noqa: E402
from app.main import app  # noqa: E402
from app.models.metric import Metric  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_circuit_breaker_store() -> None:
    """每个测试前重置熔断态共享存储，隔离 I-3 引入的模块级默认 store。

    I-3 让所有 ``store=None`` 的熔断器读写模块级 ``_DEFAULT_STORE``（跨 worker 协调用）。
    混沌测试中多个用例各自新建 ``CircuitBreaker()``（默认 name=unknown，共享同一 store key），
    若前一个用例打开熔断并写入 OPEN 态未清理，会污染后续用例。该 autouse fixture 保证每个
    测试从干净的 LocalCircuitBreakerStore 起步，使跨测试污染可控（生产行为不变）。
    """
    resilience.set_default_circuit_breaker_store(resilience.LocalCircuitBreakerStore())


def _now() -> datetime:
    return datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)


def make_metric(**overrides: object) -> Metric:
    """构造内存中的 Metric 实例（不落库）。"""
    defaults: dict[str, object] = {
        "id": 1,
        "metric_code": "sales_gmv_daily",
        "name": "每日 GMV",
        "domain": "sales",
        "type": "atomic",
        "granularity": "daily",
        "unit": "yuan",
        "currency": "CNY",
        "aggregation": "SUM",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "sla": "06:00",
        "dw_layer": "DWD",
        "metric_tier": "T3",
        "serving_mode": "BATCH_ONLY",
        "additivity": "ADDITIVE",
        "non_additive_dimensions": None,
        "definition_json": {
            "expression": "SUM(order_amount)",
            "dependencies": ["fct_order"],
            "source_fields": ["order_amount"],
            "partition_by": ["dt"],
        },
        "version": 1,
        "row_version": 1,
        "term_id": None,
        "status": "DRAFT",
        "owner_id": 1,
        "backup_owner_id": None,
        "approver_id": None,
        "pii_flag": False,
        "compliance_reviewed": False,
        "emergency_publish": False,
        "pending_conflict": False,
        "effective_version": None,
        "consumption_guide": None,
        "batch_id": None,
        "successor_code": None,
        "deprecated_at": None,
        "sunset_until": None,
        "created_at": _now(),
        "updated_at": _now(),
        "deleted_at": None,
    }
    defaults.update(overrides)
    metric = Metric()
    for key, value in defaults.items():
        setattr(metric, key, value)
    return metric


def make_create_payload(**overrides: object) -> dict:
    """构造合法的 MetricCreate 请求体（owner_id 来自鉴权，不在体内）。"""
    defaults: dict[str, object] = {
        "metric_code": "sales_gmv_amount_daily",
        "name": "每日 GMV",
        "domain": "sales",
        "type": "atomic",
        "granularity": "daily",
        "unit": "yuan",
        "currency": "CNY",
        "aggregation": "SUM",
        "time_semantics": "PERIOD",
        "freshness": "T1",
        "sla": "06:00",
        "dw_layer": "DWD",
        "metric_tier": "T3",
        "serving_mode": "BATCH_ONLY",
        "additivity": "ADDITIVE",
        "non_additive_dimensions": None,
        "definition_json": {
            "expression": "SUM(order_amount)",
            "dependencies": ["fct_order"],
            "source_fields": ["order_amount"],
            "partition_by": ["dt"],
        },
        "backup_owner_id": None,
        "pii_flag": False,
        "compliance_reviewed": False,
        "consumption_guide": None,
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
async def client():
    """ASGI 测试客户端：覆盖 DB 会话与当前用户依赖。

    会话用 AsyncMock（非 MagicMock）：API 层的 ``await db.commit()`` 属真实契约，
    不能让无法 await 的 MagicMock 掩盖「写操作未提交」类回归（对齐 D10 §6.3）。
    """

    async def fake_db():
        session = AsyncMock()
        # write_audit / ORM 以同步方式调用 session.add（不被 await），用普通 Mock 避免
        # AsyncMock 自动生成协程导致的「coroutine never awaited」告警
        session.add = MagicMock()
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(id=1, role="metric_owner")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def isolated_db_session():
    """每测试独立事务隔离（测试结束自动回滚，TECH-08）。"""
    from app.db.mysql import async_session_factory

    async with async_session_factory() as session, session.begin():
        yield session
        await session.rollback()
