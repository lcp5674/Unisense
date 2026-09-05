"""dp_sync_schema 提供者单测（F1：通道 B 的 DataSource 查询并发安全路由）。

覆盖：``_hive_sources`` 在注入 ``session_factory`` 时用**独立的短生命周期只读
session** 查询（as_map 有界并发下多路通道 B 不再并发 execute 扫描主链路共享的
AsyncSession）；无注入工厂时回退 ``db``（同步/测试形态）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.lineage.dp_sync_schema import DpSchemaProvider


class _FakeResult:
    """最小 scalars().all() 假结果（返回预置行）。"""

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[object]:
        return self._rows


def _fake_src(source_id: str = "hive-1") -> SimpleNamespace:
    return SimpleNamespace(
        source_id=source_id,
        source_type="hive",
        connection_config={"host": "hive.internal"},
    )


async def test_hive_sources_uses_independent_session() -> None:
    """F1：注入 session_factory 时，DataSource 查询走独立 session 而非共享 db。"""
    entered = {"n": 0}
    db = MagicMock()
    db.execute = AsyncMock()

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            entered["n"] += 1
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def execute(self, stmt: object) -> _FakeResult:
            # 断言收到的是 DataSource 查询（select 语句）
            return _FakeResult([_fake_src()])

    provider = DpSchemaProvider(
        db,
        dp_collector=None,
        session_factory=lambda: _FakeSession(),
    )
    srcs = await provider._hive_sources()
    assert entered["n"] == 1  # 独立 session 只开一次（轮内缓存）
    assert len(srcs) == 1 and srcs[0].source_id == "hive-1"
    db.execute.assert_not_awaited()  # 不再并发 execute 共享 db
    # 轮内缓存：第二次不重开 session
    await provider._hive_sources()
    assert entered["n"] == 1


async def test_hive_sources_falls_back_to_db_without_factory() -> None:
    """F1：无 session_factory 时回退 db（旧调用形态/测试兼容，无并发场景）。"""
    db = MagicMock()
    db.execute = AsyncMock(return_value=_FakeResult([_fake_src("hive-2")]))
    provider = DpSchemaProvider(db, dp_collector=None)
    srcs = await provider._hive_sources()
    assert len(srcs) == 1 and srcs[0].source_id == "hive-2"
    db.execute.assert_awaited_once()
