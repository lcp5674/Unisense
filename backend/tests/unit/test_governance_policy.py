"""治理策略纯函数单元测试（PDP 决策 + PII 规则识别，TD §12.5）。

覆盖：默认拒绝、跨域授权、白名单、TTL 失效、PII 门禁、行级标记、
规则命中/置信度/只升不降、脱敏映射。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.governance import RoleName, SensitivityLevel
from app.services.governance.policy import (
    ROLE_ACTIONS,
    ROLE_UI_ACTIONS,
    UI_ACTION_REGISTRY,
    Resource,
    Subject,
    decide,
    detect_pii_columns,
    infer_sensitivity,
    is_grant_effective,
    is_ui_action,
    masking_for,
)

_FUTURE = datetime.now(UTC) + timedelta(days=1)
_PAST = datetime.now(UTC) - timedelta(days=1)


def _grant(**over: object) -> dict:
    base: dict = {
        "id": 1,
        "domain": "sales",
        "metric_whitelist": [],
        "grant_type": "READ",
        "status": "ACTIVE",
        "row_level": False,
        "expires_at": None,
    }
    base.update(over)
    return base


# ------------------------------------------------------------------ PDP 决策


def test_role_name_includes_analyst() -> None:
    """analyst 是系统真实支持的只读消费者角色（User.role 枚举/deps/policy 均含）。

    对齐 RoleName 与 UserRole/User.role，避免双权威源漂移（7 角色单源）。
    """
    assert RoleName.ANALYST.value == "analyst"


def test_analyst_role_actions_read_only() -> None:
    """analyst 在策略默认动作集中为只读（read），与 User.role 枚举一致。"""
    assert "analyst" in ROLE_ACTIONS
    assert ROLE_ACTIONS["analyst"] == frozenset({"read"})
    # 用 RoleName 引用（单源），非硬编码字符串
    assert ROLE_ACTIONS[RoleName.ANALYST.value] == frozenset({"read"})


def test_unknown_action_rejected() -> None:
    d = decide(Subject(1, "platform_admin"), "delete", Resource(domain="sales"))
    assert d.allow is False
    assert d.error_code == "VALIDATION_ERROR"


def test_default_deny_without_role_or_grant() -> None:
    """fail-closed：无角色权限、无授权 → 拒绝。"""
    d = decide(Subject(1, "viewer", domain="hr"), "read", Resource(domain="sales"))
    assert d.allow is False
    assert d.error_code == "FORBIDDEN"


def test_platform_admin_cross_domain_allowed() -> None:
    d = decide(Subject(1, "platform_admin", domain="ops"), "write", Resource(domain="sales"))
    assert d.allow is True


def test_same_domain_role_allowed() -> None:
    d = decide(Subject(1, "domain_admin", domain="sales"), "approve", Resource(domain="sales"))
    assert d.allow is True


def test_viewer_cannot_write_in_own_domain() -> None:
    d = decide(Subject(1, "viewer", domain="sales"), "write", Resource(domain="sales"))
    assert d.allow is False


def test_metric_owner_cannot_write_others_metric() -> None:
    d = decide(
        Subject(7, "metric_owner", domain="sales"),
        "write",
        Resource(domain="sales", metric_code="m1", owner_id=99),
    )
    assert d.allow is False
    assert "本人" in d.reason


def test_metric_owner_can_write_own_metric() -> None:
    d = decide(
        Subject(7, "metric_owner", domain="sales"),
        "write",
        Resource(domain="sales", metric_code="m1", owner_id=7),
    )
    assert d.allow is True


def test_cross_domain_grant_allows_read() -> None:
    d = decide(
        Subject(1, "viewer", domain="hr", grants=(_grant(),)),
        "read",
        Resource(domain="sales"),
    )
    assert d.allow is True
    assert "grant#1" in d.reason


def test_read_grant_does_not_allow_write() -> None:
    d = decide(
        Subject(1, "viewer", domain="hr", grants=(_grant(),)),
        "write",
        Resource(domain="sales"),
    )
    assert d.allow is False


def test_whitelist_scopes_grant_to_listed_metric_only() -> None:
    g = _grant(metric_whitelist=["sales_gmv_daily"])
    subject = Subject(1, "viewer", domain="hr", grants=(g,))
    allowed = decide(subject, "read", Resource(domain="sales", metric_code="sales_gmv_daily"))
    denied = decide(subject, "read", Resource(domain="sales", metric_code="sales_cost_daily"))
    assert allowed.allow is True
    assert denied.allow is False


def test_expired_grant_is_ignored() -> None:
    d = decide(
        Subject(1, "viewer", domain="hr", grants=(_grant(expires_at=_PAST),)),
        "read",
        Resource(domain="sales"),
    )
    assert d.allow is False


def test_revoked_grant_is_ignored() -> None:
    d = decide(
        Subject(1, "viewer", domain="hr", grants=(_grant(status="REVOKED"),)),
        "read",
        Resource(domain="sales"),
    )
    assert d.allow is False


def test_row_level_grant_marks_restricted() -> None:
    d = decide(
        Subject(1, "viewer", domain="hr", grants=(_grant(row_level=True, expires_at=_FUTURE),)),
        "read",
        Resource(domain="sales"),
    )
    assert d.allow is True
    assert d.restricted is True


def test_unreviewed_pii_blocks_even_platform_admin() -> None:
    """COMPL-1：未复核的 PII 资产，任何角色（含超管）读取均拒绝。"""
    d = decide(
        Subject(1, "platform_admin", domain="sales"),
        "read",
        Resource(domain="sales", sensitivity="PII", compliance_reviewed=False),
    )
    assert d.allow is False
    assert d.error_code == "FORBIDDEN_PII"


def test_compliance_officer_may_review_unreviewed_pii() -> None:
    d = decide(
        Subject(2, "compliance_officer", domain="sales"),
        "review",
        Resource(domain="sales", sensitivity="PII", compliance_reviewed=False),
    )
    assert d.allow is True


def test_reviewed_pii_readable_with_hash_masking() -> None:
    d = decide(
        Subject(1, "domain_admin", domain="sales"),
        "read",
        Resource(domain="sales", sensitivity="PII", compliance_reviewed=True),
    )
    assert d.allow is True
    assert d.masking == "hash"


def test_is_grant_effective_handles_naive_and_iso() -> None:
    assert is_grant_effective(None) is True
    assert is_grant_effective(_FUTURE.replace(tzinfo=None)) is True
    assert is_grant_effective(_PAST.isoformat()) is False
    assert is_grant_effective("not-a-date") is False
    assert is_grant_effective(12345) is False


# ------------------------------------------------------------- PII 规则引擎


def test_detect_pii_by_column_name() -> None:
    hits = detect_pii_columns({"columns": [{"name": "user_phone"}, {"name": "amount"}]})
    assert [h.column for h in hits] == ["user_phone"]
    assert hits[0].rule == "phone"
    assert hits[0].matched_by == "name"


def test_sample_match_raises_confidence() -> None:
    hits = detect_pii_columns(
        {"columns": [{"name": "phone", "sample": "13800000000"}]},
    )
    assert hits[0].matched_by == "name+sample"
    assert hits[0].confidence > 0.9


def test_detect_supports_fields_key_and_plain_strings() -> None:
    hits = detect_pii_columns({"fields": ["id_card", "gmv"]})
    assert [h.rule for h in hits] == ["id_card"]


def test_detect_returns_empty_for_unknown_shape() -> None:
    assert detect_pii_columns({}) == []
    assert detect_pii_columns({"columns": [{"name": ""}]}) == []


def test_infer_sensitivity_levels() -> None:
    assert infer_sensitivity([]) is SensitivityLevel.INTERNAL
    hits = detect_pii_columns({"columns": [{"name": "id_card"}]})
    assert infer_sensitivity(hits) is SensitivityLevel.PII
    low = detect_pii_columns({"columns": [{"name": "geo_lat"}]})
    assert infer_sensitivity(low) is SensitivityLevel.CONFIDENTIAL


def test_infer_sensitivity_never_downgrades_manual_high_level() -> None:
    """人工已判 PII 的资产，规则未命中时不得被自动降级。"""
    assert infer_sensitivity([], current="PII") is SensitivityLevel.PII
    assert infer_sensitivity([], current="CONFIDENTIAL") is SensitivityLevel.CONFIDENTIAL
    assert infer_sensitivity([], current="PUBLIC") is SensitivityLevel.INTERNAL


def test_masking_mapping() -> None:
    assert masking_for(SensitivityLevel.PUBLIC) == "none"
    assert masking_for("CONFIDENTIAL") == "mask"
    assert masking_for(SensitivityLevel.PII) == "hash"
    assert masking_for("WHATEVER") == "mask"


# ------------------------------------------------- RBAC 可配置化（role_actions 参数）

def test_role_actions_override_enables_action() -> None:
    """覆盖映射为 reviewer 追加 export 后，本域可导出（默认 reviewer 无 export）。"""
    subject = Subject(1, "reviewer", domain="sales")
    resource = Resource(domain="sales")
    # 默认：reviewer 不可导出
    assert decide(subject, "export", resource).allow is False
    # 覆盖后：reviewer 可导出
    overrides = {
        "reviewer": frozenset({"read", "approve", "export"}),
    }
    d = decide(subject, "export", resource, role_actions=overrides)
    assert d.allow is True


def test_role_actions_override_revokes_action() -> None:
    """覆盖映射将 metric_owner 的 write 收窄后，本域写被拒绝（权限点可回收）。"""
    subject = Subject(7, "metric_owner", domain="sales")
    resource = Resource(domain="sales", metric_code="m1", owner_id=7)
    # 默认：本人负责指标可写
    assert decide(subject, "write", resource).allow is True
    # 覆盖为仅 read：write 被回收
    overrides = {"metric_owner": frozenset({"read"})}
    d = decide(subject, "write", resource, role_actions=overrides)
    assert d.allow is False


def test_role_actions_does_not_affect_platform_admin_direct() -> None:
    """platform_admin 跨域直通为硬编码保护，不因覆盖映射收窄。"""
    subject = Subject(1, "platform_admin", domain="ops")
    resource = Resource(domain="sales")
    overrides = {"platform_admin": frozenset({"read"})}
    d = decide(subject, "write", resource, role_actions=overrides)
    assert d.allow is True


def test_role_actions_unknown_role_still_denied() -> None:
    """覆盖映射不含的角色按映射缺失处理（fail-closed 保持）。"""
    d = decide(Subject(1, "viewer", domain="sales"), "write", Resource(domain="sales"))
    assert d.allow is False
    overrides = {"reviewer": frozenset({"read", "write"})}
    d2 = decide(
        Subject(1, "viewer", domain="sales"),
        "write",
        Resource(domain="sales"),
        role_actions=overrides,
    )
    assert d2.allow is False


# ------------------------------------------------------------------- UI 权限点注册表完整性


def test_ui_action_registry_keys_wellformed_and_unique() -> None:
    """注册表键须符合 ``模块:功能`` 格式且无重复（防止拼写漂移/脏键）。"""
    keys = list(UI_ACTION_REGISTRY)
    assert len(keys) == len(set(keys)), "存在重复权限点键"
    for k in keys:
        assert k.count(":") == 1, f"权限点须为 模块:功能 单冒号格式: {k}"
        module, func = k.split(":", 1)
        assert module.islower() and func.islower(), f"权限点须全小写: {k}"
        assert module and func, f"权限点模块/功能不可为空: {k}"


def test_ui_action_registry_entries_have_metadata() -> None:
    """每个权限点须带 module/label/description（供前端分组渲染与 Tooltip）。"""
    for key, meta in UI_ACTION_REGISTRY.items():
        assert isinstance(meta, dict), f"{key} 元数据须为 dict"
        assert meta.get("module"), f"{key} 缺 module"
        assert meta.get("label"), f"{key} 缺 label"
        assert meta.get("description"), f"{key} 缺 description"


def test_ui_action_registry_no_placeholder_residue() -> None:
    """注册表不允许占位/清理残留键（如 templates:view_extra）。"""
    assert "templates:view_extra" not in UI_ACTION_REGISTRY
    # 不允许空 dict 元数据（占位键特征）
    for key, meta in UI_ACTION_REGISTRY.items():
        assert meta, f"{key} 为空元数据（疑似占位残留）"


def test_role_ui_actions_references_all_exist() -> None:
    """ROLE_UI_ACTIONS 引用的每个权限点必须真实存在于注册表（防悬空）。"""
    registry = set(UI_ACTION_REGISTRY)
    for role, actions in ROLE_UI_ACTIONS.items():
        missing = [a for a in actions if a not in registry]
        assert not missing, f"角色 {role} 引用了未注册权限点: {missing}"


def test_every_ui_action_covered_by_some_role() -> None:
    """每个权限点至少被一个内置角色默认覆盖（无孤儿权限点）。"""
    registry = set(UI_ACTION_REGISTRY)
    covered = set().union(*ROLE_UI_ACTIONS.values())
    orphans = registry - covered
    assert not orphans, f"未分配给任何内置角色的权限点: {sorted(orphans)}"


def test_is_ui_action_matches_registry() -> None:
    """is_ui_action 与注册表键集一致（资源动词与 UI 权限点正交）。"""
    for key in UI_ACTION_REGISTRY:
        assert is_ui_action(key), f"{key} 应在 is_ui_action 命中"
    for v in ("read", "write", "approve", "export", "review"):
        assert not is_ui_action(v), f"资源动词 {v} 不应被判为 UI 权限点"


def test_ui_action_registry_not_under_10() -> None:
    """注册表规模不低于 10（防误删导致注册表塌缩的浅层防御）。"""
    assert len(UI_ACTION_REGISTRY) >= 10, "UI 权限点注册表规模异常偏小"
