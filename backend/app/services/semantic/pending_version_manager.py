"""PENDING_VERSION 版本确认期管理器（对齐 TD §12.3 / spec FR-006~FR-009）。

破坏性变更 14 天消费方确认期生命周期管理。

职责边界（P1-8 双实现合并）：本管理器只负责「创建 PENDING 确认记录」；
确认 / 拒绝 / 延期 / 超时自动接受的业务规则**唯一实现**在
``MetricService``（confirm_version / reject_version / extend_version /
auto_accept_timeout）——原 manager.confirm/reject/extend/check_timeouts 与
service 重复实现且更简（无转正/乐观锁/非 PUBLISHED 跳过等生产逻辑），已删除，
避免同规则两套代码漂移。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessError
from app.models.metric import Metric
from app.models.metric_version import MetricVersion, PendingVersionConfirmation
from app.services.semantic.repository import MetricRepository

logger = structlog.get_logger("unisense.semantic.pending_version_manager")


class PendingVersionManager:
    """PENDING_VERSION 版本确认期管理器（仅创建确认记录）。

    用法::

        mgr = PendingVersionManager(db)
        await mgr.create_pending(metric, new_version, consumer_ids=[10, 20])
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
