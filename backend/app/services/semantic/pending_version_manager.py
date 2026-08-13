"""PENDING_VERSION 版本确认期管理器（对齐 TD §12.3 / spec FR-006~FR-009）。

破坏性变更 14 天消费方确认期生命周期管理：
- 创建 PENDING 确认记录（14 天截止）
- 消费方确认 / 拒绝
- Owner 延期（+7 天，最多 1 次）
- 超时自动接受
- 数据漂移暂停
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessError
from app.models.metric import Metric
from app.models.metric_version import MetricVersion, PendingVersionConfirmation
from app.services.semantic.repository import MetricRepository

logger = structlog.get_logger("unisense.semantic.pending_version_manager")


class PendingAction(enum.StrEnum):
    """确认/拒绝后的返回动作。"""

    WAITING = "WAITING"
    SWITCH_CURRENT = "SWITCH_CURRENT"
    CANCEL = "CANCEL"


class PendingVersionManager:
    """PENDING_VERSION 版本确认期管理器。

    用法::

        mgr = PendingVersionManager(db)
        await mgr.create_pending(metric, new_version, consumer_ids=[10, 20])

        action = await mgr.confirm(metric_id=1, version=2, consumer_id=10)
        if action == PendingAction.SWITCH_CURRENT:
            # 版本转正
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = MetricRepository(db)

    async def create_pending(
        self,
        metric: Metric,
        new_version: MetricVersion,
        consumer_ids: list[int],
    ) -> None:
        """创建 PENDING 确认记录，更新版本状态为 PENDING_CONFIRMATION。

        Args:
            metric: 指标对象。
            new_version: 新版本对象。
            consumer_ids: 需确认的消费方 ID 列表。

        Raises:
            BusinessError: consumer_ids 为空。
        """
        if not consumer_ids:
            raise BusinessError(
                "PENDING_VERSION 须指定至少一个消费方",
                error_code="VALIDATION_ERROR",
            )

        now = datetime.now(UTC)
        deadline = now + timedelta(days=14)

        # 更新 MetricVersion 状态与截止时间
        stmt = (
            update(MetricVersion)
            .where(
                MetricVersion.metric_id == metric.id,
                MetricVersion.version == new_version.version,
            )
            .values(
                status="PENDING_CONFIRMATION",
                pending_deadline=deadline,
            )
        )
        await self._db.execute(stmt)

        # 为每个消费方创建确认记录
        for consumer_id in consumer_ids:
            confirmation = PendingVersionConfirmation(
                metric_id=metric.id,
                version=new_version.version,
                consumer_id=consumer_id,
                status="PENDING",
                extension_count=0,
                deadline=deadline,
            )
            await self._repo.save_pending_confirmation(confirmation)

        logger.info(
            "pending_version_created",
            metric_id=metric.id,
            metric_code=metric.metric_code,
            version=new_version.version,
            consumer_count=len(consumer_ids),
            deadline=deadline.isoformat(),
        )

    async def confirm(
        self, metric_id: int, version: int, consumer_id: int
    ) -> PendingAction:
        """消费方确认版本。

        更新确认状态为 CONFIRMED；检查是否全部消费方已确认，
        全部确认返回 SWITCH_CURRENT，否则返回 WAITING。

        Args:
            metric_id: 指标 ID。
            version: 版本号。
            consumer_id: 消费方用户 ID。

        Returns:
            PendingAction.SWITCH_CURRENT 或 PendingAction.WAITING。

        Raises:
            BusinessError: 无待确认记录或当前用户无权确认。
        """
        confirmations = await self._repo.get_pending_confirmations(metric_id, version)
        if not confirmations:
            raise BusinessError(
                f"该版本 {version} 无待确认记录",
                error_code="NO_PENDING_CONFIRMATION",
            )

        mine = next(
            (c for c in confirmations if c.consumer_id == consumer_id), None
        )
        if mine is None:
            raise BusinessError(
                "当前用户无该版本的待确认记录",
                error_code="NO_PENDING_CONFIRMATION",
            )

        # 幂等：已确认直接检查状态
        if mine.status != "CONFIRMED":
            await self._repo.update_confirmation_status(mine.id, "CONFIRMED")

        # 检查是否全部确认（刚确认的 + 之前已确认的）
        all_confirmed = all(
            c.status == "CONFIRMED" or c.id == mine.id
            for c in confirmations
        )

        if all_confirmed:
            logger.info(
                "pending_version_all_confirmed",
                metric_id=metric_id,
                version=version,
            )
            return PendingAction.SWITCH_CURRENT

        return PendingAction.WAITING

    async def reject(
        self, metric_id: int, version: int, consumer_id: int, reason: str
    ) -> PendingAction:
        """消费方拒绝版本。任一拒绝即取消整个 PENDING_VERSION。

        将 MetricVersion.status 置为 CANCELLED。

        Args:
            metric_id: 指标 ID。
            version: 版本号。
            consumer_id: 消费方用户 ID。
            reason: 拒绝原因。

        Returns:
            PendingAction.CANCEL。

        Raises:
            BusinessError: 无待确认记录或当前用户无权拒绝。
        """
        confirmations = await self._repo.get_pending_confirmations(metric_id, version)
        if not confirmations:
            raise BusinessError(
                f"该版本 {version} 无待确认记录",
                error_code="NO_PENDING_CONFIRMATION",
            )

        mine = next(
            (c for c in confirmations if c.consumer_id == consumer_id), None
        )
        if mine is None:
            raise BusinessError(
                "当前用户无该版本的待确认记录",
                error_code="NO_PENDING_CONFIRMATION",
            )

        # 更新拒绝记录
        await self._repo.update_confirmation_status(mine.id, "REJECTED", reason=reason)

        # 取消版本：MetricVersion.status → CANCELLED
        stmt = (
            update(MetricVersion)
            .where(
                MetricVersion.metric_id == metric_id,
                MetricVersion.version == version,
            )
            .values(status="CANCELLED")
        )
        await self._db.execute(stmt)

        logger.info(
            "pending_version_rejected",
            metric_id=metric_id,
            version=version,
            consumer_id=consumer_id,
            reason=reason,
        )

        return PendingAction.CANCEL

    async def extend(self, metric_id: int, version: int) -> None:
        """Owner 请求版本确认延期（+7 天，最多延期 1 次）。

        Args:
            metric_id: 指标 ID。
            version: 版本号。

        Raises:
            BusinessError: 无待确认记录或已延期满 1 次。
        """
        confirmations = await self._repo.get_pending_confirmations(metric_id, version)
        if not confirmations:
            raise BusinessError(
                f"该版本 {version} 无待确认记录",
                error_code="NO_PENDING_CONFIRMATION",
            )

        if any(c.extension_count >= 1 for c in confirmations):
            raise BusinessError(
                "版本确认已延期满 1 次，不可再延期",
                error_code="EXTEND_LIMIT_REACHED",
            )

        now = datetime.now(UTC)
        for c in confirmations:
            new_deadline = (c.deadline or now) + timedelta(days=7)
            await self._repo.extend_confirmation_deadline(c.id, new_deadline)

        # 同步更新 MetricVersion.pending_deadline
        first_new_deadline = (confirmations[0].deadline or now) + timedelta(days=7)
        stmt = (
            update(MetricVersion)
            .where(
                MetricVersion.metric_id == metric_id,
                MetricVersion.version == version,
            )
            .values(pending_deadline=first_new_deadline)
        )
        await self._db.execute(stmt)

        logger.info(
            "pending_version_extended",
            metric_id=metric_id,
            version=version,
            new_deadline=first_new_deadline.isoformat(),
        )

    async def check_timeouts(self) -> list[int]:
        """查找超时未确认的 PENDING 确认记录，自动接受（TIMEOUT_ACCEPTED）。

        Returns:
            需要切换 CURRENT 版本的 metric_id 列表。
        """
        timeout_records = await self._repo.get_timeout_pending_confirmations()
        if not timeout_records:
            return []

        # 按 (metric_id, version) 分组
        pending_groups: dict[tuple[int, int], list[PendingVersionConfirmation]] = {}
        for record in timeout_records:
            key = (record.metric_id, record.version)
            pending_groups.setdefault(key, []).append(record)

        switch_metric_ids: list[int] = []

        for (metric_id, version), records in pending_groups.items():
            # 将所有超时记录标记为 TIMEOUT_ACCEPTED
            for record in records:
                await self._repo.update_confirmation_status(
                    record.id, "TIMEOUT_ACCEPTED"
                )

            # 获取该版本的所有确认记录，判断是否全部已确认/超时接受
            all_confirmations = await self._repo.get_pending_confirmations(
                metric_id, version
            )
            all_done = all(
                c.status in ("CONFIRMED", "TIMEOUT_ACCEPTED")
                for c in all_confirmations
            )

            if all_done:
                switch_metric_ids.append(metric_id)

            logger.info(
                "pending_version_timeout_accepted",
                metric_id=metric_id,
                version=version,
                timeout_count=len(records),
                ready_to_switch=all_done,
            )

        return switch_metric_ids

    async def pause_on_drift(
        self, metric_id: int, version: int, drift_detail: dict[str, Any]
    ) -> None:
        """检测到数据漂移时暂停确认流程，通知 Owner。

        Args:
            metric_id: 指标 ID。
            version: 版本号。
            drift_detail: 漂移详情字典。
        """
        logger.warning(
            "pending_version_drift_detected",
            metric_id=metric_id,
            version=version,
            drift_detail=drift_detail,
        )

        # 通知 Owner 关于漂移的情况
        # TODO: 集成 notify 服务发送通知（当前仅日志记录）
        logger.info(
            "pending_version_drift_notification",
            metric_id=metric_id,
            version=version,
            message="检测到数据漂移，请 Owner 关注确认流程",
        )
