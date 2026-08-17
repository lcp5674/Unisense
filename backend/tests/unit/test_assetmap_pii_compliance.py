"""资产地图 PII 合规增强测试（表级复核/脱敏/标注/保留期/模板/明细列表）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import BusinessError, NotFoundError
from app.models.data_source import DBCatalog
from app.services.assetmap.repository import AssetMapRepository
from app.services.assetmap.service import AssetMapService

# ---------------------------------------------------------------- service 层


def _svc() -> tuple[AssetMapService, MagicMock]:
    db = MagicMock()
    svc = AssetMapService(db)
    repo = MagicMock()
    svc._repo = repo  # noqa: SLF001
    return svc, repo


async def _pii_entity(owner_id: int | None = 3) -> DBCatalog:
    cat = DBCatalog(
        source_id="s1",
        entity_name="users",
        entity_type="TABLE",
        schema_json={"columns": [{"name": "phone"}]},
        sensitivity_level="PII",
        owner_id=owner_id,
    )
    cat.id = 1
    return cat


async def test_review_catalog_approve() -> None:
    svc, repo = _svc()
    cat = await _pii_entity(owner_id=3)
    cat.compliance_reviewed = True
    cat.masking_policy = "hash"
    repo.get_catalog_entity = AsyncMock(return_value=cat)
    repo.review_catalog = AsyncMock(return_value=cat)
    out = await svc.review_catalog(1, "APPROVE", reviewer_id=9)
    assert out["decision"] == "APPROVE"
    assert out["compliance_reviewed"] is True
    repo.review_catalog.assert_awaited_once()


async def test_review_catalog_blocks_self_review() -> None:
    """职责分离：资产责任人（owner=3）不得复核本人资产（禁自审）。"""
    svc, repo = _svc()
    cat = await _pii_entity(owner_id=3)
    repo.get_catalog_entity = AsyncMock(return_value=cat)
    with pytest.raises(BusinessError) as exc:
        await svc.review_catalog(1, "APPROVE", reviewer_id=3)
    assert exc.value.error_code == "SELF_REVIEW_BLOCKED"
    assert not repo.review_catalog.called


async def test_review_catalog_not_found() -> None:
    svc, repo = _svc()
    repo.get_catalog_entity = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await svc.review_catalog(999, "APPROVE", reviewer_id=9)


async def test_set_masking_policy() -> None:
    svc, repo = _svc()
    cat = await _pii_entity()
    cat.masking_policy = "hash"
    repo.get_catalog_entity = AsyncMock(return_value=cat)
    repo.set_masking_policy = AsyncMock(return_value=cat)
    out = await svc.set_masking_policy(1, "hash")
    assert out["masking_policy"] == "hash"
    repo.set_masking_policy.assert_awaited_with(cat, "hash")


async def test_upsert_pii_override() -> None:
    svc, repo = _svc()
    cat = await _pii_entity()
    repo.get_catalog_entity = AsyncMock(return_value=cat)
    row = SimpleNamespace(suppressed=True, reason="误报：该列为汇总编码")
    repo.upsert_pii_override = AsyncMock(return_value=row)
    out = await svc.upsert_pii_override(1, "phone", True, "误报", actor_id=9)
    assert out["suppressed"] is True
    repo.upsert_pii_override.assert_awaited_with(1, "phone", True, "误报", 9)


async def test_set_retention_passthrough() -> None:
    svc, repo = _svc()
    cat = await _pii_entity()
    cat.retention_days = 180
    cat.legal_basis = "user_consent"
    repo.get_catalog_entity = AsyncMock(return_value=cat)
    repo.set_retention = AsyncMock(return_value=cat)
    out = await svc.set_retention(1, 180, "user_consent")
    assert out["retention_days"] == 180
    assert out["legal_basis"] == "user_consent"


async def test_pii_templates_lists_three() -> None:
    svc, _repo = _svc()
    templates = await svc.pii_templates()
    assert len(templates) == 3
    ids = {t["id"] for t in templates}
    assert {"pipil-sensitive", "financial-industry", "standard"} <= ids
    assert "BIOMETRIC" in templates[0]["sensitive_categories"]


async def test_apply_pii_template_unknown() -> None:
    svc, repo = _svc()
    repo.list_catalog_ids_for_scope = AsyncMock(return_value=[])
    with pytest.raises(NotFoundError):
        await svc.apply_pii_template("nope", catalog_ids=None, source_id=None, all_pii=False)


async def test_apply_pii_template_standard_no_change() -> None:
    svc, repo = _svc()
    cat = await _pii_entity()
    repo.list_catalog_ids_for_scope = AsyncMock(return_value=[cat])
    repo.apply_sensitivity_template = AsyncMock(
        return_value={
            "entity_id": 1,
            "entity_name": "users",
            "changed": False,
            "applied_categories": [],
        }
    )
    out = await svc.apply_pii_template(
        "standard", catalog_ids=[1], source_id=None, all_pii=False
    )
    assert out["changed"] == 0
    assert out["applied"] == 1


async def test_list_pii_assets_passthrough() -> None:
    svc, repo = _svc()
    repo.list_pii_assets = AsyncMock(return_value=([{"id": 1}], 1))
    out = await svc.list_pii_assets(
        keyword="users", review_status="unreviewed", page=1, page_size=20
    )
    assert out["total"] == 1
    assert out["page"] == 1
    repo.list_pii_assets.assert_awaited_once()


# ---------------------------------------------------------------- repository 层


def _session() -> MagicMock:
    s = MagicMock()
    s.flush = AsyncMock()
    s.add = MagicMock()
    return s


def _cat_row(**kw: object) -> SimpleNamespace:
    base = {
        "id": 1,
        "entity_name": "users",
        "entity_type": "TABLE",
        "source_id": "s1",
        "sensitivity_level": "PII",
        "owner_id": None,
        "compliance_reviewed": False,
        "masking_policy": None,
        "updated_at": datetime.now(UTC),
        "schema_json": {"columns": [{"name": "phone"}]},
        "retention_days": None,
        "legal_basis": None,
        "retention_expires_at": None,
        "retention_notified_at": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


async def test_repo_set_retention_computes_expiry() -> None:
    s = _session()
    repo = AssetMapRepository(s)
    entity = await _pii_entity()
    updated = await repo.set_retention(entity, 180, "user_consent")
    assert updated.retention_days == 180
    assert updated.legal_basis == "user_consent"
    assert updated.retention_expires_at is not None
    assert updated.retention_notified_at is None
    assert updated.retention_expires_at > datetime.now(UTC)


async def test_repo_set_retention_clear() -> None:
    s = _session()
    repo = AssetMapRepository(s)
    entity = await _pii_entity()
    entity.retention_days = 90
    entity.retention_expires_at = datetime.now(UTC) + timedelta(days=30)
    updated = await repo.set_retention(entity, None, None)
    assert updated.retention_days is None
    assert updated.retention_expires_at is None


async def test_repo_upsert_pii_override_creates() -> None:
    s = _session()
    s.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    repo = AssetMapRepository(s)
    row = await repo.upsert_pii_override(1, "phone", True, "误报", actor_id=9)
    assert row.catalog_id == 1
    assert row.suppressed is True
    assert row.reason == "误报"
    assert row.created_by == 9


async def test_repo_list_pii_assets_filters_review_status() -> None:
    s = _session()
    repo = AssetMapRepository(s)
    # total count
    r_count = MagicMock()
    r_count.scalar.return_value = 1
    # 分页查询行
    r_rows = MagicMock()
    r_rows.scalars.return_value.all.return_value = [_cat_row()]
    # _pii_asset_item → _entity_pii_fields（classification 查询 + override 查询）
    r_cls = MagicMock()
    r_cls.scalar_one_or_none.return_value = None
    r_ov = MagicMock()
    r_ov.scalars.return_value.all.return_value = []
    # enrich_catalog_items：DataSource 查询（source_id 非空）
    r_src = MagicMock()
    r_src.all.return_value = [("s1", "源1", "sales")]
    s.execute = AsyncMock(side_effect=[r_count, r_rows, r_cls, r_ov, r_src])
    items, total = await repo.list_pii_assets(review_status="unreviewed", page=1, page_size=20)
    assert total == 1
    assert len(items) == 1
    assert items[0]["compliance_reviewed"] is False
    # 仅 PII 命中（phone）→ 类别 PHONE
    assert "PHONE" in items[0]["categories"]
    assert items[0]["pii_field_count"] == 1


async def test_repo_list_pii_assets_category_filter() -> None:
    s = _session()
    repo = AssetMapRepository(s)
    r_count = MagicMock()
    r_count.scalar.return_value = 1
    r_rows = MagicMock()
    r_rows.scalars.return_value.all.return_value = [_cat_row()]
    r_cls = MagicMock()
    r_cls.scalar_one_or_none.return_value = None
    r_ov = MagicMock()
    r_ov.scalars.return_value.all.return_value = []
    r_src = MagicMock()
    r_src.all.return_value = [("s1", "源1", "sales")]
    s.execute = AsyncMock(side_effect=[r_count, r_rows, r_cls, r_ov, r_src])
    items, _total = await repo.list_pii_assets(category="PHONE", page=1, page_size=20)
    assert len(items) == 1
    # 第二次调用：重新设置 execute mock（side_effect 已耗尽）
    s.execute = AsyncMock(side_effect=[r_count, r_rows, r_cls, r_ov, r_src])
    items2, _total2 = await repo.list_pii_assets(category="HEALTH", page=1, page_size=20)
    assert items2 == []


async def test_repo_apply_sensitivity_template_upgrades() -> None:
    s = _session()
    repo = AssetMapRepository(s)
    cat = await _pii_entity()
    cat.sensitivity_level = "INTERNAL"
    # apply_sensitivity_template：实时检测字段类别（仅查询 override 标注）
    r_ov = MagicMock()
    r_ov.scalars.return_value.all.return_value = []
    s.execute = AsyncMock(side_effect=[r_ov])
    template = {"id": "financial-industry", "sensitive_categories": ["BANK_CARD", "FINANCIAL"]}
    out = await repo.apply_sensitivity_template(cat, template)
    # phone 不在金融敏感类别内 → 不升级
    assert out["changed"] is False
    assert out["applied_categories"] == []
    assert cat.sensitivity_level == "INTERNAL"


async def test_repo_apply_sensitivity_template_upgrades_health() -> None:
    s = _session()
    repo = AssetMapRepository(s)
    cat = await _pii_entity()
    cat.sensitivity_level = "INTERNAL"
    cat.schema_json = {"columns": [{"name": "diagnosis"}]}
    r_ov = MagicMock()
    r_ov.scalars.return_value.all.return_value = []
    s.execute = AsyncMock(side_effect=[r_ov])
    template = {
        "id": "pipil-sensitive",
        "sensitive_categories": ["BIOMETRIC", "HEALTH", "FINANCIAL", "GPS"],
    }
    out = await repo.apply_sensitivity_template(cat, template)
    assert out["changed"] is True
    assert "HEALTH" in out["applied_categories"]
    assert cat.sensitivity_level == "PII"


async def test_repo_review_catalog_sets_audit_fields() -> None:
    s = _session()
    repo = AssetMapRepository(s)
    entity = await _pii_entity()
    updated = await repo.review_catalog(entity, "APPROVE", reviewer_id=9)
    assert updated.compliance_reviewed is True
    assert updated.compliance_reviewed_by == 9
    assert updated.compliance_reviewed_at is not None
    # 缺省脱敏策略：PII → hash
    assert updated.masking_policy == "hash"
