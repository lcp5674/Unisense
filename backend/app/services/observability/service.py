"""可观测性服务（TD §12.10 / FR-16）。

核心能力：
1. 用户反馈提交与查询。
2. 运营大盘聚合：质量事件、API 调用、通知、血缘等统计。
3. NPS 采集与反馈采纳闭环（P2: US14）。

P3: 继承 BaseService Protocol。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.models.feedback import Feedback
from app.services.observability.repository import ObservabilityRepository
from app.services.observability.schemas import FeedbackCreate

# 反馈采纳闭环的合法状态（Feedback 表无独立 status 列，状态以 comment 内
# 标记记录；此处白名单校验防止任意字符串注入 comment 并造成不可解析状态）。
_ALLOWED_STATUSES = {"adopted", "rejected", "in_progress", "pending"}


def _last_status_marker(comment: str) -> str | None:
    """解析 comment 中最近一次记录的状态标记（``status=xxx``）。"""
    markers = re.findall(r"status=([A-Za-z_]+)", comment)
    return markers[-1] if markers else None


class ObservabilityService(BaseService):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
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
            category=data.category or "improvement",
            priority=data.priority or "medium",
            source_url=data.source_url,
        )
        result = await self._repo.save_feedback(feedback)
        await self._repo.commit()
        return result

    async def list_feedback(
        self,
        target_type: str | None,
        status: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        """反馈列表（分页 + 状态过滤）。"""
        if page_size > 100:
            page_size = 100
        items, total = await self._repo.list_feedback(target_type, status, page, page_size)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    async def nps_stats(self) -> dict[str, Any]:
        return await self._repo.nps_stats()

    async def quality_events(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self._repo.quality_events(limit)

    async def quality_stats(self) -> dict[str, Any]:
        return await self._repo.quality_stats()

    async def api_stats(self) -> dict[str, int]:
        return await self._repo.api_stats()

    async def notification_stats(self) -> dict[str, Any]:
        return await self._repo.notification_stats()

    async def lineage_stats(self) -> dict[str, int]:
        return await self._repo.lineage_stats()

    async def overview_stats(self) -> dict[str, Any]:
        """平台运营总览聚合（生产视角：健康/积压/资产/消费一次拉齐）。"""
        return await self._repo.overview_stats()

    # ----------------------------------------------------------------
    # P2 Enhancement: NPS 采集 + 反馈采纳闭环
    # ----------------------------------------------------------------

    async def submit_nps(
        self,
        user_id: int,
        score: int,
        comment: str | None = None,
        target_type: str = "platform",
        target_id: str | None = None,
    ) -> Feedback:
        """NPS 采集：用户提交 0-10 推荐度评分。

        Args:
            user_id: 评分用户 ID。
            score: NPS 分数（0-10）。
            comment: 可选评论。
            target_type: 目标类型。
            target_id: 目标 ID。

        Raises:
            UnisenseError: 分数不在 0-10 范围内。
        """
        if not (0 <= score <= 10):
            from app.core.exceptions import UnisenseError

            raise UnisenseError("NPS 分数需在 0-10 之间", error_code="INVALID_NPS_SCORE")

        feedback = Feedback(
            user_id=user_id,
            target_type=target_type,
            target_id=target_id,
            nps_score=score,
            comment=comment or f"NPS: {score}/10",
        )
        result = await self._repo.save_feedback(feedback)
        await self._repo.commit()

        await self._publish_event(
            "nps.submitted",
            {"user_id": user_id, "score": score},
            actor_id=str(user_id),
        )

        return result

    async def update_feedback_status(
        self,
        feedback_id: int,
        status: str,
        resolver_id: int | None = None,
        resolution_note: str | None = None,
    ) -> Feedback:
        """反馈采纳闭环：更新反馈状态。

        Args:
            feedback_id: 反馈 ID。
            status: 新状态（adopted/rejected/in_progress/pending）。
            resolver_id: 处理人 ID。
            resolution_note: 处理说明。

        Raises:
            UnisenseError: 状态非法。
            NotFoundError: 反馈不存在。
        """
        if status not in _ALLOWED_STATUSES:
            from app.core.exceptions import UnisenseError

            raise UnisenseError(f"非法的反馈状态: {status}", error_code="INVALID_FEEDBACK_STATUS")
        feedback = await self._repo.get_feedback(feedback_id)
        if feedback is None:
            from app.core.exceptions import NotFoundError

            raise NotFoundError(f"反馈不存在: {feedback_id}")

        # 状态落库到 status 列（此前仅写进 comment 文本，状态不可查询/过滤，
        # "反馈采纳闭环"未真正落地）；comment 保留追加的状态变更历史。
        changed = feedback.status != status or resolution_note is not None
        if changed:
            feedback.status = status
            feedback.resolver_id = resolver_id
            feedback.resolved_at = datetime.now(UTC)
            if resolution_note is not None:
                feedback.resolution_note = resolution_note
        # comment 追加状态变更记录（幂等去重：同状态且无新说明/处理人时不重复追加，
        # 防止 comment 文本无界增长）。
        existing_comment = feedback.comment or ""
        last_status = _last_status_marker(existing_comment)
        if last_status != status or resolution_note or resolver_id:
            status_note = f"[{datetime.now(UTC).isoformat()}] status={status}"
            if resolution_note:
                status_note += f" note={resolution_note}"
            if resolver_id:
                status_note += f" by={resolver_id}"
            feedback.comment = (existing_comment + "\n" + status_note).lstrip("\n")

        await self._repo.save_feedback(feedback)
        await self._repo.commit()

        await self._publish_event(
            "feedback.status_updated",
            {
                "feedback_id": feedback_id,
                "status": status,
                "resolver_id": resolver_id,
                # 通知反馈提交者（notify 消费时识别 recipient_user_id 定向投递）
                "recipient_user_id": feedback.user_id,
                "comment": (feedback.comment or "")[:200],
            },
            actor_id=str(resolver_id or ""),
        )

        return feedback
