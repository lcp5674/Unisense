"""全局聚合搜索服务（FR-18 全局搜索栏生产化）。

编排 GlobalSearchRepository 跨 9 类资源（指标/维度/术语/模板/数据源/采集目录表/
采集目录字段/主题域/度量目录）聚合搜索，返回按类型分组结果。
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

    async def search(
        self,
        q: str,
        limit: int = 5,
        *,
        visible_actor_id: int | None = None,
        visible_role: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """跨 9 类资源聚合搜索，按类型分组返回（每类至多 limit 条）。

        Args:
            q: 搜索关键词（去空白后非空）。
            limit: 每类资源返回条数上限。
            visible_actor_id: 可见性过滤（P0-3 行级隔离，D-1）——非管理角色仅检索
                公开状态 + 本人负责的未发布资产；管理角色传 None 不过滤。
            visible_role: 调用者角色（配合 visible_actor_id 判定 reviewer 放行）。

        Returns:
            分组结构 ``{"metric": [...], "dimension": [...], ...}``。
        """
        return await self._repo.search(
            q,
            limit,
            visible_actor_id=visible_actor_id,
            visible_role=visible_role,
        )
