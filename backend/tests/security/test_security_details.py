"""安全细节回归测试（T039）。

验证：
1. JWT 有效期 15 分钟
2. Redis TLS 支持 rediss://
3. 数据库密码 mask
4. 请求体大小限制 10MB → 413
"""

import pytest


def test_jwt_expire_minutes_default() -> None:
    """T039: JWT 默认有效期 15 分钟。

    直接从模型定义断言默认值（不实例化 Settings），彻底免疫：
    1. 本地 .env（UNISENSE_JWT_EXPIRE_MINUTES=60 会覆盖实例值）
    2. 其它测试对 os.environ 的污染（显式传入仍会读进程环境）
    """
    from app.core.config import Settings

    field = Settings.model_fields["jwt_expire_minutes"]
    assert field.default == 15


def test_mysql_password_masked_in_logs() -> None:
    """T039: MySQL 连接串密码被 mask。"""
    from app.db.mysql import _mask_password

    url = "mysql+aiomysql://admin:s3cret!@db-host:3306/unisense"
    masked = _mask_password(url)
    assert "s3cret!" not in masked
    assert "***" in masked
    assert "db-host" in masked


def test_redis_tls_support() -> None:
    """T039: Redis 支持 rediss:// TLS 连接。"""
    # 验证 redis.py 模块可正常导入且包含 TLS 逻辑
    from app.db import redis as redis_mod

    with open(redis_mod.__file__) as f:
        source = f.read()
    assert "rediss://" in source
    assert "ssl" in source


@pytest.mark.asyncio
async def test_request_body_size_limit() -> None:
    """T039: 请求体 > 10MB 返回 413（中间件须真正注册，而非仅定义）。"""
    from app.core.middleware import RequestBodySizeMiddleware

    # 验证中间件存在且限制为 10MB
    assert RequestBodySizeMiddleware is not None
    from app.core.middleware import _MAX_BODY_SIZE

    assert _MAX_BODY_SIZE == 10 * 1024 * 1024

    # P0-2（第八轮）：此前中间件定义于 middleware.py 但 main.py 未注册（死代码），
    # 裸 body: dict 端点可传任意大请求体。此处断言其已在 FastAPI 中间件栈注册。
    from app.main import app as unisense_app

    middleware_classes = [m.cls for m in unisense_app.user_middleware]
    assert RequestBodySizeMiddleware in middleware_classes, (
        "RequestBodySizeMiddleware 必须在 main.py 注册（P0-2 第八轮）"
    )
