"""指标数据访问层（Repository）。

对齐 DEV_GUIDE §8b.2（Repository 层：数据访问，禁止 service 直接写 ORM 查询）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.models.metric import Metric, MetricVersion


class MetricRepository:
    """指标数据访问层。

    封装所有对 metric / metric_version 表的查询，service 层不直接写 ORM 查询。
    """

    def __init__(self, db: AsyncSession) -> None:
        """初始化 repository。

        Args:
            db: 异步数据库会话。
        """
        self._db = db

    async def create(self, metric: Metric) -> Metric:
        """创建指标。

        捕获唯一键冲突（并发下的 TOCTOU），转换为 ConflictError，
        避免将 IntegrityError 暴露为 500。

        Args:
            metric: 指标 ORM 对象。

        Returns:
            创建后的指标（含 id）。
        """
        self._db.add(metric)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            await self._db.rollback()
            raise ConflictError(
                f"指标编码已存在: {getattr(metric, 'metric_code', '')}",
                error_code="CONFLICT",
            ) from exc
        await self._db.refresh(metric)
        return metric

    async def get_by_code(self, metric_code: str) -> Metric | None:
        """根据指标编码查询。

        Args:
            metric_code: 指标编码。

        Returns:
            指标对象或 None。
        """
        result = await self._db.execute(
            select(Metric).where(
                Metric.metric_code == metric_code,
                Metric.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, metric_id: int) -> Metric | None:
        """根据 ID 查询。

        Args:
            metric_id: 指标 ID。

        Returns:
            指标对象或 None。
        """
        result = await self._db.execute(
            select(Metric).where(
                Metric.id == metric_id,
                Metric.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_metrics(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        metric_tier: str | None = None,
        keyword: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Metric], int]:
        """分页查询指标列表。

        Args:
            domain: 域过滤。
            status: 状态过滤。
            metric_tier: 分级过滤。
            keyword: 关键词搜索（metric_code/name）。
            offset: 偏移量。
            limit: 每页数量。

        Returns:
            (指标列表, 总数)。
        """
        conditions: list[ColumnElement[bool]] = [Metric.deleted_at.is_(None)]
        if domain:
            conditions.append(Metric.domain == domain)
        if status:
            conditions.append(Metric.status == status)
        if metric_tier:
            conditions.append(Metric.metric_tier == metric_tier)
        if keyword:
            conditions.append(
                (Metric.metric_code.contains(keyword)) | (Metric.name.contains(keyword))
            )

        # 总数
        count_stmt = select(func.count()).select_from(Metric).where(*conditions)
        total = (await self._db.execute(count_stmt)).scalar() or 0

        # 列表
        stmt = (
            select(Metric)
            .where(*conditions)
            .order_by(Metric.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(stmt)
        items = list(result.scalars().all())

        return items, total

    async def update_with_optimistic_lock(
        self, metric_id: int, expected_row_version: int, **kwargs: Any
    ) -> Metric:
        """乐观锁更新。

        对齐 TD §4.1 row_version 乐观锁。

        Args:
            metric_id: 指标 ID。
            expected_row_version: 预期的行版本。
            **kwargs: 更新字段。

        Returns:
            更新后的指标。

        Raises:
            NotFoundError: 指标不存在。
            ConflictError: 乐观锁冲突（数据已被他人修改）。
        """
        kwargs["row_version"] = expected_row_version + 1
        stmt = (
            update(Metric)
            .where(
                Metric.id == metric_id,
                Metric.row_version == expected_row_version,
                Metric.deleted_at.is_(None),
            )
            .values(**kwargs)
        )
        result = await self._db.execute(stmt)
        if result.rowcount == 0:  # type: ignore[attr-defined]  # CursorResult.rowcount；SQLA 静态类型缺失，运行时存在
            # 区分不存在 vs 乐观锁冲突
            existing = await self.get_by_id(metric_id)
            if existing is None:
                raise NotFoundError(f"指标不存在: {metric_id}")
            raise ConflictError(
                "数据已被他人修改，请刷新后重试",
                error_code="CONCURRENT_MODIFICATION",
            )
        updated = await self.get_by_id(metric_id)
        await self._db.refresh(updated)
        assert updated is not None
        return updated

    async def soft_delete(self, metric_id: int) -> None:
        """软删除指标。

        Args:
            metric_id: 指标 ID。

        Raises:
            NotFoundError: 指标不存在。
        """
        from datetime import UTC, datetime

        stmt = (
            update(Metric)
            .where(Metric.id == metric_id, Metric.deleted_at.is_(None))
            .values(deleted_at=datetime.now(UTC))
        )
        result = await self._db.execute(stmt)
        if result.rowcount == 0:  # type: ignore[attr-defined]  # CursorResult.rowcount；SQLA 静态类型缺失，运行时存在
            raise NotFoundError(f"指标不存在: {metric_id}")

    # ---- 版本相关 ----

    async def create_version(self, version: MetricVersion) -> MetricVersion:
        """创建指标版本。

        Args:
            version: 版本 ORM 对象。

        Returns:
            创建后的版本。
        """
        self._db.add(version)
        await self._db.flush()
        await self._db.refresh(version)
        return version

    async def list_versions(self, metric_id: int) -> list[MetricVersion]:
        """查询指标的所有版本。

        Args:
            metric_id: 指标 ID。

        Returns:
            版本列表（按版本号降序）。
        """
        result = await self._db.execute(
            select(MetricVersion)
            .where(MetricVersion.metric_id == metric_id)
            .order_by(MetricVersion.version.desc())
        )
        return list(result.scalars().all())

    async def get_version(self, metric_id: int, version: int) -> MetricVersion | None:
        """按 (metric_id, version) 获取版本（用于发布时定位待发布版本）。"""
        result = await self._db.execute(
            select(MetricVersion).where(
                MetricVersion.metric_id == metric_id,
                MetricVersion.version == version,
                MetricVersion.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def mark_version_published(
        self, metric_id: int, version: int, published_at: datetime
    ) -> None:
        """将指定版本标记为 PUBLISHED 并记录发布时间（发布时版本转正）。"""
        stmt = (
            update(MetricVersion)
            .where(
                MetricVersion.metric_id == metric_id,
                MetricVersion.version == version,
            )
            .values(status="PUBLISHED", published_at=published_at)
        )
        await self._db.execute(stmt)
