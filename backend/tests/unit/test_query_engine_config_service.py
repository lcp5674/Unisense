"""查询引擎配置服务单元测试（方案 A：DB 配置化热生效）。

聚焦纯逻辑：生效配置三态解析（db/env/none）、Fernet 加密存取（密码/URL 留空
保持原值）、保存后缓存失效。DB 以 MagicMock / 假行隔离（对齐 DEV_GUIDE §8b），
不连真实依赖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.secrets import SecretManager
from app.services.query_engine.config_service import (
    QueryEngineConfigService,
    _cache,
    _invalidate_cache,
    _mask_secret,
)
from app.services.query_engine.schemas import QueryEngineConfigPayload


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个用例前清空进程级生效配置缓存（避免跨用例污染）。"""
    _invalidate_cache()
    yield
    _invalidate_cache()


class _FakeRow:
    """最小 QueryEngineConfig 假行（仅承载 service 读取的字段）。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        doris_host: str = "doris",
        olap_url: str = "",
        doris_port: int = 8030,
        doris_database: str = "unisense",
        doris_user: str = "root",
        doris_password_enc: str = "",
        mysql_fallback_url_enc: str = "",
    ) -> None:
        self.id = 1
        self.enabled = enabled
        self.doris_host = doris_host
        self.olap_url = olap_url
        self.doris_port = doris_port
        self.doris_database = doris_database
        self.doris_user = doris_user
        self.doris_password_enc = doris_password_enc
        self.mysql_fallback_url_enc = mysql_fallback_url_enc
        self.updated_by = 1
        self.updated_at = datetime.now(UTC)


def _svc(row: _FakeRow | None = None) -> QueryEngineConfigService:
    db = MagicMock()
    db.flush = AsyncMock()
    svc = QueryEngineConfigService(db)
    svc.get_row = AsyncMock(return_value=row)
    return svc


def _enc_password(value: str) -> str:
    return SecretManager.encrypt({"password": value})


def _enc_url(value: str) -> str:
    return SecretManager.encrypt({"url": value})


# ---- 生效配置解析：db / env / none ----

async def test_effective_db_row_preferred(monkeypatch) -> None:
    """DB 行 enabled 且含 doris_host → source=db，密码/URL 解密为明文。"""
    row = _FakeRow(
        doris_host="doris-prod",
        doris_port=9030,
        doris_user="reader",
        doris_password_enc=_enc_password("s3cret"),
        mysql_fallback_url_enc=_enc_url("mysql+aiomysql://e2e:e2e@mysql:3306/e2e_biz"),
    )
    # env 也有配置（应被 DB 覆盖）
    monkeypatch.setattr("app.services.query_engine.config_service.settings.olap_url", "http://env-doris:8030")
    eff = await _svc(row).get_effective()
    assert eff["source"] == "db"
    assert eff["olap_configured"] is True
    assert eff["doris_host"] == "doris-prod"
    assert eff["doris_port"] == 9030
    assert eff["doris_user"] == "reader"
    assert eff["doris_password"] == "s3cret"
    assert eff["mysql_fallback_configured"] is True
    assert eff["mysql_fallback_url"] == "mysql+aiomysql://e2e:e2e@mysql:3306/e2e_biz"


async def test_effective_env_fallback_when_no_db(monkeypatch) -> None:
    """无 DB 行 → env 兜底（olap_url 派生 host/port/db）。"""
    monkeypatch.setattr(
        "app.services.query_engine.config_service.settings.olap_url",
        "http://doris-env:8030/unisense",
    )
    monkeypatch.setattr(
        "app.services.query_engine.config_service.settings.doris_host", "doris-env"
    )
    monkeypatch.setattr(
        "app.services.query_engine.config_service.settings.doris_port", 8030
    )
    monkeypatch.setattr(
        "app.services.query_engine.config_service.settings.doris_database", "unisense"
    )
    eff = await _svc(None).get_effective()
    assert eff["source"] == "env"
    assert eff["olap_configured"] is True
    assert eff["doris_host"] == "doris-env"
    assert eff["doris_password"] == ""


async def test_effective_none_when_no_config(monkeypatch) -> None:
    """无 DB 行且 env 未配置 → source=none，双引擎均未配置。"""
    monkeypatch.setattr("app.services.query_engine.config_service.settings.olap_url", "")
    monkeypatch.setattr(
        "app.services.query_engine.config_service.settings.mysql_fallback_url", ""
    )
    eff = await _svc(None).get_effective()
    assert eff["source"] == "none"
    assert eff["olap_configured"] is False
    assert eff["mysql_fallback_configured"] is False


# ---- 保存：upsert + 加密 + 留空保持 ----

async def test_save_creates_row_with_encrypted_secrets() -> None:
    """新建行：doris 密码 / fallback URL 加密落库（无明文）。"""
    svc = _svc(None)
    payload = QueryEngineConfigPayload(
        doris_host="doris-prod",
        doris_port=9030,
        doris_user="root",
        doris_password="p@ss",
        mysql_fallback_url="mysql+aiomysql://u:p@h:3306/db",
    )
    row = await svc.save(payload, updated_by=1)
    assert row.doris_host == "doris-prod"
    assert "p@ss" not in row.doris_password_enc  # 非明文
    assert "p@ss" not in row.mysql_fallback_url_enc
    assert SecretManager.decrypt(row.doris_password_enc)["password"] == "p@ss"
    assert SecretManager.decrypt(row.mysql_fallback_url_enc)["url"] == (
        "mysql+aiomysql://u:p@h:3306/db"
    )
    # 缓存已失效（后续 get_effective 重新计算）
    assert _cache["value"] is None


async def test_save_keeps_secrets_when_blank() -> None:
    """已有行且密码/URL 留空 → 保持原密文（不覆盖）。"""
    existing = _FakeRow(
        doris_host="doris-prod",
        doris_password_enc=_enc_password("old-pass"),
        mysql_fallback_url_enc=_enc_url("mysql+aiomysql://u:old@h:3306/db"),
    )
    svc = _svc(existing)
    payload = QueryEngineConfigPayload(
        doris_host="doris-new", doris_password="", mysql_fallback_url=""
    )
    row = await svc.save(payload, updated_by=2)
    assert row.doris_host == "doris-new"  # 非密钥字段更新
    assert SecretManager.decrypt(row.doris_password_enc)["password"] == "old-pass"  # 密钥保持
    assert SecretManager.decrypt(row.mysql_fallback_url_enc)["url"] == (
        "mysql+aiomysql://u:old@h:3306/db"
    )


async def test_save_derives_doris_from_olap_url() -> None:
    """仅填 olap_url → 派生 host/port/database 落库（无需手填 doris_host）。"""
    svc = _svc(None)
    payload = QueryEngineConfigPayload(
        olap_url="http://fe.internal:8030/warehouse",
        doris_password="",
        mysql_fallback_url="",
    )
    row = await svc.save(payload, updated_by=1)
    assert row.doris_host == "fe.internal"
    assert row.doris_port == 8030
    assert row.doris_database == "warehouse"


# ---- 工具 ----

def test_mask_secret_hides_password() -> None:
    assert "p@ss" not in _mask_secret("mysql+aiomysql://u:p@ss@h:3306/db")
    masked = _mask_secret("mysql+aiomysql://u:p@ss@h:3306/db")
    assert "u:***@h" in masked
