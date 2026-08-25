"""用户偏好 API 单测（/api/v1/me/preferences）。

覆盖：列表空/有数据、upsert 新建/覆盖、软删除存在/幂等、并发唯一键冲突兜底。
用户身份由依赖覆盖为 id=1，所有断言围绕该用户展开（不存在跨用户读写）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy.exc import IntegrityError

from app.api import deps
from app.main import app
from app.models.consume import UserPreference


class PrefEnv:
    """可配置的偏好 API 测试环境（暴露 client 与 session mock）。"""

    def __init__(self, client: httpx.AsyncClient, session: MagicMock) -> None:
        self.client = client
        self.session = session

    def set_execute(
        self,
        scalar: object | None = None,
        rows: list[object] | None = None,
    ) -> None:
        """配置 execute 返回值：scalar（单行/None）或 rows（列表查询）。"""
        result = MagicMock()
        if rows is not None:
            result.scalars.return_value.all.return_value = rows
        else:
            result.scalar_one_or_none.return_value = scalar
        self.session.execute = AsyncMock(return_value=result)


def _pref(key: str = "ui", value: dict | None = None) -> UserPreference:
    return UserPreference(
        user_id=1,
        preference_key=key,
        preference_value=value if value is not None else {"sider_collapsed": True},
    )


@pytest.fixture
async def pref_env() -> AsyncIterator[PrefEnv]:
    """覆盖 DB 会话与当前用户依赖（user id=1）。"""

    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1, role="viewer", roles_all=lambda: ["viewer"], has_role=lambda r: r == "viewer"
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield PrefEnv(c, session)
    app.dependency_overrides.clear()


async def test_list_empty(pref_env: PrefEnv) -> None:
    pref_env.set_execute(rows=[])
    resp = await pref_env.client.get("/api/v1/me/preferences")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"items": [], "total": 0}


async def test_list_with_rows(pref_env: PrefEnv) -> None:
    pref_env.set_execute(rows=[_pref("default_domain", {"domain": "sales"}), _pref("ui")])
    resp = await pref_env.client.get("/api/v1/me/preferences")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 2
    assert [it["key"] for it in data["items"]] == ["default_domain", "ui"]
    assert data["items"][1]["value"] == {"sider_collapsed": True}


async def test_upsert_new(pref_env: PrefEnv) -> None:
    pref_env.set_execute(scalar=None)
    resp = await pref_env.client.put(
        "/api/v1/me/preferences/ui", json={"value": {"sider_collapsed": True}}
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["key"] == "ui"
    assert data["value"] == {"sider_collapsed": True}
    pref_env.session.add.assert_called_once()
    pref_env.session.commit.assert_awaited_once()


async def test_upsert_existing_overwrites(pref_env: PrefEnv) -> None:
    pref = _pref("ui", {"sider_collapsed": False})
    pref_env.set_execute(scalar=pref)
    resp = await pref_env.client.put(
        "/api/v1/me/preferences/ui", json={"value": {"sider_collapsed": True}}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["value"] == {"sider_collapsed": True}
    assert pref.preference_value == {"sider_collapsed": True}
    pref_env.session.add.assert_not_called()  # 覆盖既有行，不新增
    pref_env.session.commit.assert_awaited_once()


async def test_delete_existing_soft_deletes(pref_env: PrefEnv) -> None:
    pref = _pref("ui", {"sider_collapsed": True})
    pref_env.set_execute(scalar=pref)
    resp = await pref_env.client.delete("/api/v1/me/preferences/ui")
    assert resp.status_code == 200
    assert resp.json()["data"]["key"] == "ui"
    assert pref.deleted_at is not None  # 软删除留痕
    pref_env.session.commit.assert_awaited_once()


async def test_delete_missing_idempotent(pref_env: PrefEnv) -> None:
    pref_env.set_execute(scalar=None)
    resp = await pref_env.client.delete("/api/v1/me/preferences/ui")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"key": "ui", "value": None}
    pref_env.session.commit.assert_not_awaited()


async def test_upsert_concurrent_conflict_fallback(pref_env: PrefEnv) -> None:
    """并发首次写入：commit 抛唯一键冲突 → rollback → 回查既有行 → 更新。"""
    pref = _pref("ui", {"sider_collapsed": True})
    result_none = MagicMock()
    result_none.scalar_one_or_none.return_value = None
    result_row = MagicMock()
    result_row.scalar_one_or_none.return_value = pref
    # 流程：激活查询(None) → 软删除查询(None) → commit 冲突 → 回查激活行(row)
    pref_env.session.execute = AsyncMock(side_effect=[result_none, result_none, result_row])
    pref_env.session.commit = AsyncMock(side_effect=[IntegrityError("dup", None, None), None])

    resp = await pref_env.client.put(
        "/api/v1/me/preferences/ui", json={"value": {"sider_collapsed": True}}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["value"] == {"sider_collapsed": True}
    pref_env.session.rollback.assert_awaited_once()
    assert pref.preference_value == {"sider_collapsed": True}


async def test_upsert_revives_soft_deleted_row(pref_env: PrefEnv) -> None:
    """软删除行占用唯一键 (user_id, key)：恢复该行而非 INSERT（避免唯一约束冲突）。"""
    soft = _pref("ui", {"sider_collapsed": True})
    soft.deleted_at = datetime.now(UTC)
    result_none = MagicMock()
    result_none.scalar_one_or_none.return_value = None  # 首次：无激活行
    result_soft = MagicMock()
    result_soft.scalar_one_or_none.return_value = soft  # 二次：命中软删除行
    pref_env.session.execute = AsyncMock(side_effect=[result_none, result_soft])

    resp = await pref_env.client.put(
        "/api/v1/me/preferences/ui", json={"value": {"sider_collapsed": True}}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["value"] == {"sider_collapsed": True}
    assert soft.deleted_at is None  # 已恢复激活
    pref_env.session.add.assert_not_called()  # 恢复而非新增
    pref_env.session.commit.assert_awaited_once()
