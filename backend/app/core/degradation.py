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
from app.models.degradation_event import DEGRADATION_STATES, DegradationEvent
from app.models.dependency_health import (
    DEP_HEALTH_CIRCUIT,
    DEP_HEALTH_STATES,
    DependencyHealth,
)

logger = get_logger(__name__)

# 降级事件态 → 实时健康 status 映射（TD §4.13 status ENUM: HEALTHY/DEGRADED/UNAVAILABLE）
_EVENT_STATE_TO_STATUS = {
    "DEGRADED": "DEGRADED",
    "HEALTHY": "HEALTHY",
    # 半开探测中：仍视为降级（过渡态），由 circuit_state=HALF_OPEN 体现
    "PROBING": "DEGRADED",
}

# event_type 推导（对齐 TD §4.13 degradation_event.event_type ENUM）：
# state 仅区分 DEGRADED/HEALTHY，event_type 进一步刻画熔断语义。
_EVENT_TYPE_BY_STATE_REASON: dict[tuple[str, str], str] = {
    ("DEGRADED", "circuit_open"): "CIRCUIT_OPENED",
    ("HEALTHY", "circuit_recovered"): "CIRCUIT_CLOSED",
}
# 严重程度默认映射：降级=能力关停(HEAVY)，恢复=LIGHT（可被调用方覆盖）。
_SEVERITY_BY_STATE = {"DEGRADED": "HEAVY", "HEALTHY": "LIGHT"}
# 恢复动作默认：电路自动探测恢复（可被调用方覆盖）。
_RESOLUTION_BY_REASON = {"circuit_recovered": "自动探测恢复"}


def _derive_event_type(state: str, reason: str) -> str:
    """由 (state, reason) 推导 TD §4.13 event_type ENUM 值（纯函数，便于单测）。"""
    return _EVENT_TYPE_BY_STATE_REASON.get((state, reason), state)


# 哨兵：区分「调用方未提供该字段」与「显式传 None/0」。UPSERT 更新时，未提供的
# 遥测字段（P95 延迟/错误率/扩展信息）必须保留既有值，绝不能因熔断事件而清零。
_MISSING = object()

