"""术语库服务单元测试（TD §12.14 / FR-08）。"""

from __future__ import annotations

from typing import Any
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


async def test_create_term_auto_generates_code() -> None:
    """term_code 缺省时由系统自动生成（domain_name slug），非人为创造。"""
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
        term_code=None,
        name="Active User",
        definition="d",
        domain="user",
        owner_id=1,
    )
    resp = await svc.create_term(payload, 1)
    assert resp.term_code == "user_active_user"
    assert resp.status == TermStatus.DRAFT


async def test_create_term_auto_code_chinese_to_english() -> None:
    """纯中文名 → 英文 slug（如 活跃用户 + user → user_active_user）。"""
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
        term_code=None,
        name="活跃用户",
        definition="d",
        domain="user",
        owner_id=1,
    )
    resp = await svc.create_term(payload, 1)
    assert resp.term_code == "user_active_user"


# ---- 编码自动生成：slug 缺失降级分支 ----
async def test_create_term_auto_code_name_slug_only() -> None:
    """domain 无可提取字符（纯标点）时回退 term_{name_slug}。"""
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
        term_code=None,
        name="Active User",
        definition="d",
        domain="!@#",
        owner_id=1,
    )
    resp = await svc.create_term(payload, 1)
    assert resp.term_code == "term_active_user"


async def test_create_term_auto_code_both_empty() -> None:
    """domain/name 均为纯标点无可提取字符时回退基础编码 term。"""
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
        term_code=None,
        name="!!!",
        definition="d",
        domain="@@@",
        owner_id=1,
    )
    resp = await svc.create_term(payload, 1)
    assert resp.term_code == "term"


async def test_submit_term_from_deprecated_republishes() -> None:
    """状态机：已废弃术语可重新发布（DEPRECATED→PUBLISHED），已发布幂等。"""
    db = MagicMock()
    svc = GlossaryService(db)
    repo = MagicMock()
    repo.get_term = AsyncMock(return_value=None)
    repo.save_term_version = AsyncMock()
    repo.commit = AsyncMock()
    svc._repo = repo
    svc._snapshot = AsyncMock()  # 跳过快照计数

    # DEPRECATED → PUBLISHED
    deprecated = _make_term()
    deprecated.id = 1
    deprecated.status = TermStatus.DEPRECATED.value
    repo.get_term = AsyncMock(return_value=deprecated)
    resp = await svc.submit_term("c1", 1)
    assert resp.status == TermStatus.PUBLISHED

    # 已发布幂等：重复提交不报错、状态不变
    published = _make_term()
    published.id = 1
    published.status = TermStatus.PUBLISHED.value
    repo.get_term = AsyncMock(return_value=published)
    resp2 = await svc.submit_term("c1", 1)
    assert resp2.status == TermStatus.PUBLISHED


async def test_deprecate_already_deprecated_raises_business_error() -> None:
    """修复 500：已废弃术语重复废弃应抛 4xx（BusinessError）而非 500。"""
    from app.core.exceptions import BusinessError

    db = MagicMock()
    svc = GlossaryService(db)
    repo = MagicMock()
    t = _make_term()
    t.id = 1
    t.status = TermStatus.DEPRECATED.value
    repo.get_term = AsyncMock(return_value=t)
    svc._repo = repo
    try:
        await svc.deprecate_term("c1", 1)
        raise AssertionError("应抛 BusinessError")
    except BusinessError as exc:
        assert exc.http_status == 400


async def test_infer_term_suggestion_success() -> None:
    """LLM 推断术语定义/同义词/边界说明：LLM 返回结构化 JSON → 解析成功。"""
    db = MagicMock()
    svc = GlossaryService(db)

    class _FakeClient:
        def __init__(self) -> None:
            self.enabled = True
            self.closed = False

        async def chat(self, messages: list[dict[str, str]], **kw: Any) -> dict[str, Any]:
            return {
                "content": (
                    '{"definition": "成交总额（GMV），一段时间内成交订单的总金额", '
                    '"synonyms": ["gmv", "总成交额"], "boundary": "不含退款订单", '
                    '"confidence": 0.9}'
                )
            }

        async def close(self) -> None:
            self.closed = True

    svc._build_llm_client = AsyncMock(return_value=_FakeClient())
    result = await svc.infer_term_suggestion("成交总额")
    assert result["definition"].startswith("成交总额")
    assert "gmv" in result["synonyms"]
    assert result["boundary"] == "不含退款订单"
    assert result["confidence"] == 0.9


