"""语义领域性能基线数据准备。

创建独立的 perf 库（unisense_perf），执行 Alembic 迁移，并播种：
- 组织 + 平台用户（perf / Perf@123，analyst）
- 30 条指标语义定义（供列表接口负载使用）

用法（先通过 UNISENSE_DB_URL 指向独立的 perf 库）：
    UNISENSE_DB_URL='mysql+aiomysql://unisense:unisense@localhost:3307/ \
        unisense_perf?charset=utf8mb4' \
    backend/.venv/bin/python backend/tests/perf/seed_perf.py
"""

from __future__ import annotations

import asyncio
import os

from alembic import command
from alembic.config import Config

from app.core.security import hash_password
from app.db.mysql import async_session_factory, engine
from app.models.measure_catalog import MeasureCatalog
from app.models.metric import Metric
from app.models.user import Organization, User

PERF_DB = os.environ.get("UNISENSE_PERF_DB", "unisense_perf")
# 由当前 UNISENSE_DB_URL 推导“无库名”的服务器连接
_base = os.environ.get(
    "UNISENSE_DB_URL",
    "mysql+aiomysql://unisense:unisense@localhost:3307/unisense?charset=utf8mb4",
)
_server = _base.split("?")[0].rsplit("/", 1)[0]


async def _create_database() -> None:
    import pymysql

    # 解析用户/密码/主机/端口
    # mysql+aiomysql://user:pass@host:port/db
    rest = _server.split("://", 1)[1]
    user, host_part = rest.split("@", 1)
    u, p = user.split(":", 1) if ":" in user else (user, "")
    host, port = host_part.split(":", 1) if ":" in host_part else (host_part, "3306")
    port = int(str(port).split("/")[0])
    conn = pymysql.connect(host=host, port=port, user=u, password=p)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{PERF_DB}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()


def _run_migrations() -> None:
    ini = os.path.join(os.path.dirname(__file__), "..", "..", "alembic.ini")
    cfg = Config(ini)
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(ini), "alembic"))
    command.upgrade(cfg, "head")


async def _seed() -> None:
    async with async_session_factory() as session:
        org = Organization(name="perf-org", code="perf", status="ACTIVE")
        session.add(org)
        await session.flush()

        user = User(
            username="perf",
            display_name="Perf",
            email="perf@example.com",
            role="analyst",
            domain="sales",
            org_id=org.id,
            password_hash=hash_password("Perf@123"),
            status="ACTIVE",
        )
        session.add(user)
        await session.flush()

        # OneData 原子层：逻辑度量目录（30 条原子指标复用同一逻辑度量，FK 需真实行）
        measure = MeasureCatalog(
            measure_code="perf_sales_amount",
            name="Perf 销售额",
            measure_format="AMOUNT",
            default_unit="yuan",
            default_decimal_places=2,
            domain="sales",
            owner_id=user.id,
            status="PUBLISHED",
        )
        session.add(measure)
        await session.flush()

        for i in range(30):
            session.add(
                Metric(
                    metric_code=f"perf_metric_{i:03d}",
                    name=f"Perf 指标 {i}",
                    domain="sales",
                    type="atomic",
                    granularity="daily",
                    # OneData 原子层：关联逻辑度量（不绑物理表）
                    measure_id=measure.id,
                    unit="yuan",
                    currency="CNY",
                    aggregation="SUM",
                    time_semantics="PERIOD",
                    freshness="T1",
                    sla="06:00",
                    dw_layer="DWD",
                    metric_tier="T3",
                    serving_mode="BATCH_ONLY",
                    additivity="ADDITIVE",
                    definition_json={
                        "expression": "SUM(amount)",
                        "dependencies": ["fct_order"],
                        "source_fields": ["amount"],
                        "partition_by": ["dt"],
                    },
                    version=1,
                    row_version=1,
                    status="PUBLISHED",
                    owner_id=user.id,
                    pii_flag=False,
                    compliance_reviewed=False,
                )
            )
        await session.commit()


async def main() -> None:
    await _create_database()
    _run_migrations()
    # 迁移后重新指向 perf 库：engine 已按 db_url 创建，这里靠环境变量切换，
    # 因此调用方需先设置 UNISENSE_DB_URL 指向 perf 库再运行本脚本。
    await _seed()
    await engine.dispose()
    print(f"[seed] perf 库 {PERF_DB} 播种完成（组织/用户/30 指标）")


if __name__ == "__main__":
    asyncio.run(main())
