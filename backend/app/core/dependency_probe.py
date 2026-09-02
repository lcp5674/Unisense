"""周期依赖健康探针（对齐 TD §4.13 实时健康态，根治「只降不升」误报）。

背景
----
``dependency_health`` 表此前只被三类来源写入：
1. 启动种子 ``ensure_dependency_health_seed``（仅当不存在，INSERT IGNORE）；
2. 熔断器状态切换信号（``handle_circuit_signal``，只写状态**变化**）；
3. consume 查询失败时的 OLAP 降级（``fire_degradation_event``）。

三者都是「事件驱动」，**没有任何周期探针**：真实依赖可达（Neo4j/ES/LLM 网关）
却永远停留在历史降级态（GRAPH 8-17 OPEN、LLM 8-14 HALF_OPEN），运营看板
「核心依赖健康」因此误报「1/4 正常、3 个降级/不可用」。

本模块提供周期探针：
- 每 ``interval`` 秒对受监控依赖（LLM/OLAP/GRAPH/ES）实测连通性并写回真实状态；
- 已配置且可达 → ``HEALTHY``/``CLOSED``（并清除进程内降级注册表条目）；
- 已配置但不可达 → ``DEGRADED``/``CLOSED``（熔断器 OPEN 由熔断器事件另行保持，
  探针不与其冲突——熔断器事件带 circuit_opened_at 遥测，不会被探针覆盖）；
- **未配置** → ``status=HEALTHY`` + ``meta.enabled=false``（未配置≠故障，不计入
  降级统计，前端展示「未启用」），同时清除该依赖的历史降级条目——避免历史
  DEGRADED 残留把「未启用」误显示为「降级/不可用」。

探针全程 best-effort：单次探测失败仅告警，绝不阻断主链路（降级路径自身不能再降级）。
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

import httpx

from app.core.config import settings
from app.core.degradation import update_dependency_health
from app.core.degradation_registry import get_degradation_registry
from app.core.logging import get_logger

logger = get_logger("unisense.dependency_probe")

# 受监控依赖：dependency_type → (dependency_id, 启用判定 url 字段、探活方式)
# GRAPH/ES/OLAP 走 TCP 探活（复用 /ready 的 optional_dependency_status 同款语义）；
# LLM 走 HTTP 网关探活（base_url 可达即视为网关存活，模型级故障由熔断器实时反映）。
_TCP_DEPENDENCIES: list[tuple[str, str, str]] = [
    ("GRAPH", "graph", "neo4j_url"),
    ("ES", "es", "es_url"),
    ("OLAP", "olap", "olap_url"),
]
_LLM_DEPENDENCY = ("LLM", "llm")

# 降级注册表组件前缀：与熔断器 `circuit_breaker:{id}` 区分，避免互相误清。
_REGISTRY_PREFIX = "dependency_probe:"

# LLM 网关探活超时（秒）；网关可达即可（连接建立 + 任意 HTTP 响应），
# 不做完整 chat 调用——避免探针本身消耗模型 token/拖慢周期。
_LLM_PROBE_TIMEOUT = 3.0

# 降级注册表组件名 → 归一化依赖 id（注册/清除同一组件名）
def _registry_component(dep_id: str) -> str:
    return f"{_REGISTRY_PREFIX}{dep_id}"


async def _tcp_alive(url: str, timeout: float = 0.5) -> bool:
    """TCP 连通性探活（复用 /ready 同款语义：空 url 视为未启用返回 False）。"""
    if not url:
        return False
    matched = re.match(r"\w+://([^/:]+):(\d+)", url)
    if not matched:
        return False
    host, port = matched.group(1), int(matched.group(2))
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (TimeoutError, OSError):
        return False


async def _llm_gateway_alive() -> bool:
    """LLM 网关可达性探活：GET base_url，任意 HTTP 响应即视为可达（连接成功）。"""
    base_url = getattr(settings, "llm_base_url", None)
    if not base_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=_LLM_PROBE_TIMEOUT, verify=True) as client:
            await client.get(base_url)
        return True
    except (httpx.HTTPError, OSError):
        return False


async def _olap_probe_target() -> tuple[bool, str]:
    """获取 OLAP 探测目标（DB 配置优先、env 兜底；方案 A）。

    DB 配置（query_engine_config）启用时以 DB 的 doris_host/port 为准，否则回落
    env 的 ``UNISENSE_OLAP_URL``；均未配置返回 (False, '')（探针按「未启用」写回，
    避免把「未配置」误报为降级）。
    """
    try:
        from app.db.mysql import async_session_factory
        from app.services.query_engine.config_service import QueryEngineConfigService

        async with async_session_factory() as db:
            eff = await QueryEngineConfigService(db).get_effective()
            if eff.get("olap_configured"):
                host = eff["doris_host"]
                port = int(eff["doris_port"] or 8030)
                return True, f"http://{host}:{port}"
            return False, ""
    except Exception:  # noqa: BLE001 - best-effort：DB 不可达时按 env 判定
        return bool(settings.olap_url), settings.olap_url


async def _probe_dependency(
    dep_type: str, dep_id: str, enabled: bool, alive: bool
) -> None:
    """将单个依赖的真实健康态写回 dependency_health 并同步降级注册表。

    Args:
        dep_type / dep_id: 依赖标识（对齐 dependency_health 唯一键）。
        enabled: 是否已配置（未配置视为「未启用」而非故障）。
        alive: 连通性探测结果（已配置时有效）。
    """
    now = datetime.now(UTC)
    registry = get_degradation_registry()
    component = _registry_component(dep_id)
    if not enabled:
        # 未配置：不参与降级统计，展示「未启用」；同时清除历史降级残留
        await update_dependency_health(
            dep_type,
            dep_id,
            status="HEALTHY",
            circuit_state="CLOSED",
            consecutive_failures=0,
            circuit_opened_at=None,
            last_check_at=now,
            metadata={"enabled": False, "note": "未配置，未启用"},
        )
        registry.clear_degradation(component)
        return
    if alive:
        await update_dependency_health(
            dep_type,
            dep_id,
            status="HEALTHY",
            circuit_state="CLOSED",
            consecutive_failures=0,
            circuit_opened_at=None,
            last_check_at=now,
            metadata={"enabled": True, "last_probe": "ok"},
        )
        registry.clear_degradation(component)
    else:
        await update_dependency_health(
            dep_type,
            dep_id,
            status="DEGRADED",
            circuit_state="CLOSED",
            last_check_at=now,
            metadata={"enabled": True, "last_probe": "fail"},
        )
        registry.register_degradation(component, f"probe_failed: {dep_id}")


async def run_dependency_probe_once() -> dict[str, str]:
    """探测全部受监控依赖并写回真实健康态，返回 {dep_id: status}。

    并发探测（TCP 探活/HTTP 探活互不阻塞），全程 best-effort：单个依赖探测
    异常不影响其余依赖写回。
    """
    results: dict[str, str] = {}

    async def _run(dep_type: str, dep_id: str, enabled: bool, alive: bool) -> None:
        try:
            await _probe_dependency(dep_type, dep_id, enabled, alive)
            results[dep_id] = "HEALTHY" if (not enabled or alive) else "DEGRADED"
        except Exception:  # noqa: BLE001 - best-effort：探针失败仅告警
            logger.warning(
                "dependency_probe_update_failed",
                dependency_type=dep_type,
                dependency_id=dep_id,
                exc_info=True,
            )

    # TCP 探活组：已配置（url 非空）才探测，未配置直接按未启用处理。
    # OLAP 段经 DB 生效配置解析（方案 A）；GRAPH/ES 仍以 env 为准。
    tasks = []
    for dep_type, dep_id, url_attr in _TCP_DEPENDENCIES:
        if dep_id == "olap":
            enabled, probe_url = await _olap_probe_target()
        else:
            url = getattr(settings, url_attr, None)
            enabled = bool(url)
            probe_url = url
        alive = await _tcp_alive(probe_url) if enabled else False
        tasks.append(asyncio.create_task(_run(dep_type, dep_id, enabled, alive)))

    # LLM 组：已配置（base_url + api_key 均非空）才探测
    llm_enabled = bool(
        getattr(settings, "llm_base_url", None) and getattr(settings, "llm_api_key", None)
    )
    llm_alive = await _llm_gateway_alive() if llm_enabled else False
    tasks.append(
        asyncio.create_task(_run(_LLM_DEPENDENCY[0], _LLM_DEPENDENCY[1], llm_enabled, llm_alive))
    )

    await asyncio.gather(*tasks)
    return results


async def dependency_probe_loop(interval: float = 60.0) -> None:
    """周期依赖健康探针主循环（main.lifespan 中 create_task 启动）。

    Args:
        interval: 探测间隔（秒），默认 60s——既保证看板实时性，又不至于对
            外部网关（LLM/OLAP）施加过频探测压力。
    """
    logger.info("dependency_probe_loop_started", interval=interval)
    while True:
        try:
            statuses = await run_dependency_probe_once()
            degraded = [k for k, v in statuses.items() if v != "HEALTHY"]
            if degraded:
                logger.info("dependency_probe_round", degraded=degraded)
        except asyncio.CancelledError:
            logger.info("dependency_probe_loop_stopped")
            raise
        except Exception:  # noqa: BLE001 - best-effort：单轮失败不终止循环
            logger.warning("dependency_probe_round_failed", exc_info=True)
        await asyncio.sleep(interval)
