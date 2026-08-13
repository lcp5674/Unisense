"""初始化种子数据：默认组织 + 平台管理员账号。

用法（backend 目录，venv 激活）：
    export UNISENSE_DB_URL="mysql+aiomysql://unisense:unisense@localhost:3306/unisense?charset=utf8mb4"
    export UNISENSE_JWT_SECRET="<your-secret>"
    python scripts/seed_admin.py

说明：
    - 管理员密码取自 UNISENSE_SEED_ADMIN_PASSWORD（缺省 changeme123）。
    - 幂等：组织（code=default）与用户（username=admin）已存在则跳过。
    - 仅用于本地/测试引导，请勿在生产使用弱口令。
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from app.core.security import hash_password
from app.db.mysql import async_session_factory, engine
from app.models.user import Organization, User


async def seed() -> None:
    admin_password = (
        os.getenv("ADMIN_INITIAL_PASSWORD")
        or os.getenv("UNISENSE_SEED_ADMIN_PASSWORD")
        or "changeme123"
    )
    if not os.getenv("ADMIN_INITIAL_PASSWORD") and not os.getenv("UNISENSE_SEED_ADMIN_PASSWORD"):
        print(
            "[seed] 提示：可用 ADMIN_INITIAL_PASSWORD 环境变量设置初始密码，"
            "当前使用默认值 changeme123"
        )

    async with async_session_factory() as db:
        result = await db.execute(select(Organization).where(Organization.code == "default"))
        org = result.scalar_one_or_none()
        if org is None:
            org = Organization(name="默认组织", code="default", status="active")
            db.add(org)
            await db.flush()
            print(f"[seed] 创建组织 code=default id={org.id}")
        else:
            print(f"[seed] 组织已存在 code=default id={org.id}")

        result = await db.execute(select(User).where(User.username == "admin"))
        if result.scalar_one_or_none() is None:
            db.add(
                User(
                    org_id=org.id,
                    username="admin",
                    email="admin@unisense.local",
                    password_hash=await hash_password(admin_password),
                    display_name="平台管理员",
                    role="platform_admin",
                    domain=None,
                    status="active",
                )
            )
            print("[seed] 创建管理员 username=admin role=platform_admin")
        else:
            print("[seed] 管理员已存在 username=admin")

        await db.commit()

    await engine.dispose()
    print("[seed] 完成")


if __name__ == "__main__":
    asyncio.run(seed())
