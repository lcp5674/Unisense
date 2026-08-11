"""可观测性服务（TD §12.10 / FR-16）。

核心能力：
1. 用户反馈提交与查询。
2. 运营大盘聚合：质量事件、API 调用、通知、血缘等统计。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feedback import Feedback
from app.services.observability.repository import ObservabilityRepository
from app.services.observability.schemas import FeedbackCreate


class ObservabilityService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = ObservabilityRepository(session)

    async def submit_feedback(self, data: FeedbackCreate, actor_id: int | None = None) -> Feedback:
        if data.rating is not None and not (1 <= data.rating <= 5):
            from app.core.exceptions import UnisenseError

            raise UnisenseError("评分需在 1-5 之间", error_code="INVALID_RATING")
        # PLAT-2: 以服务端认证身份 actor_id 落库，忽略 client 传入的 user_id 防止伪造
        feedback = Feedback(
            user_id=actor_id if actor_id is not None else data.user_id,
            target_type=data.target_type,
            target_id=data.target_id,
            rating=data.rating,
            comment=data.comment,
        )
        result = await self._repo.save_feedback(feedback)
        await self._repo.commit()
        return result

    async def list_feedback(self, target_type: str | None, limit: int) -> list[Feedback]:
        return await self._repo.list_feedback(target_type, limit)

    async def quality_stats(self) -> dict[str, Any]:
        return await self._repo.quality_stats()

    async def api_stats(self) -> dict[str, int]:
        return await self._repo.api_stats()

    async def notification_stats(self) -> dict[str, Any]:
        return await self._repo.notification_stats()

    async def lineage_stats(self) -> dict[str, int]:
        return await self._repo.lineage_stats()
