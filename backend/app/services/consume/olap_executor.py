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
import re
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

# SQLAlchemy 命名参数占位符（:name）
_PLACEHOLDER_RE = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")


class DorisSqlError(Exception):
    """Doris SQL 语义/语法错误（HTTP 4xx 或 code!=0 的查询错误）。

    R-3（第七轮韧性）：Doris 返回 4xx/查询错误是**用户 SQL 本身的问题**，不是引擎故障——
    不应触发「降级 MySQL 重跑」（会掩盖 SQL 问题且浪费）、不应污染共享熔断器（坏 SQL 会让
    olap_breaker 误熔断、连累正常查询降级）。此类错误如实上抛，由消费方呈现给用户。
    """

    def __init__(self, message: str, *, status_code: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body[:1000]


def _sql_literal(value: Any) -> str:
    """把 Python 值编码为 SQL 字面量（IN 子句列表展开用，单引号翻倍防注入）。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _to_doris_sql(sql: str, params: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """把 SQLAlchemy ``:name`` 占位符转换为 Doris HTTP variables 的 ``${name}``。

    Doris ``/api/query`` 的 ``variables`` 是**纯文本替换**（``${var}``），不识别
    SQLAlchemy 的 ``:name``——此前参数化查询直发 Doris 必然语法错误（OLAP 链路
    实际不可用）。本转换在发送边界落地：

    - 字符串标量 → SQL 中写 ``'${name}'`` 且 variables 值单引号翻倍：Doris 文本
      替换后得到带引号且注入安全的字面量（``'BJ'' OR 1=1'`` 在引号内）；
    - 数值/布尔标量 → ``${name}``，variables 原样（不加引号）；
    - ``None`` → ``NULL``；
    - 列表参数（``IN :key``）→ 原地展开为内联字面量 ``('a','b')``（Doris 对
      数组替换行为因版本而异，展开最稳），值经 ``_sql_literal`` 转义防注入；
    - 未知占位符（params 中不存在）保持原样，交由 Doris 报错暴露。

    Args:
        sql: 含 SQLAlchemy 占位符的 SQL。
        params: 参数映射。

    Returns:
        (Doris 兼容 SQL, Doris variables 参数映射)。
    """
    if not params:
        return sql, {}
    variables: dict[str, Any] = {}

    def _repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            return match.group(0)
        value = params[name]
        if isinstance(value, (list, tuple)):
            inner = ", ".join(_sql_literal(v) for v in value)
            return f"({inner})"
        if value is None:
            return "NULL"
        if isinstance(value, str):
            # Doris variables 纯文本替换不做转义：字符串必须带引号包裹，
            # 且值内单引号翻倍，杜绝文本替换注入。
            variables[name] = value.replace("'", "''")
            return f"'${{{name}}}'"
        variables[name] = value
        return f"${{{name}}}"

    doris_sql = _PLACEHOLDER_RE.sub(_repl, sql)
    return doris_sql, variables


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
        """从 Redis 读取缓存并校验反序列化结果（损坏即按未命中处理，防下游崩溃）。"""
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
            if raw is None:
                return None
            data = json.loads(raw)
            rows = data.get("rows")
            if not isinstance(rows, list):
                logger.warning("olap_cache_corrupt_rows", key=key)
                await self._redis.delete(key)
                return None
            total = data.get("total", 0)
            if not isinstance(total, int):
                try:
                    total = int(total)
                except (TypeError, ValueError):
                    total = len(rows)
            return OLAPResult(
                rows=rows,
                total=total,
                elapsed_ms=float(data.get("elapsed_ms") or 0.0),
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
            # R-3：SQL 语义/语法错误（DorisSqlError）是用户 SQL 问题，不记熔断失败——
            # 否则坏 SQL 会让共享熔断器误开路，连累后续正常查询全部降级 MySQL。
            if not isinstance(exc, DorisSqlError):
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

        # 发送前把 SQLAlchemy :name 占位符转换为 Doris ${name} variables 语法
        # （否则参数化查询直发 Doris 必然语法错误，OLAP 链路实际不可用）
        doris_sql, variables = _to_doris_sql(sql, params)

        # 构建请求参数
        request_params: dict[str, Any] = {
            "sql": doris_sql,
        }
        if variables:
            # Doris HTTP API 支持 variables 参数传递命名参数
            request_params["variables"] = json.dumps(variables)

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
            # R-3：4xx = SQL 语法/参数错误（用户 SQL 问题）→ DorisSqlError 如实上抛、
            # 不降级不熔断；5xx = 引擎故障 → 降级错误（触发 MySQL 降级 + 熔断计数）。
            if 400 <= response.status_code < 500:
                raise DorisSqlError(
                    f"Doris SQL 错误（HTTP {response.status_code}）",
                    status_code=response.status_code,
                    body=response.text,
                )
            raise _make_degraded_error(f"Doris 返回 HTTP {response.status_code}")

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
        校验严格：列缺失、行列不匹配、空行按错误处理（防静默截断产生脏数据）。
        """
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            logger.error("doris_response_parse_error", body_preview=body[:500])
            raise _make_degraded_error("Doris 响应解析失败") from None
        if not isinstance(data, dict):
            logger.error("doris_response_shape_invalid", body_preview=body[:500])
            raise _make_degraded_error("Doris 响应格式错误") from None

        # Doris 可能返回不同格式的响应
        code = data.get("code", -1)
        if code != 0:
            message = data.get("message", "未知错误")
            # R-3：Doris 200 + code!=0 是 SQL 查询错误（语法/表不存在/权限）——用户 SQL 问题，
            # 如实上抛不降级，避免坏 SQL 污染熔断器并连累正常查询。
            raise DorisSqlError(f"Doris 查询错误: {message}", body=body)

        result_data = data.get("data")
        if not isinstance(result_data, dict):
            # 兼容无 data 包裹、行即字典数组的扁平响应
            result_data = data
        elif result_data.get("data") is not None and isinstance(result_data.get("data"), dict):
            # Doris 偶发双层 data 包裹
            result_data = result_data["data"]

        columns = result_data.get("columns", [])
        raw_rows = result_data.get("rows", [])
        if not isinstance(raw_rows, list):
            logger.error("doris_response_rows_not_list", body_preview=body[:500])
            raise _make_degraded_error("Doris 响应 rows 缺失") from None

        # 行已为字典格式：直接复用（无列时无需列映射）
        if raw_rows and isinstance(raw_rows[0], dict):
            return OLAPResult(rows=raw_rows, total=len(raw_rows))

        # 列 + 行值数组格式：严格校验列数与每行宽度，防止静默截断
        col_names = [
            c.get("name", f"col_{i}") if isinstance(c, dict) else str(c)
            for i, c in enumerate(columns)
        ]
        if not col_names:
            if raw_rows:
                logger.error("doris_response_no_columns", body_preview=body[:500])
                raise _make_degraded_error("Doris 响应缺失列定义") from None
            return OLAPResult(rows=[], total=0)

        rows: list[dict[str, Any]] = []
        for idx, row in enumerate(raw_rows):
            if not isinstance(row, (list, tuple)):
                logger.error("doris_response_row_invalid", row_idx=idx, body_preview=body[:500])
                raise _make_degraded_error("Doris 响应行格式错误") from None
            if len(row) != len(col_names):
                logger.error(
                    "doris_response_width_mismatch",
                    row_idx=idx,
                    expected=len(col_names),
                    actual=len(row),
                    body_preview=body[:500],
                )
                raise _make_degraded_error("Doris 响应列数与行宽不匹配") from None
            rows.append(dict(zip(col_names, row, strict=True)))
        return OLAPResult(rows=rows, total=len(rows))

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
