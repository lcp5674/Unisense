"""韧性层：熔断器 + 可选依赖探活（对齐 TD §11 韧性 / DEV_GUIDE §17）。

语义领域核心依赖仅为 MySQL；Redis（缓存）、Neo4j（血缘）、ES（检索）、
OLAP（查询）均为可选依赖。任一可选依赖宕机时，核心链路应降级而非整体不可用。
"""

from __future__ import annotations

import re
import socket
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import redis

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
    # 依赖实例标识（如 olap / neo4j-cluster-2 / ds-123）。与 dependency_type 区分：
    # type 是依赖「种类」（OLAP/GRAPH/ES…），id 是「具体实例」。缺省空串时由
    # handle_circuit_signal 回退到 dependency_type.lower()，兼容单实例依赖。
    dependency_id: str = ""


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
        dependency_id=self._dependency_id,
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


# ---------------------------------------------------------------------------
# 熔断态共享存储（跨 worker / 副本协调，对齐 TD §5.2a Redis 状态键）
# ---------------------------------------------------------------------------
# 单进程内熔断器状态仅本 worker 可见；多 worker / 多副本部署时，worker A 打开熔断、
# worker B 仍各自持有 CLOSED，会继续向故障依赖打流量（雪崩）。共享存储让「OPEN 状态」
# 与「半开单飞探针」跨进程协调：任一 worker 打开 → 全部 worker 拒绝；仅一个 worker 探测。


class CircuitBreakerStore:
    """熔断态共享存储抽象（同步接口，best-effort）。

    熔断判定在同步上下文执行，故存储接口亦为同步；调用方（CircuitBreaker）已对异常兜底，
    实现内无需再抛。状态键对齐 TD §5.2a：``cb:{dep}:state`` / ``cb:{dep}:opened_at`` /
    ``cb:{dep}:probing``（单飞探针锁）。
    """

    def load_state(self, dep_key: str) -> tuple[str, float | None] | None:
        """读取共享熔断态 (state, opened_at)；无记录返回 None。"""
        raise NotImplementedError

    def save_state(self, dep_key: str, state: str, opened_at: float | None, ttl: float) -> None:
        """写入共享熔断态并设置 TTL（秒），到期自动失效防永久集群 OPEN。"""
        raise NotImplementedError

    def try_acquire_probe(self, dep_key: str, ttl: float) -> bool:
        """尝试获取集群级半开探针锁：成功(本 worker 探测)返回 True，已被持锁返回 False。"""
        raise NotImplementedError

    def clear_probe(self, dep_key: str) -> None:
        """释放探针锁（恢复或探测失败后）。"""


class LocalCircuitBreakerStore(CircuitBreakerStore):
    """进程内熔断态存储（单 worker / Redis 不可用时降级）。

    单 worker 的 intra-worker 单飞由 ``CircuitBreaker._probing`` 保证，
    故 :meth:`try_acquire_probe` 恒放行（无需跨进程锁）。
    """

    def __init__(self) -> None:
        self._states: dict[str, tuple[str, float | None]] = {}

    def load_state(self, dep_key: str) -> tuple[str, float | None] | None:
        return self._states.get(dep_key)

    def save_state(self, dep_key: str, state: str, opened_at: float | None, ttl: float) -> None:
        self._states[dep_key] = (state, opened_at)

    def try_acquire_probe(self, dep_key: str, ttl: float) -> bool:
        return True

    def clear_probe(self, dep_key: str) -> None:
        return None  # noqa: RET501  (无跨进程锁，no-op)


# 模块级默认存储：初始为进程内 Local；lifespan 中 Redis 可用时经
# :func:`init_circuit_breaker_store` 切换为 RedisCircuitBreakerStore（影响所有熔断器单例）。
_DEFAULT_STORE: CircuitBreakerStore = LocalCircuitBreakerStore()
# 共享态本地缓存 TTL（秒）：避免每次 allow() 都打 Redis，限制协调开销（仍保证秒级视图一致）。
_STORE_CACHE_TTL = 1.0


def get_default_circuit_breaker_store() -> CircuitBreakerStore:
    """获取当前默认熔断态存储（供测试 / 诊断）。"""
    return _DEFAULT_STORE


def set_default_circuit_breaker_store(store: CircuitBreakerStore) -> None:
    """切换默认熔断态存储（测试用，不进入生产路径）。"""
    global _DEFAULT_STORE
    _DEFAULT_STORE = store


