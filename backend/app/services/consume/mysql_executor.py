"""MySQL 查询降级执行引擎（OLAP 不可用时的只读兜底，对齐 TD §12.6 / FR-5）。

核心能力：
1. 只读护栏：仅允许 ``SELECT`` 开头的 SQL（前置校验，拒绝任意写/DDL）。
2. 参数化执行：SQLAlchemy ``text()`` 原生支持 ``:name`` 占位符（与 consume
   ``_build_query_sql`` 产出的参数格式一致，无需转换）。
3. 超时控制：``asyncio.wait_for`` 兜底（MySQL 语句级超时不可靠）。
4. 熔断保护：复用全局 ``olap_breaker``（OLAP/MySQL 同为查询引擎，故障状态共享）。
5. 值序列化：``Decimal``/``date``/``datetime`` → JSON 可序列化（快照 value_json 直接落 JSON 列）。
6. 连接池：SQLAlchemy async engine 复用（``pool_pre_ping`` 防死连接）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import settings
from app.core.logging import get_logger
from app.core.resilience import olap_breaker
from app.services.consume.olap_executor import OLAPResult

logger = get_logger(__name__)

# 单次查询兜底超时（秒）：防止慢查询/挂起的连接长时间占用
_DEFAULT_TIMEOUT = 30.0


def _to_jsonable(value: Any) -> Any:
    """把 MySQL 原生值转换为 JSON 可序列化值（快照落 JSON 列前必须）。"""
    if isinstance(value, Decimal):
        # 金额/数值：转 float 便于前端直接展示与聚合
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


class MysqlExecutor:
    """MySQL 只读查询执行器（OLAP 不可用时的降级兜底）。"""

    def __init__(
        self,
        url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._url = url or settings.mysql_fallback_url
        self._timeout = timeout
        self._engine: AsyncEngine | None = None
        # 与 OLAP 共享查询引擎熔断器：OLAP/MySQL 任一故障都计入同一降级状态
        self._breaker = olap_breaker

    @property
    def enabled(self) -> bool:
        """是否配置了 MySQL 降级引擎（未配置 URL 则不可用）。"""
        return bool(self._url)

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self._url,
                pool_pre_ping=True,
                pool_recycle=3600,
            )
        return self._engine

    async def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> OLAPResult:
        """执行只读 SELECT 并返回结果（OLAPResult 兼容结构）。

        Args:
            sql: 只读 SELECT 语句（含 ``:name`` 参数占位符）。
            params: 参数映射（``_build_query_sql`` 产出格式）。
            timeout: 查询超时（秒），None 使用默认值。

        Raises:
            BusinessError: 熔断器打开 / 非 SELECT / 超时时抛出 DEPENDENCY_DEGRADED_ENGINE。
        """
        if not self.enabled:
            from app.core.error_codes import ErrorCode
            from app.core.exceptions import BusinessError

            raise BusinessError(
                "MySQL 降级引擎未配置",
                error_code=ErrorCode.DEPENDENCY_DEGRADED_ENGINE,
                ctx={"retry_after": 30},
            )
        statement = sql.strip()
        if not statement.upper().startswith("SELECT"):
            from app.core.error_codes import ErrorCode
            from app.core.exceptions import BusinessError

            raise BusinessError(
                "MySQL 降级引擎仅允许只读 SELECT",
                error_code=ErrorCode.DEPENDENCY_DEGRADED_ENGINE,
            )
        if not self._breaker.allow():
            from app.core.error_codes import ErrorCode
            from app.core.exceptions import BusinessError

            raise BusinessError(
                "查询引擎熔断器已打开，请稍后重试",
                error_code=ErrorCode.DEPENDENCY_DEGRADED_ENGINE,
                ctx={"retry_after": 30},
            )

        start = time.monotonic()
        query_timeout = timeout or self._timeout
        try:
            async def _run() -> OLAPResult:
                engine = self._get_engine()
                async with engine.connect() as conn:
                    result = await conn.execute(text(statement), params or {})
                    rows_mapping = result.mappings().all()
                    rows = [
                        {k: _to_jsonable(v) for k, v in row.items()}
                        for row in rows_mapping
                    ]
                    return OLAPResult(rows=rows, total=len(rows))

            result = await asyncio.wait_for(_run(), timeout=query_timeout)
            self._breaker.record_success()
            result.elapsed_ms = round((time.monotonic() - start) * 1000, 2)
            return result
        except TimeoutError:
            self._breaker.record_failure()
            logger.warning("mysql_fallback_timeout", sql_preview=sql[:200], timeout=query_timeout)
            raise
        except Exception as exc:
            self._breaker.record_failure()
            logger.warning(
                "mysql_fallback_execute_failed",
                sql_preview=sql[:200],
                error=str(exc),
                exc_info=True,
            )
            raise

    async def close(self) -> None:
        """释放异步引擎连接池。"""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
