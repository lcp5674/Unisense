"""MySQL 降级执行器单测（TD §12.6 降级引擎）。

覆盖：只读护栏（非 SELECT 拒绝）、未配置降级、值序列化（Decimal/date）、
熔断打开拒绝、成功执行返回 OLAPResult 兼容结构。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.error_codes import ErrorCode
from app.core.exceptions import BusinessError
from app.services.consume.mysql_executor import MysqlExecutor, _to_jsonable


def _executor(url: str = "mysql+aiomysql://u:p@h:3306/db") -> MysqlExecutor:
    ex = MysqlExecutor(url=url)
    # 熔断器隔离：单测不触碰全局真实熔断状态
    ex._breaker = MagicMock()
    ex._breaker.allow.return_value = True
    ex._breaker.record_success = MagicMock()
    ex._breaker.record_failure = MagicMock()
    return ex


async def test_execute_rejects_non_select() -> None:
    ex = _executor()
    with pytest.raises(BusinessError) as exc:
        await ex.execute("DELETE FROM t")
    assert exc.value.error_code == ErrorCode.DEPENDENCY_DEGRADED_ENGINE


async def test_execute_not_configured() -> None:
    ex = _executor(url="")
    with pytest.raises(BusinessError) as exc:
        await ex.execute("SELECT 1")
    assert exc.value.error_code == ErrorCode.DEPENDENCY_DEGRADED_ENGINE


async def test_execute_breaker_open() -> None:
    ex = _executor()
    ex._breaker.allow.return_value = False
    with pytest.raises(BusinessError) as exc:
        await ex.execute("SELECT 1")
    assert exc.value.error_code == ErrorCode.DEPENDENCY_DEGRADED_ENGINE


async def test_execute_success_serializes_values() -> None:
    ex = _executor()
    result = MagicMock()
    result.mappings.return_value.all.return_value = [
        {
            "gmv": Decimal("100.50"),
            "dt": date(2026, 8, 1),
            "created_at": datetime(2026, 8, 1, 10, 30, 0),
        }
    ]
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=result)
    conn_ctx = AsyncMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=conn)
    conn_ctx.__aexit__ = AsyncMock(return_value=False)
    engine = MagicMock()
    engine.connect.return_value = conn_ctx
    ex._get_engine = MagicMock(return_value=engine)

    out = await ex.execute("SELECT gmv, dt, created_at FROM t", {"m": "gmv"})

    assert out.total == 1
    row = out.rows[0]
    assert row["gmv"] == 100.5  # Decimal → float
    assert row["dt"] == "2026-08-01"  # date → ISO
    assert row["created_at"] == "2026-08-01T10:30:00"
    ex._breaker.record_success.assert_called_once()
    # 参数化透传
    conn.execute.assert_awaited_once()


def test_to_jsonable_leaf_types() -> None:
    assert _to_jsonable(Decimal("1.5")) == 1.5
    assert _to_jsonable(date(2026, 1, 1)) == "2026-01-01"
    assert _to_jsonable(b"abc") == "abc"
    assert _to_jsonable("plain") == "plain"
    assert _to_jsonable(3) == 3
    assert _to_jsonable(None) is None
