"""治理策略纯函数单元测试（PDP 决策 + PII 规则识别，TD §12.5）。

覆盖：默认拒绝、跨域授权、白名单、TTL 失效、PII 门禁、行级标记、
规则命中/置信度/只升不降、脱敏映射。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.governance import SensitivityLevel
from app.services.governance.policy import (
    Resource,
    Subject,
    decide,
    detect_pii_columns,
    infer_sensitivity,
    is_grant_effective,
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
