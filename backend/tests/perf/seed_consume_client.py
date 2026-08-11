"""consume 性能基线接入方种子（补齐 unisense_perf 的 api_client 行）。

依赖：先通过 UNISENSE_DB_URL 指向已迁移/已播种指标的 perf 库（见 seed_perf.py）。
幂等：client_id 已存在则跳过，可重复执行。

用法：
    UNISENSE_DB_URL='mysql+aiomysql://unisense:unisense@localhost:3307/unisense_perf?\
charset=utf8mb4' \\
    poetry run python backend/tests/perf/seed_consume_client.py
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from app.core.security import hash_password
from app.db.mysql import async_session_factory, engine
from app.models.consume import ApiClient, ApiClientStatus

CLIENT_ID = os.environ.get("PERF_CONSUME_CLIENT_ID", "perf_client")
CLIENT_SECRET = os.environ.get("PERF_CONSUME_SECRET", "PerfClient@123")


async def _seed() -> None:
    async with async_session_factory() as session:
        existing = await session.execute(select(ApiClient).where(ApiClient.client_id == CLIENT_ID))
        if existing.scalar_one_or_none() is not None:
            print(f"[seed] api_client {CLIENT_ID} 已存在，跳过")
            return
        # 取一个已存在用户作为 created_by（NOT NULL 外键语义由业务保证，此处填最小可用值）
        first_user = (await session.execute(select(ApiClient.id))).first()
        created_by = 1
        if first_user is None:
            # 退路：若 api_client 无记录，取任意 user
            from app.models.user import User

            u = (await session.execute(select(User.id).limit(1))).first()
            created_by = u[0] if u else 1

        client = ApiClient(
            client_id=CLIENT_ID,
            client_secret_ref=hash_password(CLIENT_SECRET),
            scope_domain=None,
            metric_whitelist=None,
            qps=200,
            daily_quota=1_000_000,
            scan_row_limit=None,
            status=ApiClientStatus.ACTIVE,
            created_by=created_by,
        )
        session.add(client)
        await session.commit()
        print(f"[seed] api_client {CLIENT_ID} 播种完成（qps=200, daily_quota=1_000_000）")


async def main() -> None:
    await _seed()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
