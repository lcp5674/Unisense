"""FastAPI 应用入口。

对齐 DEV_GUIDE §8b.1（应用入口）和 TD §5（中间件/降级/审计）。

启动:
    poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

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
from app.api.subject_domain import router as subject_domain_router
from app.api.system_dict import router as system_dict_router
from app.api.tracking import router as tracking_router
from app.api.users import router as users_router
from app.core.config import ConfigurationError, settings
from app.core.degradation import ensure_dependency_health_seed, handle_circuit_signal
from app.core.degradation_registry import init_degradation_registry
from app.core.dlq import init_dlq
from app.core.eventbus import get_eventbus, init_eventbus
from app.core.feature_flags import get_feature_flag_manager, init_feature_flag_manager
from app.core.logging import configure_logging
from app.core.metrics import MetricsMiddleware
from app.core.middleware import (
    DegradationMiddleware,
    ErrorHandlerMiddleware,
    SecurityHeadersMiddleware,
    TraceIdMiddleware,
)
from app.core.resilience import init_circuit_breaker_store, register_degradation_listener
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

    # ---- 死信队列初始化（TECH-04）：承接 Redis 发布失败的事件，定时重放兜底 ----
    init_dlq()
    logger.info("dlq_initialized")

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
    _register_notify_event_consumers()
    logger.info("notify_event_consumers_registered")

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


#: 业务事件类型（metric/quality/conflict/governance）→ 通知闭环订阅集合（TD §5.5）
#: 必须与各服务 EventBus 实际发布的事件类型完全一致，否则事件永不进入通知闭环：
#:   metric 发布 metric.created/submitted/approved/rejected/deprecated/promoted/
#:     rolled_back/emergency_published/health_critical（services/semantic/service.py）
#:   conflict 发布 conflict_open/conflict_ruled/conflict_escalated/pii_conflict
#:   （services/conflict/service.py）
#:   governance 发布 grant.*/classification.*/pii.*（services/governance/*）
#:   quality 发布 quality.anomaly/reconciliation.alert/benchmark.imported
#:   （services/quality/*）
_BUSINESS_EVENT_TYPES: tuple[str, ...] = (
    "metric.created",
    "metric.submitted",
    "metric.resubmitted",
    "metric.approved",
    "metric.rejected",
    "metric.deprecated",
    "metric.promoted",
    "metric.rolled_back",
    "metric.emergency_published",
    "metric.health_critical",
    # 冲突仲裁「保留差异+指定一方改名」→ 定向通知指标 Owner 去详情页改名（TD §12.4）
    "metric.rename_required",
    # 冲突仲裁「选权威」→ 定向通知落败方指标 Owner：指标已废弃（DEPRECATED）或
    # 已作废（软删），后继=胜方（TD §12.4）
    "metric.voided",
    "quality.anomaly",
    "reconciliation.alert",
    "benchmark.imported",
    "conflict_open",
    "conflict_ruled",
    "conflict_escalated",
    "pii_conflict",
    "grant.granted",
    "grant.revoked",
    "grant.expired",
    "pii.reviewed",
    "pii.propagated",
    "classification.changed",
    "classification.done",
    "escalation.triggered",
    # observability / audit（走 EventBus 的可接入业务事件，TD §5.5）
    "feedback.status_updated",
    "nps.submitted",
    "audit.capacity_warning",
    # 采集/血缘断链修复：collector/lineage 双发 EventBus 的目录血缘事件（TD §5.5）
    "catalog_registered",
    "catalog_schema_drifted",
    "lineage_parsed",
    "lineage_ingested",
    # 采集定向通知（collector/service.py 经 notify_user 直发源 Owner，模板注册）
    "catalog.deprecated",
    "collect.degraded",
    "collect.failed",
    "catalog.connection_failed",
    # 核心依赖降级（core/degradation.py 已发布 EventBus，供 notify 消费告警）
    "degradation.state_changed",
    # 冲突重开（conflict/service.py 经 _safe_publish 发布，原仅存于失效旧 HTTP 通道）
    "conflict_reopened",
    # 账号安全/组织（users.py/organizations.py 经 notify_user 定向通知，模板注册）
    "user.created",
    "user.status_changed",
    "user.password_reset",
    "org.status_changed",
    # 授权到期提醒 / PII 复核待办（定向通知，模板注册）
    "grant.expiring_soon",
    "pii.review_pending",
)


def _register_notify_event_consumers() -> None:
    """注册业务事件 → 通知闭环消费者（best-effort，异常不阻断业务主流程）。

    事件经 EventBus 本地订阅者消费，写入 notify 的 EventLog 并按订阅扇出投递
    （Webhook/钉钉/SMTP/console）。单体进程内同步执行，Redis 仅作跨进程广播。
    """
    from app.db.mysql import async_session_factory
    from app.services.notify.service import NotifyService

    async def _consume(event: dict[str, Any]) -> None:
        async with async_session_factory() as session:
            await NotifyService(session).handle_business_event(event)

    bus = get_eventbus()
    for event_type in _BUSINESS_EVENT_TYPES:
        bus.subscribe(event_type, _consume)


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
    # 降级舱壁：仅拦截依赖降级异常（DEPENDENCY_DEGRADED_*）并标注 degraded，置于
    # ErrorHandlerMiddleware 内层（先执行），非降级异常上抛交由 ErrorHandler 统一处理。
    app.add_middleware(DegradationMiddleware)
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
    app.include_router(collection_run_router, prefix="/api/v1")
    app.include_router(lineage_router, prefix="/api/v1")
    app.include_router(conflict_router, prefix="/api/v1")
    app.include_router(governance_router, prefix="/api/v1")
    app.include_router(quality_router, prefix="/api/v1")
    app.include_router(consume_router, prefix="/api/v1")
    app.include_router(glossary_router, prefix="/api/v1")
    app.include_router(dimension_router, prefix="/api/v1")
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
    app.include_router(feature_flags_router, prefix="/api/v1")
    app.include_router(admin_key_rotation_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")

    return app


app = create_app()
