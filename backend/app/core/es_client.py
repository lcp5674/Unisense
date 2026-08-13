"""可选依赖：Elasticsearch 检索客户端（对齐 TD §4.13 / §5.2 降级矩阵）。

工业级要点：
- 可选依赖守卫：``elasticsearch`` 包缺失或未配置 ``es_url`` 时客户端自动禁用，调用方优雅降级；
  **绝不因缺少依赖导致进程启动失败**（try/except ImportError 守卫，兼容本仓库 elasticsearch
  虽声明于 pyproject 但部分环境未安装的情况）。
- 熔断器保护：所有检索操作经模块级 ``es_breaker`` 保护，熔断开启时抛出
  :class:`CircuitOpenError`，调用方降级到 MySQL/Neo4j 全文或返回空结果。
- 进程内单例复用连接池（避免每请求新建客户端泄漏，FR-06 同类缺陷防护）。
- 就绪探针通过 :meth:`EsClient.health` 进行**真实**探活（``.ping()``），使 ``es_breaker``
  真正进入降级矩阵调用路径（消除「死代码」缺口）。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.resilience import CircuitBreaker, es_breaker

logger = get_logger(__name__)

# 守卫式导入：elasticsearch 为可选依赖，缺失时客户端自动禁用。
# 用独立别名 _ESClientClass（Any）承接导入类，缺失时回退 None（避免与 import 同名触发 no-redef）。
_ESClientClass: Any = None
try:
    from elasticsearch import AsyncElasticsearch as _AsyncES

    _ESClientClass = _AsyncES
except ImportError:
    pass  # pragma: no cover - 环境守卫，由 reload 测试验证


class SearchUnavailableError(Exception):
    """ES 检索不可用（客户端禁用或底层异常），调用方应优雅降级。"""


class CircuitOpenError(Exception):
    """ES 熔断器开启，请求被拒绝。"""


def _es_search_kwargs(body: dict[str, Any]) -> dict[str, Any]:
    """将 ES 查询体映射为 elasticsearch-py 8.x 的命名参数。

    8.x 移除了 ``body=`` 参数，需将查询体字段展开为命名参数；其中 ``from`` 是 Python
    关键字，映射为 ``from_``。其余合法字段原样透传，由底层客户端校验。
    """
    kwargs: dict[str, Any] = {}
    for key, value in body.items():
        kwargs["from_" if key == "from" else key] = value
    return kwargs


class EsClient:
    """Elasticsearch 检索客户端（可选依赖 + 熔断保护）。

    Args:
        client: 注入的底层客户端（测试用）；为 None 时按配置构造。
        breaker: 注入的熔断器（测试用）；默认模块级 ``es_breaker``。
    """

    def __init__(self, client: Any = None, breaker: CircuitBreaker | None = None) -> None:
        self._breaker = breaker or es_breaker
        self._client: Any = None
        self._enabled = False

        if client is not None:
            # 测试注入或显式传入：直接使用
            self._client = client
            self._enabled = True
            return

        if _ESClientClass is None:
            logger.warning("es_client_disabled", reason="package_not_installed")
            return
        if not settings.es_url:
            logger.info("es_client_disabled", reason="es_url_not_configured")
            return
        try:
            auth = (settings.es_username, settings.es_password) if settings.es_username else None
            self._client = _ESClientClass(
                settings.es_url,
                basic_auth=auth,
                # 工业级容错：显式请求超时，避免慢/挂的 ES 阻塞就绪探针与调用方
                # （默认 10s 过长，生产应短）。缺配置时回退 3s。
                request_timeout=settings.es_request_timeout,
            )
            self._enabled = True
        except Exception:
            logger.warning("es_client_init_failed", exc_info=True)

    @property
    def enabled(self) -> bool:
        """客户端是否可用（包已装 + es_url 已配 + 构造成功）。"""
        return self._enabled

    async def search(self, index: str, body: dict[str, Any], *, size: int = 20) -> Any:
        """执行检索，失败经熔断器统计。

        Raises:
            SearchUnavailableError: 客户端禁用或 ES 调用异常。
            CircuitOpenError: 熔断器开启。
        """
        if not self._enabled or self._client is None:
            raise SearchUnavailableError("elasticsearch client disabled")
        if not self._breaker.allow():
            raise CircuitOpenError("es circuit open")
        try:
            # 8.x 命名参数化：body 中的 size 优先，未指定时才用默认 size，
            # 避免与展开后的 body.size 重复传参（TypeError: multiple values for 'size'）。
            kwargs = _es_search_kwargs(body)
            kwargs.setdefault("size", size)
            resp = await self._client.search(index=index, **kwargs)
            self._breaker.record_success()
            return resp
        except Exception as exc:
            self._breaker.record_failure()
            raise SearchUnavailableError(f"es search failed: {exc}") from exc

    async def index(
        self,
        index: str,
        document: dict[str, Any],
        *,
        doc_id: str | None = None,
    ) -> Any:
        """索引文档，失败经熔断器统计。"""
        if not self._enabled or self._client is None:
            raise SearchUnavailableError("elasticsearch client disabled")
        if not self._breaker.allow():
            raise CircuitOpenError("es circuit open")
        try:
            resp = await self._client.index(index=index, document=document, id=doc_id)
            self._breaker.record_success()
            return resp
        except Exception as exc:
            self._breaker.record_failure()
            raise SearchUnavailableError(f"es index failed: {exc}") from exc

    async def health(self) -> bool:
        """真实探活（供 /ready 探针），结果经熔断器统计。"""
        if not self._enabled or self._client is None:
            return False
        if not self._breaker.allow():
            return False
        try:
            await self._client.ping()
            self._breaker.record_success()
            return True
        except Exception:
            self._breaker.record_failure()
            return False

    async def close(self) -> None:
        """关闭底层连接池（best-effort）。"""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                logger.warning("es_client_close_failed", exc_info=True)


_client_singleton: EsClient | None = None


def get_es_client() -> EsClient:
    """返回进程内共享的 EsClient 单例（惰性构造，复用连接池）。"""
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = EsClient()
    return _client_singleton