async def test_infer_term_suggestion_llm_unavailable_raises() -> None:
    """LLM 未配置 → 抛 BusinessError(LLM_INFER_UNAVAILABLE)，前端可提示。"""
    from app.core.exceptions import BusinessError

    db = MagicMock()
    svc = GlossaryService(db)

    class _DisabledClient:
        enabled = False

    svc._build_llm_client = AsyncMock(return_value=_DisabledClient())
    try:
        await svc.infer_term_suggestion("成交总额")
        raise AssertionError("应抛 BusinessError")
    except BusinessError as exc:
        assert exc.error_code == "LLM_INFER_UNAVAILABLE"


async def test_update_term_changes_code() -> None:
    """编辑术语编码：变更 term_code + 快照记录新编码。"""
    db = MagicMock()
    svc = GlossaryService(db)
    term = _make_term()
    _persist(term)
    repo = MagicMock()
    # 第一次 get_term(原编码) 命中当前术语；第二次 get_term(新编码) 无冲突
    repo.get_term = AsyncMock(side_effect=[term, None])
    repo.count_term_versions = AsyncMock(return_value=0)
    repo.save_term_version = AsyncMock()
    repo.all_terms = AsyncMock(return_value=[])
    repo.save_conflict = AsyncMock()
    repo.commit = AsyncMock()
    svc._repo = repo

    from app.services.glossary.schemas import TermUpdate

    resp = await svc.update_term("c1", TermUpdate(term_code="c1_new", name="活跃用户新"), 1)
    assert term.term_code == "c1_new"
    assert resp.term_code == "c1_new"
    repo.commit.assert_awaited()


async def test_update_term_code_conflict_raises() -> None:
    """编辑编码与已有术语冲突 → ConflictError(TERM_EXISTS)。"""
    from app.core.exceptions import ConflictError

    db = MagicMock()
    svc = GlossaryService(db)
    term = _make_term()
    _persist(term)
    other = _make_term()
    other.term_code = "c2"
    repo = MagicMock()
    # 第一次 get_term(原编码) 命中当前术语；随后 get_term(新编码) 命中冲突术语
    repo.get_term = AsyncMock(side_effect=[term, other])
    svc._repo = repo

    from app.services.glossary.schemas import TermUpdate

    try:
        await svc.update_term("c1", TermUpdate(term_code="c2"), 1)
        raise AssertionError("应抛 ConflictError")
    except ConflictError as exc:
        assert exc.error_code == "TERM_EXISTS"


async def test_batch_submit_terms_partial_failure() -> None:
    """批量发布：逐条处理，部分失败不阻断成功项（207 语义）。"""
    db = MagicMock()
    svc = GlossaryService(db)
    # submit_term 内部依赖 _require_term + _snapshot + _repo.commit
    repo = MagicMock()
    draft = _make_term()
    draft.term_code = "ok1"
    _persist(draft)
    repo.get_term = AsyncMock(side_effect=lambda code: draft if code == "ok1" else None)
    repo.count_term_versions = AsyncMock(return_value=0)
    repo.save_term_version = AsyncMock()
    repo.commit = AsyncMock()
    svc._repo = repo

    results = await svc.batch_submit_terms(["ok1", "missing2"], 1)
    assert len(results) == 2
    assert results[0]["ok"] is True and results[0]["status"] == "PUBLISHED"
    assert results[1]["ok"] is False


async def test_batch_deprecate_terms_all_success() -> None:
    """批量废弃全成功。"""
    db = MagicMock()
    svc = GlossaryService(db)
    repo = MagicMock()
    t1 = _make_term()
    t1.term_code = "a1"
    t2 = _make_term()
    t2.term_code = "a2"
    _persist(t1)
    _persist(t2)
    repo.get_term = AsyncMock(side_effect=lambda code: t1 if code == "a1" else t2)
    repo.count_term_versions = AsyncMock(return_value=0)
    repo.save_term_version = AsyncMock()
    repo.commit = AsyncMock()
    svc._repo = repo

    results = await svc.batch_deprecate_terms(["a1", "a2"], 1)
    assert all(r["ok"] for r in results)
    assert all(r["status"] == "DEPRECATED" for r in results)


def test_relation_type_enum_has_eight_values() -> None:
    """关系类型枚举扩展为 8 种（产品丰富增强）。"""
    from app.models.glossary import TermRelationType

    values = {e.value for e in TermRelationType}
    assert values == {
        "SYNONYM_OF",
        "BROADER_THAN",
        "NARROWER_THAN",
        "RELATED_TO",
        "ANTONYM_OF",
        "DEPENDS_ON",
        "DERIVED_FROM",
        "INSTANCE_OF",
    }
