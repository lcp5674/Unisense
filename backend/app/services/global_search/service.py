"""全局聚合搜索服务（FR-18 全局搜索栏生产化）。

编排 GlobalSearchRepository 跨 8 类资源聚合搜索，返回按类型分组结果。
只读能力，无业务状态流转；失败由全局错误处理器统一兜底。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.services.global_search.repository import GlobalSearchRepository


class GlobalSearchService(BaseService):
    """全局聚合搜索服务。"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._session = session
        self._repo = GlobalSearchRepository(session)

    async def search(self, q: str, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
        """跨 8 类资源聚合搜索，按类型分组返回（每类至多 limit 条）。

        Args:
            q: 搜索关键词（去空白后非空）。
            limit: 每类资源返回条数上限。

        Returns:
            分组结构 ``{"metric": [...], "dimension": [...], ...}``。
        """
        return await self._repo.search(q, limit)
