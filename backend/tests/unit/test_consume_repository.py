"""consume 服务 Repository 单测（补齐覆盖率）。

针对 consume/repository.py 的 41% 覆盖率，补充以下场景：
- ApiClientRepo: get_by_client_id, create, list, count
- SnapshotRepo: create, list_by_metric, list
- FavoriteRepo: upsert_pinned, get, list_pinned
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.consume import ApiClient, MetricValueSnapshot, UserPreference
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

    async def test_get_found(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = UserPreference(
            user_id=1, preference_key="pinned_metrics"
        )
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        result = await repo.get(user_id=1, key="pinned_metrics")
        assert result is not None

    async def test_get_not_found(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        result = await repo.get(user_id=999, key="pinned_metrics")
        assert result is None

    async def test_list_pinned_empty(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        results = await repo.list_pinned(user_id=1)
        assert results == []

    async def test_list_pinned_with_data(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        pref = UserPreference(
            user_id=1,
            preference_key="pinned_metrics",
            preference_value={"metrics": ["M1", "M2"]},
        )
        mock_result.scalar_one_or_none.return_value = pref
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        results = await repo.list_pinned(user_id=1)
        assert results == ["M1", "M2"]

    async def test_upsert_pinned_creates_new(self, db: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        result = await repo.upsert_pinned(user_id=1, metric_codes=["M1"])
        assert result.preference_value == {"metrics": ["M1"]}
        db.add.assert_called_once()

    async def test_upsert_pinned_updates_existing(self, db: MagicMock) -> None:
        existing = UserPreference(
            user_id=1,
            preference_key="pinned_metrics",
            preference_value={"metrics": ["M1"]},
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        db.execute = AsyncMock(return_value=mock_result)
        repo = FavoriteRepo(db)
        result = await repo.upsert_pinned(user_id=1, metric_codes=["M1", "M2"])
        assert result.preference_value == {"metrics": ["M1", "M2"]}
        # update path calls add but should not create a new record
        assert db.add.call_count == 1
