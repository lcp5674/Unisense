"""韧性层：熔断器 + 可选依赖探活（对齐 TD §11 韧性 / DEV_GUIDE §17）。

语义领域核心依赖仅为 MySQL；Redis（缓存）、Neo4j（血缘）、ES（检索）、
OLAP（查询）均为可选依赖。任一可选依赖宕机时，核心链路应降级而非整体不可用。
"""

from __future__ import annotations

import re
import socket
import time

from app.core.config import settings


class CircuitBreaker:
    """最小可用熔断器：closed -> open -> half-open。

    连续失败达到阈值后进入 open（拒绝请求，避免雪崩），
    超过 reset_timeout 后允许一次探测（half-open），成功则复位。
    """

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 30.0) -> None:
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._failures = 0
        self._opened_at: float | None = None
        self._open = False

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

    def allow(self) -> bool:
        if not self._open:
            return True
        half_open = (
            self._opened_at is not None
            and (time.monotonic() - self._opened_at) >= self._reset_timeout
        )
        return half_open  # 半开窗口允许一次探测

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._open = True
            self._opened_at = time.monotonic()

    def record_success(self) -> None:
        self._failures = 0
        self._open = False
        self._opened_at = None


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
olap_breaker = CircuitBreaker(failure_threshold=5, reset_timeout=30.0)

# Neo4j 熔断器：血缘图查询，连续 3 次失败后熔断（图查询更脆弱），20s 后半开探测
neo4j_breaker = CircuitBreaker(failure_threshold=3, reset_timeout=20.0)

# ES 熔断器：全文检索，连续 5 次失败后熔断，30s 后半开探测
es_breaker = CircuitBreaker(failure_threshold=5, reset_timeout=30.0)


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
