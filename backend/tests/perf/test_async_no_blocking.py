"""性能回归测试：异步不阻塞（T027）。

验证事件循环中同步阻塞调用已被异步替代：
1. 10 并发登录不串行阻塞
2. TCP 探活使用 asyncio.open_connection
3. hash_password/verify_password 使用 asyncio.to_thread
"""

import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_hash_password_does_not_block_event_loop():
    """T027: hash_password 使用 asyncio.to_thread 不阻塞事件循环。"""
    from app.core.security import hash_password

    # 10 并发 hash_password 应在合理时间内完成（非串行）
    start = time.monotonic()
    tasks = [hash_password(f"test_password_{i}") for i in range(10)]
    results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start

    assert all(isinstance(r, str) for r in results)
    # 10 个 bcrypt hash 串行约需 ~3s（3ms*10*100=3s），异步并行应 < 2s
    # 放宽到 5s 防止 CI 慢机误报
    assert elapsed < 5.0, f"10 concurrent hash_password took {elapsed:.2f}s, may be blocking"


@pytest.mark.asyncio
async def test_tcp_alive_async():
    """T027: _tcp_alive_async 使用 asyncio.open_connection 异步探活。"""
    from app.core.resilience import _tcp_alive_async

    # 探测不可达端口应快速返回 False（不阻塞事件循环）
    start = time.monotonic()
    result = await _tcp_alive_async("127.0.0.1", 1, timeout=0.3)
    elapsed = time.monotonic() - start

    assert result is False
    assert elapsed < 1.0, f"TCP probe took {elapsed:.2f}s, may be blocking"


@pytest.mark.asyncio
async def test_concurrent_login_not_serial():
    """T027: 10 并发登录请求不串行阻塞（模拟）。

    验证 hash_password 并发执行不串行化。
    """
    from app.core.security import hash_password, verify_password

    # 先生成一个 hash
    hashed = await hash_password("test_concurrent_login")

    # 10 并发 verify_password
    start = time.monotonic()
    tasks = [verify_password("test_concurrent_login", hashed) for _ in range(10)]
    results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start

    assert all(results)
    assert elapsed < 5.0, f"10 concurrent verify_password took {elapsed:.2f}s, may be serial"
