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
from app.api.quality import router as quality_router
from app.api.recommend import router as recommend_router
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.metrics import MetricsMiddleware
from app.core.middleware import ErrorHandlerMiddleware, SecurityHeadersMiddleware, TraceIdMiddleware

logger = structlog.get_logger("unisense.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期管理。

    初始化全局状态（如通知服务 URL）。
    """
    configure_logging()
    # 配置通知服务 URL（供 conflict/governance 事件发布使用）
    if settings.notify_webhook_url:
        app.state.notify_url = settings.notify_webhook_url
    logger.info("app_starting", env=settings.env, version="0.1.0")
    yield
    logger.info("app_shutting_down")


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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
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
    app.include_router(assetmap_router, prefix="/api/v1")
    app.include_router(recommend_router, prefix="/api/v1")
    app.include_router(ai_router, prefix="/api/v1")

    return app


app = create_app()