# 同 (dependency_type, dependency_id) 最近一次已上报的状态。用于抑制「同状态重复事件」：
# WORM 审计表不应被同一降级态的重复上报刷爆；热路径（如 OLAP 未配置时每次查询都触发
# DEGRADED）也不应每请求写库/发事件。仅在状态发生变化、或调用方携带新遥测
# （circuit_opened_at / metadata）时才真正落库。降级态在 down→up→down 往复时仍能各自产生事件。
_fired_state: dict[tuple[str, str], str] = {}


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
    severity: str | None = None,
    affected_capabilities: list[str] | None = None,
    affected_user_count: int = 0,
) -> None:
    """记录一次降级开始/恢复审计事件（best-effort，不抛异常）。

    对齐 TD §4.13：补齐 event_type / severity / affected_capabilities /
    affected_user_count / started_at / recovered_at / duration_seconds /
    trigger_reason / resolution_action，使看板可计算降级时长与影响用户数（Gap #4）。

    Args:
        dependency_type: 依赖类型（LLM/OLAP/GRAPH/ES/DATASOURCE/NOTIFICATION）。
        dependency_id: 依赖实例标识（如 olap / neo4j / redis）。
        state: ``DEGRADED`` 或 ``HEALTHY``。
        reason: 触发原因（如 circuit_open / circuit_recovered / olap_not_configured）。
        actor_id: 触发方（0=系统自动）。
        trace_id: 链路追踪 ID（可选）。
        severity: 严重程度（LIGHT/HEAVY），缺省按 state 推导（降级=HEAVY/恢复=LIGHT）。
        affected_capabilities: 受影响能力列表（如 ["ai_prefill","nl2sql"]），可选。
        affected_user_count: 预估受影响用户数，缺省 0。
    """
    # 边界处理：审计事件仅接受 DEGRADED/HEALTHY 枚举值，非法 state（拼写/误传 PROBING）
    # 直接丢弃并告警，避免向 MySQL ENUM 列写入非法值被静默拒绝（best-effort 吞错无法定位）。
    if state not in DEGRADATION_STATES:
        logger.warning(
            "degradation_record_invalid_state",
            state=state,
            dependency_type=dependency_type,
            reason=reason,
        )
        return
    event_type = _derive_event_type(state, reason)
    sev = severity or _SEVERITY_BY_STATE.get(state, "LIGHT")
    now = datetime.now(UTC)
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
                "event_type": event_type,
                "severity": sev,
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
            event = DegradationEvent(
                dependency_type=dependency_type,
                dependency_id=dependency_id,
                state=state,
                reason=reason,
                actor_id=actor_id,
                # TD §4.13 度量字段：降级事件记 started_at，恢复事件记 recovered_at
                event_type=event_type,
                severity=sev,
                affected_capabilities=affected_capabilities,
                affected_user_count=affected_user_count,
                started_at=now if state == "DEGRADED" else None,
                recovered_at=now if state == "HEALTHY" else None,
                trigger_reason=reason,
            )
            session.add(event)
            # 恢复配对（best-effort）：回填最近一次未恢复的 DEGRADED 事件的
            # recovered_at / duration_seconds / resolution_action，使看板可直接聚合降级
            # 持续时长（无需回放历史）。此逻辑独立容错——即便配对查询/回填失败，
            # 主降级审计事件仍须落库（审计记录最关键，绝不可因配对异常丢失）。
            if state == "HEALTHY":
                try:
                    paired = await session.execute(
                        select(DegradationEvent)
                        .where(
                            DegradationEvent.dependency_type == dependency_type,
                            DegradationEvent.dependency_id == dependency_id,
                            DegradationEvent.state == "DEGRADED",
                            DegradationEvent.recovered_at.is_(None),
                        )
                        .order_by(DegradationEvent.started_at.desc())
                        .limit(1)
                    )
                    degraded = paired.scalars().first()
                    if degraded is not None and degraded.started_at is not None:
                        degraded.recovered_at = now
                        degraded.duration_seconds = int((now - degraded.started_at).total_seconds())
                        degraded.resolution_action = _RESOLUTION_BY_REASON.get(
                            reason, "自动探测恢复"
                        )
                except Exception:
                    logger.warning(
                        "degradation_recovery_pairing_failed",
                        dependency_type=dependency_type,
                        dependency_id=dependency_id,
                        exc_info=True,
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
    consecutive_failures: int | object = _MISSING,
    circuit_opened_at: datetime | None | object = _MISSING,
    last_check_at: datetime | None | object = _MISSING,
    latency_p95_ms: int | None | object = _MISSING,
    error_rate_pct: float | object = _MISSING,
    metadata: dict[str, Any] | None | object = _MISSING,
) -> None:
    """UPSERT 依赖实时健康态（best-effort，按 (dependency_type, dependency_id) 唯一键）。

    对齐 TD §4.13 dependency_health：实时熔断态 + 连续失败数 + 最近探测时间。
    看板/运维直接查询此表即得各依赖健康，无需回放 degradation_event 历史。

    关键语义：可选遥测字段（``latency_p95_ms`` / ``error_rate_pct`` / ``meta`` 等）以
    哨兵 ``_MISSING`` 表示「未提供」。UPSERT 时，仅更新调用方**显式提供**的字段；
    未提供的字段保留既有行值，绝不因一次熔断事件把探针采集的 P95 延迟 / 错误率 /
    扩展信息清零（否则看板会在熔断开启瞬间谎报错误率 0%、丢失阈值等元数据）。

    Args:
        dependency_type / dependency_id: 依赖标识。
        status: HEALTHY/DEGRADED/UNAVAILABLE（必填）。
        circuit_state: CLOSED/OPEN/HALF_OPEN（必填）。
        consecutive_failures: 连续失败次数（未提供则保留）。
        circuit_opened_at: 熔断开启时间（UTC），未开启为 None（未提供则保留）。
        last_check_at: 最近探测时间（UTC），未提供则默认 now；每次更新都会刷新（即便未提供），
            使看板反映最近一次健康态变更时刻，而非停留在首次插入时刻。
        latency_p95_ms: 近5分钟 P95 延迟（ms）（未提供则保留）。
        error_rate_pct: 近5分钟错误率（%）（未提供则保留）。
        metadata: 扩展信息（如熔断阈值/活跃连接数）（未提供则保留）。
    """
    now = datetime.now(UTC)
    # 边界处理：status/circuit_state 必须为合法 ENUM，非法值直接丢弃并告警，
    # 避免向 MySQL ENUM 列写入非法值被静默拒绝（best-effort 吞错导致健康态更新丢失、
    # 看板读到陈旧/错误状态）。record_degradation 已校验 state，此处补齐健康态校验。
    if status not in DEP_HEALTH_STATES:
        logger.warning(
            "dependency_health_invalid_status",
            status=status,
            dependency_type=dependency_type,
            dependency_id=dependency_id,
        )
        return
    if circuit_state not in DEP_HEALTH_CIRCUIT:
        logger.warning(
            "dependency_health_invalid_circuit_state",
            circuit_state=circuit_state,
            dependency_type=dependency_type,
            dependency_id=dependency_id,
        )
        return
    # 插入期默认值：未显式提供的字段用合理初值（仅影响首次插入的新行）
    insert_values: dict[str, Any] = {
        "dependency_type": dependency_type,
        "dependency_id": dependency_id,
        "status": status,
        "circuit_state": circuit_state,
        "consecutive_failures": 0 if consecutive_failures is _MISSING else consecutive_failures,
        "circuit_opened_at": None if circuit_opened_at is _MISSING else circuit_opened_at,
        "last_check_at": now if last_check_at is _MISSING else last_check_at,
        "latency_p95_ms": None if latency_p95_ms is _MISSING else latency_p95_ms,
        "error_rate_pct": 0.0 if error_rate_pct is _MISSING else error_rate_pct,
        "meta": None if metadata is _MISSING else metadata,
        "created_at": now,
        "updated_at": now,
    }
    try:
        async with async_session_factory() as session:
            stmt = mysql_insert(DependencyHealth).values(**insert_values)
            # 仅更新调用方显式提供的字段，保护未提供的遥测值
            update_values: dict[str, Any] = {
                "status": stmt.inserted["status"],
                "circuit_state": stmt.inserted["circuit_state"],
                "updated_at": now,
            }
            if consecutive_failures is not _MISSING:
                update_values["consecutive_failures"] = stmt.inserted["consecutive_failures"]
            if circuit_opened_at is not _MISSING:
                update_values["circuit_opened_at"] = stmt.inserted["circuit_opened_at"]
            # 每次健康态更新都代表一次探测/状态变更，刷新最近探测时间（即便未显式提供），
            # 否则看板 last_check_at 会冻结在首次插入时刻、误报「很久未探测」。
            # stmt.inserted["last_check_at"] 在提供时为提供值、未提供时为 now。
            update_values["last_check_at"] = stmt.inserted["last_check_at"]
            if latency_p95_ms is not _MISSING:
                update_values["latency_p95_ms"] = stmt.inserted["latency_p95_ms"]
            if error_rate_pct is not _MISSING:
                update_values["error_rate_pct"] = stmt.inserted["error_rate_pct"]
            if metadata is not _MISSING:
                update_values["meta"] = stmt.inserted["meta"]
            stmt = stmt.on_duplicate_key_update(**update_values)
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
    severity: str | None = None,
    affected_capabilities: list[str] | None = None,
    affected_user_count: int = 0,
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
            severity=severity,
            affected_capabilities=affected_capabilities,
            affected_user_count=affected_user_count,
        )
    status = _EVENT_STATE_TO_STATUS.get(state, "DEGRADED")
    # 仅当 metadata 真实提供时才下发；否则 update_dependency_health 会保留既有 meta，
    # 避免熔断事件（通常不携带元数据）把探针写入的阈值/连接数等扩展信息清零。
    health_kwargs: dict[str, Any] = {
        "status": status,
        "circuit_state": circuit_state,
        "consecutive_failures": consecutive_failures,
        "circuit_opened_at": circuit_opened_at,
    }
    if metadata is not None:
        health_kwargs["metadata"] = metadata
    await update_dependency_health(dependency_type, dependency_id, **health_kwargs)