class RedisCircuitBreakerStore(CircuitBreakerStore):
    """Redis 熔断态存储（跨 worker / 副本共享，对齐 TD §5.2a Redis 状态键）。

    同步 redis 客户端（socket_timeout 极短，best-effort）：任一 Redis 异常均被调用方吞掉，
    降级为「无共享态」（由本 worker 本地状态机兜底），绝不因 Redis 抖动阻断主流程或雪崩。
    """

    def __init__(self, url: str, *, socket_timeout: float = 0.1) -> None:
        self._url = url
        self._socket_timeout = socket_timeout
        # sync redis 客户端（redis-py 同步接口动态返回 Any，故以 Any 标注规避 stub 歧义）
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = redis.Redis.from_url(
                self._url,
                decode_responses=True,
                socket_timeout=self._socket_timeout,
                socket_connect_timeout=self._socket_timeout,
            )
        return self._client

    @staticmethod
    def _state_key(dep_key: str) -> str:
        return f"cb:{dep_key}:state"

    @staticmethod
    def _opened_key(dep_key: str) -> str:
        return f"cb:{dep_key}:opened_at"

    @staticmethod
    def _probe_key(dep_key: str) -> str:
        return f"cb:{dep_key}:probing"

    def load_state(self, dep_key: str) -> tuple[str, float | None] | None:
        client = self._get_client()
        state = client.get(self._state_key(dep_key))
        if state is None:
            return None
        raw = client.get(self._opened_key(dep_key))
        opened_at = float(raw) if raw is not None else None
        return (state, opened_at)

    def save_state(self, dep_key: str, state: str, opened_at: float | None, ttl: float) -> None:
        client = self._get_client()
        client.set(self._state_key(dep_key), state, ex=int(ttl))
        if opened_at is not None:
            client.set(self._opened_key(dep_key), str(opened_at), ex=int(ttl))
        else:
            # 恢复（CLOSED）：清理 opened_at，避免残留
            client.delete(self._opened_key(dep_key))

    def try_acquire_probe(self, dep_key: str, ttl: float) -> bool:
        client = self._get_client()
        # SET NX EX：仅当无人持锁时获取成功（集群级单飞），到期自动释放防死锁
        return bool(client.set(self._probe_key(dep_key), "1", nx=True, ex=int(ttl)))

    def clear_probe(self, dep_key: str) -> None:
        client = self._get_client()
        client.delete(self._probe_key(dep_key))


