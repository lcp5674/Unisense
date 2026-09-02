"""查询引擎（OLAP/MySQL 降级）DB 配置服务（方案 A：前端可配置化）。

配置优先级（逐段 DB 优先、env 兜底，仿 ``llm_config`` 范式）：
- OLAP 段：query_engine_config 行（enabled=true 且 host/olap_url 非空） >
  env（UNISENSE_OLAP_URL） > 未配置；
- MySQL 降级段：行（enabled=true 且 url 密文非空） >
  env（UNISENSE_MYSQL_FALLBACK_URL） > 未配置。

密钥（doris_password / mysql_fallback_url 完整连接串）经 ``SecretManager`` Fernet
加密落库，读取回显一律脱敏；``get_effective`` 返回进程内明文配置供 consume
执行器按指纹热重建（进程级 30s TTL 缓存 + 保存后立即失效，跨 worker 最长 30s
生效——配置变更低频，无需 Redis 广播）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.secrets import SecretManager
from app.models.query_engine_config import QueryEngineConfig
from app.services.query_engine.schemas import (
    QueryEngineConfigPayload,
    QueryEngineTestResult,
)

logger = get_logger("unisense.query_engine.config")

#: 进程内生效配置缓存 TTL（秒）：配置变更低频，30s 内各 worker 自然收敛。
_EFFECTIVE_TTL = 30.0

#: 连通性测试超时（秒）
_TCP_TIMEOUT = 2.0
_HTTP_TIMEOUT = 5.0

#: 进程内生效配置缓存（{at, value}；value 为明文 dict）
_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _derive_doris_from_url(url: str) -> tuple[str, int, str]:
    """从 OLAP 基础 URL 派生 Doris (host, port, database)。

    与 config.py ``_derive_doris_from_olap_url`` 同语义：``http://fe:8030/unisense``
    → (fe, 8030, unisense)。URL 不合法/无 host 时返回空 host（由调用方视为未配置）。
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or 8030
        db = parsed.path.strip("/")
        return host, port, db
    except ValueError:
        return "", 8030, ""


def _mask_secret(value: str) -> str:
    """把含密码的 URL 掩码为 ``scheme://user:***@host/...``（日志/提示用）。"""
    if not value:
        return ""
    try:
        parsed = urlparse(value)
        if parsed.password is not None:
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            user = f"{parsed.username or ''}:***@"
            netloc = f"{user}{host}{port}"
            return parsed._replace(netloc=netloc).geturl()
    except ValueError:
        pass
    return value


def _invalidate_cache() -> None:
    """保存/删除后清空进程内生效配置缓存（本 worker 立即生效）。"""
    _cache["at"] = 0.0
    _cache["value"] = None


async def _decrypt_field(token: str) -> str:
    """解密 Fernet 令牌为字符串；空令牌/解密失败返回空串（不抛异常）。"""
    if not token:
        return ""
    try:
        data = SecretManager.decrypt(token)
        if isinstance(data, dict):
            return str(data.get("password") or data.get("url") or "")
        return str(data)
    except Exception as exc:  # noqa: BLE001 - 解密失败不阻断，按未配置处理并告警
        logger.error("query_engine_decrypt_failed: %s", exc)
        return ""


