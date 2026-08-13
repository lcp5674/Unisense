"""韧性层：熔断器 + 可选依赖探活（对齐 TD §11 韧性 / DEV_GUIDE §17）。

语义领域核心依赖仅为 MySQL；Redis（缓存）、Neo4j（血缘）、ES（检索）、
OLAP（查询）均为可选依赖。任一可选依赖宕机时，核心链路应降级而非整体不可用。
"""

from __future__ import annotations

import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class DegradationSignal:
    """熔断器状态切换信号（供 degradation 模块记录事件 + 实时健康表）。

    设计为纯数据载体，使 resilience 层保持无 db/eventbus 依赖（监听逻辑在
    ``app.core.degradation`` 中，经 :func:`register_degradation_listener` 注入）。

    Attributes:
        dependency_type: 依赖类型（OLAP/GRAPH/ES/LLM/DATASOURCE/NOTIFICATION），即熔断器 name。
        event_state: 降级事件态 —— ``DEGRADED``(熔断开启)/``HEALTHY``(恢复)/``PROBING``(半开探测)。
        reason: 触发原因（circuit_open / circuit_recovered / circuit_half_open）。
        circuit_state: 实时熔断态（OPEN/CLOSED/HALF_OPEN），对应 TD §4.13 circuit_state。
        consecutive_failures: 截至当前的连续失败次数。
        opened_at: 熔断开启时刻（time.monotonic 时间戳），``None`` 表示未开启。
    """

    dependency_type: str
    event_state: str
    reason: str
    circuit_state: str
    consecutive_failures: int
    opened_at: float | None


# 降级监听器：熔断器状态切换时回调，供 degradation 模块记录事件与实时健康。
# 签名：(DegradationSignal) -> None；同步调用，回调内不得抛异常（已兜底捕获）。
DegradationListener = Callable[[DegradationSignal], None]
_degradation_listeners: list[DegradationListener] = []


def register_degradation_listener(fn: DegradationListener) -> None:
    """注册降级状态切换监听器（main.lifespan 中注册，避免 resilience 反向依赖 db/eventbus）。"""
    _degradation_listeners.append(fn)


# 熔断器内部 state 为 hyphenated（open/closed/half-open），需映射到 TD §4.13
# circuit_state ENUM（CLOSED/OPEN/HALF_OPEN，下划线），否则落库值非法。
_CIRCUIT_STATE_ENUM = {"closed": "CLOSED", "open": "OPEN", "half-open": "HALF_OPEN"}


def _emit_degradation(self: CircuitBreaker, event_state: str, reason: str) -> None:
    """构造 DegradationSignal 并通知所有监听器（降级开始/恢复/半开探测）。"""
    signal = DegradationSignal(
        dependency_type=self._name,
        event_state=event_state,
        reason=reason,
        circuit_state=_CIRCUIT_STATE_ENUM[self.state],
        consecutive_failures=self._failures,
        opened_at=self._opened_at,
    )
    for fn in _degradation_listeners:
        try:
            fn(signal)
        except Exception:
            logger.warning(
                "degradation_listener_failed",
                listener=getattr(fn, "__qualname__", repr(fn)),
                dependency_type=signal.dependency_type,
                event_state=event_state,
                exc_info=True,
            )


