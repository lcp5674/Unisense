"""OLAP 执行引擎：通过 Apache Doris HTTP API 执行 SQL（对齐 TD §12.6 / FR-5）。

核心能力：
1. SQL 执行：HTTP POST to Doris FE (8030) 提交查询
2. 结果解析：解析 Doris JSON 结果集为结构化 OLAPResult
3. 连接池：httpx.AsyncClient 复用连接 (max_connections=20)
4. 超时控制：查询超时由参数控制
5. 熔断保护：CircuitBreaker 包裹，连续失败后降级
6. SQL 结果缓存：Redis 缓存，5 分钟 TTL
7. 异步并发执行
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.resilience import olap_breaker

logger = get_logger(__name__)

# 缓存 TTL：5 分钟
_CACHE_TTL_SECONDS = 300
_CACHE_PREFIX = "olap:cache:"


@dataclass
class OLAPResult:
    """OLAP 查询结果。"""

    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    elapsed_ms: float = 0.0
    from_cache: bool = False


class OLAPExecutor:
    """Doris HTTP API 执行器。

    通过 Doris FE 的 HTTP API (port 8030) 提交 SQL 查询，
    解析 JSON 结果集返回结构化结果。
    连接池复用 + 超时控制 + 熔断保护 + Redis 结果缓存。
    """

    def __init__(
        self,
        doris_host: str | None = None,
        doris_port: int | None = None,
        doris_database: str | None = None,
        timeout: float = 30.0,
        redis: Any | None = None,
    ) -> None:
        self._host = doris_host or settings.doris_host
        self._port = doris_port or settings.doris_port
        self._database = doris_database or settings.doris_database
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        # 使用全局共享熔断器实例，确保跨请求状态一致
        self._breaker = olap_breaker
        self._redis = redis

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 httpx 异步客户端（连接池复用，max_connections=20）。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=5.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    def _cache_key(self, sql: str, params: dict[str, Any] | None) -> str:
        """生成缓存键。"""
        raw = sql + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"{_CACHE_PREFIX}{digest}"

    async def _get_cache(self, key: str) -> OLAPResult | None:
        """从 Redis 读取缓存。"""
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
            if raw is None:
                return None
            data = json.loads(raw)
            return OLAPResult(
                rows=data.get("rows", []),
                total=data.get("total", 0),
                elapsed_ms=data.get("elapsed_ms", 0.0),
                from_cache=True,
            )
        except Exception:
            logger.warning("olap_cache_read_failed", key=key, exc_info=True)
            return None

    async def _set_cache(self, key: str, result: OLAPResult) -> None:
        """写入 Redis 缓存（5 分钟 TTL）。"""
        if self._redis is None:
            return
        try:
            payload = json.dumps(
                {
                    "rows": result.rows,
                    "total": result.total,
                    "elapsed_ms": result.elapsed_ms,
                },
                ensure_ascii=False,
                default=str,
            )
            await self._redis.set(key, payload, ex=_CACHE_TTL_SECONDS)
        except Exception:
            logger.warning("olap_cache_write_failed", key=key, exc_info=True)

    async def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> OLAPResult:
        """执行 SQL 查询并返回结果（带 Redis 缓存）。

        Args:
            sql: SQL 查询语句。
            params: 参数化查询参数。
            timeout: 查询超时（秒），None 使用默认值。

        Returns:
            OLAPResult 包含行数据、总数和耗时。

        Raises:
            BusinessError: 熔断器打开时抛出 DEPENDENCY_DEGRADED_ENGINE。
        """
        # 检查缓存
        cache_key = self._cache_key(sql, params)
        cached = await self._get_cache(cache_key)
        if cached is not None:
            logger.info("olap_cache_hit", cache_key=cache_key)
            return cached

        # 熔断检查
        if not self._breaker.allow():
            raise _make_degraded_error("OLAP 熔断器已打开，请稍后重试")

        start = time.monotonic()
        try:
            result = await self._do_execute(sql, params, timeout)
            self._breaker.record_success()
            elapsed = (time.monotonic() - start) * 1000
            result.elapsed_ms = round(elapsed, 2)
            # 写入缓存
            await self._set_cache(cache_key, result)
            return result
        except Exception as exc:
            self._breaker.record_failure()
            elapsed = (time.monotonic() - start) * 1000
            logger.warning(
                "olap_execute_failed",
                sql_preview=sql[:200],
                elapsed_ms=round(elapsed, 2),
                error=str(exc),
                exc_info=True,
            )
            raise

    async def _do_execute(
        self,
        sql: str,
        params: dict[str, Any] | None,
        timeout: float | None,
    ) -> OLAPResult:
        """实际执行 SQL 查询。"""
        client = await self._get_client()

        # Doris HTTP API: POST http://fe_host:8030/api/{database}/{table}
        # 简单查询使用 /api/query endpoint
        url = f"http://{self._host}:{self._port}/api/query"

        # 构建请求参数
        request_params: dict[str, Any] = {
            "sql": sql,
        }
        if params:
            # Doris HTTP API 支持 variables 参数传递命名参数
            request_params["variables"] = json.dumps(params)

        request_timeout = timeout or self._timeout

        response = await client.post(
            url,
            params=request_params,
            headers={"Content-Type": "application/json"},
            timeout=request_timeout,
        )

        if response.status_code != 200:
            logger.error(
                "doris_http_error",
                status_code=response.status_code,
                body=response.text[:500],
            )
            raise _make_degraded_error(
                f"Doris 返回 HTTP {response.status_code}"
            )

        return self._parse_response(response.text)

    def _parse_response(self, body: str) -> OLAPResult:
        """解析 Doris HTTP API 响应。

        Doris 返回 JSON 格式：
        {
            "code": 0,
            "message": "",
            "data": {
                "type": "select",
                "columns": [...],
                "rows": [...]
            }
        }
        """
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            logger.error("doris_response_parse_error", body_preview=body[:500])
            raise _make_degraded_error("Doris 响应解析失败") from None

        # Doris 可能返回不同格式的响应
        code = data.get("code", -1)
        if code != 0:
            message = data.get("message", "未知错误")
            raise _make_degraded_error(f"Doris 查询错误: {message}")

        result_data = data.get("data", data)

        # 解析列和行
        columns = result_data.get("columns", [])
        raw_rows = result_data.get("rows", [])

        if not columns and isinstance(raw_rows, list):
            # 行已经是字典格式
            rows = raw_rows if raw_rows and isinstance(raw_rows[0], dict) else []
            if not rows and raw_rows:
                # 列名 + 行值分开格式
                col_names = [
                    c.get("name", f"col_{i}") for i, c in enumerate(columns)
                ]
                rows = [dict(zip(col_names, row, strict=False)) for row in raw_rows]
        elif columns and raw_rows:
            col_names = [
                c.get("name", f"col_{i}") if isinstance(c, dict) else str(c)
                for i, c in enumerate(columns)
            ]
            rows = [dict(zip(col_names, row, strict=False)) for row in raw_rows]
        else:
            rows = []

        return OLAPResult(
            rows=rows,
            total=len(rows),
        )

    async def close(self) -> None:
        """关闭 HTTP 客户端连接池。"""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


def _make_degraded_error(message: str) -> Exception:
    """创建降级错误。"""
    from app.core.error_codes import ErrorCode
    from app.core.exceptions import BusinessError

    return BusinessError(
        message,
        error_code=ErrorCode.DEPENDENCY_DEGRADED_ENGINE,
        ctx={"retry_after": 30},
    )