class QueryEngineConfigService:
    """查询引擎配置读写（单行 upsert）+ 生效解析 + 连通性测试。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ---- 读取 ----

    async def get_row(self) -> QueryEngineConfig | None:
        """读取单行配置（未软删除，取最新一条；正常场景仅一行）。"""
        res = await self._db.execute(
            select(QueryEngineConfig)
            .where(QueryEngineConfig.deleted_at.is_(None))
            .order_by(QueryEngineConfig.id.desc())
            .limit(1)
        )
        return res.scalar_one_or_none()

    async def get_effective(self) -> dict[str, Any]:
        """获取生效配置（明文，进程内）。DB 段优先、env 兜底、none 三态。

        返回结构（供 consume 执行器构建/重建）：
        source: db | env | none；olap_configured / mysql_fallback_configured；
        olap_url / doris_host / doris_port / doris_database / doris_user /
        doris_password（明文）/ mysql_fallback_url（明文）；updated_by / updated_at。
        """
        now = time.monotonic()
        if _cache["value"] is not None and now - _cache["at"] < _EFFECTIVE_TTL:
            return _cache["value"]
        value = await self._compute_effective()
        _cache["at"] = time.monotonic()
        _cache["value"] = value
        return value

    async def _compute_effective(self) -> dict[str, Any]:
        """计算生效配置（进程内锁防并发重复查库；DB 行逐段解析）。"""
        row = await self.get_row()
        db_enabled = row is not None and row.enabled
        # OLAP 段
        olap_url = doris_host = doris_database = doris_user = doris_password = ""
        doris_port = 8030
        db_olap_configured = False
        db_updated_by: int | None = None
        db_updated_at: str | None = None
        if db_enabled:
            db_updated_by = row.updated_by
            db_updated_at = str(row.updated_at) if row.updated_at else None
            if row.doris_host or row.olap_url:
                db_olap_configured = True
                olap_url = row.olap_url or ""
                doris_host = row.doris_host or ""
                if not doris_host and olap_url:
                    doris_host, _, _ = _derive_doris_from_url(olap_url)
                doris_port = row.doris_port or 8030
                doris_database = row.doris_database or ""
                doris_user = row.doris_user or ""
                doris_password = await _decrypt_field(row.doris_password_enc)
        if not db_olap_configured and (
            settings.olap_url or settings.doris_host not in ("", "localhost")
        ):
            # env 兜底（settings 已由 config.py 从 olap_url 派生 doris_host/port/db）
            olap_url = settings.olap_url or ""
            doris_host = settings.doris_host or ""
            doris_port = settings.doris_port
            doris_database = settings.doris_database or ""
            doris_user = getattr(settings, "doris_user", "") or ""
        # MySQL 降级段
        mysql_fallback_url = ""
        db_mysql_configured = False
        if db_enabled and row.mysql_fallback_url_enc:
            url = await _decrypt_field(row.mysql_fallback_url_enc)
            if url:
                db_mysql_configured = True
                mysql_fallback_url = url
        if not db_mysql_configured:
            mysql_fallback_url = settings.mysql_fallback_url or ""
        if db_enabled and (db_olap_configured or db_mysql_configured):
            source = "db"
        elif settings.olap_url or settings.mysql_fallback_url:
            source = "env"
        else:
            source = "none"
        return {
            "source": source,
            "olap_url": olap_url,
            "doris_host": doris_host,
            "doris_port": doris_port,
            "doris_database": doris_database,
            "doris_user": doris_user,
            "doris_password": doris_password,
            "mysql_fallback_url": mysql_fallback_url,
            "olap_configured": bool(doris_host),
            "mysql_fallback_configured": bool(mysql_fallback_url),
            "updated_by": db_updated_by if db_enabled else None,
            "updated_at": db_updated_at if db_enabled else None,
        }

    # ---- 写入 ----

    async def save(
        self, payload: QueryEngineConfigPayload, updated_by: int
    ) -> QueryEngineConfig:
        """整行 upsert；密码/URL 留空且已有行时保持原值。"""
        row = await self.get_row()
        olap_url = payload.olap_url.strip()
        doris_host = payload.doris_host.strip()
        doris_port = payload.doris_port
        doris_database = payload.doris_database.strip()
        doris_user = payload.doris_user.strip()
        if not doris_host and olap_url:
            host, port, db = _derive_doris_from_url(olap_url)
            if host:
                doris_host = host
                doris_port = port
                if not doris_database:
                    doris_database = db
        if row is None:
            row = QueryEngineConfig(
                olap_url=olap_url,
                doris_host=doris_host,
                doris_port=doris_port,
                doris_database=doris_database,
                doris_user=doris_user,
                doris_password_enc="",
                mysql_fallback_url_enc="",
                enabled=payload.enabled,
                updated_by=updated_by,
            )
            self._db.add(row)
        else:
            row.olap_url = olap_url
            row.doris_host = doris_host
            row.doris_port = doris_port
            row.doris_database = doris_database
            row.doris_user = doris_user
            row.enabled = payload.enabled
            row.updated_by = updated_by
        # 密钥：留空且已有行 → 保持原值；非空 → 加密覆盖
        if payload.doris_password.strip():
            row.doris_password_enc = SecretManager.encrypt(
                {"password": payload.doris_password.strip()}
            )
        if payload.mysql_fallback_url.strip():
            row.mysql_fallback_url_enc = SecretManager.encrypt(
                {"url": payload.mysql_fallback_url.strip()}
            )
        await self._db.flush()
        _invalidate_cache()
        return row

    # ---- 连通性测试 ----

    async def test_connection(
        self, engine: str, payload: QueryEngineConfigPayload | None
    ) -> QueryEngineTestResult:
        """测试引擎连通性：olap=TCP 探活(+可选 basic auth)、mysql=真实 SELECT 1。

        payload 为空时使用当前生效配置；否则用载荷临时测试（不落库）。
        """
        effective = await self.get_effective()
        if engine == "mysql":
            url = payload.mysql_fallback_url.strip() if payload else effective["mysql_fallback_url"]
            return await self._test_mysql(url)
        return await self._test_olap(payload, effective)

    async def _test_olap(
        self, payload: QueryEngineConfigPayload | None, effective: dict[str, Any]
    ) -> QueryEngineTestResult:
        """OLAP 连通性测试：TCP 探活 host:port + basic auth 时 HTTP /api/bootstrap。"""
        if payload is not None:
            host = payload.doris_host.strip()
            port = payload.doris_port
            if not host and payload.olap_url.strip():
                host, port, _ = _derive_doris_from_url(payload.olap_url.strip())
            user = payload.doris_user.strip()
            password = payload.doris_password.strip()
        else:
            host = effective["doris_host"]
            port = effective["doris_port"]
            user = effective["doris_user"]
            password = effective["doris_password"]
        if not host:
            return QueryEngineTestResult(
                ok=False, engine="olap", error="未配置 OLAP 主机（doris_host / olap_url）"
            )
        # 1) TCP 探活
        start = time.monotonic()
        alive, tcp_err = await _tcp_probe(host, port)
        if not alive:
            latency = int((time.monotonic() - start) * 1000)
            return QueryEngineTestResult(
                ok=False,
                engine="olap",
                latency_ms=latency,
                error=f"无法连接 Doris FE {host}:{port}（{tcp_err}）",
                detail={"host": host, "port": port},
            )
        tcp_ms = int((time.monotonic() - start) * 1000)
        # 2) 配置了用户名时做 HTTP basic auth 鉴权校验（/api/bootstrap）
        if user:
            auth = await _doris_auth_probe(host, port, user, password)
            if not auth["ok"]:
                return QueryEngineTestResult(
                    ok=False,
                    engine="olap",
                    latency_ms=tcp_ms + int(auth.get("latency_ms", 0)),
                    error=auth["error"],
                    detail={"host": host, "port": port, "user": user},
                )
            return QueryEngineTestResult(
                ok=True,
                engine="olap",
                latency_ms=tcp_ms + int(auth.get("latency_ms", 0)),
                detail={"host": host, "port": port, "user": user, "auth": "basic"},
            )
        logger.info("query_engine_olap_probe_ok: %s:%s latency=%dms", host, port, tcp_ms)
        return QueryEngineTestResult(
            ok=True,
            engine="olap",
            latency_ms=tcp_ms,
            detail={"host": host, "port": port, "auth": "none"},
        )

    async def _test_mysql(self, url: str) -> QueryEngineTestResult:
        """MySQL 降级引擎测试：真实建立连接执行 SELECT 1（最高保真）。"""
        if not url:
            return QueryEngineTestResult(
                ok=False, engine="mysql", error="未配置 MySQL 降级引擎 URL"
            )
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        start = time.monotonic()
        engine = create_async_engine(url, pool_pre_ping=True)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - 测试失败返回原因
            latency = int((time.monotonic() - start) * 1000)
            masked = _mask_secret(url)
            logger.warning(
                "query_engine_mysql_probe_failed: url=%s error=%s", masked, exc
            )
            return QueryEngineTestResult(
                ok=False,
                engine="mysql",
                latency_ms=latency,
                error=f"{type(exc).__name__}: {exc}",
                detail={"url": masked},
            )
        finally:
            await engine.dispose()
        latency = int((time.monotonic() - start) * 1000)
        logger.info("query_engine_mysql_probe_ok: latency=%dms", latency)
        return QueryEngineTestResult(
            ok=True, engine="mysql", latency_ms=latency, detail={"url": _mask_secret(url)}
        )


async def _tcp_probe(host: str, port: int) -> tuple[bool, str]:
    """TCP 连通性探活（复用 /ready 同款语义）。"""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=_TCP_TIMEOUT
        )
        writer.close()
        await writer.wait_closed()
        return True, ""
    except (TimeoutError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def _doris_auth_probe(
    host: str, port: int, user: str, password: str
) -> dict[str, Any]:
    """Doris basic auth 鉴权探活：GET /api/bootstrap（Doris FE HTTP 健康端点）。"""
    url = f"http://{host}:{port}/api/bootstrap"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_HTTP_TIMEOUT),
            auth=(user, password),
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        latency = int((time.monotonic() - start) * 1000)
        return {
            "ok": False,
            "latency_ms": latency,
            "error": f"{type(exc).__name__}: {exc}（请求 {url}）",
        }
    latency = int((time.monotonic() - start) * 1000)
    if resp.status_code == 200:
        return {"ok": True, "latency_ms": latency, "status_code": 200}
    return {
        "ok": False,
        "latency_ms": latency,
        "error": f"Doris 鉴权失败（HTTP {resp.status_code}，请求 {url}）: {resp.text[:200]}",
    }
