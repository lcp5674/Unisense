"""把部署托管账号（默认 admin）的密码对齐到部署环境变量。

用途：
    种子管理员 admin 的口令由部署侧 ``UNISENSE_SEED_ADMIN_PASSWORD`` 统一托管。
    应用内「自助改密 / 管理员重置」对托管账号一律 403 拒绝（见
    ``app/api/users.py`` 的 ``_assert_not_managed``）。当库中 admin 口令与 env
    不一致（如曾被人工改密、或需要轮换部署口令）时，用本脚本从 env 重新对齐：

        export UNISENSE_DB_URL="mysql+aiomysql://unisense:***@db:3306/unisense?charset=utf8mb4"
        export UNISENSE_SEED_ADMIN_PASSWORD="<部署口令>"
        python scripts/align_admin_password.py            # 预览差异
        python scripts/align_admin_password.py --apply    # 实际写库

行为：
    - 密码取自 ``ADMIN_INITIAL_PASSWORD`` / ``UNISENSE_SEED_ADMIN_PASSWORD``（缺省拒绝执行，
      绝不回退弱口令）。
    - 目标账号取自 ``UNISENSE_MANAGED_ACCOUNTS``（逗号分隔，默认 admin），逐个对齐。
    - 对齐后 ``must_change_password=False``——部署口令是受控的，不应触发首登改密
      （否则改密入口又对托管账号 403，会导致账号无法正常使用）。
    - 默认 dry-run 只报告「哈希是否已匹配」，``--apply`` 才提交；已匹配则不写库。
    - 全程不打印明文密码。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import configure_logging
from app.core.security import hash_password, verify_password
from app.db.mysql import async_session_factory, engine
from app.models.user import User

logger = structlog.get_logger("unisense.align_admin_password")


def _resolve_admin_password() -> str | None:
    """从 env 解析部署口令；未配置则返回 None（调用方拒绝执行，不回退弱口令）。"""
    for key in ("ADMIN_INITIAL_PASSWORD", "UNISENSE_SEED_ADMIN_PASSWORD"):
        value = os.getenv(key)
        if value:
            return value
    return None


def _resolve_managed_accounts() -> list[str]:
    """解析待对齐的托管账号用户名列表（默认 admin）。"""
    raw = os.getenv("UNISENSE_MANAGED_ACCOUNTS", "admin")
    return [n.strip() for n in raw.split(",") if n.strip()]


async def _align_one(db: AsyncSession, username: str, password: str, *, apply: bool) -> dict:
    """对齐单个账号，返回结果摘要（不含明文/哈希）。"""
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None:
        logger.warning("align_user_not_found", username=username)
        return {"username": username, "status": "not_found"}

    already = await verify_password(password, user.password_hash)
    if already and not user.must_change_password:
        logger.info("align_user_already_matched", username=username, id=user.id)
        return {"username": username, "status": "already_matched", "user_id": user.id}

    if not apply:
        logger.info(
            "align_dry_run_would_update",
            username=username,
            id=user.id,
            hash_matches=already,
            must_change=bool(user.must_change_password),
        )
        return {
            "username": username,
            "status": "would_update",
            "user_id": user.id,
            "hash_matches": already,
        }

    user.password_hash = await hash_password(password)
    # 部署口令受控：清除首登改密标记，避免与「托管账号禁止应用内改密」形成死锁。
    user.must_change_password = False
    logger.info("align_user_updated", username=username, id=user.id)
    return {"username": username, "status": "updated", "user_id": user.id}


async def run(*, apply: bool) -> list[dict]:
    password = _resolve_admin_password()
    if not password:
        logger.error(
            "align_password_env_missing",
            hint="请先设置 UNISENSE_SEED_ADMIN_PASSWORD（部署口令），拒绝在无口令下执行",
        )
        raise SystemExit("UNISENSE_SEED_ADMIN_PASSWORD 未配置，终止。")

    accounts = _resolve_managed_accounts()
    configure_logging()
    async with async_session_factory() as db:
        try:
            summaries = []
            for username in accounts:
                summaries.append(await _align_one(db, username, password, apply=apply))
            if apply:
                await db.commit()
                logger.info("align_complete_committed", results=summaries)
            else:
                await db.rollback()
                logger.info("align_dry_run_complete_no_commit", results=summaries)
            return summaries
        except Exception:
            await db.rollback()
            logger.exception("align_failed")
            raise
        finally:
            await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="对齐托管账号密码到部署环境变量")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写库提交；缺省为 dry-run（只报告不写库）。",
    )
    args = parser.parse_args()
    summaries = asyncio.run(run(apply=args.apply))
    updated = [s for s in summaries if s["status"] in ("updated", "would_update")]
    if not args.apply and updated:
        print(f"[dry-run] {len(updated)} 个账号口令与 env 不一致，加 --apply 写库对齐。")
    elif args.apply:
        print(f"[apply] 已对齐 {len(updated)} 个账号。")
    else:
        print("全部托管账号口令已与 env 一致，无需变更。")


if __name__ == "__main__":
    sys.exit(main())
