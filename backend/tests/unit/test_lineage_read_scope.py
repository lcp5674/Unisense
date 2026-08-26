"""血缘读路径域收敛测试（X-2）。

覆盖：
- ``_effective_read_domain``：platform_admin 可跨域，其余角色强制本域；
- ``_assert_node_read_access``：节点可解析且不在本域时拒绝，未知节点放行；
- ``export_lineage`` 域收敛：仅保留端点命中允许域（或两端均无法解析）的边。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.lineage import (
    _assert_node_read_access,
    _effective_read_domain,
)
from app.core.exceptions import AuthError
from app.services.lineage.service import LineageService


def _user(role: str, domain: str | None) -> SimpleNamespace:
    u = SimpleNamespace(domain=domain)
    u.has_role = lambda r: r == role
    return u


def test_effective_read_domain_platform_admin_passthrough() -> None:
    user = _user("platform_admin", None)
    assert _effective_read_domain(user, None) is None
    assert _effective_read_domain(user, "finance") == "finance"


def test_effective_read_domain_non_admin_forced_own_domain() -> None:
    user = _user("viewer", "finance")
    # 请求他域/不限域，一律收敛到本域
    assert _effective_read_domain(user, "marketing") == "finance"
    assert _effective_read_domain(user, None) == "finance"
    # domain_admin 同样收敛（防跨域导出/图谱窥探）
    da = _user("domain_admin", "finance")
    assert _effective_read_domain(da, "marketing") == "finance"


async def test_assert_node_read_access_own_domain_allowed() -> None:
    svc = MagicMock()
    svc.node_meta = AsyncMock(return_value=[SimpleNamespace(domain="finance")])
    # 不抛异常即通过
    await _assert_node_read_access(_user("viewer", "finance"), svc, "metric:gmv_day")


async def test_assert_node_read_access_cross_domain_rejected() -> None:
    svc = MagicMock()
    svc.node_meta = AsyncMock(return_value=[SimpleNamespace(domain="marketing")])
    with pytest.raises(AuthError):
        await _assert_node_read_access(_user("viewer", "finance"), svc, "metric:gmv_day")


async def test_assert_node_read_access_unknown_node_allowed() -> None:
    svc = MagicMock()
    svc.node_meta = AsyncMock(return_value=[SimpleNamespace(domain=None)])
    await _assert_node_read_access(_user("viewer", "finance"), svc, "external:x")


async def test_assert_node_read_access_platform_admin_allowed() -> None:
    svc = MagicMock()
    svc.node_meta = AsyncMock(return_value=[SimpleNamespace(domain="marketing")])
    await _assert_node_read_access(_user("platform_admin", None), svc, "metric:gmv_day")


def test_export_domain_filter_keeps_in_domain_and_unknown_edges() -> None:
    """export_lineage 域收敛：命中允许域或两端均无法解析的边保留，他域边剔除。"""
    svc = LineageService.__new__(LineageService)
    svc._repo = MagicMock()
    svc._repo.resolve_node_meta = AsyncMock(
        return_value={
            "metric:gmv_day": {"domain": "finance"},
            "table:dwd.orders": {"domain": "finance"},
            "metric:user_cnt": {"domain": "marketing"},
            "external:ext": {"domain": None},
        }
    )

    edges = [
        SimpleNamespace(  # 同域保留
            source_node="metric:gmv_day", target_node="table:dwd.orders"
        ),
        SimpleNamespace(  # 跨域（一端命中）保留
            source_node="metric:gmv_day", target_node="metric:user_cnt"
        ),
        SimpleNamespace(  # 跨域（一端命中）保留
            source_node="metric:user_cnt", target_node="metric:gmv_day"
        ),
        SimpleNamespace(  # 均无域保留
            source_node="external:ext", target_node="external:ext"
        ),
        SimpleNamespace(  # 他域剔除
            source_node="metric:user_cnt", target_node="metric:user_cnt"
        ),
    ]

    kept = pytest_sync_run(svc._filter_edges_by_domains(edges, {"finance"}))
    assert len(kept) == 4
    # 剔除的唯一一条是两端都在 marketing 域的边
    removed = [e for e in edges if e not in kept]
    assert len(removed) == 1
    assert removed[0].source_node == "metric:user_cnt"
    assert removed[0].target_node == "metric:user_cnt"


def pytest_sync_run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)
