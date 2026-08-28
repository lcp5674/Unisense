"""SSRF 防护：连接目标主机校验（对齐 TD §13 安全 / DEV_GUIDE §9）。

拒绝采集器连接回环 / 私有 / 链路本地 / 保留 / 组播地址——防止通过数据源
连接配置（test-connection / list-databases / list-tables / 定时采集）探测
内网主机、云 metadata（169.254.169.254）等 SSRF 向量。

校验语义：
- 从连接配置提取候选 host（含 Kafka ``bootstrap_servers`` 逗号分隔的多个
  ``host:port`` 对）。
- 对每个 host 做 DNS 解析（``socket.getaddrinfo``），任一解析 IP 命中禁区
  即抛 ``BusinessError(SSRF_TARGET_FORBIDDEN)``，连接不建立。
- 连接器 URL 一律由受控字段（host/port/user/password）构建，禁止任意
  ``db_url`` 覆盖（mysql/postgres/doris 已移除该能力）。

本模块为纯逻辑（不依赖请求上下文），便于单测注入解析结果。
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from collections.abc import Iterable
from typing import Any

from app.core.exceptions import BusinessError

#: 显式禁区网段（``ip.is_private`` 之外的补充：CGNAT 100.64/10 等）。
_FORBIDDEN_NETWORKS: list[Any] = [
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),  # IETF 协议分配
    ipaddress.ip_network("198.18.0.0/15"),  # 基准测试保留
]

#: Kafka bootstrap_servers 用逗号分隔 host:port 对（bootstrap_servers 或 host 回退）。
_KAFKA_BOOTSTRAP_KEY = "bootstrap_servers"
#: Kafka Schema Registry 出站 URL 键（真实 HTTP GET，属 SSRF 向量）。
_KAFKA_REGISTRY_KEY = "registry_url"


def _collect_hostport(hosts: list[str], value: Any) -> None:
    """把 str 或 list 型的 host[:port] 候选统一追加（HIGH-4：list 型不得绕过）。"""
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
    elif isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value if isinstance(p, str) and p.strip()]
    else:
        return
    hosts.extend(_strip_port(p) for p in parts)


def _extract_hosts(cfg: dict[str, Any]) -> list[str]:
    """从连接配置提取候选 host 列表（不含端口）。

    覆盖：Kafka ``bootstrap_servers``（str 逗号分隔或 list）、``host``（str/list）、
    ``registry_url``（Schema Registry 真实出站 URL 的 hostname）、
    ``sample_connection.host``（HMS 采样连接指向 HiveServer2，属 SSRF 向量）——
    任一遗漏都会让 SSRF 校验被旁路（HIGH-4 回归防护）。
    """
    hosts: list[str] = []
    _collect_hostport(hosts, cfg.get(_KAFKA_BOOTSTRAP_KEY))
    _collect_hostport(hosts, cfg.get("host"))
    registry = cfg.get(_KAFKA_REGISTRY_KEY)
    if isinstance(registry, str) and registry.strip():
        parsed = urllib.parse.urlparse(registry.strip())
        if parsed.hostname:
            hosts.append(parsed.hostname)
    # 采样连接（hive_metastore 的 sample_connection）指向 HiveServer2，
    # 采集时会真实连接执行 SELECT——必须与主连接同等 SSRF 校验。
    sample_conn = cfg.get("sample_connection")
    if isinstance(sample_conn, dict):
        _collect_hostport(hosts, sample_conn.get("host"))
    return hosts


def _strip_port(hostport: str) -> str:
    """剥离 host:port 中的端口（IPv6 用 [::1]:port 表示，端口在最后一个冒号后）。"""
    if hostport.startswith("["):
        end = hostport.find("]")
        return hostport[1:end] if end > 0 else hostport
    if hostport.count(":") == 1:
        return hostport.split(":")[0]
    # 无端口（纯 IPv6 或裸 host）
    return hostport


def _is_forbidden_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_private: bool
) -> bool:
    """判断单个 IP 是否命中 SSRF 禁区。

    Args:
        ip: 待判断 IP。
        allow_private: True 时放行私有网段（RFC1918——已存数据源采集
            场景生产库就在内网）；回环/链路本地/保留/组播等其余禁区始终拒绝。
    """
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    if not allow_private and ip.is_private:
        return True
    return any(ip in net for net in _FORBIDDEN_NETWORKS)


def validate_ips(ips: Iterable[str], *, allow_private: bool = False) -> list[str]:
    """纯函数校验：返回禁区内的 IP 列表（测试友好，不依赖 DNS）。"""
    forbidden: list[str] = []
    for raw in ips:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            # 非 IP（域名未解析时等）不在此层判定
            continue
        if _is_forbidden_ip(ip, allow_private=allow_private):
            forbidden.append(raw)
    return forbidden


def _resolve_host(host: str) -> list[str]:
    """DNS 解析 host 为 IP 列表（解析失败返回空，交由上层判断是否放行）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return [str(info[4][0]) for info in infos]


def validate_connection_host(cfg: dict[str, Any], *, allow_private: bool = False) -> None:
    """校验连接配置的目标主机不在 SSRF 禁区。

    Args:
        cfg: 明文连接配置。
        allow_private: True 时放行私有网段（仅用于**已落库数据源**的采集/
            探活——生产库就在内网，属平台管理员授权的连接目标）；探活/枚举
            （任意配置，SSRF 主向量）保持严格模式拒绝私有网段。

    Raises:
        BusinessError: 任一候选 host 解析出的 IP 命中禁区
            （error_code=SSRF_TARGET_FORBIDDEN）。
    """
    hosts = _extract_hosts(cfg)
    if not hosts:
        return
    resolved: list[str] = []
    for host in hosts:
        resolved.extend(_resolve_host(host))
    if not resolved:
        # DNS 解析失败：无法判定目标，按禁区处理（fail-closed，宁可拒绝连接）
        raise BusinessError(
            "连接目标主机解析失败，已拒绝连接",
            error_code="SSRF_TARGET_FORBIDDEN",
            ctx={"hosts": hosts},
        )
    forbidden = validate_ips(resolved, allow_private=allow_private)
    if forbidden:
        raise BusinessError(
            "连接目标主机不允许访问（内网/回环/保留地址）",
            error_code="SSRF_TARGET_FORBIDDEN",
            ctx={"forbidden_ips": forbidden},
        )