def handle_circuit_signal(signal: DegradationSignal) -> None:
    """熔断器监听器：将状态切换信号转为降级事件 + 实时健康落库（fire-and-forget）。

    注册于 ``main.lifespan``，使 resilience 层保持无 db/eventbus 依赖。
    ``PROBING``（半开探测）等中间态仅更新实时健康、不写审计事件。

    ``dependency_id`` 优先取信号携带的真实实例标识；为空（未配置多实例）时回退到
    ``dependency_type.lower()``，与 dependency_health 种子 id 对齐，兼容单实例依赖。
    """
    health = _signal_to_health_params(signal)
    dependency_id = signal.dependency_id or signal.dependency_type.lower()
    fire_degradation_event(
        signal.dependency_type,
        dependency_id,
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
    避免任务被 GC 提前回收（asyncio 官方推荐做法）。done_callback 显式取回异常，
    避免「Task exception was never retrieved」告警并保留可观测性（降级记录失败应被看到）。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的事件循环（如离线脚本），跳过记录
        return

    def _on_done(task: asyncio.Task[None]) -> None:
        _in_flight_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("degradation_persist_task_failed", exc_info=exc)

    task = loop.create_task(coro)
    _in_flight_tasks.add(task)
    task.add_done_callback(_on_done)


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
    severity: str | None = None,
    affected_capabilities: list[str] | None = None,
    affected_user_count: int = 0,
) -> None:
    """同步入口：在低层客户端 / 同步熔断器中触发降级记录（fire-and-forget）。

    降级检测多发生在同步上下文（熔断器 ``record_failure``）或没有请求会话的客户端层，
    此处将异步 :func:`_persist_degradation_and_health` 提交到事件循环，不阻塞调用方。
    对 ``PROBING``（半开探测）等中间态仅更新实时健康、不写审计事件。

    去重：同 (dependency_type, dependency_id) 状态未变化、且调用方未携带新遥测
    （``circuit_opened_at`` / ``metadata``）时直接跳过，避免 WORM 审计表被重复上报刷爆、
    以及热路径（如 OLAP 未配置时每次查询都触发 DEGRADED）每请求写库/发事件。
    降级态在 down→up→down 往复时仍能各自产生事件（状态发生变化）。

    Args:
        同 :func:`_persist_degradation_and_health`；``state`` 可为 DEGRADED/HEALTHY/PROBING。
    """
    # 同状态且无可观测增量 → 跳过（去重）。熔断驱动的 DEGRADED 携带 circuit_opened_at、
    # 探针携带 metadata，均视为新信息，不被去重。
    has_telemetry = circuit_opened_at is not None or metadata is not None
    key = (dependency_type, dependency_id)
    if not has_telemetry and _fired_state.get(key) == state:
        return
    _fired_state[key] = state
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
            severity=severity,
            affected_capabilities=affected_capabilities,
            affected_user_count=affected_user_count,
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
