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

#: 允许编辑挂载的指标状态白名单（B4 审查修复）：废弃/数据源下线指标禁止改删挂载，
#: 与 semantic.update_metric 的状态守卫对齐（DEPRECATED/DATA_SOURCE_DROPPED 明确禁更）。
_EDITABLE_STATUSES = ("DRAFT", "REVIEW", "PUBLISHED")


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
        # 指标域一致性（用户级越权修复）：挂载 domain 必须等于指标 domain——
        # 否则域管理员可用「本域挂载 + 跨域指标」组合绕过域作用域守卫（_assert_domain_scope
        # 只校验挂载行 domain），变相操作跨域指标。
        if metric.domain != data.domain:
            raise UnisenseError(
                f"挂载业务域 {data.domain} 与指标所属域 {metric.domain} 不一致，"
                f"请以指标域为准",
                error_code="MOUNT_DOMAIN_MISMATCH",
            )
        mount = MetricMount(
            metric_id=data.metric_id,
            source_table=data.source_table,
            source_column=data.source_column,
            granularity=data.granularity,
            granularity_dims=data.granularity_dims,
            default_period=data.default_period,
            domain=data.domain,
            business_filter=data.business_filter,
            product_owner_id=data.product_owner_id,
            tech_owner_id=data.tech_owner_id,
            dw_developer_id=data.dw_developer_id,
            product_owner_name=data.product_owner_name,
            tech_owner_name=data.tech_owner_name,
            dw_developer_name=data.dw_developer_name,
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

    async def get_mount_with_metric(
        self, mount_id: int
    ) -> tuple[MetricMount, Metric | None] | None:
        """取挂载并 LEFT JOIN 指标（详情/编辑用，供可见性校验）。"""
        return await self._repo.get_with_metric(mount_id)

    async def get_mount_by_metric(self, metric_id: int) -> MetricMount | None:
        """按指标取默认变体挂载（多变体下取 default_period 优先/id 最小行）。

        2026-08-27 放开一指标多挂载后「取唯一挂载行」语义失效，统一走
        ``get_default_mount`` 默认变体解析，避免 MultipleResultsFound。
        """
        return await self._repo.get_default_mount(metric_id)

    async def list_mounts(
        self,
        metric_id: int | None,
        domain: str | None,
        *,
        page: int = 1,
        page_size: int = 20,
        visible_actor_id: int | None = None,
        visible_role: str | None = None,
        visible_user_domains: list[str] | None = None,
    ) -> tuple[list[tuple[MetricMount, Metric | None]], int]:
        limit = min(max(page_size, 1), 200)
        offset = (max(page, 1) - 1) * limit
        return await self._repo.list(
            metric_id,
            domain,
            limit=limit,
            offset=offset,
            visible_actor_id=visible_actor_id,
            visible_role=visible_role,
            visible_user_domains=visible_user_domains,
        )

    async def update_mount(self, mount_id: int, data: MetricMountUpdate) -> MetricMount:
        mount = await self.get_mount(mount_id)
        metric = await self._require_metric(mount.metric_id)
        # B4（审查修复）：非 DRAFT/REVIEW/PUBLISHED 状态禁止改挂载——废弃/数据源下线
        # 指标的挂载粒度/源表/度量列/域等不可直改直删（与 semantic.update_metric
        # 状态守卫对齐，此前仅拦 PUBLISHED，DEPRECATED 指标挂载可被悄悄改动）。
        if metric.status not in _EDITABLE_STATUSES:
            raise UnisenseError(
                f"指标状态 {metric.status} 禁止修改挂载（仅 DRAFT/REVIEW/PUBLISHED 可编辑）",
                error_code="MOUNT_EDIT_FORBIDDEN",
            )
        # 指标域一致性（与 create 同源）：不允许把挂载域改成与指标域不一致——
        # 否则域管理员可把挂载域改成本域绕过域作用域守卫，变相操作跨域指标。
        if data.domain is not None and data.domain != metric.domain:
            raise UnisenseError(
                f"挂载业务域 {data.domain} 与指标所属域 {metric.domain} 不一致，"
                f"请以指标域为准",
                error_code="MOUNT_DOMAIN_MISMATCH",
            )
        # 破坏性口径变更（源表/粒度/粒度维度变化）：PUBLISHED 须经消费方确认流，
        # REVIEW 须经指标编辑接口（编辑即撤回 DRAFT，评审人知情）——挂载端点一律
        # 禁止绕过确认流直接生效。
        # 仅实际变更拦截；传相同值或改非破坏字段（度量列/周期/域/限定）放行。
        disruptive = (
            (data.source_table is not None and data.source_table != mount.source_table)
            or (data.granularity is not None and data.granularity != mount.granularity)
            or (
                data.granularity_dims is not None
                and data.granularity_dims != (mount.granularity_dims or [])
            )
        )
        if metric.status in ("PUBLISHED", "REVIEW") and disruptive:
            raise UnisenseError(
                (
                    "已发布指标修改挂载粒度/粒度维度/源表属破坏性变更，须经消费方确认"
                    if metric.status == "PUBLISHED"
                    else "评审中指标修改挂载粒度/粒度维度/源表须撤回评审——"
                    "请通过指标详情「编辑」提交（变更原因必填，确认后生效）"
                ),
                error_code="MOUNT_UPDATE_REQUIRES_CONFIRMATION",
            )
        if data.source_table is not None:
            mount.source_table = data.source_table
        if data.source_column is not None:
            mount.source_column = data.source_column
        if data.granularity is not None:
            mount.granularity = data.granularity
            # 挂载粒度变更同步回填默认变体粒度（冗余列）
            await self._refresh_granularity(mount.metric_id)
        if data.granularity_dims is not None:
            # 完整替换语义：None=不更新，[]=清空（纯时间粒度），[..]=设置
            mount.granularity_dims = data.granularity_dims or None
            # 粒度维度属唯一性构成，变更与改粒度同级——回填默认变体粒度
            await self._refresh_granularity(mount.metric_id)
        if data.default_period is not None:
            mount.default_period = data.default_period
            # 默认周期变化可能改变"默认变体"选择，回填默认变体粒度
            await self._refresh_granularity(mount.metric_id)
        if data.domain is not None:
            mount.domain = data.domain
        if data.business_filter is not None:
            mount.business_filter = data.business_filter
        # 变体级责任方（治理属性，非破坏性；空值不覆盖——缺省继承指标级语义）
        if data.product_owner_id is not None:
            mount.product_owner_id = data.product_owner_id
        if data.tech_owner_id is not None:
            mount.tech_owner_id = data.tech_owner_id
        if data.dw_developer_id is not None:
            mount.dw_developer_id = data.dw_developer_id
        if data.product_owner_name is not None:
            mount.product_owner_name = data.product_owner_name
        if data.tech_owner_name is not None:
            mount.tech_owner_name = data.tech_owner_name
        if data.dw_developer_name is not None:
            mount.dw_developer_name = data.dw_developer_name
        await self._repo.commit()
        return mount

    async def delete_mount(self, mount_id: int) -> None:
        mount = await self.get_mount(mount_id)
        metric = await self._require_metric(mount.metric_id)
        # B4（审查修复）：非 DRAFT/REVIEW/PUBLISHED 状态禁止删挂载（废弃/下线指标
        # 不得再解除挂载，退役语义由指标生命周期管理）。
        if metric.status not in _EDITABLE_STATUSES:
            raise UnisenseError(
                f"指标状态 {metric.status} 禁止删除挂载（仅 DRAFT/REVIEW/PUBLISHED 可编辑）",
                error_code="MOUNT_EDIT_FORBIDDEN",
            )
        # 已发布/评审中指标解除挂载 = 破坏性口径变更（消费方取数底座变化，等同
        # 删源表/改粒度）：禁止绕过确认流直接软删——须经指标更新接口提交
        # （mounts 去行 + 变更原因），由 semantic._sync_mounts 判定破坏性走
        # PENDING_VERSION 消费方确认（14 天）或评审撤回。
        if metric.status in ("PUBLISHED", "REVIEW"):
            raise UnisenseError(
                (
                    "已发布指标解除挂载属破坏性变更，须经消费方确认"
                    if metric.status == "PUBLISHED"
                    else "评审中指标解除挂载须撤回评审——请通过指标详情「编辑」提交"
                ),
                error_code="MOUNT_DELETE_REQUIRES_CONFIRMATION",
            )
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
        # T3（审查修复）：过滤软删指标——已删指标不可参与挂载/回填（此前不过滤
        # deleted_at，已删指标可被"复活"参与挂载并回填冗余粒度列）
        stmt = select(Metric).where(
            Metric.id == metric_id,
            Metric.deleted_at.is_(None),
        )
        metric = (await self._session.execute(stmt)).scalar_one_or_none()
        if metric is None:
            raise NotFoundError(f"指标不存在: {metric_id}")
        return metric
