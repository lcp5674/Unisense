"""查询引擎配置 API 测试（GET /query-engine/config + 密钥回显 /config/secrets）。

覆盖：
- GET /config：读视图脱敏（不含明文密钥，仅 has_* 标记）——任意登录可读；
- GET /config/secrets：platform_admin 可取明文密钥（回填/查看用，写审计）；
- GET /config/secrets：viewer 等非管理角色 403（读视图与密钥回显隔离）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from app.api import deps
from app.core.secrets import SecretManager
from app.main import app
from app.services.query_engine.config_service import _invalidate_cache


@pytest.fixture(autouse=True)
def _clear_effective_cache():
    """每个用例前清空进程级生效配置缓存（避免跨用例/跨测试污染）。"""
    _invalidate_cache()
    yield
    _invalidate_cache()


def _make_session(row: object | None) -> MagicMock:
    """构造 mock 会话：execute 返回配置了 scalar_one_or_none=row 的 Result。"""
    session = MagicMock()
    session.flush = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session.execute = AsyncMock(return_value=result)
    return session


def _fake_row() -> MagicMock:
    """构造 query_engine_config 行 mock（含 Fernet 加密的密码/降级连接串）。"""
    row = MagicMock()
    row.id = 1
    row.enabled = True
    row.olap_url = ""
    row.doris_host = "doris-prod"
    row.doris_port = 9030
    row.doris_database = "unisense"
    row.doris_user = "reader"
    row.doris_password_enc = SecretManager.encrypt({"password": "s3cret"})
    row.mysql_fallback_url_enc = SecretManager.encrypt(
        {"url": "mysql+aiomysql://e2e:e2e@mysql:3306/e2e_biz"}
    )
    row.updated_by = 1
    row.updated_at = datetime.now(UTC)
    return row


def _override(session: MagicMock, role: str) -> None:
    async def fake_db():
        yield session

    app.dependency_overrides[deps.get_db_session] = fake_db
    app.dependency_overrides[deps.get_current_user] = lambda: MagicMock(
        id=1,
        role=role,
        roles_all=lambda: [role],
        has_role=lambda r: r == role,
    )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_get_config_masked_for_any_role() -> None:
    """读视图（任意登录可读）一律脱敏：仅 has_* 标记，不含明文密钥/连接串。"""
    session = _make_session(_fake_row())
    _override(session, "reviewer")
    async with _client() as c:
        resp = await c.get("/api/v1/query-engine/config")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["row"]["doris_user"] == "reader"
    assert data["row"]["has_doris_password"] is True
    assert data["row"]["has_mysql_fallback"] is True
    # 脱敏：不含明文
    body = resp.text
    assert "s3cret" not in body
    assert "e2e@mysql" not in body
    assert data["can_edit"] is False  # reviewer 不可编辑


async def test_get_secrets_plaintext_for_platform_admin() -> None:
    """platform_admin 经 /config/secrets 取明文密钥（编辑回填/查看，写审计）。"""
    session = _make_session(_fake_row())
    _override(session, "platform_admin")
    async with _client() as c:
        resp = await c.get("/api/v1/query-engine/config/secrets")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["source"] == "db"
    assert data["doris_user"] == "reader"
    assert data["doris_password"] == "s3cret"
    assert data["mysql_fallback_url"] == "mysql+aiomysql://e2e:e2e@mysql:3306/e2e_biz"
    assert data["has_doris_password"] is True
    assert data["has_mysql_fallback"] is True
    # 访问写入审计（session.add/commit 被调用）
    assert session.add.called is True


async def test_get_secrets_forbidden_for_non_admin() -> None:
    """非平台管理员（viewer）调 /config/secrets → 403（读视图与密钥回显隔离）。"""
    session = _make_session(_fake_row())
    _override(session, "viewer")
    async with _client() as c:
        resp = await c.get("/api/v1/query-engine/config/secrets")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
