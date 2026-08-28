"""指标挂载实体 Repository（OneData 挂载层，TD §4.2 dataset_metric）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric import Metric
from app.models.metric_mount import MetricMount


class MetricMountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, obj: MetricMount) -> MetricMount:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def get(self, mount_id: int) -> MetricMount | None:
        stmt = select(MetricMount).where(
            MetricMount.id == mount_id, MetricMount.deleted_at.is_(None)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_by_metric(self, metric_id: int) -> list[MetricMount]:
        """按指标列出全部挂载行（多变体；按 id 升序，稳定默认变体取行）。"""
        stmt = (
            select(MetricMount)
            .where(MetricMount.metric_id == metric_id, MetricMount.deleted_at.is_(None))
            .order_by(MetricMount.id.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

    async def get_default_mount(self, metric_id: int) -> MetricMount | None:
        """默认变体解析（混合渐进消费契约 a 兜底）：default_period 非空行优先
        （多行取 id 最小），全空则取 id 最小行。多挂载下消费缺省用默认变体，
        显式 variant 由上层按匹配规则覆盖。"""
        mounts = await self.list_by_metric(metric_id)
        if not mounts:
            return None
        for m in mounts:
            if m.default_period:
                return m
        return mounts[0]

    async def list(
        self,
        metric_id: int | None,
        domain: str | None,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[tuple[MetricMount, Metric | None]], int]:
        """分页列出挂载并 LEFT JOIN Metric 取指标信息，返回 (列表, total)。

        total 用独立 count（不含 JOIN），与列表共用同一过滤条件，保证分页一致性。
        """
        conditions = [MetricMount.deleted_at.is_(None)]
        if metric_id is not None:
            conditions.append(MetricMount.metric_id == metric_id)
        if domain:
            conditions.append(MetricMount.domain == domain)
        count_stmt = (
            select(func.count()).select_from(MetricMount).where(*conditions)
        )
        total = (await self._session.execute(count_stmt)).scalar() or 0
        stmt = (
            select(MetricMount, Metric)
            .outerjoin(Metric, Metric.id == MetricMount.metric_id)
            .where(*conditions)
            .order_by(MetricMount.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(mount, metric) for mount, metric in rows], int(total)

    async def soft_delete(self, mount_id: int) -> None:
        """软删除挂载（deleted_at 置位，列表过滤已排除）。"""
        await self._session.execute(
            update(MetricMount)
            .where(MetricMount.id == mount_id)
            .values(deleted_at=datetime.now(UTC))
        )

    async def update_metric_granularity(self, metric_id: int, granularity: str | None) -> None:
        """回填 metric.granularity 冗余展示列（对齐 semantic 创建路径，C2 第七轮）。

        metric.granularity 是「冗余回填」列（供列表/详情展示），挂载粒度变更须同步，
        否则详情页出现「挂载卡新粒度 vs 主表旧粒度」同页矛盾。
        T15（审查修复）：仅回填未软删指标（与 _require_metric 过滤一致），
        防止已删指标被挂载变更"复活"回填。
        """
        await self._session.execute(
            update(Metric)
            .where(Metric.id == metric_id, Metric.deleted_at.is_(None))
            .values(granularity=granularity)
        )

    async def clear_metric_granularity(self, metric_id: int) -> None:
        """解除挂载后清空 metric.granularity 冗余列（挂载不在则粒度无权威来源）。"""
        await self._session.execute(
            update(Metric)
            .where(Metric.id == metric_id, Metric.deleted_at.is_(None))
            .values(granularity=None)
        )

    async def commit(self) -> None:
        await self._session.commit()