def init_circuit_breaker_store(redis_pool: object | None = None) -> None:
    """初始化熔断态共享存储（main.lifespan 中调用）。

    Redis 可用时构建 RedisCircuitBreakerStore（同步客户端，由 ``settings.redis_url`` 推导），
    影响全部熔断器单例的跨进程协调；否则降级为进程内 Local 存储。

    Args:
        redis_pool: 异步 Redis 连接池（可用性信号）；为 None 或 Redis 不可用时降级 Local。
    """
    global _DEFAULT_STORE
    if redis_pool is None or not getattr(settings, "redis_url", None):
        _DEFAULT_STORE = LocalCircuitBreakerStore()
        if redis_pool is None:
            logger.info("circuit_breaker_store_initialized", type="local_fallback")
        return
    try:
        _DEFAULT_STORE = RedisCircuitBreakerStore(str(settings.redis_url))
        logger.info("circuit_breaker_store_initialized", type="redis")
    except Exception:
        logger.warning("circuit_breaker_store_init_failed", exc_info=True)
        _DEFAULT_STORE = LocalCircuitBreakerStore()


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
        dependency_id: str | None = None,
        *,
        error_rate_threshold: float | None = None,
        error_rate_window: int = 20,
        store: CircuitBreakerStore | None = None,
    ) -> None:
        self._name = name
        # 依赖实例标识：缺省按 name.lower() 推断（单实例依赖 OLAP/GRAPH/ES 与
        # dependency_health 种子 id 对齐）；多实例依赖（如多 DATASOURCE）显式传入。
        self._dependency_id = dependency_id if dependency_id is not None else name.lower()
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        # 探测结果丢失（调用方未回调 record_*）时的自愈上限：超过该时长允许重新放行一次探测，
        # 避免半开窗口“卡死”导致熔断永久拒绝、依赖无法恢复（工业级容错）。
        # 默认取 max(reset_timeout, 10s)：保证半开单飞窗口至少跨越一个合理论探完成期，
        # 既防同刻重复放行（恢复期雪崩），又能在探测真正丢失时自愈。
        self._probe_timeout = (
            probe_timeout if probe_timeout is not None else max(reset_timeout, 10.0)
        )
        # 错误率滑动窗口阈值（对齐 TD §5.2a：错误率超阈切 OPEN）。为 None 时关闭错误率触发，
        # 仅依赖连续失败阈值（向后兼容）。error_rate_window 为窗口内保留的最近调用样本数
        # （有界 deque，无内存泄漏）；仅当样本数达到窗口才判定，避免小样本误开。
        self._error_rate_threshold = error_rate_threshold
        self._error_rate_window = error_rate_window
        # 最近调用结果滑动窗口（True=成功 / False=失败），有界 deque 防止内存增长。
        self._recent_outcomes: deque[bool] = deque(maxlen=error_rate_window)
        # 共享存储：None 表示使用模块级默认存储（_DEFAULT_STORE，可经 init 切换为 Redis）。
        self._store = store
        # 共享态本地缓存（秒级 TTL）：限制 Redis 协调频率，避免每次 allow() 都打网络。
        self._shared_cache: tuple[str, float | None] | None = None
        self._shared_cache_at: float = 0.0
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

    @property
    def error_rate(self) -> float:
        """最近窗口内失败率（0.0~1.0）；样本不足时返回 0.0。"""
        if not self._recent_outcomes:
            return 0.0
        return 1.0 - sum(self._recent_outcomes) / len(self._recent_outcomes)

    # --- 共享存储协调（跨 worker / 副本，对齐 TD §5.2a）---

    @property
    def _effective_store(self) -> CircuitBreakerStore | None:
        """实际使用的存储：实例级覆盖优先，否则模块级默认（可被 init 切换为 Redis）。"""
        return self._store or _DEFAULT_STORE

    @property
    def _store_key(self) -> str:
        """共享存储键（按 依赖类型:实例 唯一）。"""
        return f"{self._name}:{self._dependency_id}"

    def _load_shared_state(self) -> tuple[str, float | None] | None:
        """读取共享熔断态（带秒级本地缓存，限制 Redis 频率）。"""
        store = self._effective_store
        if store is None:
            return None
        now = time.monotonic()
        if self._shared_cache is not None and (now - self._shared_cache_at) < _STORE_CACHE_TTL:
            return self._shared_cache
        try:
            state = store.load_state(self._store_key)
        except Exception:
            logger.warning("circuit_store_load_failed", dependency=self._name, exc_info=True)
            return None
        self._shared_cache = state
        self._shared_cache_at = now
        return state

    def _save_shared_state(self, state: str, opened_at: float | None) -> None:
        """写入共享熔断态（带 TTL）并刷新本地缓存。best-effort。"""
        store = self._effective_store
        if store is None:
            return
        try:
            store.save_state(self._store_key, state, opened_at, self._reset_timeout + 10.0)
            self._shared_cache = (state, opened_at)
            self._shared_cache_at = time.monotonic()
        except Exception:
            logger.warning("circuit_store_save_failed", dependency=self._name, exc_info=True)

    def _acquire_probe_lock(self) -> bool:
        """尝试获取集群级半开探针锁；失败默认放行（安全侧，避免永久拒绝）。"""
        store = self._effective_store
        if store is None:
            return True
        try:
            return store.try_acquire_probe(self._store_key, self._probe_timeout)
        except Exception:
            logger.warning("circuit_store_probe_failed", dependency=self._name, exc_info=True)
            return True

    def _release_probe_lock(self) -> None:
        store = self._effective_store
        if store is None:
            return
        try:
            store.clear_probe(self._store_key)
        except Exception:
            logger.warning("circuit_store_clear_probe_failed", dependency=self._name, exc_info=True)

    def _error_rate_should_open(self) -> bool:
        """本地错误率是否超阈（小样本不触发）。"""
        return (
            self._error_rate_threshold is not None
            and len(self._recent_outcomes) >= self._error_rate_window
            and self.error_rate > self._error_rate_threshold
        )

    def allow(self) -> bool:
        now = time.monotonic()
        # 1. 本地错误率超阈 → 打开（同步写共享态，集群级可见）
        if not self._open and self._error_rate_should_open():
            self._open = True
            self._opened_at = now
            self._save_shared_state("OPEN", now)
            _emit_degradation(self, "DEGRADED", "circuit_open")
            return False
        # 2. 本地关闭：与共享态协调（集群中已有 peer 打开且冷却未过 → 本 worker 亦拒绝，不重复上报）
        if not self._open:
            shared = self._load_shared_state()
            peer_open = (
                shared is not None
                and shared[0] == "OPEN"
                and shared[1] is not None
                and (now - shared[1]) < self._reset_timeout
            )
            return not peer_open
        # 3. 本地已打开：冷却未到 → 拒绝（集群仍 OPEN）
        half_open = self._opened_at is not None and (now - self._opened_at) >= self._reset_timeout
        if not half_open:
            return False
        # 4. 进入半开：单飞探针（集群级，避免多 worker 同时探测雪崩）
        if self._probing:
            # 探测结果丢失（超 probe_timeout）视为失效，释放窗口允许重新探测，防止永久拒绝
            if (
                self._probing_since is not None
                and (now - self._probing_since) >= self._probe_timeout
            ):
                self._probing = False
                self._probing_since = None
            else:
                return False
        if not self._acquire_probe_lock():
            return False  # 另一 worker 持有探针锁，本 worker 拒绝（不重复探测）
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
        self._recent_outcomes.append(False)
        # 半开探测失败或连续失败达到阈值 -> 打开熔断并重置计时
        if was_probe or self._failures >= self._failure_threshold:
            self._open = True
            self._opened_at = time.monotonic()
            # 同步写共享态：任一 worker 打开 → 全部 worker 拒绝（集群级防雪崩）
            self._save_shared_state("OPEN", self._opened_at)
            # 仅在「从关闭/半开切到打开」这一刻上报，避免每次失败重复刷事件
            if not was_open:
                _emit_degradation(self, "DEGRADED", "circuit_open")

    def record_success(self) -> None:
        was_open = self._open
        self._probing = False
        self._probing_since = None
        self._failures = 0
        self._recent_outcomes.append(True)
        self._open = False
        self._opened_at = None
        # 仅在「从打开切回关闭（恢复）」这一刻上报，满足 TD §5.2.4 恢复事件
        if was_open:
            self._save_shared_state("CLOSED", None)
            self._release_probe_lock()
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

