"""consume 层 Repository（TD §12.6 / FR-12,13）。

三个仓储：ApiClientRepo（接入方）、SnapshotRepo（结果快照 WORM）、FavoriteRepo（收藏）。
仅做数据访问，不含业务校验/审计（审计在 service/api 层）。对齐 DEV_GUIDE §2（分层）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consume import ApiClient, MetricValueSnapshot, UserPreference


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
    """用户偏好/收藏仓储（key=pinned_metrics 承载收藏指标码列表）。"""

    PINNED_KEY = "pinned_metrics"

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def upsert_pinned(self, user_id: int, metric_codes: list[str]) -> UserPreference:
        pref = await self.get(user_id, self.PINNED_KEY)
        if pref is None:
            pref = UserPreference(
                user_id=user_id,
                preference_key=self.PINNED_KEY,
                preference_value={"metrics": metric_codes},
            )
            self._db.add(pref)
        else:
            pref.preference_value = {"metrics": metric_codes}
            self._db.add(pref)
        await self._db.flush()
        return pref

    async def get(self, user_id: int, key: str) -> UserPreference | None:
        stmt = select(UserPreference).where(
            UserPreference.user_id == user_id,
            UserPreference.preference_key == key,
            UserPreference.deleted_at.is_(None),
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def list_pinned(self, user_id: int) -> list[str]:
        pref = await self.get(user_id, self.PINNED_KEY)
        if pref is None:
            return []
        return list(pref.preference_value.get("metrics", []))
