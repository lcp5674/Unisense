"""资产地图服务单元测试（TD §12.11 / FR-18）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.models.data_source import DBCatalog
from app.services.assetmap.service import AssetMapService


async def _svc() -> tuple[AssetMapService, MagicMock]:
    db = MagicMock()
    svc = AssetMapService(db)
    repo = MagicMock()
    repo.catalog_summary = AsyncMock(
        return_value={"total": 3, "by_entity_type": {}, "by_sensitivity": {}, "orphan_assets": 1}
    )
    repo.classification_summary = AsyncMock(return_value={"by_sensitivity": {"PII": 2}})
    repo.metric_summary = AsyncMock(
        return_value={"by_domain": {"sales": 1}, "by_status": {"PUBLISHED": 1}}
    )
    repo.list_tables = AsyncMock(
        return_value=[
            DBCatalog(source_id="s", entity_name="t", entity_type="table", schema_json={})
        ]
    )
    repo.orphan_assets = AsyncMock(return_value=[])
    svc._repo = repo  # noqa: SLF001
    return svc, repo


async def test_catalog_summary() -> None:
    svc, repo = await _svc()
    out = await svc.catalog_summary()
    assert out["total"] == 3
    assert out["orphan_assets"] == 1
    repo.catalog_summary.assert_awaited()


async def test_classification_summary() -> None:
    svc, repo = await _svc()
    out = await svc.classification_summary()
    assert out["by_sensitivity"]["PII"] == 2


async def test_list_tables() -> None:
    svc, repo = await _svc()
    items = await svc.list_tables(None, None, 100)
    assert len(items) == 1
    repo.list_tables.assert_awaited()
