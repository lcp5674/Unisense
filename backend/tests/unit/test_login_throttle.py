"""登录限流（app/core/login_throttle.py）单测。

覆盖：组合桶（username+IP）与 IP 级独立桶（S5 审查修复：防换账号轰炸）。
存储走 Redis 降级内存路径（无 Redis 环境），验证计数/锁定/重置语义。
"""

from __future__ import annotations

import pytest

from app.core.login_throttle import (
    MAX_IP_FAILURES,
    is_ip_blocked,
    is_login_blocked,
    record_ip_failure,
    record_login_failure,
    reset_ip_failures,
    reset_login_failures,
)


@pytest.fixture(autouse=True)
def _clean_memory():
    from app.core import login_throttle as lt

    lt._memory.clear()
    yield
    lt._memory.clear()


async def test_combined_bucket_blocks_after_limit() -> None:
    key = "admin:1.2.3.4"
    for _ in range(10):
        assert await is_login_blocked(key) is False
        await record_login_failure(key)
    assert await is_login_blocked(key) is True


async def test_ip_bucket_independent_of_username() -> None:
    """S5：同一 IP 换 username 轰炸时，IP 级桶独立累计并最终锁定。"""
    ip = "1.2.3.4"
    for i in range(MAX_IP_FAILURES):
        assert await is_ip_blocked(ip) is False
        await record_ip_failure(ip)
        # 组合桶按不同 username 不累计（换账号轰炸）
        assert await is_login_blocked(f"user{i}:{ip}") is False
    assert await is_ip_blocked(ip) is True


async def test_ip_bucket_does_not_lock_single_account_prematurely() -> None:
    """S5：IP 桶上限（20）宽于组合桶（10）——单账号爆破 10 次先锁组合桶，
    但 IP 桶尚未满，合法用户换 IP 不受影响。"""
    ip = "9.9.9.9"
    for _ in range(10):
        await record_ip_failure(ip)
    # 10 次失败后 IP 桶未满（上限 20）
    assert await is_ip_blocked(ip) is False
    # 组合桶 10 次已锁
    assert await is_login_blocked("admin:9.9.9.9") is False  # 组合桶未记录，故未锁
    for _ in range(10):
        await record_login_failure("admin:9.9.9.9")
    assert await is_login_blocked("admin:9.9.9.9") is True


async def test_reset_ip_failures_on_success() -> None:
    ip = "5.6.7.8"
    for _ in range(5):
        await record_ip_failure(ip)
    await reset_ip_failures(ip)
    assert await is_ip_blocked(ip) is False
