"""术语库服务单元测试（TD §12.14 / FR-08）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.glossary import GlossaryConflict, TermRelation, TermVersion
from app.models.term import Term
from app.services.glossary.schemas import (
    TermCreate,
    TermRelationResponse,
    TermStatus,
    TermUpdate,
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
    # P2-11: _add_conflict 会查询绑定到术语的指标（ref_metric_id 填充）；无绑定时 first()=None
    db.execute = AsyncMock(return_value=MagicMock(first=lambda: None))
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
    # 无绑定指标 → ref_metric_id 为 None
    assert conflict.ref_metric_id is None


async def test_add_conflict_sets_ref_metric_id_when_term_bound() -> None:
    """P2-11: 术语已绑定指标（metric.term_id）→ 冲突行 ref_metric_id 填充。"""
    db = MagicMock()
    # 绑定到该术语的指标 id=77
    db.execute = AsyncMock(return_value=MagicMock(first=lambda: (77,)))
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

    payload = TermCreate(
        term_code="c2",
        name="活跃用户",
        definition="d",
        domain="user",
        synonyms=["au", "active_user", "x"],
        owner_id=1,
    )
    await svc.create_term(payload, 1)

    conflict: GlossaryConflict = repo.save_conflict.call_args.args[0]
    assert conflict.ref_metric_id == 77


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


async def test_publish_term_from_deprecated_republishes() -> None:
    """状态机：admin 直发 publish_term 已废弃术语可再次发布（DEPRECATED→PUBLISHED），已发布幂等。"""
    db = MagicMock()
    svc = GlossaryService(db)
    repo = MagicMock()
    repo.get_term = AsyncMock(return_value=None)
    repo.save_term_version = AsyncMock()
    repo.commit = AsyncMock()
    svc._repo = repo
    svc._snapshot = AsyncMock()  # 跳过快照计数

    # DEPRECATED → PUBLISHED（publish_term 直发通道，含"再次发布"能力）
    deprecated = _make_term()
    deprecated.id = 1
    deprecated.status = TermStatus.DEPRECATED.value
    repo.get_term = AsyncMock(return_value=deprecated)
    resp = await svc.publish_term("c1", 1)
    assert resp.status == TermStatus.PUBLISHED

    # 已发布幂等：重复发布不报错、状态不变
    published = _make_term()
    published.id = 1
    published.status = TermStatus.PUBLISHED.value
    repo.get_term = AsyncMock(return_value=published)
    resp2 = await svc.publish_term("c1", 1)
    assert resp2.status == TermStatus.PUBLISHED


async def test_submit_term_goes_to_review() -> None:
    """审核流：业务用户 submit_term 提交审核（DRAFT → REVIEW），并写入提交人/清空驳回。"""
    from app.services.master_data_review.schemas import ReviewSubmitRequest

    db = MagicMock()
    svc = GlossaryService(db)
    repo = MagicMock()
    term = _make_term()
    term.id = 1
    term.status = TermStatus.DRAFT.value
    repo.get_term = AsyncMock(return_value=term)
    repo.commit = AsyncMock()
    svc._repo = repo
    svc._notify_reviewers = AsyncMock()

    resp = await svc.submit_term(
        "c1", ReviewSubmitRequest(change_reason="完善术语定义后提审"), 1, "metric_owner", "user"
    )
    assert resp.status == TermStatus.REVIEW
    assert term.submitted_by == 1


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


def _svc_with_term(
    status: str, owner_id: int = 1, deleted_at=None
) -> tuple[GlossaryService, MagicMock, Term]:
    """构造挂到指定状态术语的 service + fake repo（供生命周期用例复用）。"""
    db = MagicMock()
    svc = GlossaryService(db)
    repo = MagicMock()
    t = _make_term()
    t.id = 1
    t.owner_id = owner_id
    t.status = status
    t.deleted_at = deleted_at
    repo.get_term = AsyncMock(return_value=t)
    repo.get_term_including_deleted = AsyncMock(return_value=t)
    repo.soft_delete_term = AsyncMock()
    repo.restore_term = AsyncMock()
    repo.commit = AsyncMock()
    repo.count_term_versions = AsyncMock(return_value=0)
    repo.save_term_version = AsyncMock()
    repo.all_terms = AsyncMock(return_value=[])
    repo.save_conflict = AsyncMock()
    svc._repo = repo
    return svc, repo, t


async def test_reactivate_term_deprecated_to_draft() -> None:
    """生命周期：DEPRECATED 术语重新启用 → DRAFT（回草稿重新走审核），快照留痕。"""
    svc, repo, t = _svc_with_term(TermStatus.DEPRECATED.value)
    resp = await svc.reactivate_term("c1", actor_id=1, role="domain_admin")
    assert resp.status == TermStatus.DRAFT
    assert t.status == TermStatus.DRAFT.value
    repo.save_term_version.assert_awaited()
    repo.commit.assert_awaited()


async def test_reactivate_term_requires_deprecated() -> None:
    """仅 DEPRECATED 可重新启用：PUBLISHED 抛 INVALID_STATE。"""
    from app.core.exceptions import UnisenseError

    svc, _repo, _t = _svc_with_term(TermStatus.PUBLISHED.value)
    with pytest.raises(UnisenseError) as ei:
        await svc.reactivate_term("c1", actor_id=1, role="domain_admin")
    assert ei.value.error_code == "INVALID_STATE"


async def test_reactivate_term_forbidden_for_non_owner() -> None:
    """非管理员且非原 Owner 不可重新启用。"""
    from app.core.exceptions import UnisenseError

    svc, _repo, _t = _svc_with_term(TermStatus.DEPRECATED.value, owner_id=99)
    with pytest.raises(UnisenseError) as ei:
        await svc.reactivate_term("c1", actor_id=1, role="metric_owner")
    assert ei.value.error_code == "FORBIDDEN"


async def test_delete_term_draft_soft_deletes() -> None:
    """生命周期：DRAFT 术语软删 → repo.soft_delete_term 被调 + 快照留痕。"""
    svc, repo, t = _svc_with_term(TermStatus.DRAFT.value)
    resp = await svc.delete_term("c1", actor_id=1, role="metric_owner")
    assert resp.status == TermStatus.DRAFT
    repo.soft_delete_term.assert_awaited_with(1)
    repo.save_term_version.assert_awaited()
    repo.commit.assert_awaited()


async def test_delete_term_review_or_published_rejected() -> None:
    """审核中/启用中不可删：REVIEW/PUBLISHED 抛 INVALID_STATE（用户决策边界）。"""
    from app.core.exceptions import UnisenseError

    for status in (TermStatus.REVIEW.value, TermStatus.PUBLISHED.value):
        svc, _repo, _t = _svc_with_term(status)
        with pytest.raises(UnisenseError) as ei:
            await svc.delete_term("c1", actor_id=1, role="domain_admin")
        assert ei.value.error_code == "INVALID_STATE"


async def test_delete_term_forbidden_for_non_owner() -> None:
    """非管理员且非原 Owner 不可删除。"""
    from app.core.exceptions import UnisenseError

    svc, _repo, _t = _svc_with_term(TermStatus.DRAFT.value, owner_id=99)
    with pytest.raises(UnisenseError) as ei:
        await svc.delete_term("c1", actor_id=1, role="metric_owner")
    assert ei.value.error_code == "FORBIDDEN"


async def test_restore_term_recovers_deleted() -> None:
    """生命周期：回收站恢复软删术语 → 清除 deleted_at（repo.restore_term 被调）。"""
    from datetime import UTC, datetime

    svc, repo, t = _svc_with_term(TermStatus.DRAFT.value, deleted_at=datetime.now(UTC))
    resp = await svc.restore_term("c1", actor_id=1, role="domain_admin")
    assert resp.term_code == "c1"
    repo.restore_term.assert_awaited_with(1)
    repo.commit.assert_awaited()


async def test_restore_term_requires_deleted() -> None:
    """未删除的术语无需恢复：抛 INVALID_STATE。"""
    from app.core.exceptions import UnisenseError

    svc, _repo, _t = _svc_with_term(TermStatus.DRAFT.value, deleted_at=None)
    with pytest.raises(UnisenseError) as ei:
        await svc.restore_term("c1", actor_id=1, role="domain_admin")
    assert ei.value.error_code == "INVALID_STATE"


async def test_get_term_visible_public_for_anyone() -> None:
    """已发布术语对任何登录用户可见（消费场景）。"""

    svc, _repo, _t = _svc_with_term(TermStatus.PUBLISHED.value, owner_id=3)
    resp = await svc.get_term_visible("c1", actor_id=7, role="analyst")
    assert resp.term_code == "c1"
    # 管理角色/owner/reviewer 均可见
    await svc.get_term_visible("c1", actor_id=7, role="platform_admin")
    await svc.get_term_visible("c1", actor_id=3, role="analyst")
    await svc.get_term_visible("c1", actor_id=9, role="reviewer")


async def test_get_term_visible_draft_only_owner_or_admin() -> None:
    """草稿术语仅本人/管理角色可见；他人读取按不存在处理（不泄露存在性）。"""
    from app.core.exceptions import NotFoundError

    svc, _repo, _t = _svc_with_term(TermStatus.DRAFT.value, owner_id=3)
    # 本人可见
    await svc.get_term_visible("c1", actor_id=3, role="analyst")
    # 管理角色可见
    await svc.get_term_visible("c1", actor_id=1, role="platform_admin")
    # 他人不可见（NotFound，非 403——不泄露存在性）
    with pytest.raises(NotFoundError):
        await svc.get_term_visible("c1", actor_id=7, role="analyst")
    # reviewer 对 DRAFT 不可见（仅 REVIEW 待审放行）
    with pytest.raises(NotFoundError):
        await svc.get_term_visible("c1", actor_id=9, role="reviewer")


async def test_get_term_visible_review_reviewer_sees() -> None:
    """待审（REVIEW）术语：评审人可见（审批工作台）；普通他人不可见。"""
    from app.core.exceptions import NotFoundError

    svc, _repo, _t = _svc_with_term(TermStatus.REVIEW.value, owner_id=3)
    await svc.get_term_visible("c1", actor_id=9, role="reviewer")
    with pytest.raises(NotFoundError):
        await svc.get_term_visible("c1", actor_id=7, role="analyst")


async def test_get_term_visible_internal_no_context_passthrough() -> None:
    """内部调用（actor/role 均为 None）不过滤——端点层必传鉴权上下文。"""
    svc, _repo, _t = _svc_with_term(TermStatus.DRAFT.value, owner_id=3)
    resp = await svc.get_term_visible("c1")
    assert resp.term_code == "c1"


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


async def test_update_term_optimistic_lock_conflict() -> None:
    """P11 C-2：row_version 不匹配（他人已改）→ 409 乐观锁冲突，不落库。"""
    from app.core.exceptions import ConflictError
    from app.services.glossary.schemas import TermUpdate

    db = MagicMock()
    svc = GlossaryService(db)
    term = _make_term()
    term.row_version = 3
    _persist(term)
    repo = MagicMock()
    repo.get_term = AsyncMock(side_effect=[term, None])
    repo.commit = AsyncMock()
    svc._repo = repo

    with pytest.raises(ConflictError) as exc:
        await svc.update_term("c1", TermUpdate(name="新名", row_version=2), 1)
    assert exc.value.error_code == "OPTIMISTIC_LOCK_CONFLICT"
    assert term.name == "活跃用户"  # 未被修改
    repo.commit.assert_not_awaited()


async def test_update_term_optimistic_lock_success_increments() -> None:
    """P11 C-2：row_version 匹配 → 成功更新并递增版本。"""
    from app.services.glossary.schemas import TermUpdate

    db = MagicMock()
    svc = GlossaryService(db)
    term = _make_term()
    term.row_version = 2
    _persist(term)
    repo = MagicMock()
    repo.get_term = AsyncMock(side_effect=[term, None])
    repo.count_term_versions = AsyncMock(return_value=0)
    repo.save_term_version = AsyncMock()
    repo.all_terms = AsyncMock(return_value=[])
    repo.save_conflict = AsyncMock()
    repo.commit = AsyncMock()
    svc._repo = repo

    resp = await svc.update_term("c1", TermUpdate(name="新名", row_version=2), 1)
    assert resp.name == "新名"
    assert term.row_version == 3  # 2 -> 3
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


async def test_list_term_relations_outgoing_and_incoming() -> None:
    """查术语关系：出向（本术语→对端）与入向（对端→本术语）都返回，带对端信息。"""
    from unittest.mock import MagicMock

    from app.models.glossary import TermRelation

    db = MagicMock()
    svc = GlossaryService(db)
    repo = MagicMock()

    term = _make_term()
    term.id = 1
    repo.get_term = AsyncMock(return_value=term)

    peer_src = _make_term()
    peer_src.id = 2
    peer_src.term_code = "c2"
    peer_src.name = "源头术语"
    peer_tgt = _make_term()
    peer_tgt.id = 3
    peer_tgt.term_code = "c3"
    peer_tgt.name = "目标术语"

    rel_out = TermRelation(id=10, source_term_id=1, target_term_id=3, relation_type="RELATED_TO")
    rel_in = TermRelation(id=11, source_term_id=2, target_term_id=1, relation_type="BROADER_THAN")
    repo.list_term_relations = AsyncMock(
        return_value=[
            {"relation": rel_out, "relation_type": "RELATED_TO", "peer": peer_tgt},
            {"relation": rel_in, "relation_type": "BROADER_THAN", "peer": peer_src},
        ]
    )
    svc._repo = repo

    out = await svc.list_term_relations("c1")
    assert len(out) == 2
    assert out[0]["direction"] == "outgoing"  # 源是 term → 对端是 target
    assert out[0]["relation_type"] == "RELATED_TO"
    assert out[0]["peer"]["term_code"] == "c3"
    assert out[1]["direction"] == "incoming"  # 对端是 source → 本术语是 target
    assert out[1]["relation_type"] == "BROADER_THAN"
    assert out[1]["peer"]["name"] == "源头术语"


async def test_create_term_relation_duplicate_raises_conflict() -> None:
    """同对（源/目标/类型）关系重复创建时 409（预检拦截，而非 uk_term_pair 500）。"""
    from unittest.mock import MagicMock

    from app.core.exceptions import ConflictError
    from app.models.glossary import TermRelation
    from app.services.glossary.schemas import TermRelationCreate

    db = MagicMock()
    svc = GlossaryService(db)
    repo = MagicMock()

    term = _make_term()
    term.id = 1
    target = _make_term()
    target.id = 2

    repo.get_term = AsyncMock(return_value=term)
    repo.get_term_by_id = AsyncMock(return_value=target)
    # 同对关系已存在 → 预检命中 → 409
    repo.get_term_relation = AsyncMock(return_value=TermRelation())
    repo.save_term_relation = AsyncMock()
    svc._repo = repo

    payload = TermRelationCreate(
        target_term_id=2,
        relation_type="RELATED_TO",
        source_type="MANUAL",
        declared_by=1,
    )
    with pytest.raises(ConflictError) as exc:
        await svc.create_term_relation("c1", payload)
    assert exc.value.error_code == "DUPLICATE_TERM_RELATION"
    repo.save_term_relation.assert_not_awaited()


async def test_update_term_forbidden_for_cross_domain_domain_admin() -> None:
    """域管理员不可更新他域术语（越权加固：域作用域）。"""
    from app.core.exceptions import AuthError

    svc, _repo, _t = _svc_with_term(TermStatus.DRAFT.value)
    with pytest.raises(AuthError) as ei:
        await svc.update_term(
            "c1",
            TermUpdate(name="改名"),
            actor_id=1,
            role="domain_admin",
            user_domains=["finance"],
        )
    assert ei.value.error_code == "FORBIDDEN"


async def test_update_term_forbidden_for_other_owner() -> None:
    """metric_owner 不可更新他人术语（owner 校验）。"""
    from app.core.exceptions import AuthError

    svc, _repo, _t = _svc_with_term(TermStatus.DRAFT.value, owner_id=99)
    with pytest.raises(AuthError) as ei:
        await svc.update_term(
            "c1",
            TermUpdate(name="改名"),
            actor_id=1,
            role="metric_owner",
            user_domains=["user"],
        )
    assert ei.value.error_code == "FORBIDDEN"


async def test_update_term_ok_for_same_domain_domain_admin() -> None:
    """同域 domain_admin 可更新本域术语（不误伤）。"""
    svc, repo, t = _svc_with_term(TermStatus.DRAFT.value)
    resp = await svc.update_term(
        "c1",
        TermUpdate(name="改名"),
        actor_id=1,
        role="domain_admin",
        user_domains=["user"],
    )
    assert t.name == "改名"
    assert resp.status == TermStatus.DRAFT
    repo.save_term_version.assert_awaited()


async def test_create_term_forbidden_cross_domain() -> None:
    """域管理员不可创建他域术语。"""
    from app.core.exceptions import AuthError

    db = MagicMock()
    svc = GlossaryService(db)
    repo = MagicMock()
    repo.get_term = AsyncMock(return_value=None)
    svc._repo = repo
    payload = TermCreate(name="跨域术语", definition="x", domain="finance", synonyms=[])
    with pytest.raises(AuthError) as ei:
        await svc.create_term(payload, actor_id=1, role="domain_admin", user_domains=["user"])
    assert ei.value.error_code == "FORBIDDEN"


async def test_deprecate_term_forbidden_cross_domain() -> None:
    """域管理员不可废弃他域术语（deprecate 也纳入域作用域）。"""
    from app.core.exceptions import AuthError

    svc, _repo, _t = _svc_with_term(TermStatus.PUBLISHED.value)
    with pytest.raises(AuthError) as ei:
        await svc.deprecate_term(
            "c1", actor_id=1, role="domain_admin", user_domains=["finance"]
        )
    assert ei.value.error_code == "FORBIDDEN"
