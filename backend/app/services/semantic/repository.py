"""指标数据访问层（Repository）。

对齐 DEV_GUIDE §8b.2（Repository 层：数据访问，禁止 service 直接写 ORM 查询）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.exceptions import SystemError as AppSystemError
from app.models.conflict import Conflict
from app.models.consume import MetricValueSnapshot
from app.models.data_source import DataSource, DBCatalog
from app.models.dimension import Dimension, MetricDimension
from app.models.lineage import LineageEdge
from app.models.metric import Metric
from app.models.metric_health import MetricHealthScore
from app.models.metric_mount import MetricMount
from app.models.metric_template import MetricTemplate
from app.models.metric_version import MetricVersion, PendingVersionConfirmation
from app.models.quality import QualityEvent
from app.models.system_dict import SystemDict
from app.models.term import Term
from app.models.user import User


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

    async def get_user_display_names(self, user_ids: set[int]) -> dict[int, str]:
        """批量查询用户显示名（P2-14 对比治理：owner_id → 责任人姓名映射）。

        Args:
            user_ids: 用户 ID 集合（空集直接返回空映射）。

        Returns:
            ``{user_id: display_name}``；display_name 缺失时回退 username，
            两者皆缺回退 ``用户#{id}``（确保映射永远可读，不返回裸数字）。
        """
        if not user_ids:
            return {}
        from app.models.user import User

        rows = (
            await self._db.execute(
                select(User.id, User.display_name, User.username).where(User.id.in_(user_ids))
            )
        ).all()
        return {
            uid: (display_name or username or f"用户#{uid}")
            for uid, display_name, username in rows
        }

    async def get_archived_by_code(self, metric_code: str) -> Metric | None:
        """根据编码查询已软删指标（含 deleted_at 置位的作废记录）。

        仅供详情直访的「友好作废引导」使用：命中仲裁作废（软删 + successor）
        的指标时返回其指针信息，避免对历史链接直接给出裸 404。

        Args:
            metric_code: 指标编码。

        Returns:
            指标对象（含软删记录）或 None。
        """
        result = await self._db.execute(
            select(Metric).where(Metric.metric_code == metric_code)
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

    # ---- FULLTEXT 关键词搜索（审查 P4：LIKE 前导通配符全表扫 → FULLTEXT 加速）----
    # 进程级能力标志：None=未探测 / True=可用 / False=不可用（SQLite 或索引缺失 → 回退 LIKE）。
    _fulltext_ok: bool | None = None

    @staticmethod
    def _metric_search_like(kw: str) -> Any:
        """LIKE 回退条件（autoescape 防 %/_ 模糊放大；匹配编码/名称/描述）。"""
        return (
            (Metric.metric_code.contains(kw, autoescape=True))
            | (Metric.name.contains(kw, autoescape=True))
            | (Metric.description.contains(kw, autoescape=True))
        )

    @staticmethod
    def _metric_search_fulltext(kw: str) -> Any:
        """FULLTEXT 短语条件（ngram 2-gram，短语模式语义≈子串）。"""
        phrase = kw.strip().replace('"', "")
        return text(
            "MATCH(metric.name, metric.description, metric.metric_code) "
            "AGAINST (:kw IN BOOLEAN MODE)"
        ).bindparams(kw=f'"{phrase}"')

    async def _metric_search_cond(self, kw: str) -> Any:
        """选择关键词条件：MySQL + ≥2 字符 → FULLTEXT（一次进程级探测，失败永久回退 LIKE）；
        其余 → LIKE。"""
        kw = kw.strip()
        if not kw:
            return None
        if self._fulltext_ok is False or len(kw) < 2:
            return self._metric_search_like(kw)
        try:
            bind = self._session.get_bind()
            if bind.dialect.name != "mysql":
                type(self)._fulltext_ok = False
                return self._metric_search_like(kw)
        except Exception:  # noqa: BLE001 - 探测失败回退 LIKE
            return self._metric_search_like(kw)
        return self._metric_search_fulltext(kw)

    async def list_metrics(
        self,
        *,
        deleted: bool = False,
        domain: str | None = None,
        status: str | None = None,
        exclude_statuses: list[str] | None = None,
        metric_tier: str | None = None,
        metric_type: str | None = None,
        keyword: str | None = None,
        owner_id: int | None = None,
        approver_id: int | None = None,
        reviewed_by: int | None = None,
        pii_flag: bool | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        updated_after: datetime | None = None,
        updated_before: datetime | None = None,
        batch_id: str | None = None,
        has_downstream: bool | None = None,
        health_level: str | None = None,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        offset: int = 0,
        limit: int = 20,
        visible_actor_id: int | None = None,
        visible_role: str | None = None,
        visible_user_domain: str | None = None,
    ) -> tuple[list[Metric], int]:
        """分页查询指标列表。

        Args:
            domain: 域过滤。
            status: 状态过滤。
            metric_tier: 分级过滤。
            metric_type: 指标类型过滤（atomic/derived/composite）。
            keyword: 关键词搜索（metric_code/name）。
            owner_id: 责任人（Owner）ID 过滤。
            pii_flag: PII 过滤（True 仅 PII，False 仅非 PII，None 不过滤）。
            created_after/created_before: 创建时间区间过滤（生命周期快筛）。
            updated_after/updated_before: 更新时间区间过滤（生命周期快筛）。
            health_level: 健康度档位过滤（EXCELLENT/GOOD/WARNING/CRITICAL）；无健康评分
                的指标不命中任何档位（与目录页健康列空值语义一致）。
            sort_by: 排序字段（白名单映射，防注入）。
            sort_order: 排序方向（asc/desc）。
            offset: 偏移量。
            limit: 每页数量。
            visible_actor_id: 读路径行级隔离（P0-3）——非管理角色的可见性过滤：
                仅公开状态（PUBLISHED/EXPERIMENTAL/DEPRECATED）+ 本人 Owner/副 Owner
                （DRAFT/REVIEW）+ 评审角色（REVIEW 待审）可见；其余不可见。
                管理角色（platform_admin/domain_admin）传 None 即不加过滤。
            visible_role: 调用者角色（配合 visible_actor_id 判定 reviewer 放行）。
            visible_user_domain: 调用者所属域（配合 visible_role=reviewer 判定
                domain 指派的同域评审组可见性）。

        Returns:
            (指标列表, 总数)。
        """
        conditions: list[ColumnElement[bool]] = (
            [Metric.deleted_at.is_not(None)] if deleted else [Metric.deleted_at.is_(None)]
        )
        # P0-3 读路径行级隔离：非管理角色只可见公开资产 + 本人负责的未发布资产。
        # 指标目录是数据资产公开目录（已发布/灰度/废弃均可被消费方发现），但
        # 未发布草稿/审核中是指标 Owner 的私有工作区——他人不得窥探（域隔离在
        # 写路径已有，读路径此前完全缺失，任意 viewer 可翻到 DRAFT 完整口径）。
        if (
            visible_actor_id is not None
            and visible_role is not None
            and visible_role not in ("platform_admin", "domain_admin")
        ):
            visibility: list[ColumnElement[bool]] = [
                Metric.status.in_(("PUBLISHED", "EXPERIMENTAL", "DEPRECATED")),
                Metric.owner_id == visible_actor_id,
                Metric.backup_owner_id == visible_actor_id,
            ]
            if visible_role == "reviewer":
                # 仅被指派评审人可看待审（REVIEW）指标（TD §13 治理闭环）：
                # - reviewer_type=user：仅 reviewer_id 指定的用户可见（评审工作台/目录
                #   不泄露他人待审项）
                # - reviewer_type=domain：仅同域评审组可见（reviewer_domain 与用户域一致）
                # - 未指派：由域管理员兜底评审（reviewer 角色不可见，不在此放行）
                visibility.append(
                    and_(
                        Metric.status == "REVIEW",
                        or_(
                            and_(
                                Metric.reviewer_type == "user",
                                Metric.reviewer_id == visible_actor_id,
                            ),
                            and_(
                                Metric.reviewer_type == "domain",
                                Metric.reviewer_domain == visible_user_domain,
                            ),
                        ),
                    )
                )
            conditions.append(or_(*visibility))
        if domain:
            conditions.append(Metric.domain == domain)
        if status:
            conditions.append(Metric.status == status)
        # 排除状态（资产地图「指标总数」下钻：与 metric_summary by_domain 统计口径
        # 一致排除 DRAFT/DEPRECATED，避免明细多出草稿/已废弃造成总数与明细不一致）
        if exclude_statuses:
            conditions.append(Metric.status.not_in(exclude_statuses))
        if metric_tier:
            conditions.append(Metric.metric_tier == metric_tier)
        if metric_type:
            conditions.append(Metric.type == metric_type)
        if owner_id is not None:
            conditions.append(Metric.owner_id == owner_id)
        if approver_id is not None:
            conditions.append(Metric.approver_id == approver_id)
        if reviewed_by is not None:
            # 评审历史完整视图：通过(approver_id) 或 驳回(reject_reviewer_id) 任一命中
            conditions.append(
                or_(Metric.approver_id == reviewed_by, Metric.reject_reviewer_id == reviewed_by)
            )
        if pii_flag is not None:
            conditions.append(Metric.pii_flag.is_(pii_flag))
        if created_after is not None:
            conditions.append(Metric.created_at >= created_after)
        if created_before is not None:
            conditions.append(Metric.created_at <= created_before)
        # 批次过滤（P2）：按批量注册批次 ID 精确匹配（SQL/宽表批量创建的指标）
        if batch_id:
            conditions.append(Metric.batch_id == batch_id)
        # 下游引用过滤（批量废弃前按引用收敛）：语义与 downstream-check 一致——
        # 活跃边（deleted_at 置位 / stale 不计）中 source_node = "metric:{code}"
        # 且 edge_type 为 DERIVED_FROM（派生该指标的派生指标）或 CONSUMED_BY（消费方）。
        if has_downstream is not None:
            ref_subq = (
                select(LineageEdge.source_node)
                .where(
                    LineageEdge.deleted_at.is_(None),
                    LineageEdge.stale.is_(False),
                    LineageEdge.edge_type.in_(["DERIVED_FROM", "CONSUMED_BY"]),
                )
                .distinct()
            )
            prefixed_code = func.concat("metric:", Metric.metric_code)
            if has_downstream:
                conditions.append(prefixed_code.in_(ref_subq))
            else:
                conditions.append(~prefixed_code.in_(ref_subq))
        if updated_after is not None:
            conditions.append(Metric.updated_at >= updated_after)
        if updated_before is not None:
            conditions.append(Metric.updated_at <= updated_before)
        # 健康度档位过滤（仪表盘/可观测中心分布下钻）：命中 metric_health_score.level 的
        # 指标。用 IN 子查询而非 JOIN，避免影响主查询基数（count/list 语义一致）；
        # 无健康评分记录（未评分/评分任务未跑）的指标不命中任何档位。
        if health_level:
            conditions.append(
                Metric.id.in_(
                    select(MetricHealthScore.metric_id).where(
                        MetricHealthScore.level == health_level
                    )
                )
            )
        if keyword:
            # P4（审查修复）：关键词优先走 FULLTEXT（MySQL ngram，≥2 字符），
            # 避免 LIKE '%kw%' 前导通配符致 B-tree 索引失效 → count+列表 2 次全表扫；
            # 非 MySQL/<2 字符/探测失败回退 LIKE（autoescape 防 %/_ 模糊放大）。
            # keyword 同时匹配编码/名称/描述（对齐"编码/名称/描述"搜索契约）。
            cond = await self._metric_search_cond(keyword)
            if cond is not None:
                conditions.append(cond)

        # 总数
        count_stmt = select(func.count()).select_from(Metric).where(*conditions)
        total = (await self._db.execute(count_stmt)).scalar() or 0

        # 排序字段白名单（防 SQL 注入）
        sort_columns: dict[str, Any] = {
            "updated_at": Metric.updated_at,
            "created_at": Metric.created_at,
            "version": Metric.version,
            "metric_code": Metric.metric_code,
            "name": Metric.name,
        }
        sort_col = sort_columns.get(sort_by, Metric.updated_at)
        # 排序稳定性：单字段排序在同值（如同日批量创建/编辑的 updated_at）时翻页会重复/遗漏，
        # 追加主键 id 作为次级排序（与主排序同向），保证分页记录不重不漏（工业级排序稳定性）。
        asc_order = sort_order == "asc"
        if asc_order:
            sort_order_clause = (sort_col.asc(), Metric.id.asc())
        else:
            sort_order_clause = (sort_col.desc(), Metric.id.desc())

        # 列表
        stmt = (
            select(Metric)
            .where(*conditions)
            .order_by(*sort_order_clause)
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

    async def restore_metric(self, metric_id: int) -> None:
        """恢复软删指标：清除 deleted_at（回收站恢复）。

        Args:
            metric_id: 指标 ID。

        Raises:
            NotFoundError: 指标不存在或未处于软删状态。
        """
        stmt = (
            update(Metric)
            .where(Metric.id == metric_id, Metric.deleted_at.is_not(None))
            .values(deleted_at=None)
        )
        result = await self._db.execute(stmt)
        if result.rowcount == 0:  # type: ignore[attr-defined]  # CursorResult.rowcount；SQLA 静态类型缺失，运行时存在
            raise NotFoundError(f"指标不存在或未处于已删除状态: {metric_id}")

    async def purge_metric(self, metric_id: int, metric_code: str) -> None:
        """彻底删除已软删指标（回收站硬删，物理删除不可恢复）。

        级联清理全部关联数据（版本/待确认/维度/健康度/值快照/挂载/血缘边）后
        删除主行，单事务保证原子性。调用方（service）已校验 deleted_at 置位
        与平台管理员权限，本方法仅执行物理删除。
        """
        node = f"metric:{metric_code}"
        await self._db.execute(
            delete(LineageEdge).where(
                or_(LineageEdge.source_node == node, LineageEdge.target_node == node)
            )
        )
        await self._db.execute(
            delete(MetricValueSnapshot).where(MetricValueSnapshot.metric_code == metric_code)
        )
        await self._db.execute(delete(MetricVersion).where(MetricVersion.metric_id == metric_id))
        await self._db.execute(
            delete(PendingVersionConfirmation).where(
                PendingVersionConfirmation.metric_id == metric_id
            )
        )
        await self._db.execute(
            delete(MetricDimension).where(MetricDimension.metric_id == metric_id)
        )
        await self._db.execute(
            delete(MetricHealthScore).where(MetricHealthScore.metric_id == metric_id)
        )
        await self._db.execute(delete(MetricMount).where(MetricMount.metric_id == metric_id))
        # B2（审查修复）：补齐级联遗漏——质量规则（quality_rule.metric_id）、
        # 冲突（metric_a/metric_b 引用指标主键）、收藏（favorite.asset_id=业务编码）
        await self._db.execute(delete(QualityRule).where(QualityRule.metric_id == metric_id))
        await self._db.execute(
            delete(Conflict).where(
                or_(Conflict.metric_a == metric_id, Conflict.metric_b == metric_id)
            )
        )
        await self._db.execute(
            delete(Favorite).where(
                Favorite.asset_type == "METRIC", Favorite.asset_id == metric_code
            )
        )
        await self._db.execute(delete(Metric).where(Metric.id == metric_id))

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
        # L-2 版本保留上限治理：创建新版本后自动把超限的已定稿旧版本标记 ARCHIVED
        # （WORM 保留完整记录，仅收敛"活跃版本"展示面，防版本爆炸）。
        await self._archive_excess_versions(version.metric_id)
        return version

    #: L-2 每指标保留的"活跃"版本数上限（超出部分标记 ARCHIVED，历史仍可查）
    _VERSION_RETAIN_LIMIT = 50
    #: 仅归档已定稿版本（DRAFT/PENDING_CONFIRMATION 不归档，避免影响确认流程）
    _ARCHIVABLE_VERSION_STATUSES = ("PUBLISHED", "EXPERIMENTAL", "CANCELLED")

    async def _archive_excess_versions(self, metric_id: int) -> int:
        """将超出保留上限的已定稿旧版本标记 ARCHIVED（L-2 版本爆炸治理）。

        Args:
            metric_id: 指标 ID。

        Returns:
            本次归档的版本数。
        """
        result = await self._db.execute(
            select(MetricVersion.id, MetricVersion.status)
            .where(
                MetricVersion.metric_id == metric_id,
                MetricVersion.deleted_at.is_(None),
                MetricVersion.status.in_(self._ARCHIVABLE_VERSION_STATUSES),
            )
            .order_by(MetricVersion.version.desc())
        )
        rows = result.all()
        if len(rows) <= self._VERSION_RETAIN_LIMIT:
            return 0
        excess_ids = [row.id for row in rows[self._VERSION_RETAIN_LIMIT :]]
        await self._db.execute(
            update(MetricVersion)
            .where(MetricVersion.id.in_(excess_ids))
            .values(status="ARCHIVED")
        )
        return len(excess_ids)

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

    async def has_pending_version(self, metric_id: int) -> bool:
        """是否存在未完成的破坏性变更确认期（PENDING_CONFIRMATION）版本。

        PENDING 确认期内禁止再次发起破坏性变更——否则多个 PENDING 版本并存，
        转正低版本号时会把主表 version 回退并覆盖高版本口径（版本历史倒挂）。

        Returns:
            True 如果存在未转正的 PENDING_CONFIRMATION 版本。
        """
        result = await self._db.execute(
            select(MetricVersion.id).where(
                MetricVersion.metric_id == metric_id,
                MetricVersion.status == "PENDING_CONFIRMATION",
                MetricVersion.deleted_at.is_(None),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def mark_version_published(
        self, metric_id: int, version: int, published_at: datetime, *, status: str = "PUBLISHED"
    ) -> None:
        """将指定版本标记为指定状态（默认 PUBLISHED）并记录发布时间（发布时版本转正）。"""
        stmt = (
            update(MetricVersion)
            .where(
                MetricVersion.metric_id == metric_id,
                MetricVersion.version == version,
            )
            .values(status=status, published_at=published_at, effective_at=published_at)
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
        self, metric_id: int, version: int, *, for_update: bool = False
    ) -> list[PendingVersionConfirmation]:
        """获取指定版本的 PENDING 确认记录列表。

        Args:
            metric_id: 指标 ID。
            version: 版本号。
            for_update: 是否加行锁（``SELECT ... FOR UPDATE``）。confirm_version
                的「全部确认→转正」判定需串行化（P1-3）：并发最后两名消费方各自
                读到对方 PENDING 会都不转正、版本滞留。加锁后最后一个确认者
                重读拿到对方已 CONFIRMED，可靠触发转正。
        """
        stmt = (
            select(PendingVersionConfirmation)
            .where(
                PendingVersionConfirmation.metric_id == metric_id,
                PendingVersionConfirmation.version == version,
                PendingVersionConfirmation.deleted_at.is_(None),
            )
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def list_active_for_conflict(self, *, page_size: int = 500) -> list[Metric]:
        """冲突预检加载全部活动指标（P1-F 排除 DEPRECATED + P1-G 分页全量）。

        修复前 `_load_existing_metrics` 用 ``list_metrics(limit=1000)``：
        - 不过滤状态 → DEPRECATED 废弃指标也参与比对，仲裁台噪音；
        - 1000 条截断 → 超出部分（通常是更早的历史指标）不参与比对，
          而新指标往往正与历史指标冲突，漏检。
        此处按 ``id`` 分页全量加载非软删、非 DEPRECATED 指标，返回完整清单。
        """
        rows: list[Metric] = []
        offset = 0
        while True:
            stmt = (
                select(Metric)
                .where(Metric.deleted_at.is_(None), Metric.status != "DEPRECATED")
                .order_by(Metric.id)
                .offset(offset)
                .limit(page_size)
            )
            page = list((await self._db.execute(stmt)).scalars().all())
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return rows

    async def count_confirmations_by_versions(
        self, metric_id: int, versions: list[int]
    ) -> dict[int, tuple[int, int]]:
        """按版本号批量统计确认进度（一次 IN 查询，避免 N+1）。

        Returns:
            ``{version: (confirmed_count, total_count)}``。
            total_count 含已确认（CONFIRMED）/已超时接受（TIMEOUT_ACCEPTED）
            的记录；confirmed_count 仅计明确确认（CONFIRMED）——用于版本历史
            展示「已确认 X/N」进度。
        """
        if not versions:
            return {}
        confirmed = (
            select(
                PendingVersionConfirmation.version,
                func.count().label("n"),
            )
            .where(
                PendingVersionConfirmation.metric_id == metric_id,
                PendingVersionConfirmation.version.in_(versions),
                PendingVersionConfirmation.status == "CONFIRMED",
                PendingVersionConfirmation.deleted_at.is_(None),
            )
            .group_by(PendingVersionConfirmation.version)
        )
        total = (
            select(
                PendingVersionConfirmation.version,
                func.count().label("n"),
            )
            .where(
                PendingVersionConfirmation.metric_id == metric_id,
                PendingVersionConfirmation.version.in_(versions),
                PendingVersionConfirmation.deleted_at.is_(None),
            )
            .group_by(PendingVersionConfirmation.version)
        )
        confirmed_map = {
            row[0]: int(row[1]) for row in (await self._db.execute(confirmed)).all()
        }
        total_map = {
            row[0]: int(row[1]) for row in (await self._db.execute(total)).all()
        }
        return {
            v: (confirmed_map.get(v, 0), total_map.get(v, 0))
            for v in versions
            if total_map.get(v, 0) > 0
        }

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

    async def get_pending_confirmation(
        self, metric_id: int, version: int, consumer_id: int
    ) -> PendingVersionConfirmation | None:
        """获取指定消费方的单条 PENDING 确认记录（供确认/拒绝/延期）。"""
        result = await self._db.execute(
            select(PendingVersionConfirmation).where(
                PendingVersionConfirmation.metric_id == metric_id,
                PendingVersionConfirmation.version == version,
                PendingVersionConfirmation.consumer_id == consumer_id,
                PendingVersionConfirmation.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def extend_confirmation_deadline(
        self, confirmation_id: int, new_deadline: datetime
    ) -> None:
        """将确认记录延期至新截止时间（extension_count + 1）。"""
        stmt = (
            update(PendingVersionConfirmation)
            .where(PendingVersionConfirmation.id == confirmation_id)
            .values(
                deadline=new_deadline,
                extension_count=PendingVersionConfirmation.extension_count + 1,
            )
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

    async def _count_by_status(
        self, model: type[Any], status_col: ColumnElement[Any]
    ) -> dict[str, int]:
        """按状态列分组计数（软删除过滤），返回 {status: count}。

        Args:
            model: 资产 ORM 模型（须含 deleted_at 软删除字段）。
            status_col: 状态列（枚举/字符串/布尔均可，键统一转 str）。

        Returns:
            状态 → 数量映射。
        """
        stmt = (
            select(status_col, func.count().label("cnt"))
            .where(model.deleted_at.is_(None))
            .group_by(status_col)
        )
        rows = (await self._db.execute(stmt)).all()
        return {str(row[0]): row[1] for row in rows}

    async def count_review_assigned(self, actor_id: int, user_domain: str | None) -> int:
        """统计指派给当前用户/所在域评审组的待审（REVIEW）指标数（TD §13）。

        - ``reviewer_type=user``：仅 ``reviewer_id`` 指定的用户可见/可审。
        - ``reviewer_type=domain``：仅同域评审组可见（reviewer_domain 与用户域一致）。
        - 未指派（reviewer_type IS NULL）：由域管理员兜底评审，reviewer 角色不可见，不在此统计。

        Args:
            actor_id: 当前用户 ID。
            user_domain: 当前用户所属域。

        Returns:
            指派给该用户/其域评审组的 REVIEW 指标数。
        """
        stmt = (
            select(func.count())
            .select_from(Metric)
            .where(
                Metric.deleted_at.is_(None),
                Metric.status == "REVIEW",
                or_(
                    and_(Metric.reviewer_type == "user", Metric.reviewer_id == actor_id),
                    and_(Metric.reviewer_type == "domain", Metric.reviewer_domain == user_domain),
                ),
            )
        )
        return (await self._db.execute(stmt)).scalar_one() or 0

    async def _aggregate_assets(self) -> dict[str, dict[str, Any]]:
        """聚合各类数据资产的计数与状态分布（对齐 TD §12.11 资产总览）。

        各资产按其治理/运行状态字段分组：
        - table 数据表：sensitivity_level 敏感级别（含 NEEDS_REVIEW）。
        - source 数据源：health_status 健康状态（healthy/unhealthy/unknown）。
        - dimension 维度 / term 术语：生命周期状态（DRAFT/PUBLISHED/DEPRECATED）。
        - template 指标模板 / system_dict 数据字典：启用状态（active/inactive）。

        Returns:
            {资产键: {total, by_status}}。
        """
        table = await self._count_by_status(DBCatalog, DBCatalog.sensitivity_level)
        source = await self._count_by_status(DataSource, DataSource.health_status)
        dimension = await self._count_by_status(Dimension, Dimension.status)
        term = await self._count_by_status(Term, Term.status)
        # 布尔启用列统一映射为 active/inactive，避免 "True"/"False" 键泄漏到前端
        template_raw = await self._count_by_status(MetricTemplate, MetricTemplate.is_active)
        template = {
            "active": template_raw.get("True", 0),
            "inactive": template_raw.get("False", 0),
        }
        system_dict_raw = await self._count_by_status(SystemDict, SystemDict.status)
        system_dict = {
            "active": system_dict_raw.get("active", 0),
            "inactive": system_dict_raw.get("inactive", 0),
        }

        def stat(by_status: dict[str, int]) -> dict[str, Any]:
            return {"total": sum(by_status.values()), "by_status": by_status}

        return {
            "table": stat(table),
            "source": stat(source),
            "dimension": stat(dimension),
            "term": stat(term),
            "template": stat(template),
            "system_dict": stat(system_dict),
        }

    async def aggregate_dashboard(
        self, domain: str | None = None, owner_id: int | None = None
    ) -> dict[str, Any]:
        """工作台仪表盘聚合（P2 性能审查：约 20 个全表聚合 SQL 无缓存，加 30s cache-aside）。

        key 含 domain/owner 维度区分；写操作（指标/挂载/治理变更）后可显式失效。
        """
        from app.core.agg_cache import agg_cached

        return await agg_cached(
            f"dashboard:{domain or 'all'}:{owner_id or 'all'}",
            lambda: self._aggregate_dashboard_uncached(domain, owner_id),
        )

    async def _aggregate_dashboard_uncached(
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
            func.sum(func.if_(Metric.pii_flag.is_(True), 1, 0)).label("pii_count"),
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

        # 按域分组（对齐主查询：domain 参数同样收敛，避免 ?domain=X 时域分布仍全量）
        domain_conditions: list[ColumnElement[bool]] = [Metric.deleted_at.is_(None)]
        if domain:
            domain_conditions.append(Metric.domain == domain)
        if owner_id:
            domain_conditions.append(Metric.owner_id == owner_id)
        domain_stmt = (
            select(Metric.domain, func.count().label("cnt"))
            .where(*domain_conditions)
            .group_by(Metric.domain)
        )
        domain_rows = (await self._db.execute(domain_stmt)).all()
        by_domain = {row[0]: row[1] for row in domain_rows}

        # ---- 完整指标体系（对齐方案 C：一眼看到几乎全部资产/治理状态）----
        # 1) Owner 责任分布（跨资产）：指标/数据表/维度/术语/指标模板按 owner 分组，
        #    再统一查 User 显示名（兼容"仅有非指标资产"的责任人）
        owner_metric_stmt = (
            select(Metric.owner_id, Metric.status, func.count().label("cnt"))
            .where(*conditions)
            .group_by(Metric.owner_id, Metric.status)
        )
        owner_metric_rows = (await self._db.execute(owner_metric_stmt)).all()

        owner_table_stmt = (
            select(DBCatalog.owner_id, func.count().label("cnt"))
            .join(DataSource, DataSource.source_id == DBCatalog.source_id)
            .where(
                DBCatalog.owner_id.isnot(None),
                DBCatalog.deleted_at.is_(None),
                *([DataSource.domain == domain] if domain else []),
                *([DBCatalog.owner_id == owner_id] if owner_id is not None else []),
            )
            .group_by(DBCatalog.owner_id)
        )
        owner_table_rows = (await self._db.execute(owner_table_stmt)).all()

        owner_dim_stmt = (
            select(Dimension.owner_id, Dimension.status, func.count().label("cnt"))
            .where(
                Dimension.deleted_at.is_(None),
                *([Dimension.domain == domain] if domain else []),
                *([Dimension.owner_id == owner_id] if owner_id is not None else []),
            )
            .group_by(Dimension.owner_id, Dimension.status)
        )
        owner_dim_rows = (await self._db.execute(owner_dim_stmt)).all()

        owner_term_stmt = (
            select(Term.owner_id, Term.status, func.count().label("cnt"))
            .where(
                Term.deleted_at.is_(None),
                *([Term.domain == domain] if domain else []),
                *([Term.owner_id == owner_id] if owner_id is not None else []),
            )
            .group_by(Term.owner_id, Term.status)
        )
        owner_term_rows = (await self._db.execute(owner_term_stmt)).all()

        owner_tpl_stmt = (
            select(MetricTemplate.owner_id, func.count().label("cnt"))
            .where(
                MetricTemplate.owner_id.isnot(None),
                MetricTemplate.deleted_at.is_(None),
                *([MetricTemplate.domain == domain] if domain else []),
                *([MetricTemplate.owner_id == owner_id] if owner_id is not None else []),
            )
            .group_by(MetricTemplate.owner_id)
        )
        owner_tpl_rows = (await self._db.execute(owner_tpl_stmt)).all()

        owner_source_stmt = (
            select(DataSource.owner_id, func.count().label("cnt"))
            .where(
                DataSource.owner_id.isnot(None),
                DataSource.deleted_at.is_(None),
                *([DataSource.domain == domain] if domain else []),
                *([DataSource.owner_id == owner_id] if owner_id is not None else []),
            )
            .group_by(DataSource.owner_id)
        )
        owner_source_rows = (await self._db.execute(owner_source_stmt)).all()

        owner_ids = {
            r[0]
            for r in (
                owner_metric_rows
                + owner_table_rows
                + owner_dim_rows
                + owner_term_rows
                + owner_tpl_rows
                + owner_source_rows
            )
        }
        owner_names: dict[int, str] = {}
        if owner_ids:
            owner_name_stmt = select(User.id, User.display_name).where(User.id.in_(owner_ids))
            owner_names = dict((await self._db.execute(owner_name_stmt)).all())

        def _owner_entry(owner_id_: int) -> dict[str, Any]:
            # 统一 AssetStat 结构 {total, by_status}：指标/维度/术语有真实状态；
            # 数据表/数据源/模板无状态概念，by_status 留空但结构一致便于前端统一读取。
            return {
                "name": owner_names.get(owner_id_, f"用户 #{owner_id_}"),
                "total": 0,
                "metrics": {"total": 0, "by_status": {}},
                "tables": {"total": 0, "by_status": {}},
                "sources": {"total": 0, "by_status": {}},
                "dimensions": {"total": 0, "by_status": {}},
                "terms": {"total": 0, "by_status": {}},
                "templates": {"total": 0, "by_status": {}},
            }

        by_owner: dict[int, dict[str, Any]] = {}
        for owner_id_, status_, cnt in owner_metric_rows:
            entry = by_owner.setdefault(owner_id_, _owner_entry(owner_id_))
            entry["metrics"]["total"] += cnt
            entry["metrics"]["by_status"][status_] = (
                entry["metrics"]["by_status"].get(status_, 0) + cnt
            )
        for owner_id_, cnt in owner_table_rows:
            entry = by_owner.setdefault(owner_id_, _owner_entry(owner_id_))
            entry["tables"]["total"] += cnt
        for owner_id_, status_, cnt in owner_dim_rows:
            entry = by_owner.setdefault(owner_id_, _owner_entry(owner_id_))
            entry["dimensions"]["total"] += cnt
            entry["dimensions"]["by_status"][status_] = (
                entry["dimensions"]["by_status"].get(status_, 0) + cnt
            )
        for owner_id_, status_, cnt in owner_term_rows:
            entry = by_owner.setdefault(owner_id_, _owner_entry(owner_id_))
            entry["terms"]["total"] += cnt
            entry["terms"]["by_status"][status_] = (
                entry["terms"]["by_status"].get(status_, 0) + cnt
            )
        for owner_id_, cnt in owner_tpl_rows:
            by_owner.setdefault(owner_id_, _owner_entry(owner_id_))["templates"]["total"] += cnt
        for owner_id_, cnt in owner_source_rows:
            by_owner.setdefault(owner_id_, _owner_entry(owner_id_))["sources"]["total"] += cnt
        for entry in by_owner.values():
            entry["total"] = (
                entry["metrics"]["total"]
                + entry["tables"]["total"]
                + entry["sources"]["total"]
                + entry["dimensions"]["total"]
                + entry["terms"]["total"]
                + entry["templates"]["total"]
            )

        # 2) 质量健康：质量事件按严重级分布 + 待处理（OPEN+ACK）
        #    仅统计未关闭事件（OPEN/ACK），与可观测中心 pending_quality 同口径——
        #    已关闭（RESOLVED/CLOSED）事件不计入「当前质量健康」大数字
        quality_sev_stmt = (
            select(QualityEvent.level, func.count().label("cnt"))
            .where(
                QualityEvent.deleted_at.is_(None),
                QualityEvent.status.in_(["OPEN", "ACK"]),
            )
            .group_by(QualityEvent.level)
        )
        quality_sev_rows = (await self._db.execute(quality_sev_stmt)).all()
        by_severity = {row[0]: row[1] for row in quality_sev_rows}
        quality_status_stmt = (
            select(QualityEvent.status, func.count().label("cnt"))
            .where(QualityEvent.deleted_at.is_(None))
            .group_by(QualityEvent.status)
        )
        quality_status_rows = (await self._db.execute(quality_status_stmt)).all()
        quality_by_status = {row[0]: row[1] for row in quality_status_rows}
        quality_pending = quality_by_status.get("OPEN", 0) + quality_by_status.get("ACK", 0)

        # 3) 合规：已合规复核指标数（软删过滤 + 当前筛选条件）
        compliance_stmt = (
            select(Metric.compliance_reviewed, func.count().label("cnt"))
            .where(*conditions)
            .group_by(Metric.compliance_reviewed)
        )
        compliance_rows = (await self._db.execute(compliance_stmt)).all()
        compliance_reviewed = dict(compliance_rows).get(True, 0)

        # 4) 冲突风险：未关闭冲突按状态分布（软删过滤）
        conflict_stmt = (
            select(Conflict.status, func.count().label("cnt"))
            .where(Conflict.deleted_at.is_(None))
            .group_by(Conflict.status)
        )
        conflict_rows = (await self._db.execute(conflict_stmt)).all()
        conflict_by_status = {row[0]: row[1] for row in conflict_rows}

        # 5) 新鲜度：近 30 天更新的指标数
        cutoff = datetime.now() - timedelta(days=30)
        freshness_stmt = select(func.count()).where(
            Metric.deleted_at.is_(None),
            Metric.updated_at >= cutoff,
        )
        updated_30d = (await self._db.execute(freshness_stmt)).scalar() or 0

        return {
            "total": total,
            "by_status": by_status,
            "by_tier": by_tier,
            "by_domain": by_domain,
            "pii_count": pii_count,
            "pii_ratio": round(pii_count / max(total, 1), 4),
            "by_owner": by_owner,
            "quality": {
                # 大数字 = 当前待处理质量事件（by_severity 已过滤 OPEN/ACK）
                "total": sum(by_severity.values()),
                "by_severity": by_severity,
                "pending": quality_pending,
            },
            "compliance": {
                "total": total,
                "reviewed": compliance_reviewed,
                "pending": max(total - compliance_reviewed, 0),
                "reviewed_ratio": round(compliance_reviewed / max(total, 1), 4),
            },
            "conflict": {
                # 与可观测中心同口径：未关闭 = OPEN + NEGOTIATING + ESCALATED
                # （RULED/CLOSED 等已决冲突不计入「未关闭冲突总数」）
                "total": (
                    conflict_by_status.get("OPEN", 0)
                    + conflict_by_status.get("NEGOTIATING", 0)
                    + conflict_by_status.get("ESCALATED", 0)
                ),
                "open": conflict_by_status.get("OPEN", 0)
                + conflict_by_status.get("NEGOTIATING", 0),
                "escalated": conflict_by_status.get("ESCALATED", 0),
                "by_status": conflict_by_status,
            },
            "freshness": {
                "total": total,
                "updated_30d": updated_30d,
                "updated_30d_ratio": round(updated_30d / max(total, 1), 4),
            },
            "assets": {
                "metric": {"total": total, "by_status": by_status},
                **await self._aggregate_assets(),
            },
        }
