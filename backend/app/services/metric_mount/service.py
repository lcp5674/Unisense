"""指标挂载实体服务（OneData 挂载层，TD §4.2 dataset_metric）。

核心规则：
1. 仅派生指标可挂载（原子=逻辑度量不绑物理表；复合=派生组合，不直接挂表）。
2. 一个派生指标一个挂载点（uk_mount_metric 唯一约束）——首期语义。
3. 粒度/周期/域在挂载上承载（granularity 从 metric 下沉到此）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.exceptions import ConflictError, NotFoundError, UnisenseError
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
        if await self._repo.get_by_metric(data.metric_id) is not None:
            raise ConflictError(
                f"指标 {metric.metric_code} 已有挂载点（一个派生指标一个挂载）",
                error_code="MOUNT_EXISTS",
            )
        mount = MetricMount(
            metric_id=data.metric_id,
            source_table=data.source_table,
            source_column=data.source_column,
            granularity=data.granularity,
            default_period=data.default_period,
            domain=data.domain,
        )
        saved = await self._repo.save(mount)
        # C2（第七轮）：回填 metric.granularity 冗余展示列（对齐 semantic 创建路径），
        # 否则详情页「粒度」读主表列而挂载卡读 mount，出现同页矛盾。
        if data.granularity:
            await self._repo.update_metric_granularity(data.metric_id, data.granularity)
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
            # C2（第七轮）：挂载粒度变更同步回填主表冗余列
            await self._repo.update_metric_granularity(mount.metric_id, data.granularity)
        if data.default_period is not None:
            mount.default_period = data.default_period
        if data.domain is not None:
            mount.domain = data.domain
        await self._repo.commit()
        return mount

    async def delete_mount(self, mount_id: int) -> None:
        mount = await self.get_mount(mount_id)
        await self._repo.soft_delete(mount.id)
        # C2（第七轮）：解除挂载 → 清空指标粒度冗余列（挂载不在则粒度无权威来源），
        # 避免详情页残留「旧粒度 + 未挂载」矛盾。
        await self._repo.clear_metric_granularity(mount.metric_id)
        await self._repo.commit()

    async def _require_metric(self, metric_id: int) -> Metric:
        stmt = select(Metric).where(Metric.id == metric_id)
        metric = (await self._session.execute(stmt)).scalar_one_or_none()
        if metric is None:
            raise NotFoundError(f"指标不存在: {metric_id}")
        return metric
