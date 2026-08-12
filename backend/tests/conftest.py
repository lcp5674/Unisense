"""测试基础设施（对齐 DEV_GUIDE §8b 单元测试标准）。

必须在导入 app 之前设置环境变量（settings 在导入时即读取）。
提供：
- make_metric / make_create_payload：构造内存中 ORM 对象与请求体
- client：ASGI 测试客户端（覆盖 DB session / 当前用户依赖）
"""

from __future__ import annotations

import os

# 必须在导入 app 之前设置（settings 在导入时即读取环境变量）
os.environ.setdefault(
    "UNISENSE_DB_URL",
    "mysql+aiomysql://unisense:unisense@localhost:3306/unisense?charset=utf8mb4",
)
os.environ.setdefault("UNISENSE_JWT_SECRET", "test-secret-key-for-unit-tests")
os.environ.setdefault("UNISENSE_ENVIRONMENT", "test")

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.main import app
from app.models.metric import Metric


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
