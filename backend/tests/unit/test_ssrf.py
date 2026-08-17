"""SSRF 防护与探活限流单元测试（P0-2）。

覆盖：IP 禁区判定（含 allow_private 语义）、DNS 解析校验（fail-closed）、
Kafka bootstrap_servers 多主机、registry 构建入口校验、探活限流。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import BusinessError
from app.core.probe_throttle import check_probe_rate
from app.core.secrets import SecretManager
from app.core.ssrf import validate_connection_host, validate_ips

# ---------- validate_ips 纯函数 ----------


def test_validate_ips_rejects_loopback_private_linklocal() -> None:
    """禁区：回环/私有/链路本地/保留/组播一律拒绝。"""
    ips = ["127.0.0.1", "::1", "10.0.0.1", "192.168.1.1", "172.16.0.5"]
    ips += ["169.254.169.254", "0.0.0.0", "224.0.0.1"]
    assert sorted(validate_ips(ips)) == sorted(ips)


def test_validate_ips_allows_public() -> None:
    """公网 IP 放行。"""
    assert validate_ips(["8.8.8.8", "114.114.114.114"]) == []


def test_validate_ips_cgnat_rejected() -> None:
    """CGNAT 100.64/10 显式禁区（即使 allow_private 也不放行）。"""
    assert validate_ips(["100.64.0.1"]) == ["100.64.0.1"]
    assert validate_ips(["100.64.0.1"], allow_private=True) == ["100.64.0.1"]


def test_validate_ips_allow_private_keeps_loopback_blocked() -> None:
    """allow_private=True 放行私有网段，但回环/链路本地仍拒绝。"""
    assert validate_ips(["10.0.0.1", "192.168.1.1"], allow_private=True) == []
    assert validate_ips(["127.0.0.1", "169.254.169.254"], allow_private=True) == [
        "127.0.0.1",
        "169.254.169.254",
    ]


# ---------- validate_connection_host（DNS 解析） ----------


def _patch_resolve(host_to_ips: dict[str, list[str]]) -> None:
    """mock socket.getaddrinfo：host → 固定 IP 列表。"""

    def fake_getaddrinfo(host: str, _port: object) -> list[tuple]:
        ips = host_to_ips.get(host, [])
        return [(2, 1, 6, "", (ip, 0)) for ip in ips]

    patcher = patch("app.core.ssrf.socket.getaddrinfo", side_effect=fake_getaddrinfo)
    patcher.start()
    return patcher  # type: ignore[return-value]


def test_validate_connection_host_rejects_private() -> None:
    patcher = _patch_resolve({"db.internal": ["10.0.0.5"]})
    try:
        with pytest.raises(BusinessError) as exc:
            validate_connection_host({"host": "db.internal"})
        assert exc.value.error_code == "SSRF_TARGET_FORBIDDEN"
    finally:
        patcher.stop()


def test_validate_connection_host_allows_public() -> None:
    patcher = _patch_resolve({"db.pub.com": ["8.8.8.8"]})
    try:
        validate_connection_host({"host": "db.pub.com"})
    finally:
        patcher.stop()


def test_validate_connection_host_allow_private() -> None:
    patcher = _patch_resolve({"db.internal": ["10.0.0.5"]})
    try:
        # 已存源采集路径：放行私有网段
        validate_connection_host({"host": "db.internal"}, allow_private=True)
    finally:
        patcher.stop()


def test_validate_connection_host_rejects_metadata() -> None:
    patcher = _patch_resolve({"metadata": ["169.254.169.254"]})
    try:
        with pytest.raises(BusinessError):
            validate_connection_host({"host": "metadata"})
    finally:
        patcher.stop()


def test_validate_connection_host_resolve_failure_fail_closed() -> None:
    patcher = _patch_resolve({"no.such.host": []})
    try:
        with pytest.raises(BusinessError) as exc:
            validate_connection_host({"host": "no.such.host"})
        assert exc.value.error_code == "SSRF_TARGET_FORBIDDEN"
    finally:
        patcher.stop()


def test_validate_connection_host_kafka_bootstrap() -> None:
    """Kafka bootstrap_servers 多主机任一路径命中即拒绝。"""
    patcher = _patch_resolve(
        {"broker-a": ["10.0.0.1"], "broker-b": ["8.8.8.8"], "192.168.1.1": ["192.168.1.1"]}
    )
    try:
        with pytest.raises(BusinessError):
            validate_connection_host({"bootstrap_servers": "broker-a:9092,broker-b:9092"})
        with pytest.raises(BusinessError):
            validate_connection_host({"bootstrap_servers": "192.168.1.1:9092"})
        # 全公网放行
        validate_connection_host({"bootstrap_servers": "broker-b:9092"})
    finally:
        patcher.stop()


def test_validate_connection_host_empty_cfg_passes() -> None:
    """无 host/bootstrap_servers 的配置直接放行（不阻断）。"""
    validate_connection_host({})


# ---------- registry 构建入口校验 ----------


def test_registry_build_from_cfg_rejects_private() -> None:
    """探活/枚举：严格模式拒绝内网目标。"""
    from app.services.collector.connectors import registry

    patcher = _patch_resolve({"db.internal": ["10.0.0.5"]})
    try:
        with pytest.raises(BusinessError) as exc:
            registry.build_from_cfg("mysql", {"host": "db.internal", "user": "u", "password": "p"})
        assert exc.value.error_code == "SSRF_TARGET_FORBIDDEN"
    finally:
        patcher.stop()


def test_registry_build_allow_private() -> None:
    """已落库源采集：build(..., allow_private=True) 放行内网并构建连接器。"""
    from app.services.collector.connectors import registry

    patcher = _patch_resolve({"db.internal": ["10.0.0.5"]})
    try:
        encrypted = SecretManager.encrypt(
            {"host": "db.internal", "user": "u", "password": "p"}
        )
        collector = registry.build("mysql", encrypted, allow_private=True)
        assert collector is not None
    finally:
        patcher.stop()


def test_registry_build_rejects_loopback_by_default() -> None:
    """build 默认严格：回环仍拒绝（连已存源也不该连本机）。"""
    from app.services.collector.connectors import registry

    patcher = _patch_resolve({"127.0.0.1": ["127.0.0.1"]})
    try:
        encrypted = SecretManager.encrypt(
            {"host": "127.0.0.1", "user": "u", "password": "p"}
        )
        with pytest.raises(BusinessError):
            registry.build("mysql", encrypted, allow_private=True)
    finally:
        patcher.stop()


# ---------- 探活限流 ----------


async def test_probe_rate_allows_within_window() -> None:
    with patch("app.db.redis.get_redis") as mock_get:
        redis = MagicMock()
        pipe = MagicMock()
        pipe.incr.return_value = pipe
        pipe.expire.return_value = pipe
        pipe.execute = AsyncMock(return_value=(1, 1))
        redis.pipeline.return_value = pipe
        mock_get.return_value = redis
        await check_probe_rate("user:1")  # 不应抛异常


async def test_probe_rate_blocks_over_limit() -> None:
    with patch("app.db.redis.get_redis") as mock_get:
        redis = MagicMock()
        pipe = MagicMock()
        pipe.incr.return_value = pipe
        pipe.expire.return_value = pipe
        pipe.execute = AsyncMock(return_value=(16, 1))
        redis.pipeline.return_value = pipe
        mock_get.return_value = redis
        with pytest.raises(BusinessError) as exc:
            await check_probe_rate("user:1", max_probes=15)
        assert exc.value.error_code == "PROBE_RATE_LIMITED"


async def test_probe_rate_redis_failure_memory_fallback() -> None:
    """Redis 不可用时降级内存计数（best-effort，不阻断主流程）。"""
    from app.core import probe_throttle

    with patch("app.db.redis.get_redis", side_effect=RuntimeError("no redis")):
        probe_throttle._memory.clear()
        for _ in range(15):
            await check_probe_rate("user:2")  # 15 次内放行
        with pytest.raises(BusinessError):
            await check_probe_rate("user:2")  # 第 16 次超限
        # 内存窗口过期后重置
        probe_throttle._memory["user:2"] = (0, 0)  # 模拟过期
        await check_probe_rate("user:2")
