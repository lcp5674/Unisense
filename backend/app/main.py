"""FastAPI 应用入口。

对齐 DEV_GUIDE §8b.1（应用入口）和 TD §5（中间件/降级/审计）。

启动:
    poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.ai import router as ai_router
from app.api.assetmap import router as assetmap_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.collector import catalog_router, source_router
from app.api.conflict import router as conflict_router
from app.api.consume import router as consume_router
from app.api.dimension import router as dimension_router
from app.api.glossary import router as glossary_router
from app.api.governance import router as governance_router
from app.api.health import router as health_router
from app.api.lineage import router as lineage_router
from app.api.metrics import router as metrics_router
from app.api.notify import router as notify_router
from app.api.observability import router as observability_router
from app.api.preferences import router as preferences_router
from app.api.quality import router as quality_router
from app.api.recommend import router as recommend_router
from app.api.semantic import router as semantic_router
from app.api.tracking import router as tracking_router
from app.core.config import ConfigurationError, settings
from app.core.eventbus import init_eventbus
from app.core.logging import configure_logging
from app.core.metrics import MetricsMiddleware
from app.core.middleware import ErrorHandlerMiddleware, SecurityHeadersMiddleware, TraceIdMiddleware
from app.db.redis import close_redis_pool, init_redis_pool
from app.services.consume.rate_limiter import init_rate_limiter

logger = structlog.get_logger("unisense.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期管理。

    初始化全局状态：
    1. 日志配置
    2. Redis 连接池初始化
    3. EventBus 初始化（注入 Redis）
    4. 启动时配置校验（生产环境强制校验）
    5. 通知服务 URL 配置
    """
    configure_logging()

    # ---- 启动时配置校验 ----
    try:
        _validate_config()
    except ConfigurationError as exc:
        logger.error("config_validation_failed", error=str(exc))
        raise SystemExit(1) from exc

    # ---- Redis 连接池初始化 ----
    try:
        redis_pool = await init_redis_pool()
        logger.info("redis_pool_initialized")
    except Exception:
        logger.warning("redis_pool_init_failed", exc_info=True)
        redis_pool = None

    # ---- EventBus 初始化 ----
    init_eventbus(redis_pool)
    logger.info("eventbus_initialized")

    # ---- 限流器初始化（Redis 可用时启用分布式限流，否则 InMemory 降级）----
    init_rate_limiter(redis_pool)
    logger.info("rate_limiter_initialized")

    # 配置通知服务 URL（供 conflict/governance 事件发布使用）
    if settings.notify_webhook_url:
        app.state.notify_url = settings.notify_webhook_url

    logger.info("app_starting", env=settings.env, version="0.1.0")
    yield

    # ---- 关闭 ----
    await close_redis_pool()
    logger.info("app_shutting_down")


def _validate_config() -> None:
    """启动时配置校验（生产环境强制）。"""
    if settings.env == "prod":
        if len(settings.jwt_secret) < 32:
            raise ConfigurationError(
                f"生产环境 UNISENSE_JWT_SECRET 必须≥32字符，当前长度={len(settings.jwt_secret)}"
            )
        if not settings.fernet_key:
            raise ConfigurationError(
                "生产环境 UNISENSE_FERNET_KEY 必须独立配置，禁止从 JWT_SECRET 派生降级"
            )
        if not settings.olap_url:
            raise ConfigurationError(
                "生产环境 UNISENSE_OLAP_URL 必须非空，consume 查询需要 OLAP 执行引擎"
            )
        # CORS 严格校验：allow_credentials=True 时禁止通配符
        origins = settings.cors_origins_list
        if "*" in origins:
            raise ConfigurationError(
                "生产环境 CORS 不允许通配符与 credentials=True 组合，请配置具体 Origin"
            )
    logger.info("config_validation_passed", env=settings.env)


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例。

    Returns:
        配置好的 FastAPI 应用。
    """
    app = FastAPI(
        title="Unisense",
        description="指标语义中台 — 统一指标语义平台",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # ---- 中间件（顺序：后添加的先执行）----
    # CORS 严格读取 settings.cors_origins_list，allow_credentials=True 时禁止通配符
    origins = settings.cors_origins_list
    if "*" in origins and settings.env != "local":
        # 非本地开发环境不允许通配符 + credentials 组合
        logger.warning(
            "cors_wildcard_with_credentials",
            msg="CORS 通配符与 credentials=True 组合不安全，已移除通配符",
        )
        origins = [o for o in origins if o != "*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Version", "X-Trace-Id"],
    )
    app.add_middleware(ErrorHandlerMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TraceIdMiddleware)
    app.add_middleware(MetricsMiddleware)

    # ---- 路由 ----
    app.include_router(health_router)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(audit_router, prefix="/api/v1")
    app.include_router(metrics_router, prefix="/api/v1")
    app.include_router(source_router, prefix="/api/v1")
    app.include_router(catalog_router, prefix="/api/v1")
    app.include_router(lineage_router, prefix="/api/v1")
    app.include_router(conflict_router, prefix="/api/v1")
    app.include_router(governance_router, prefix="/api/v1")
    app.include_router(quality_router, prefix="/api/v1")
    app.include_router(consume_router, prefix="/api/v1")
    app.include_router(glossary_router, prefix="/api/v1")
    app.include_router(dimension_router, prefix="/api/v1")
    app.include_router(notify_router, prefix="/api/v1")
    app.include_router(observability_router, prefix="/api/v1")
    app.include_router(preferences_router, prefix="/api/v1")
    app.include_router(assetmap_router, prefix="/api/v1")
    app.include_router(recommend_router, prefix="/api/v1")
    app.include_router(ai_router, prefix="/api/v1")
    app.include_router(tracking_router, prefix="/api/v1")
    app.include_router(semantic_router, prefix="/api/v1")

    return app


app = create_app()
