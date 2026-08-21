"""临时为 k6 live 压测创建 perf 用户并登录拿 JWT。

仅用于本地 live 压测（打运行中的 backend 容器所用主库）。
用法：
    UNISENSE_DB_URL='mysql+aiomysql://unisense:test@127.0.0.1:3307/unisense?charset=utf8mb4' \
    backend/.venv/bin/python backend/tests/perf/_create_k6_user.py
"""
from __future__ import annotations

import asyncio
import os

from sqlalchemy import select, text

from app.core.security import hash_password
from app.db.mysql import async_session_factory

USERNAME = "perf_k6"
PASSWORD = "PerfK62026!"
ROLE = "domain_admin"  # 可读 metric-definitions + 可触发发布类端点


async def main() -> None:
    async with async_session_factory() as s:
        existing = await s.execute(
            select(text("id")).select_from(text("user")).where(text("username=:u")),
            {"u": USERNAME},
        )
        if existing.scalar():
            print(f"用户已存在: {USERNAME}")
        else:
            await s.execute(
                text(
                    "INSERT INTO user (email, username, display_name, role, password_hash, status, org_id, created_at, updated_at) "
                    "VALUES (:email, :username, :display_name, :role, :ph, 'active', 1, NOW(), NOW())"
                ),
                {
                    "email": f"{USERNAME}@unisense.local",
                    "username": USERNAME,
                    "display_name": "Perf K6",
                    "role": ROLE,
                    "ph": await hash_password(PASSWORD),
                },
            )
            await s.commit()
            print(f"已创建用户: {USERNAME} / {PASSWORD} (role={ROLE})")

        # 顺带打印可用指标编码，供 baseline 轮询
        codes = await s.execute(
            text("SELECT metric_code FROM metric WHERE status='PUBLISHED' LIMIT 10")
        )
        print("PUBLISHED metric_codes:", [r[0] for r in codes.fetchall()])


if __name__ == "__main__":
    asyncio.run(main())
