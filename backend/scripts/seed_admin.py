"""初始化种子数据：默认组织 + 平台管理员账号。

用法（backend 目录，venv 激活）：
    export UNISENSE_DB_URL="mysql+aiomysql://unisense:unisense@localhost:3306/unisense?charset=utf8mb4"
    export UNISENSE_JWT_SECRET="<your-secret>"
    python scripts/seed_admin.py

说明：
    - 管理员密码取自 UNISENSE_SEED_ADMIN_PASSWORD（缺省 changeme123）。
    - 幂等：组织（code=default）与用户（username=admin）已存在则跳过。
    - 仅用于本地/测试引导，请勿在生产使用弱口令。

容器部署自举（scripts/bootstrap.py）复用 ``seed_admin(db)``：它只 flush 不 commit，
事务边界交给调用方，便于「admin + 主题域」在同一事务内整体提交。CLI 入口 ``seed()``
保留原有行为（自建会话 + commit + 释放全局引擎）。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import configure_logging
from app.core.security import hash_password
from app.db.mysql import async_session_factory, engine
from app.models.user import Organization, User

logger = structlog.get_logger("unisense.seed_admin")


def _resolve_admin_password() -> tuple[str, bool]:
    """解析初始密码，返回 ``(密码, 是否使用默认弱口令)``。"""
    for key in ("ADMIN_INITIAL_PASSWORD", "UNISENSE_SEED_ADMIN_PASSWORD"):
        value = os.getenv(key)
        if value:
            return value, False
    return "changeme123", True


async def seed_admin(db: AsyncSession) -> dict[str, Any]:
    """预置默认组织 + 平台管理员（幂等，只 flush 不 commit）。

    Args:
        db: 复用调用方会话（CLI 或部署自举）；本函数不提交、不释放引擎。

    Returns:
        ``{org_id, admin_id, org_created, admin_created, using_default_password}``。
    """
    admin_password, using_default = _resolve_admin_password()
    if using_default:
        logger.warning(
            "seed_admin_using_default_password",
            hint="设置 UNISENSE_SEED_ADMIN_PASSWORD 注入强口令，勿在生产使用默认值",
        )

    result = await db.execute(select(Organization).where(Organization.code == "default"))
    org = result.scalar_one_or_none()
    org_created = False
    if org is None:
        org = Organization(name="默认组织", code="default", status="active")
        db.add(org)
        await db.flush()
        org_created = True
        logger.info("seed_org_created", code="default", id=org.id)
    else:
        logger.info("seed_org_exists", code="default", id=org.id)

    result = await db.execute(select(User).where(User.username == "admin"))
    admin = result.scalar_one_or_none()
    admin_created = False
    if admin is None:
        admin = User(
            org_id=org.id,
            username="admin",
            email="admin@unisense.local",
            password_hash=await hash_password(admin_password),
            display_name="平台管理员",
            role="platform_admin",
            domain=None,
            status="active",
        )
        db.add(admin)
        await db.flush()
        admin_created = True
        logger.info("seed_admin_created", username="admin", role="platform_admin")
    else:
        logger.info("seed_admin_exists", username="admin", id=admin.id)

    return {
        "org_id": org.id,
        "admin_id": admin.id,
        "org_created": org_created,
        "admin_created": admin_created,
        "using_default_password": using_default,
    }


async def seed() -> None:
    """CLI 入口：自建会话 → 播种 → 提交 → 释放全局引擎（行为保持原样）。"""
    configure_logging()
    async with async_session_factory() as db:
        try:
            summary = await seed_admin(db)
            await db.commit()
            logger.info("seed_admin_complete", **summary)
        except Exception:
            await db.rollback()
            logger.exception("seed_admin_failed")
            raise

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
