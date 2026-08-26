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

from app.api.admin_key_rotation import router as admin_key_rotation_router
from app.api.ai import router as ai_router
from app.api.assetmap import router as assetmap_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.collector import catalog_router, collection_run_router, source_router
from app.api.conflict import router as conflict_router
from app.api.consume import router as consume_router
from app.api.dimension import router as dimension_router
from app.api.feature_flags import router as feature_flags_router
from app.api.glossary import router as glossary_router
from app.api.governance import router as governance_router
from app.api.health import router as health_router
from app.api.lineage import router as lineage_router
from app.api.measure_catalog import router as measure_catalog_router
from app.api.metric_mount import router as metric_mount_router
from app.api.metric_stats import router as metric_stats_router
from app.api.metrics import router as metrics_router
from app.api.notify import router as notify_router
from app.api.observability import router as observability_router
from app.api.organizations import router as organizations_router
from app.api.preferences import router as preferences_router
from app.api.quality import router as quality_router
from app.api.recommend import router as recommend_router
from app.api.search import router as search_router
from app.api.semantic import quickbi_compat_router as semantic_quickbi_compat_router
from app.api.semantic import router as semantic_router
from app.api.sensitive_rules import router as sensitive_rules_router
from app.api.subject_domain import router as subject_domain_router
from app.api.system_dict import router as system_dict_router
from app.api.tracking import router as tracking_router
from app.api.users import router as users_router
from app.core.config import ConfigurationError, settings
from app.core.degradation import ensure_dependency_health_seed, handle_circuit_signal
from app.core.degradation_registry import init_degradation_registry
from app.core.dlq import get_dlq, init_dlq
from app.core.eventbus import init_eventbus
from app.core.feature_flags import get_feature_flag_manager, init_feature_flag_manager
from app.core.logging import configure_logging
from app.core.metrics import MetricsMiddleware
from app.core.middleware import (
    DegradationMiddleware,
    ErrorHandlerMiddleware,
    RateLimitMiddleware,
    RequestBodySizeMiddleware,
    SecurityHeadersMiddleware,
    TraceIdMiddleware,
)
from app.core.resilience import init_circuit_breaker_store, register_degradation_listener
from app.db.redis import close_redis_pool, init_redis_pool
from app.services.consume.rate_limiter import init_rate_limiter
from app.services.notify.consumers import register_notify_event_consumers

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

    # ---- 死信队列初始化（TECH-04）：承接 Redis 发布失败的事件，定时重放兜底 ----
    init_dlq()
    logger.info("dlq_initialized")
    # P11 C-4：启动 DLQ 重放循环（此前 start_replay_loop 全仓仅定义无调用——事件入队后永不重放）。
    # 事件总线重试耗尽的事件经此每 5 分钟重放一批，避免「发布失败即丢失」。
    try:
        await get_dlq().start_replay_loop()
        logger.info("dlq_replay_loop_started")
    except Exception:  # noqa: BLE001 - 重放循环启动失败不应阻断启动
        logger.warning("dlq_replay_loop_start_failed", exc_info=True)

    # ---- 降级注册中心初始化（OPS-05）：统一降级面板 + /health/degraded ----
    init_degradation_registry()
    logger.info("degradation_registry_initialized")

    # ---- 特性开关初始化（OPS-09）：注册默认开关（默认开启，非破坏性）+ Redis 刷新 ----
    init_feature_flag_manager()
    ffm = get_feature_flag_manager()
    ffm.register_flag("emergency_publish", enabled=True, description="紧急发布能力开关（PA/DA）")
    ffm.register_flag("quickbi", enabled=True, description="QuickBI 报表嵌入票据签发")
    ffm.register_flag("ai.nl2sql", enabled=True, description="AI 问数 NL2SQL 能力")
    ffm.register_flag("audit_archive", enabled=True, description="审计日志冷热归档任务")
    if redis_pool is not None:
        ffm.refresh_from_redis(redis_pool)
    logger.info("feature_flags_initialized", count=len(ffm.get_all_flags()))

    # ---- 业务事件 → 通知闭环（TD §5.5）：quality/conflict/governance 事件落 notify 并投递 ----
    register_notify_event_consumers()
    logger.info("notify_event_consumers_registered")

    # ---- 运维级事件默认订阅播种（P11）：platform_admin 默认订阅表增长/容量/降级/采集失败等 ----
    # 运维级事件此前无任何生产订阅 → 只落 EventLog 不触达（死信告警）。启动 best-effort 播种。
    try:
        from app.db.mysql import async_session_factory
        from app.services.notify.service import NotifyService

        async with async_session_factory() as _session:
            created = await NotifyService(_session).ensure_ops_subscriptions()
        logger.info("ops_subscriptions_seeded", created=created)
    except Exception:  # noqa: BLE001 - 播种失败不应阻断启动（DB 未就绪/无 platform_admin 均合法）
        logger.warning("ops_subscriptions_seed_failed", exc_info=True)

    # ---- 降级事件上报（TD §5.2.4/§5.2.5）：熔断器 open/close 回调持久化 + 告警 ----
    register_degradation_listener(handle_circuit_signal)
    logger.info("degradation_listener_registered")

    # ---- 熔断态共享存储（TD §5.2a）：Redis 可用时跨 worker/副本协调 OPEN 态与半开单飞探针 ----
    init_circuit_breaker_store(redis_pool)
    logger.info("circuit_breaker_store_registered")

    # ---- 幂等播种依赖健康初值（仅当不存在），使运营看板即便依赖始终健康也不缺失行 ----
    await ensure_dependency_health_seed()
    logger.info("dependency_health_seeded")

    # ---- 限流器初始化（Redis 可用时启用分布式限流，否则 InMemory 降级）----
    init_rate_limiter(redis_pool)
    logger.info("rate_limiter_initialized")

    # 配置通知服务 URL（供 conflict/governance 事件发布使用）
    if settings.notify_webhook_url:
        app.state.notify_url = settings.notify_webhook_url

    logger.info("app_starting", env=settings.env, version="0.1.0")
    yield

    # ---- 关闭 ----
    await get_dlq().stop()
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
    # API 限流（P1-12）：置于最外层业务中间件之前，命中即返回 429，避免限流请求
    # 进入后续重链路（限流本身不依赖 trace_id，缺失时回退 X-Trace-Id header）。
    app.add_middleware(RateLimitMiddleware)
    # 请求体大小限制（P0-2，第八轮）：定义于 middleware.py 但此前未注册（死代码），
    # 裸 body: dict 端点可传任意大请求体。注册后 POST/PUT/PATCH 超 10MB 返回 413。
    app.add_middleware(RequestBodySizeMiddleware)
    app.add_middleware(ErrorHandlerMiddleware)
    # 降级舱壁：仅拦截依赖降级异常（DEPENDENCY_DEGRADED_*）并标注 degraded，置于
    # ErrorHandlerMiddleware 内层（先执行），非降级异常上抛交由 ErrorHandler 统一处理。
    app.add_middleware(DegradationMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TraceIdMiddleware)
    app.add_middleware(MetricsMiddleware)
    # P2（第六轮）：响应 GZip 压缩——parse-sql-batch 等大响应（语句 100 × sql[:2000]
    # + 候选 200 最坏 ~400KB）压缩传输，避免 JSON 大载荷拖慢工业网络环境
    from starlette.middleware.gzip import GZipMiddleware

    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

    # ---- 路由 ----
    app.include_router(health_router)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(audit_router, prefix="/api/v1")
    app.include_router(metrics_router, prefix="/api/v1")
    app.include_router(metric_stats_router, prefix="/api/v1")
    app.include_router(source_router, prefix="/api/v1")
    app.include_router(catalog_router, prefix="/api/v1")
    app.include_router(collection_run_router, prefix="/api/v1")
    app.include_router(lineage_router, prefix="/api/v1")
    app.include_router(conflict_router, prefix="/api/v1")
    app.include_router(governance_router, prefix="/api/v1")
    app.include_router(quality_router, prefix="/api/v1")
    app.include_router(consume_router, prefix="/api/v1")
    app.include_router(glossary_router, prefix="/api/v1")
    app.include_router(dimension_router, prefix="/api/v1")
    app.include_router(measure_catalog_router, prefix="/api/v1")
    app.include_router(metric_mount_router, prefix="/api/v1")
    app.include_router(notify_router, prefix="/api/v1")
    app.include_router(observability_router, prefix="/api/v1")
    app.include_router(organizations_router, prefix="/api/v1")
    app.include_router(preferences_router, prefix="/api/v1")
    app.include_router(assetmap_router, prefix="/api/v1")
    app.include_router(recommend_router, prefix="/api/v1")
    app.include_router(search_router, prefix="/api/v1")
    app.include_router(ai_router, prefix="/api/v1")
    app.include_router(tracking_router, prefix="/api/v1")
    app.include_router(semantic_router, prefix="/api/v1")
    app.include_router(semantic_quickbi_compat_router, prefix="/api/v1")
    app.include_router(subject_domain_router, prefix="/api/v1")
    app.include_router(system_dict_router, prefix="/api/v1")
    app.include_router(sensitive_rules_router, prefix="/api/v1")
    app.include_router(feature_flags_router, prefix="/api/v1")
    app.include_router(admin_key_rotation_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")

    return app


app = create_app()
