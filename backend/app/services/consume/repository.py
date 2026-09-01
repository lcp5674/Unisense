"""consume 层 Repository（TD §12.6 / FR-12,13）。

三个仓储：ApiClientRepo（接入方）、SnapshotRepo（结果快照 WORM）、FavoriteRepo（收藏）。
仅做数据访问，不含业务校验/审计（审计在 service/api 层）。对齐 DEV_GUIDE §2（分层）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consume import ApiClient, ApiClientStatus, Favorite, MetricValueSnapshot


class ApiClientRepo:
    """接入方仓储。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_by_client_id(self, client_id: str) -> ApiClient | None:
        stmt = select(ApiClient).where(
            ApiClient.client_id == client_id, ApiClient.deleted_at.is_(None)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def get_any_by_client_id(self, client_id: str) -> ApiClient | None:
        """按 client_id 取任意记录（**含软删**，B3 创建预检用）。

        软删客户保留原 client_id（唯一键仍占用），创建预检若只查未删行会漏检
        软删占位 → 直插唯一键冲突落 500。本方法不过滤 deleted_at，命中即冲突。
        """
        stmt = select(ApiClient).where(ApiClient.client_id == client_id)
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def create(self, client: ApiClient) -> ApiClient:
        self._db.add(client)
        await self._db.flush()
        return client

    async def list(self, domain: str | None, limit: int, offset: int) -> list[ApiClient]:
        stmt = select(ApiClient).where(ApiClient.deleted_at.is_(None))
        if domain:
            stmt = stmt.where(ApiClient.scope_domain == domain)
        stmt = stmt.order_by(ApiClient.id.desc()).limit(limit).offset(offset)
        return list((await self._db.execute(stmt)).scalars().all())

    async def count(self, domain: str | None) -> int:
        stmt = select(func.count()).select_from(ApiClient).where(ApiClient.deleted_at.is_(None))
        if domain:
            stmt = stmt.where(ApiClient.scope_domain == domain)
        return int((await self._db.execute(stmt)).scalar_one() or 0)

    async def get_many(self, client_ids: list[str]) -> list[ApiClient]:
        """批量按 client_id 取未删接入方（顺序无关，供批量操作用）。"""
        stmt = select(ApiClient).where(
            ApiClient.client_id.in_(client_ids), ApiClient.deleted_at.is_(None)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def update_status(self, client_id: str, status: ApiClientStatus) -> ApiClient | None:
        """停用/启用接入方（仅未删记录可操作）。"""
        row = await self.get_by_client_id(client_id)
        if row is None:
            return None
        row.status = status
        await self._db.flush()
        return row

    async def soft_delete(self, client_id: str) -> ApiClient | None:
        """软删接入方：置 deleted_at + REVOKED（保留审计追溯，已签短效令牌随状态失效）。"""
        row = await self.get_by_client_id(client_id)
        if row is None:
            return None
        row.deleted_at = func.now()
        row.status = ApiClientStatus.REVOKED
        await self._db.flush()
        return row


class SnapshotRepo:
    """结果快照仓储（WORM：仅写、不更新、不删）。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(self, snapshot: MetricValueSnapshot) -> MetricValueSnapshot:
        self._db.add(snapshot)
        await self._db.flush()
        return snapshot

    async def get_by_unique(
        self,
        metric_code: str,
        version: int,
        date_range: str,
        dims_signature: str | None,
    ) -> MetricValueSnapshot | None:
        """按同口径唯一键查快照（metric/version/date_range/dims_signature）。

        WORM 去重前置检查：同口径已存在则跳过（唯一索引竞态兜底在 service 层
        捕获 IntegrityError）。
        """
        stmt = select(MetricValueSnapshot).where(
            MetricValueSnapshot.metric_code == metric_code,
            MetricValueSnapshot.version == version,
            MetricValueSnapshot.date_range == date_range,
            MetricValueSnapshot.dims_signature == dims_signature,
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def list_by_metric(
        self, metric_code: str, limit: int, offset: int
    ) -> list[MetricValueSnapshot]:
        stmt = (
            select(MetricValueSnapshot)
            .where(MetricValueSnapshot.metric_code == metric_code)
            .order_by(MetricValueSnapshot.generated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def list(self, limit: int, offset: int) -> list[MetricValueSnapshot]:
        stmt = (
            select(MetricValueSnapshot)
            .order_by(MetricValueSnapshot.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._db.execute(stmt)).scalars().all())


class FavoriteRepo:
    """通用收藏仓储（favorite 表：user_id × asset_type × asset_id）。

    取代原 pinned_metrics JSON 数组存储；多资产类型统一由 asset_type 区分。
    仅做数据访问，资产存在性校验在 service 层。
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def add(self, user_id: int, asset_type: str, asset_id: str) -> Favorite:
        fav = Favorite(user_id=user_id, asset_type=asset_type, asset_id=asset_id)
        self._db.add(fav)
        await self._db.flush()
        return fav

    async def remove(self, user_id: int, asset_type: str, asset_id: str) -> bool:
        stmt = select(Favorite).where(
            Favorite.user_id == user_id,
            Favorite.asset_type == asset_type,
            Favorite.asset_id == asset_id,
            Favorite.deleted_at.is_(None),
        )
        fav = (await self._db.execute(stmt)).scalar_one_or_none()
        if fav is None:
            return False
        fav.deleted_at = func.now()
        await self._db.flush()
        return True

    async def get(self, user_id: int, asset_type: str, asset_id: str) -> Favorite | None:
        stmt = select(Favorite).where(
            Favorite.user_id == user_id,
            Favorite.asset_type == asset_type,
            Favorite.asset_id == asset_id,
            Favorite.deleted_at.is_(None),
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def list(self, user_id: int) -> list[Favorite]:
        """按收藏时间倒序返回用户全部收藏。"""
        stmt = (
            select(Favorite)
            .where(Favorite.user_id == user_id, Favorite.deleted_at.is_(None))
            .order_by(Favorite.created_at.desc())
        )
        return list((await self._db.execute(stmt)).scalars().all())

    async def list_by_types(
        self, user_id: int, asset_types: list[str]
    ) -> list[Favorite]:
        """按资产类型过滤收藏（详情聚合用，仍按收藏时间倒序）。"""
        stmt = (
            select(Favorite)
            .where(
                Favorite.user_id == user_id,
                Favorite.asset_type.in_(asset_types),
                Favorite.deleted_at.is_(None),
            )
            .order_by(Favorite.created_at.desc())
        )
        return list((await self._db.execute(stmt)).scalars().all())