class CircuitBreaker:
    """最小可用熔断器：closed -> open -> half-open。

    连续失败达到阈值后进入 open（拒绝请求，避免雪崩），
    超过 reset_timeout 后允许一次探测（half-open），成功则复位。

    状态切换（open/close）通过 :func:`register_degradation_listener` 注册的监听器
    上报降级事件（DEGRADED/HEALTHY），对齐 TD §5.2.4 恢复事件与 §5.2.5 降级审计。
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        name: str = "unknown",
        probe_timeout: float | None = None,
    ) -> None:
        self._name = name
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        # 探测结果丢失（调用方未回调 record_*）时的自愈上限：超过该时长允许重新放行一次探测，
        # 避免半开窗口“卡死”导致熔断永久拒绝、依赖无法恢复（工业级容错）。
        # 默认取 max(reset_timeout, 10s)：保证半开单飞窗口至少跨越一个合理论探完成期，
        # 既防同刻重复放行（恢复期雪崩），又能在探测真正丢失时自愈。
        self._probe_timeout = (
            probe_timeout if probe_timeout is not None else max(reset_timeout, 10.0)
        )
        self._failures = 0
        self._opened_at: float | None = None
        self._open = False
        # 半开窗口内是否已放行一次探测（同 loop 内无 await，判定+置位原子，避免恢复期雪崩）
        self._probing = False
        # 探测放行的 monotonic 时间戳；用于 probe_timeout 判定探测是否“丢失”。
        self._probing_since: float | None = None

    @property
    def state(self) -> str:
        if not self._open:
            return "closed"
        if (
            self._opened_at is not None
            and (time.monotonic() - self._opened_at) >= self._reset_timeout
        ):
            return "half-open"
        return "open"

    @property
    def failures(self) -> int:
        """当前连续失败次数（实时健康表/看板用）。"""
        return self._failures

    def allow(self) -> bool:
        if not self._open:
            return True
        now = time.monotonic()
        half_open = self._opened_at is not None and (now - self._opened_at) >= self._reset_timeout
        if not half_open:
            return False
        # 半开窗口内已放行过探测：若探测结果因调用方异常/丢失而迟迟未回调 record_*，
        # 超过 probe_timeout 视为本次探测失效，释放窗口允许重新探测，防止永久拒绝。
        if self._probing:
            if (
                self._probing_since is not None
                and (now - self._probing_since) >= self._probe_timeout
            ):
                self._probing = False
                self._probing_since = None
            else:
                return False
        self._probing = True
        self._probing_since = now
        # 进入半开探测：上报 PROBING（实时健康表 circuit_state=HALF_OPEN），不写审计事件
        _emit_degradation(self, "PROBING", "circuit_half_open")
        return True

    def record_failure(self) -> None:
        was_probe = self._probing
        was_open = self._open
        self._probing = False
        self._probing_since = None
        self._failures += 1
        # 半开探测失败或连续失败达到阈值 -> 打开熔断并重置计时
        if was_probe or self._failures >= self._failure_threshold:
            self._open = True
            self._opened_at = time.monotonic()
            # 仅在「从关闭/半开切到打开」这一刻上报，避免每次失败重复刷事件
            if not was_open:
                _emit_degradation(self, "DEGRADED", "circuit_open")

    def record_success(self) -> None:
        was_open = self._open
        self._probing = False
        self._probing_since = None
        self._failures = 0
        self._open = False
        self._opened_at = None
        # 仅在「从打开切回关闭（恢复）」这一刻上报，满足 TD §5.2.4 恢复事件
        if was_open:
            _emit_degradation(self, "HEALTHY", "circuit_recovered")


def _tcp_alive(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_host_port(url: str) -> tuple[str, int] | None:
    """从连接串解析 host:port（支持 bolt://、http://、mysql+aiomysql:// 等）。"""
    matched = re.match(r"\w+://([^/:]+):(\d+)", url)
    if not matched:
        return None
    return matched.group(1), int(matched.group(2))


def optional_dependency_status() -> dict[str, bool]:
    """探活可选依赖（Neo4j / ES / OLAP），返回各依赖是否存活。

    仅做 TCP 连通性探测，不引入额外驱动；空 url 视为未启用（跳过）。
    """
    result: dict[str, bool] = {}
    checks: dict[str, str] = {
        "neo4j": settings.neo4j_url,
        "elasticsearch": settings.es_url,
        "olap": settings.olap_url,
    }
    for name, url in checks.items():
        if not url:
            continue
        hp = _parse_host_port(url)
        result[name] = _tcp_alive(*hp) if hp else False
    return result


# ---- P2/P3: 预构建熔断器实例（OLAP / Neo4j / ES）----

# OLAP 熔断器：consume 语义查询下推，连续 5 次失败后熔断，30s 后半开探测
olap_breaker = CircuitBreaker(name="OLAP", failure_threshold=5, reset_timeout=30.0)

# Neo4j 熔断器：血缘图查询（GRAPH 依赖类型），连续 3 次失败后熔断（图查询更脆弱），20s 后半开探测
neo4j_breaker = CircuitBreaker(name="GRAPH", failure_threshold=3, reset_timeout=20.0)

# ES 熔断器：全文检索，连续 5 次失败后熔断，30s 后半开探测。
# ES 客户端已接入（app/core/es_client.py）：就绪探针经 EsClient.health() 真实 .ping() 探活、
# 检索经 EsClient.search()/index() 全程受 es_breaker 保护；熔断器已进入降级矩阵真实调用路径
# （见 app/api/health.py 与 app/core/es_client.py），非死代码。ES 包缺失或未配置 es_url 时客户端
# 自动禁用，调用方优雅降级（SearchUnavailableError），绝不因缺依赖导致启动失败。
es_breaker = CircuitBreaker(name="ES", failure_threshold=5, reset_timeout=30.0)


def get_circuit_breaker(service: str) -> CircuitBreaker:
    """获取指定服务的熔断器实例。

    Args:
        service: 服务名（olap / neo4j / es）。

    Returns:
        对应的 CircuitBreaker 实例；未知服务返回新实例。
    """
    breakers = {
        "olap": olap_breaker,
        "neo4j": neo4j_breaker,
        "es": es_breaker,
    }
    return breakers.get(service, CircuitBreaker())
