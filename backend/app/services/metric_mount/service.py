"""指标挂载实体服务（OneData 挂载层，TD §4.2 dataset_metric）。

核心规则：
1. 仅派生指标可挂载（原子=逻辑度量不绑物理表；复合=派生组合，不直接挂表）。
2. 一指标可挂多个挂载点（多变体：粒度/业务限定/周期组合，2026-08-27 放开
   uk_mount_metric 唯一约束）——每个变体一条挂载行。
3. 粒度/周期/域/业务限定在挂载上承载（granularity 从 metric 下沉到此）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.exceptions import NotFoundError, UnisenseError
from app.models.metric import Metric
from app.models.metric_mount import MetricMount
from app.services.metric_mount.repository import MetricMountRepository
from app.services.metric_mount.schemas import MetricMountCreate, MetricMountUpdate

#: 可挂载的指标类型（原子不挂表、复合不直接挂表）
_MOUNTABLE_TYPES = ("derived",)


class MetricMountService(BaseService):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self._session = session
        self._repo = MetricMountRepository(session)

    async def create_mount(self, data: MetricMountCreate) -> MetricMount:
        metric = await self._require_metric(data.metric_id)
        if metric.type not in _MOUNTABLE_TYPES:
            raise UnisenseError(
                f"仅派生指标可挂载物理表，当前类型 {metric.type}（原子/复合不挂载）",
                error_code="INVALID_MOUNT_TARGET",
            )
        mount = MetricMount(
            metric_id=data.metric_id,
            source_table=data.source_table,
            source_column=data.source_column,
            granularity=data.granularity,
            default_period=data.default_period,
            domain=data.domain,
            business_filter=data.business_filter,
        )
        saved = await self._repo.save(mount)
        # 多变体：新增挂载后回填默认变体粒度（冗余展示列），对齐 semantic 创建路径
        await self._refresh_granularity(data.metric_id)
        return saved

    async def get_mount(self, mount_id: int) -> MetricMount:
        mount = await self._repo.get(mount_id)
        if mount is None:
            raise NotFoundError(f"挂载不存在: {mount_id}")
        return mount

    async def get_mount_by_metric(self, metric_id: int) -> MetricMount | None:
        return await self._repo.get_by_metric(metric_id)

    async def list_mounts(
        self,
        metric_id: int | None,
        domain: str | None,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[tuple[MetricMount, Metric | None]], int]:
        limit = min(max(page_size, 1), 200)
        offset = (max(page, 1) - 1) * limit
        return await self._repo.list(metric_id, domain, limit=limit, offset=offset)

    async def update_mount(self, mount_id: int, data: MetricMountUpdate) -> MetricMount:
        mount = await self.get_mount(mount_id)
        if data.source_table is not None:
            mount.source_table = data.source_table
        if data.source_column is not None:
            mount.source_column = data.source_column
        if data.granularity is not None:
            mount.granularity = data.granularity
            # 挂载粒度变更同步回填默认变体粒度（冗余列）
            await self._refresh_granularity(mount.metric_id)
        if data.default_period is not None:
            mount.default_period = data.default_period
            # 默认周期变化可能改变"默认变体"选择，回填默认变体粒度
            await self._refresh_granularity(mount.metric_id)
        if data.domain is not None:
            mount.domain = data.domain
        if data.business_filter is not None:
            mount.business_filter = data.business_filter
        await self._repo.commit()
        return mount

    async def delete_mount(self, mount_id: int) -> None:
        mount = await self.get_mount(mount_id)
        await self._repo.soft_delete(mount.id)
        # 多变体：删除一行后回填默认变体粒度；无剩余挂载则清空（挂载不在则粒度无权威来源）
        await self._refresh_granularity(mount.metric_id)
        await self._repo.commit()

    async def _refresh_granularity(self, metric_id: int) -> None:
        """挂载增删/变更后回填默认变体粒度（冗余展示列，对齐 semantic 创建路径）。

        默认变体 = default_period 非空行优先（多行取首个），否则 id 最小行；
        无剩余挂载 → 清空 metric.granularity（避免详情页残留「旧粒度 + 未挂载」矛盾）。
        """
        mounts = await self._repo.list_by_metric(metric_id)
        if not mounts:
            await self._repo.clear_metric_granularity(metric_id)
            return
        default_mount = next((m for m in mounts if m.default_period), mounts[0])
        await self._repo.update_metric_granularity(metric_id, default_mount.granularity)

    async def _require_metric(self, metric_id: int) -> Metric:
        stmt = select(Metric).where(Metric.id == metric_id)
        metric = (await self._session.execute(stmt)).scalar_one_or_none()
        if metric is None:
            raise NotFoundError(f"指标不存在: {metric_id}")
        return metric
