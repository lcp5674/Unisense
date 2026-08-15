"""consume 层 Repository（TD §12.6 / FR-12,13）。

三个仓储：ApiClientRepo（接入方）、SnapshotRepo（结果快照 WORM）、FavoriteRepo（收藏）。
仅做数据访问，不含业务校验/审计（审计在 service/api 层）。对齐 DEV_GUIDE §2（分层）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consume import ApiClient, Favorite, MetricValueSnapshot


class ApiClientRepo:
    """接入方仓储。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_by_client_id(self, client_id: str) -> ApiClient | None:
        stmt = select(ApiClient).where(
            ApiClient.client_id == client_id, ApiClient.deleted_at.is_(None)
        )
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


class SnapshotRepo:
    """结果快照仓储（WORM：仅写、不更新、不删）。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(self, snapshot: MetricValueSnapshot) -> MetricValueSnapshot:
        self._db.add(snapshot)
        await self._db.flush()
        return snapshot

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
