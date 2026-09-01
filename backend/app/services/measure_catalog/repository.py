"""逻辑度量目录 Repository（OneData 原子层，TD §4.2 / FR-02-08）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.measure_catalog import MeasureCatalog
from app.models.metric import Metric


class MeasureCatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, obj: MeasureCatalog) -> MeasureCatalog:
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def get(self, measure_code: str) -> MeasureCatalog | None:
        stmt = select(MeasureCatalog).where(MeasureCatalog.measure_code == measure_code)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_active(self, measure_code: str) -> MeasureCatalog | None:
        """仅查活跃（未软删）记录——T14（审查修复）：create 预检应只把
        未软删的同名记录判为占用；软删记录单独识别，避免「编码被回收站
        永久占位却只报通用已存在」的误导。"""
        stmt = select(MeasureCatalog).where(
            MeasureCatalog.measure_code == measure_code,
            MeasureCatalog.deleted_at.is_(None),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_deleted(self, measure_code: str) -> MeasureCatalog | None:
        """仅查已软删记录（回收站）。"""
        stmt = select(MeasureCatalog).where(
            MeasureCatalog.measure_code == measure_code,
            MeasureCatalog.deleted_at.is_not(None),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, measure_id: int) -> MeasureCatalog | None:
        stmt = select(MeasureCatalog).where(MeasureCatalog.id == measure_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def soft_delete_measure(self, measure_id: int) -> None:
        """软删逻辑度量：置 deleted_at（回收站可恢复）。"""
        from sqlalchemy import update

        await self._session.execute(
            update(MeasureCatalog)
            .where(MeasureCatalog.id == measure_id, MeasureCatalog.deleted_at.is_(None))
            .values(deleted_at=datetime.now(UTC))
        )

    async def restore_measure(self, measure_id: int) -> None:
        """恢复软删逻辑度量：清除 deleted_at（回收站恢复）。"""
        from sqlalchemy import update

        await self._session.execute(
            update(MeasureCatalog)
            .where(MeasureCatalog.id == measure_id, MeasureCatalog.deleted_at.is_not(None))
            .values(deleted_at=None)
        )

    async def purge_measure(self, measure_id: int) -> None:
        """彻底删除逻辑度量（回收站硬删，物理删除不可恢复）。"""
        from sqlalchemy import delete

        await self._session.execute(delete(MeasureCatalog).where(MeasureCatalog.id == measure_id))

    async def list(
        self,
        domain: str | None,
        status: str | None,
        keyword: str | None = None,
        owner_id: int | None = None,
        *,
        reviewed_by: int | None = None,
        deleted: bool = False,
        limit: int = 20,
        offset: int = 0,
        visible_actor_id: int | None = None,
        visible_role: str | None = None,
    ) -> tuple[list[MeasureCatalog], int]:
        """分页列出逻辑度量，返回 (列表, total)。

        - total 用独立 count（不含 JOIN），与列表共用同一过滤条件，保证分页一致性
        - keyword 参数化 LIKE + 通配符转义（对齐 FR-035：% / _ 须转义，防模糊放大）
        - deleted=True 时列出已软删记录（回收站视图）
        - reviewed_by 非空时过滤"我审过的"（通过/驳回人 ID 匹配，供统一主数据审批工作台）
        - visible_actor_id/visible_role：读路径行级隔离（P0-3，对齐指标/维度/术语）——
          非管理角色仅可见公开状态（PUBLISHED/DEPRECATED）+ 本人负责的未发布
          （DRAFT/REVIEW）；评审人可看待审（REVIEW）。管理角色传 None 即不加过滤。
        """
        conditions = (
            [MeasureCatalog.deleted_at.is_not(None)]
            if deleted
            else [MeasureCatalog.deleted_at.is_(None)]
        )
        # P0-3 读路径行级隔离（对齐 dimension/glossary）：度量 DRAFT/REVIEW 是创建者
        # 私有工作区，他人不得窥探；公开状态（PUBLISHED/DEPRECATED）可被发现。
        if (
            visible_actor_id is not None
            and visible_role is not None
            and visible_role not in ("platform_admin", "domain_admin")
        ):
            visibility: list[Any] = [
                MeasureCatalog.status.in_(("PUBLISHED", "DEPRECATED")),
                MeasureCatalog.owner_id == visible_actor_id,
            ]
            if visible_role == "reviewer":
                visibility.append(MeasureCatalog.status == "REVIEW")
            conditions.append(or_(*visibility))
        if domain:
            conditions.append(MeasureCatalog.domain == domain)
        if status:
            conditions.append(MeasureCatalog.status == status)
        if owner_id is not None:
            conditions.append(MeasureCatalog.owner_id == owner_id)
        if reviewed_by is not None:
            conditions.append(
                or_(
                    MeasureCatalog.approver_id == reviewed_by,
                    MeasureCatalog.reject_reviewer_id == reviewed_by,
                )
            )
        if keyword:
            escaped = keyword.replace("/", "//").replace("%", "/%").replace("_", "/_")
            conditions.append(
                or_(
                    MeasureCatalog.measure_code.like(f"%{escaped}%", escape="/"),
                    MeasureCatalog.name.like(f"%{escaped}%", escape="/"),
                    MeasureCatalog.description.like(f"%{escaped}%", escape="/"),
                )
            )
        count_stmt = (
            select(func.count()).select_from(MeasureCatalog).where(*conditions)
        )
        total = (await self._session.execute(count_stmt)).scalar() or 0
        stmt = (
            select(MeasureCatalog)
            .where(*conditions)
            .order_by(MeasureCatalog.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all()), int(total)

    async def count_metrics_by_measure(self, measure_id: int) -> int:
        """统计逻辑度量被多少指标引用（废弃保护：被引用度量禁止废弃）。"""
        stmt = select(func.count(Metric.id)).where(Metric.measure_id == measure_id)
        return int((await self._session.execute(stmt)).scalar_one() or 0)

    async def commit(self) -> None:
        await self._session.commit()
