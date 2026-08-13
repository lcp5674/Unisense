"""降级事件记录与实时健康（对齐 TD §4.13 + §5.2.4/§5.2.5 降级矩阵）。

职责：
- 熔断器状态切换（open/close）或显式降级点经本模块记录：
  * degradation_event：降级开始/恢复**审计事件**（只写不删，WORM）。
  * dependency_health：每个依赖的**实时健康态**（熔断态/连续失败数/最近探测时间），
    供运营看板实时查询（TD §4.13），与 degradation_event 形成「明细 + 快照」双表。
- 所有持久化走**独立数据库会话**（best-effort）：降级常伴随请求事务回滚，独立会话避免事件
  随主事务回滚丢失（与 H-1/PLAT-3 同类缺陷相反的保护）。
- 同步 publish 到 EventBus（``degradation.state_changed``），供 notify/observability 消费告警。
- 全部 best-effort：DB 或 EventBus 不可用时仅记告警日志，绝不阻断主流程（降级路径自身不能再降级）。

设计取舍：
- TD §5.2.5 的「写 audit_log action=DEGRADE」由 degradation_event 等价满足，不重复落库。
- dependency_health 与 degradation_event 通过 (dependency_type, dependency_id) 逻辑关联
  （不建硬 FK，避免对 data_source 的外键耦合；DATASOURCE 仅为依赖类型之一）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.core.eventbus import get_eventbus
from app.core.logging import get_logger
from app.core.resilience import DegradationSignal
from app.db.mysql import async_session_factory
from app.models.degradation_event import DegradationEvent
from app.models.dependency_health import DependencyHealth

logger = get_logger(__name__)

# 降级事件态 → 实时健康 status 映射（TD §4.13 status ENUM: HEALTHY/DEGRADED/UNAVAILABLE）
_EVENT_STATE_TO_STATUS = {
    "DEGRADED": "DEGRADED",
    "HEALTHY": "HEALTHY",
    # 半开探测中：仍视为降级（过渡态），由 circuit_state=HALF_OPEN 体现
    "PROBING": "DEGRADED",
}


def _signal_to_health_params(signal: DegradationSignal) -> dict[str, Any]:
    """将熔断器信号映射为 dependency_health 持久化参数（纯函数，便于单测）。"""
    if signal.event_state == "HEALTHY":
        status, circuit_state, consecutive_failures, circuit_opened_at = (
            "HEALTHY",
            "CLOSED",
            0,
            None,
        )
    elif signal.event_state == "PROBING":
        status, circuit_state, consecutive_failures, circuit_opened_at = (
            "DEGRADED",
            "HALF_OPEN",
            signal.consecutive_failures,
            None,
        )
    else:  # DEGRADED
        status, circuit_state, consecutive_failures, circuit_opened_at = (
            "DEGRADED",
            "OPEN",
            signal.consecutive_failures,
            datetime.now(UTC),
        )
    return {
        "status": status,
        "circuit_state": circuit_state,
        "consecutive_failures": consecutive_failures,
        "circuit_opened_at": circuit_opened_at,
    }


async def record_degradation(
    dependency_type: str,
    dependency_id: str,
    state: str,
    reason: str,
    *,
    actor_id: int = 0,
    trace_id: str = "",
) -> None:
    """记录一次降级开始/恢复审计事件（best-effort，不抛异常）。

    Args:
        dependency_type: 依赖类型（LLM/OLAP/GRAPH/ES/DATASOURCE/NOTIFICATION）。
        dependency_id: 依赖实例标识（如 olap / neo4j / redis）。
        state: ``DEGRADED`` 或 ``HEALTHY``。
        reason: 触发原因（如 circuit_open / circuit_recovered / olap_not_configured）。
        actor_id: 触发方（0=系统自动）。
        trace_id: 链路追踪 ID（可选）。
    """
    # 1. 事件总线（best-effort，失败仅告警）
    try:
        await get_eventbus().publish(
            "degradation.state_changed",
            {
                "dependency_type": dependency_type,
                "dependency_id": dependency_id,
                "state": state,
                "reason": reason,
                "actor_id": actor_id,
                "trace_id": trace_id,
            },
        )
    except Exception:
        logger.warning(
            "degradation_event_publish_failed",
            dependency_type=dependency_type,
            state=state,
            exc_info=True,
        )

    # 2. 持久化（独立会话 best-effort，独立于请求事务，避免随回滚丢失）
    try:
        async with async_session_factory() as session:
            session.add(
                DegradationEvent(
                    dependency_type=dependency_type,
                    dependency_id=dependency_id,
                    state=state,
                    reason=reason,
                    actor_id=actor_id,
                )
            )
            await session.commit()
    except Exception:
        logger.warning(
            "degradation_event_persist_failed",
            dependency_type=dependency_type,
            state=state,
            exc_info=True,
        )


async def update_dependency_health(
    dependency_type: str,
    dependency_id: str,
    *,
    status: str,
    circuit_state: str,
    consecutive_failures: int = 0,
    circuit_opened_at: datetime | None = None,
    last_check_at: datetime | None = None,
    latency_p95_ms: int | None = None,
    error_rate_pct: float = 0.0,
    metadata: dict[str, Any] | None = None,
) -> None:
    """UPSERT 依赖实时健康态（best-effort，按 (dependency_type, dependency_id) 唯一键）。

    对齐 TD §4.13 dependency_health：实时熔断态 + 连续失败数 + 最近探测时间。
    看板/运维直接查询此表即得各依赖健康，无需回放 degradation_event 历史。

    Args:
        dependency_type / dependency_id: 依赖标识。
        status: HEALTHY/DEGRADED/UNAVAILABLE。
        circuit_state: CLOSED/OPEN/HALF_OPEN。
        consecutive_failures: 连续失败次数。
        circuit_opened_at: 熔断开启时间（UTC），未开启为 None。
        last_check_at: 最近探测时间（UTC），默认 now。
        latency_p95_ms: 近5分钟 P95 延迟（ms），可空。
        error_rate_pct: 近5分钟错误率（%）。
        metadata: 扩展信息（如熔断阈值/活跃连接数）。
    """
    now = datetime.now(UTC)
    try:
        async with async_session_factory() as session:
            stmt = mysql_insert(DependencyHealth).values(
                dependency_type=dependency_type,
                dependency_id=dependency_id,
                status=status,
                circuit_state=circuit_state,
                consecutive_failures=consecutive_failures,
                circuit_opened_at=circuit_opened_at,
                last_check_at=last_check_at or now,
                latency_p95_ms=latency_p95_ms,
                error_rate_pct=error_rate_pct,
                meta=metadata,
                created_at=now,
                updated_at=now,
            )
            stmt = stmt.on_duplicate_key_update(
                status=stmt.inserted["status"],
                circuit_state=stmt.inserted["circuit_state"],
                consecutive_failures=stmt.inserted["consecutive_failures"],
                circuit_opened_at=stmt.inserted["circuit_opened_at"],
                last_check_at=stmt.inserted["last_check_at"],
                latency_p95_ms=stmt.inserted["latency_p95_ms"],
                error_rate_pct=stmt.inserted["error_rate_pct"],
                meta=stmt.inserted["meta"],
                updated_at=now,
            )
            await session.execute(stmt)
            await session.commit()
    except Exception:
        logger.warning(
            "dependency_health_persist_failed",
            dependency_type=dependency_type,
            dependency_id=dependency_id,
            exc_info=True,
        )


async def _persist_degradation_and_health(
    dependency_type: str,
    dependency_id: str,
    state: str,
    reason: str,
    *,
    actor_id: int = 0,
    trace_id: str = "",
    circuit_state: str = "CLOSED",
    consecutive_failures: int = 0,
    circuit_opened_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """同时落 degradation_event（仅 DEGRADED/HEALTHY）与 dependency_health（始终）。"""
    if state in ("DEGRADED", "HEALTHY"):
        await record_degradation(
            dependency_type,
            dependency_id,
            state,
            reason,
            actor_id=actor_id,
            trace_id=trace_id,
        )
    status = _EVENT_STATE_TO_STATUS.get(state, "DEGRADED")
    await update_dependency_health(
        dependency_type,
        dependency_id,
        status=status,
        circuit_state=circuit_state,
        consecutive_failures=consecutive_failures,
        circuit_opened_at=circuit_opened_at,
        metadata=metadata,
    )


def handle_circuit_signal(signal: DegradationSignal) -> None:
    """熔断器监听器：将状态切换信号转为降级事件 + 实时健康落库（fire-and-forget）。

    注册于 ``main.lifespan``，使 resilience 层保持无 db/eventbus 依赖。
    ``PROBING``（半开探测）等中间态仅更新实时健康、不写审计事件。
    """
    health = _signal_to_health_params(signal)
    fire_degradation_event(
        signal.dependency_type,
        signal.dependency_type.lower(),
        signal.event_state,
        signal.reason,
        circuit_state=health["circuit_state"],
        consecutive_failures=health["consecutive_failures"],
        circuit_opened_at=health["circuit_opened_at"],
    )


# 防止 fire-and-forget 任务因无强引用被 GC 提前回收（asyncio 已知陷阱：
# loop.create_task 后的 Task 仅存于事件循环的弱引用集合，若无外部强引用，
# 可能在执行前被回收并抛 “Task was destroyed but it is pending”，导致降级记录静默丢失）。
_in_flight_tasks: set[asyncio.Task[None]] = set()


def _schedule_persist(coro: Coroutine[Any, Any, None]) -> None:
    """将异步持久化协程提交到事件循环并保留强引用直至完成。

    通过 ``loop.create_task`` 调度，并把 Task 加入模块级集合、以 done_callback 移除，
    避免任务被 GC 提前回收（asyncio 官方推荐做法）。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的事件循环（如离线脚本），跳过记录
        return
    task = loop.create_task(coro)
    _in_flight_tasks.add(task)
    task.add_done_callback(lambda _t: _in_flight_tasks.discard(task))


