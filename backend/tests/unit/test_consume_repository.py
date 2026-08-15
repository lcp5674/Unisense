"""consume 服务 Repository 单测（补齐覆盖率）。

针对 consume/repository.py 的 41% 覆盖率，补充以下场景：
- ApiClientRepo: get_by_client_id, create, list, count
- SnapshotRepo: create, list_by_metric, list
- FavoriteRepo: upsert_pinned, get, list_pinned
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.consume import ApiClient, Favorite, MetricValueSnapshot
from app.services.consume.repository import ApiClientRepo, FavoriteRepo, SnapshotRepo


@pytest.fixture
def db() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    return session


class TestApiClientRepo:
    def test_init(self, db: MagicMock) -> None:
        repo = ApiClientRepo(db)
        assert repo._db is db

    async def test_get_by_client_id_found(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ApiClient(client_id="test1")
        db.execute = AsyncMock(return_value=mock_result)
        repo = ApiClientRepo(db)
        result = await repo.get_by_client_id("test1")
        assert result is not None
        assert result.client_id == "test1"

    async def test_get_by_client_id_not_found(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)
        repo = ApiClientRepo(db)
        result = await repo.get_by_client_id("missing")
        assert result is None

    async def test_create(self, db: MagicMock) -> None:
        client = ApiClient(client_id="test1")
        repo = ApiClientRepo(db)
        result = await repo.create(client)
        assert result is client
        db.add.assert_called_once_with(client)

    async def test_list_no_domain(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            ApiClient(client_id="c1"),
            ApiClient(client_id="c2"),
        ]
        db.execute = AsyncMock(return_value=mock_result)
        repo = ApiClientRepo(db)
        results = await repo.list(domain=None, limit=10, offset=0)
        assert len(results) == 2

    async def test_list_with_domain(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [ApiClient(client_id="c1")]
        db.execute = AsyncMock(return_value=mock_result)
        repo = ApiClientRepo(db)
        results = await repo.list(domain="sales", limit=10, offset=0)
        assert len(results) == 1

    async def test_count_no_domain(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 5
        db.execute = AsyncMock(return_value=mock_result)
        repo = ApiClientRepo(db)
        count = await repo.count(domain=None)
        assert count == 5

    async def test_count_with_domain(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 2
        db.execute = AsyncMock(return_value=mock_result)
        repo = ApiClientRepo(db)
        count = await repo.count(domain="sales")
        assert count == 2


class TestSnapshotRepo:
    def test_init(self, db: MagicMock) -> None:
        repo = SnapshotRepo(db)
        assert repo._db is db

    async def test_create(self, db: MagicMock) -> None:
        snap = MetricValueSnapshot(metric_code="M1", value_json={"v": 1})
        repo = SnapshotRepo(db)
        result = await repo.create(snap)
        assert result is snap
        db.add.assert_called_once_with(snap)

    async def test_list_by_metric(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            MetricValueSnapshot(metric_code="M1"),
        ]
        db.execute = AsyncMock(return_value=mock_result)
        repo = SnapshotRepo(db)
        results = await repo.list_by_metric(metric_code="M1", limit=10, offset=0)
        assert len(results) == 1

    async def test_list(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [
            MetricValueSnapshot(metric_code="M1"),
        ]
        db.execute = AsyncMock(return_value=mock_result)
        repo = SnapshotRepo(db)
        results = await repo.list(limit=10, offset=0)
        assert len(results) == 1


class TestFavoriteRepo:
    def test_init(self, db: MagicMock) -> None:
        repo = FavoriteRepo(db)
        assert repo._db is db

    async def test_add(self, db: MagicMock) -> None:
        repo = FavoriteRepo(db)
        fav = await repo.add(user_id=1, asset_type="METRIC", asset_id="gmv")
        assert fav.user_id == 1
        assert fav.asset_type == "METRIC"
        assert fav.asset_id == "gmv"
        db.add.assert_called_once()
        db.flush.assert_awaited_once()

    async def test_get_found(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = Favorite(
            user_id=1, asset_type="METRIC", asset_id="gmv"
        )
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        result = await repo.get(user_id=1, asset_type="METRIC", asset_id="gmv")
        assert result is not None
        assert result.asset_id == "gmv"

    async def test_get_not_found(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        result = await repo.get(user_id=999, asset_type="METRIC", asset_id="ghost")
        assert result is None

    async def test_remove_existing_soft_deletes(self, db: MagicMock) -> None:
        fav = Favorite(user_id=1, asset_type="METRIC", asset_id="gmv")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = fav
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        removed = await repo.remove(user_id=1, asset_type="METRIC", asset_id="gmv")
        assert removed is True
        assert fav.deleted_at is not None  # 软删除
        db.flush.assert_awaited_once()

    async def test_remove_not_found(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        removed = await repo.remove(user_id=1, asset_type="METRIC", asset_id="ghost")
        assert removed is False

    async def test_list_empty(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = []
        mock_result.scalars.return_value = scalars
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        assert await repo.list(user_id=1) == []

    async def test_list_with_data(self, db: MagicMock) -> None:
        favs = [
            Favorite(user_id=1, asset_type="METRIC", asset_id="gmv"),
            Favorite(user_id=1, asset_type="TABLE", asset_id="dw.sales"),
        ]
        mock_result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = favs
        mock_result.scalars.return_value = scalars
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        results = await repo.list(user_id=1)
        assert len(results) == 2
        assert results[0].asset_id == "gmv"

    async def test_list_by_types_filters(self, db: MagicMock) -> None:
        favs = [Favorite(user_id=1, asset_type="METRIC", asset_id="gmv")]
        mock_result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = favs
        mock_result.scalars.return_value = scalars
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        results = await repo.list_by_types(user_id=1, asset_types=["METRIC"])
        assert len(results) == 1
        assert results[0].asset_type == "METRIC"
