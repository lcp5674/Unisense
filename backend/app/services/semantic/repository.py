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
from app.core.exceptions import SystemError as AppSystemError
from app.models.metric import Metric, MetricVersion
from app.models.metric_health import MetricHealthScore
from app.models.metric_version import PendingVersionConfirmation


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
            # LIKE 通配符转义（对齐 FR-035：% 和 _ 须转义）
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(
                (Metric.metric_code.contains(escaped)) | (Metric.name.contains(escaped))
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
        if updated is None:
            raise AppSystemError(
                f"乐观锁更新后指标 {metric_id} 不存在（数据一致性异常）",
                error_code="INTERNAL_ERROR",
            )
        await self._db.refresh(updated)
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

        捕获唯一键冲突（metric_id, version），转换为 ConflictError，
        避免将 IntegrityError 暴露为 500（对齐 FR-036）。

        Args:
            version: 版本 ORM 对象。

        Returns:
            创建后的版本。

        Raises:
            ConflictError: 版本号冲突。
        """
        self._db.add(version)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            await self._db.rollback()
            raise ConflictError(
                f"指标版本已存在: metric_id={version.metric_id}, version={version.version}",
                error_code="CONFLICT",
            ) from exc
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
            .values(status="PUBLISHED", published_at=published_at, effective_at=published_at)
        )
        await self._db.execute(stmt)

    # ---- PENDING_VERSION 确认相关 ----

    async def save_pending_confirmation(
        self, confirmation: PendingVersionConfirmation
    ) -> PendingVersionConfirmation:
        """保存 PENDING_VERSION 确认记录。"""
        self._db.add(confirmation)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            await self._db.rollback()
            raise ConflictError(
                f"确认记录已存在: metric_id={confirmation.metric_id}, "
                f"version={confirmation.version}, consumer_id={confirmation.consumer_id}",
                error_code="CONFLICT",
            ) from exc
        await self._db.refresh(confirmation)
        return confirmation

    async def get_pending_confirmations(
        self, metric_id: int, version: int
    ) -> list[PendingVersionConfirmation]:
        """获取指定版本的 PENDING 确认记录列表。"""
        result = await self._db.execute(
            select(PendingVersionConfirmation).where(
                PendingVersionConfirmation.metric_id == metric_id,
                PendingVersionConfirmation.version == version,
                PendingVersionConfirmation.deleted_at.is_(None),
            )
        )
        return list(result.scalars().all())

    async def update_confirmation_status(
        self,
        confirmation_id: int,
        status: str,
        reason: str | None = None,
    ) -> None:
        """更新确认记录状态。"""
        from datetime import UTC

        values: dict[str, Any] = {
            "status": status,
            "confirmed_at": datetime.now(UTC),
        }
        if reason is not None:
            values["reason"] = reason

        stmt = (
            update(PendingVersionConfirmation)
            .where(PendingVersionConfirmation.id == confirmation_id)
            .values(**values)
        )
        await self._db.execute(stmt)

    async def get_timeout_pending_confirmations(self) -> list[PendingVersionConfirmation]:
        """获取超时未确认的 PENDING 确认记录。"""
        from datetime import UTC

        now = datetime.now(UTC)
        result = await self._db.execute(
            select(PendingVersionConfirmation).where(
                PendingVersionConfirmation.status == "PENDING",
                PendingVersionConfirmation.deadline < now,
                PendingVersionConfirmation.deleted_at.is_(None),
            )
        )
        return list(result.scalars().all())

    # ---- 健康度评分相关 ----

    async def save_health_score(self, score: MetricHealthScore) -> MetricHealthScore:
        """保存健康度评分（upsert）。"""
        existing = await self._db.execute(
            select(MetricHealthScore).where(
                MetricHealthScore.metric_id == score.metric_id,
                MetricHealthScore.deleted_at.is_(None),
            )
        )
        existing_score = existing.scalar_one_or_none()
        if existing_score is not None:
            # 更新
            stmt = (
                update(MetricHealthScore)
                .where(MetricHealthScore.id == existing_score.id)
                .values(
                    score=score.score,
                    level=score.level,
                    completeness_score=score.completeness_score,
                    activity_score=score.activity_score,
                    quality_score=score.quality_score,
                    owner_response_score=score.owner_response_score,
                    lineage_coverage_score=score.lineage_coverage_score,
                    missing_dimensions=score.missing_dimensions,
                    calculated_at=score.calculated_at,
                )
            )
            await self._db.execute(stmt)
            await self._db.refresh(existing_score)
            return existing_score
        else:
            self._db.add(score)
            await self._db.flush()
            await self._db.refresh(score)
            return score

    async def get_health_score(self, metric_id: int) -> MetricHealthScore | None:
        """获取指标健康度评分。"""
        result = await self._db.execute(
            select(MetricHealthScore).where(
                MetricHealthScore.metric_id == metric_id,
                MetricHealthScore.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_critical_metrics(self, level: str = "CRITICAL") -> list[Metric]:
        """列出指定健康度级别的指标。"""
        result = await self._db.execute(
            select(Metric)
            .join(MetricHealthScore, MetricHealthScore.metric_id == Metric.id)
            .where(
                MetricHealthScore.level == level,
                Metric.deleted_at.is_(None),
                MetricHealthScore.deleted_at.is_(None),
            )
        )
        return list(result.scalars().all())

    # ---- Dashboard 聚合 ----

    async def aggregate_dashboard(
        self, domain: str | None = None, owner_id: int | None = None
    ) -> dict[str, Any]:
        """单次聚合 SQL 查询仪表盘数据（对齐 FR-043）。

        使用 CASE WHEN + GROUP BY 条件聚合，替代 5 次独立查询。
        加 deleted_at IS NULL 过滤。

        Args:
            domain: 域过滤。
            owner_id: Owner 过滤。

        Returns:
            聚合结果 dict。
        """
        conditions: list[ColumnElement[bool]] = [Metric.deleted_at.is_(None)]
        if domain:
            conditions.append(Metric.domain == domain)
        if owner_id:
            conditions.append(Metric.owner_id == owner_id)

        # 单次查询: total + by_status + by_tier + by_domain + pii_count
        stmt = select(
            func.count().label("total"),
            func.sum(
                func.if_(Metric.pii_flag.is_(True), 1, 0)
            ).label("pii_count"),
        ).where(*conditions)

        result = await self._db.execute(stmt)
        row = result.one()
        total = row.total or 0
        pii_count = row.pii_count or 0

        # 按状态分组
        status_stmt = (
            select(Metric.status, func.count().label("cnt"))
            .where(*conditions)
            .group_by(Metric.status)
        )
        status_rows = (await self._db.execute(status_stmt)).all()
        by_status = {row[0]: row[1] for row in status_rows}

        # 按分级分组
        tier_stmt = (
            select(Metric.metric_tier, func.count().label("cnt"))
            .where(*conditions)
            .group_by(Metric.metric_tier)
        )
        tier_rows = (await self._db.execute(tier_stmt)).all()
        by_tier = {row[0]: row[1] for row in tier_rows}

        # 按域分组
        domain_conditions: list[ColumnElement[bool]] = [Metric.deleted_at.is_(None)]
        if owner_id:
            domain_conditions.append(Metric.owner_id == owner_id)
        domain_stmt = (
            select(Metric.domain, func.count().label("cnt"))
            .where(*domain_conditions)
            .group_by(Metric.domain)
        )
        domain_rows = (await self._db.execute(domain_stmt)).all()
        by_domain = {row[0]: row[1] for row in domain_rows}

        return {
            "total": total,
            "by_status": by_status,
            "by_tier": by_tier,
            "by_domain": by_domain,
            "pii_count": pii_count,
            "pii_ratio": round(pii_count / max(total, 1), 4),
        }
