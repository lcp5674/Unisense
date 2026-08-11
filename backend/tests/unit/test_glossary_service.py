"""术语库服务单元测试（TD §12.14 / FR-08）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.models.glossary import GlossaryConflict, TermRelation, TermVersion
from app.models.term import Term
from app.services.glossary.schemas import (
    TermCreate,
    TermRelationResponse,
    TermStatus,
    TermVersionResponse,
)
from app.services.glossary.service import GlossaryService, _normalize, _overlap_ratio


def _make_term() -> Term:
    return Term(
        term_code="c1",
        name="活跃用户",
        definition="近 30 天有行为的用户",
        domain="user",
        synonyms=["au", "active_user"],
        status=TermStatus.DRAFT.value,
        owner_id=1,
    )


def _persist(t: Term) -> Term:
    t.id = 1
    return t


def test_normalize_lowercases_and_strips() -> None:
    assert _normalize("  Active User ") == "active user"


def test_overlap_ratio_full_and_empty() -> None:
    assert _overlap_ratio(["a", "b"], ["b", "a"]) == 1.0
    assert _overlap_ratio([], ["a"]) == 0.0
    assert _overlap_ratio(["a", "b"], ["c"]) == 0.0


def test_overlap_ratio_partial() -> None:
    # {a,b} ∩ {b,c} = {b}; union = {a,b,c} => 1/3
    assert abs(_overlap_ratio(["a", "b"], ["b", "c"]) - 1 / 3) < 1e-9


def test_term_version_from_model() -> None:
    v = TermVersion(id=1, term_id=2, version=3, snapshot={"name": "x"}, changed_by=5)
    resp = TermVersionResponse.from_model(v)
    assert resp.id == 1 and resp.version == 3 and resp.changed_by == 5


def test_term_relation_from_model() -> None:
    r = TermRelation(
        id=7,
        source_term_id=1,
        target_term_id=2,
        relation_type="SYNONYM_OF",
        declared_by=3,
        source_type="MANUAL",
    )
    resp = TermRelationResponse.from_model(r)
    assert resp.id == 7 and resp.relation_type == "SYNONYM_OF"


async def test_create_term_persists_and_snapshots() -> None:
    db = MagicMock()
    svc = GlossaryService(db)
    repo = MagicMock()
    repo.get_term = AsyncMock(return_value=None)
    repo.save_term = AsyncMock(side_effect=lambda t: _persist(t))
    repo.count_term_versions = AsyncMock(return_value=0)
    repo.save_term_version = AsyncMock()
    repo.all_terms = AsyncMock(return_value=[])
    repo.save_conflict = AsyncMock()
    repo.commit = AsyncMock()
    svc._repo = repo

    payload = TermCreate(
        term_code="c1",
        name="活跃用户",
        definition="d",
        domain="user",
        synonyms=["au"],
        owner_id=1,
    )
    resp = await svc.create_term(payload, 1)
    assert resp.term_code == "c1"
    assert resp.status == TermStatus.DRAFT
    repo.save_term.assert_awaited()
    repo.save_term_version.assert_awaited()
    repo.commit.assert_awaited()
    repo.save_conflict.assert_not_awaited()


async def test_create_term_detects_alias_overlap() -> None:
    db = MagicMock()
    svc = GlossaryService(db)
    other = _make_term()
    repo = MagicMock()
    repo.get_term = AsyncMock(return_value=None)
    repo.save_term = AsyncMock(side_effect=lambda t: _persist(t))
    repo.count_term_versions = AsyncMock(return_value=0)
    repo.save_term_version = AsyncMock()
    repo.all_terms = AsyncMock(return_value=[other])
    repo.save_conflict = AsyncMock()
    repo.commit = AsyncMock()
    svc._repo = repo

    # 重合同义词超过 80% 应触发冲突
    payload = TermCreate(
        term_code="c2",
        name="活跃用户",
        definition="d",
        domain="user",
        synonyms=["au", "active_user", "x"],
        owner_id=1,
    )
    await svc.create_term(payload, 1)
    repo.save_conflict.assert_awaited()
    conflict: GlossaryConflict = repo.save_conflict.call_args.args[0]
    assert conflict.conflict_type in ("alias_overlap", "name_overlap")