# OLAP 熔断器：consume 语义查询下推。阈值对齐 TD §5.2a（OLAP 连续失败阈值=3，冷却期=30s，
# 错误率阈值=5%）。错误率超阈（窗口内样本达 20 且失败率>5%）亦切 OPEN，与连续失败互补。
olap_breaker = CircuitBreaker(
    name="OLAP", failure_threshold=3, reset_timeout=30.0, error_rate_threshold=0.05
)

# Neo4j 熔断器：血缘图查询（GRAPH）。阈值对齐 TD §5.2a：连续失败阈值=5，冷却期=30s，错误率=5%。
neo4j_breaker = CircuitBreaker(
    name="GRAPH", failure_threshold=5, reset_timeout=30.0, error_rate_threshold=0.05
)

# ES 熔断器：全文检索。阈值对齐 TD §5.2a（ES 连续失败阈值=5，冷却期=30s，错误率=10%）。
# ES 客户端已接入（app/core/es_client.py）：就绪探针经 EsClient.health() 真实 .ping() 探活、
# 检索经 EsClient.search()/index() 全程受 es_breaker 保护；熔断器已进入降级矩阵真实调用路径
# （见 app/api/health.py 与 app/core/es_client.py），非死代码。ES 包缺失或未配置 es_url 时客户端
# 自动禁用，调用方优雅降级（SearchUnavailableError），绝不因缺依赖导致启动失败。
es_breaker = CircuitBreaker(
    name="ES", failure_threshold=5, reset_timeout=30.0, error_rate_threshold=0.10
)


# 预构建熔断器注册表（对齐 TD §5.2a 各依赖类型阈值）。模块级单例，保证跨调用共享同一熔断状态。
_BREAKERS: dict[str, CircuitBreaker] = {
    "olap": olap_breaker,
    "neo4j": neo4j_breaker,
    "es": es_breaker,
}

# 未知服务惰性创建的熔断器缓存：保证跨调用返回同一实例（否则每次新建→状态丢失→熔断失效）。
_unknown_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(service: str) -> CircuitBreaker:
    """获取指定服务的熔断器实例（共享单例，跨调用状态稳定）。

    Args:
        service: 服务名（olap / neo4j / es 等）。

    Returns:
        对应的 CircuitBreaker 单例；未知服务惰性创建并缓存（name 取 service.upper()），
        同样跨调用共享，避免「每调用新建实例 → 熔断状态丢失」的隐性故障。
    """
    if service in _BREAKERS:
        return _BREAKERS[service]
    if service not in _unknown_breakers:
        _unknown_breakers[service] = CircuitBreaker(name=service.upper())
    return _unknown_breakers[service]