def fire_degradation_event(
    dependency_type: str,
    dependency_id: str,
    state: str,
    reason: str,
    *,
    actor_id: int = 0,
    trace_id: str = "",
    circuit_state: str = "CLOSED",
    consecutive_failures: int = 0,
    circuit_opened_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """同步入口：在低层客户端 / 同步熔断器中触发降级记录（fire-and-forget）。

    降级检测多发生在同步上下文（熔断器 ``record_failure``）或没有请求会话的客户端层，
    此处将异步 :func:`_persist_degradation_and_health` 提交到事件循环，不阻塞调用方。
    对 ``PROBING``（半开探测）等中间态仅更新实时健康、不写审计事件。

    Args:
        同 :func:`_persist_degradation_and_health`；``state`` 可为 DEGRADED/HEALTHY/PROBING。
    """
    _schedule_persist(
        _persist_degradation_and_health(
            dependency_type,
            dependency_id,
            state,
            reason,
            actor_id=actor_id,
            trace_id=trace_id,
            circuit_state=circuit_state,
            consecutive_failures=consecutive_failures,
            circuit_opened_at=circuit_opened_at,
            metadata=metadata,
        )
    )


async def read_dependency_health(
    dependency_type: str | None = None,
) -> list[dict[str, Any]]:
    """读取依赖实时健康态快照（dependency_health），供运营看板实时查询（TD §4.13）。

    best-effort：DB 不可用时返回空列表，绝不阻断看板/探针。

    Args:
        dependency_type: 可选过滤（如 ``"OLAP"``）；为空返回全部。

    Returns:
        健康态字典列表（含 status / circuit_state / consecutive_failures / 时间等）。
    """
    try:
        async with async_session_factory() as session:
            stmt = select(DependencyHealth)
            if dependency_type:
                stmt = stmt.where(DependencyHealth.dependency_type == dependency_type)
            stmt = stmt.order_by(DependencyHealth.dependency_type, DependencyHealth.dependency_id)
            result = await session.execute(stmt)
            return [_row_to_dict(r) for r in result.scalars().all()]
    except Exception:
        logger.warning("dependency_health_read_failed", exc_info=True)
        return []


def _row_to_dict(row: DependencyHealth) -> dict[str, Any]:
    """将 DependencyHealth ORM 行转为看板友好的字典（时间统一 ISO8601）。"""
    return {
        "dependency_type": row.dependency_type,
        "dependency_id": row.dependency_id,
        "status": row.status,
        "circuit_state": row.circuit_state,
        "consecutive_failures": row.consecutive_failures,
        "last_check_at": row.last_check_at.isoformat() if row.last_check_at else None,
        "latency_p95_ms": row.latency_p95_ms,
        "error_rate_pct": row.error_rate_pct,
        "circuit_opened_at": (row.circuit_opened_at.isoformat() if row.circuit_opened_at else None),
        "meta": row.meta,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def ensure_dependency_health_seed() -> None:
    """幂等播种受监控依赖（OLAP/GRAPH/ES）的健康初值（仅当不存在）。

    使运营看板即便依赖始终健康也不会缺失对应行；已存在则 ``INSERT IGNORE`` 跳过，
    绝不覆盖真实运行态。best-effort：DB 不可用时仅告警。
    """
    now = datetime.now(UTC)
    known: list[tuple[str, str]] = [
        ("OLAP", "olap"),
        ("GRAPH", "graph"),
        ("ES", "es"),
    ]
    try:
        async with async_session_factory() as session:
            for dep_type, dep_id in known:
                stmt = (
                    mysql_insert(DependencyHealth)
                    .prefix_with("IGNORE")
                    .values(
                        dependency_type=dep_type,
                        dependency_id=dep_id,
                        status="HEALTHY",
                        circuit_state="CLOSED",
                        consecutive_failures=0,
                        last_check_at=now,
                        error_rate_pct=0.0,
                        meta=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.execute(stmt)
            await session.commit()
    except Exception:
        logger.warning("dependency_health_seed_failed", exc_info=True)
