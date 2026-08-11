"""安全工具：密码哈希与 JWT 签发。

对齐 TD §5（鉴权）与 §12.4（密钥配置）。
- 密码哈希使用 bcrypt（存储于 user.password_hash）。
- JWT 使用 HS256，密钥取自 settings.jwt_secret。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import settings


def hash_password(password: str) -> str:
    """对明文密码进行 bcrypt 哈希。

    Args:
        password: 明文密码。

    Returns:
        bcrypt 哈希字符串。
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


async def verify_password(password: str, hashed: str) -> bool:
    """校验明文密码与 bcrypt 哈希是否匹配。

    bcrypt 为 CPU 密集型计算，若直接在 async 路由中同步调用会阻塞整个事件循环，
    导致单实例吞吐量被锁死（perf 实测 10 VU 仅 ~5 req/s）。改为在线程池中执行，
    释放事件循环以并发处理其他请求。

    Args:
        password: 明文密码。
        hashed: 存储的 bcrypt 哈希。

    Returns:
        匹配返回 True，否则 False（含非法哈希格式）。
    """
    try:
        return await asyncio.to_thread(
            bcrypt.checkpw, password.encode("utf-8"), hashed.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


def create_access_token(
    *,
    sub: int | str,
    role: str,
    org_id: int,
    expire_minutes: int | None = None,
) -> str:
    """签发 JWT 访问令牌。

    Args:
        sub: 用户 ID（subject）。
        role: 用户角色。
        org_id: 所属组织 ID。
        expire_minutes: 过期分钟数，缺省取 settings.jwt_expire_minutes。

    Returns:
        签名后的 JWT 字符串。
    """
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=expire_minutes or settings.jwt_expire_minutes)
    payload = {
        "sub": str(sub),
        "role": role,
        "org_id": org_id,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
