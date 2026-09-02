"""governance 服务编排单元测试（FakeRepo + FakeEvents，无真实 DB）。

覆盖：授权新建/幂等合并/范围校验/TTL、批量 dry-run 与执行、到期回收、
权限快照、PII 复核（含自审拦截）、分级重扫（含引擎降级）、PDP 决策。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.exceptions import AuthError, NotFoundError, ValidationError
from app.models.governance import (
    Grant,
    GrantStatus,
    GrantType,
    Role,
    RoleName,
    RolePermission,
    SensitivityLevel,
)
from app.services.governance.schemas import (
    ClassificationRescanRequest,
    GrantBatchRequest,
    GrantCreate,
    GrantListParams,
    PermissionCheckRequest,
    PiiReviewRequest,
    RoleCreate,
    RolePermissionUpdate,
    UiActionMeta,
)
from app.services.governance.service import GovernanceService

_FUTURE = datetime.now(UTC) + timedelta(days=30)
_SOON = datetime.now(UTC) + timedelta(days=3)
_PAST = datetime.now(UTC) - timedelta(days=1)


class FakeEvents:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, event: dict[str, Any]) -> None:
        self.published.append(event)

    def types(self) -> list[str]:
        return [e["event_type"] for e in self.published]


class FakeUser:
    def __init__(
        self,
        uid: int = 1,
        role: str = "viewer",
        domain: str | None = "hr",
        extra_roles: list[str] | None = None,
    ) -> None:
        self.id = uid
        self.role = role
        self.domain = domain
        self._extra_roles = extra_roles or []

    def roles_all(self) -> list[str]:
        """方案 A 多角色：主角色 + 扩展角色。"""
        return [self.role, *self._extra_roles]

    def has_role(self, role: str) -> bool:
        return role in self.roles_all()

    def domains_all(self) -> list[str]:
        """权限域并集（对齐 User.domains_all 语义；FakeUser 仅主域）。"""
        return [d for d in (self.domain,) if d]


class FakeMetric:
    def __init__(self, **over: Any) -> None:
        self.id = over.get("id", 1)
        self.metric_code = over.get("metric_code", "sales_gmv_daily")
        self.domain = over.get("domain", "sales")
        self.owner_id = over.get("owner_id", 5)
        self.pii_flag = over.get("pii_flag", False)
        self.compliance_reviewed = over.get("compliance_reviewed", False)
        self.definition_json = over.get("definition_json", {})


class FakeCatalog:
    def __init__(
        self, cid: int, name: str, schema: dict[str, Any], level: str, source_id: str | None = None
    ) -> None:
        self.id = cid
        self.entity_name = name
        self.schema_json = schema
        self.sensitivity_level = level
        self.source_id = source_id


class FakeRepo:
    def __init__(self) -> None:
        self.grants: list[Grant] = []
        self.roles: list[Role] = []
        self.role_permissions: list[RolePermission] = []
        self.metrics: dict[str, FakeMetric] = {}
        self.catalogs: list[FakeCatalog] = []
        self.classifications: list[dict[str, Any]] = []
        self.catalog_updates: list[tuple[int, str]] = []
        self._seq = 0

    # role
    async def get_role_by_name(self, name: str) -> Role | None:
        return next((r for r in self.roles if str(r.name) == str(name)), None)

    async def create_role(self, role: Role) -> Role:
        self._seq += 1
        role.id = self._seq
        self.roles.append(role)
        return role

    async def list_custom_roles(self) -> list[Role]:
        return [r for r in self.roles if getattr(r, "is_custom", False)]

    async def list_all_roles(self) -> list[Role]:
        return list(self.roles)

    async def count_users_by_role(self, role: str) -> int:
        return int(getattr(self, "user_role_counts", {}).get(role, 0))

    async def delete_role(self, role: Role) -> None:
        self.roles = [r for r in self.roles if r is not role]

    # role permission (RBAC 可配置化)
    async def list_role_permissions(self) -> list[RolePermission]:
        return list(self.role_permissions)

    async def replace_role_permissions(self, role: str, actions: list[str]) -> None:
        self.role_permissions = [rp for rp in self.role_permissions if rp.role != role]
        for action in actions:
            self._seq += 1
            rp = RolePermission(role=role, action=action)
            rp.id = self._seq
            self.role_permissions.append(rp)

    async def reset_role_permissions(self, role: str) -> int:
        before = len(self.role_permissions)
        self.role_permissions = [rp for rp in self.role_permissions if rp.role != role]
        return before - len(self.role_permissions)

    # user permission（用户直挂按钮权限点）
    async def list_user_ui_permissions(self, user_id: int) -> tuple[set[str], set[str]]:
        allows = set(getattr(self, "user_permissions", {}).get(user_id, set()))
        denies = set(getattr(self, "user_permission_denies", {}).get(user_id, set()))
        return allows, denies

    async def replace_user_ui_permissions(
        self,
        user_id: int,
        actions: list[str],
        granted_by: int | None,
        reason: str | None,
        deny_actions: list[str] | None = None,
    ) -> None:
        if not hasattr(self, "user_permissions"):
            self.user_permissions = {}
        if not hasattr(self, "user_permission_denies"):
            self.user_permission_denies = {}
        self.user_permissions[user_id] = set(actions)
        self.user_permission_denies[user_id] = set(deny_actions or [])

    # grant
    async def create_grant(self, grant: Grant) -> Grant:
        self._seq += 1
        grant.id = self._seq
        self.grants.append(grant)
        return grant

    async def get_grant(self, grant_id: int) -> Grant | None:
        return next((g for g in self.grants if g.id == grant_id), None)

    async def find_active_grant(
        self, user_id: int, role_id: int | None, domain: str | None, grant_type: GrantType
    ) -> Grant | None:
        for g in self.grants:
            if (
                g.user_id == user_id
                and g.role_id == role_id
                and g.domain == domain
                and g.grant_type == grant_type
                and g.status is GrantStatus.ACTIVE
            ):
                return g
        return None

    async def list_grants(
        self, user_id: int | None, domain: str | None, status: str | None, page: int, ps: int
    ) -> tuple[list[Grant], int]:
        rows = [g for g in self.grants if user_id is None or g.user_id == user_id]
        return rows, len(rows)

    async def active_grants_for_user(self, user_id: int) -> list[Grant]:
        return [g for g in self.grants if g.user_id == user_id and g.status is GrantStatus.ACTIVE]

    async def set_grant_status(
        self, grant: Grant, status: GrantStatus, reason: str | None = None
    ) -> Grant:
        grant.status = status
        if reason:
            grant.reason = reason
        return grant

    async def expire_due_grants(self, now: datetime | None = None) -> list[Grant]:
        ref = now or datetime.now(UTC)
        due = [
            g
            for g in self.grants
            if g.status is GrantStatus.ACTIVE and g.expires_at is not None and g.expires_at < ref
        ]
        for g in due:
            g.status = GrantStatus.EXPIRED
        return due

    async def list_expiring_grants(
        self, window: timedelta, now: datetime | None = None
    ) -> list[Grant]:
        ref = now or datetime.now(UTC)
        deadline = ref + window
        return [
            g
            for g in self.grants
            if g.status is GrantStatus.ACTIVE
            and g.expires_at is not None
            and ref < g.expires_at <= deadline
            and g.expiring_reminded_at is None
        ]

    async def mark_expiring_reminded(
        self, grant_ids: list[int], now: datetime | None = None
    ) -> None:
        ref = now or datetime.now(UTC)
        for g in self.grants:
            if g.id in grant_ids:
                g.expiring_reminded_at = ref

    # metric
    async def get_metric_by_code(self, metric_code: str) -> FakeMetric | None:
        return self.metrics.get(metric_code)

    async def set_compliance_reviewed(self, metric: FakeMetric, reviewed: bool) -> FakeMetric:
        metric.compliance_reviewed = reviewed
        return metric

    # catalog / classification
    async def list_catalog(
        self,
        source_id: str | None,
        catalog_ids: list[int] | None,
        limit: int,
        source_ids: list[str] | None = None,
    ) -> list[FakeCatalog]:
        rows = self.catalogs
        if source_ids:
            rows = [c for c in rows if getattr(c, "source_id", None) in source_ids]
        if catalog_ids:
            rows = [c for c in rows if c.id in catalog_ids]
        return rows[:limit]

    async def update_catalog_sensitivity(self, catalog_id: int, level: SensitivityLevel) -> None:
        self.catalog_updates.append((catalog_id, level.value))

    async def get_classification(self, catalog_id: int) -> None:
        return None

    async def upsert_classification(
        self,
        catalog_id: int,
        level: SensitivityLevel,
        pii_columns: list[dict[str, Any]],
        classified_by: str,
        model_version: str,
    ) -> dict[str, Any]:
        row = {
            "catalog_id": catalog_id,
            "level": level.value,
            "pii_columns": pii_columns,
            "classified_by": classified_by,
            "model_version": model_version,
        }
        self.classifications.append(row)
        return row


class FakeDB:
    """最小 AsyncSession 替身：仅服务 ``_ensure_user_exists`` 与 ``flush``。"""

    def __init__(self, user: FakeUser | None) -> None:
        self._user = user
        self.flushed = 0

    async def execute(self, stmt: Any) -> Any:
        user = self._user

        class _R:
            def scalar_one_or_none(self) -> FakeUser | None:
                return user

        return _R()

    async def flush(self) -> None:
        self.flushed += 1


def _svc(user: FakeUser | None = None) -> tuple[GovernanceService, FakeRepo, FakeEvents]:
    events = FakeEvents()
    svc = GovernanceService(db=FakeDB(user or FakeUser()), events=events)  # type: ignore[arg-type]
    repo = FakeRepo()
    svc._repo = repo  # type: ignore[assignment]
    return svc, repo, events


def _payload(**over: Any) -> GrantCreate:
    base: dict[str, Any] = {"user_id": 1, "domain": "sales", "grant_type": GrantType.READ}
    base.update(over)
    return GrantCreate(**base)


# ------------------------------------------------------------------- 角色


async def test_create_role_is_idempotent() -> None:
    svc, repo, _ = _svc()
    first = await svc.create_role(RoleCreate(name=RoleName.COMPLIANCE_OFFICER))
    second = await svc.create_role(RoleCreate(name=RoleName.COMPLIANCE_OFFICER))
    assert first.id == second.id
    assert len(repo.roles) == 1


# ------------------------------------------------------------------- 授权


async def test_grant_creates_row_and_publishes_event() -> None:
    svc, repo, events = _svc(FakeUser(role="platform_admin"))
    row = await svc.grant(_payload(metric_whitelist=["m1"]), actor_id=9)
    assert row.id == 1
    assert row.status is GrantStatus.ACTIVE
    assert row.granted_by == 9
    assert events.types() == ["grant.granted"]


async def test_grant_merges_whitelist_and_extends_ttl() -> None:
    svc, repo, _ = _svc(FakeUser(role="platform_admin"))
    await svc.grant(_payload(metric_whitelist=["m1"], expires_at=_SOON), actor_id=9)
    row = await svc.grant(_payload(metric_whitelist=["m2"], expires_at=_FUTURE), actor_id=9)
    assert len(repo.grants) == 1
    assert row.metric_whitelist == ["m1", "m2"]
    assert row.expires_at == _FUTURE


async def test_grant_rejects_empty_scope() -> None:
    svc, _, _ = _svc()
    with pytest.raises(ValidationError):
        await svc.grant(GrantCreate(user_id=1), actor_id=9)


async def test_grant_rejects_past_expiry() -> None:
    svc, _, _ = _svc()
    with pytest.raises(ValidationError):
        await svc.grant(_payload(expires_at=_PAST), actor_id=9)


async def test_grant_rejects_unknown_user() -> None:
    svc, _, _ = _svc(user=None)
    svc._db = FakeDB(None)  # type: ignore[assignment]
    with pytest.raises(NotFoundError):
        await svc.grant(_payload(), actor_id=9)


async def test_revoke_flow_and_guards() -> None:
    svc, repo, events = _svc(FakeUser(role="platform_admin"))
    row = await svc.grant(_payload(), actor_id=9)
    revoked = await svc.revoke(row.id, actor_id=9, reason="离职")
    assert revoked.status is GrantStatus.REVOKED
    assert "grant.revoked" in events.types()
    with pytest.raises(ValidationError):
        await svc.revoke(row.id, actor_id=9)
    with pytest.raises(NotFoundError):
        await svc.revoke(999, actor_id=9)


# --------------------------------------------------- 回收范围校验（D10 §3.5 缺口补齐）


def _grant(user_id: int = 1, domain: str | None = "sales") -> Grant:
    return Grant(
        id=1,
        user_id=user_id,
        domain=domain,
        grant_type=GrantType.READ,
        status=GrantStatus.ACTIVE,
    )


async def test_revoke_platform_admin_any_domain() -> None:
    svc, _, _ = _svc(FakeUser(uid=1, role="platform_admin", domain="hr"))
    # 平台管理员可回收任意域授权，不抛异常
    actor = FakeUser(uid=1, role="platform_admin", domain="hr")
    svc._assert_revoke_scope(actor, _grant(2, "sales"))


async def test_revoke_domain_admin_same_domain_ok() -> None:
    svc, _, _ = _svc(FakeUser(uid=1, role="domain_admin", domain="sales"))
    # 本域管理员可回收本域授权
    actor = FakeUser(uid=1, role="domain_admin", domain="sales")
    svc._assert_revoke_scope(actor, _grant(2, "sales"))


async def test_revoke_domain_admin_cross_domain_forbidden() -> None:
    svc, _, _ = _svc(FakeUser(uid=1, role="domain_admin", domain="sales"))
    with pytest.raises(AuthError):
        svc._assert_revoke_scope(
            FakeUser(uid=1, role="domain_admin", domain="sales"), _grant(2, "hr")
        )


async def test_revoke_domain_admin_no_domain_forbidden() -> None:
    svc, _, _ = _svc(FakeUser(uid=1, role="domain_admin", domain="sales"))
    # 无域归属的授权视为跨域，域管理员不得回收（须平台管理员）
    with pytest.raises(AuthError):
        svc._assert_revoke_scope(
            FakeUser(uid=1, role="domain_admin", domain="sales"), _grant(2, None)
        )


async def test_revoke_owner_self_ok() -> None:
    svc, _, _ = _svc(FakeUser(uid=10, role="analyst", domain="sales"))
    # 授权本人（不论角色）可回收自己的授权
    actor = FakeUser(uid=10, role="analyst", domain="sales")
    svc._assert_revoke_scope(actor, _grant(10, "sales"))


async def test_revoke_other_user_forbidden_for_non_admin() -> None:
    svc, _, _ = _svc(FakeUser(uid=10, role="analyst", domain="sales"))
    with pytest.raises(AuthError):
        svc._assert_revoke_scope(
            FakeUser(uid=10, role="analyst", domain="sales"), _grant(2, "sales")
        )


async def test_revoke_endpoint_applies_scope_domain_admin_cross_domain() -> None:
    svc, repo, _ = _svc(FakeUser(uid=1, role="domain_admin", domain="sales"))
    repo.grants.append(_grant(2, "hr"))
    with pytest.raises(AuthError):
        await svc.revoke(1, actor_id=1, reason="x")


async def test_revoke_endpoint_owner_self_success() -> None:
    svc, repo, events = _svc(FakeUser(uid=10, role="analyst", domain="sales"))
    repo.grants.append(_grant(10, "sales"))
    revoked = await svc.revoke(1, actor_id=10, reason="自管")
    assert revoked.status is GrantStatus.REVOKED
    assert "grant.revoked" in events.types()


async def test_batch_revoke_domain_admin_cross_domain_forbidden() -> None:
    svc, repo, _ = _svc(FakeUser(uid=1, role="domain_admin", domain="sales"))
    repo.grants.append(_grant(2, "hr"))
    req = GrantBatchRequest(operation="revoke", items=[_payload(user_id=2, domain="hr")])
    with pytest.raises(AuthError):
        await svc.batch(req, actor_id=1, dry_run=False)


async def test_batch_revoke_domain_admin_same_domain_ok() -> None:
    svc, repo, _ = _svc(FakeUser(uid=1, role="domain_admin", domain="sales"))
    repo.grants.append(_grant(2, "sales"))
    req = GrantBatchRequest(operation="revoke", items=[_payload(user_id=2, domain="sales")])
    result = await svc.batch(req, actor_id=1, dry_run=False)
    assert result.succeeded == 1
    assert repo.grants[0].status is GrantStatus.REVOKED


async def test_list_grants_delegates_to_repo() -> None:
    svc, _, _ = _svc(FakeUser(role="platform_admin"))
    await svc.grant(_payload(), actor_id=9)
    rows, total = await svc.list_grants(GrantListParams(user_id=1))
    assert total == 1
    assert rows[0].user_id == 1


# --------------------------------------------------------------- 批量操作


async def test_batch_dry_run_does_not_write() -> None:
    svc, repo, events = _svc()
    req = GrantBatchRequest(items=[_payload(), _payload(domain="hr")])
    result = await svc.batch(req, actor_id=9, dry_run=True)
    assert result.dry_run is True
    assert result.affected_users == 1
    assert result.succeeded == 2
    assert repo.grants == []
    assert events.published == []


async def test_batch_dry_run_marks_invalid_items() -> None:
    svc, _, _ = _svc()
    req = GrantBatchRequest(items=[_payload(), _payload(expires_at=_PAST)])
    result = await svc.batch(req, actor_id=9, dry_run=True)
    assert result.succeeded == 1
    assert result.failed == 1
    assert result.items[1].ok is False


async def test_batch_grant_executes_and_counts_metrics() -> None:
    svc, repo, _ = _svc(FakeUser(role="platform_admin"))
    req = GrantBatchRequest(
        items=[_payload(metric_whitelist=["m1"]), _payload(domain="hr", metric_whitelist=["m2"])]
    )
    result = await svc.batch(req, actor_id=9, dry_run=False)
    assert result.succeeded == 2
    assert result.affected_metrics == 2
    assert len(repo.grants) == 2


async def test_batch_revoke_raises_when_no_active_grant() -> None:
    svc, _, _ = _svc()
    req = GrantBatchRequest(operation="revoke", items=[_payload()])
    with pytest.raises(NotFoundError):
        await svc.batch(req, actor_id=9, dry_run=False)


async def test_batch_revoke_dry_run_reports_missing() -> None:
    svc, _, _ = _svc()
    req = GrantBatchRequest(operation="revoke", items=[_payload()])
    result = await svc.batch(req, actor_id=9, dry_run=True)
    assert result.failed == 1
    assert "无匹配" in result.items[0].detail


# --------------------------------------------------------------- 到期回收


async def test_expire_due_grants_marks_and_notifies() -> None:
    svc, repo, events = _svc(FakeUser(role="platform_admin"))
    row = await svc.grant(_payload(expires_at=_SOON), actor_id=9)
    row.expires_at = _PAST  # 模拟时间流逝
    count = await svc.expire_due_grants()
    assert count == 1
    assert repo.grants[0].status is GrantStatus.EXPIRED
    assert "grant.expired" in events.types()


# --------------------------------------------------------------- 权限快照


async def test_my_permissions_snapshot() -> None:
    viewer = FakeUser(uid=1, role="viewer", domain="hr")
    # 授权人须为平台管理员（新越权防护 P0），快照主体仍用 viewer
    svc, repo, _ = _svc(FakeUser(uid=9, role="platform_admin", domain="hr"))
    await svc.grant(_payload(metric_whitelist=["m1"], expires_at=_SOON), actor_id=9)
    await svc.grant(_payload(domain="ops", row_level=True), actor_id=9)
    snap = await svc.my_permissions(viewer)  # type: ignore[arg-type]
    assert snap.user_id == 1
    assert snap.home_domain == "hr"
    assert snap.granted_domains == ["ops", "sales"]
    assert snap.metric_whitelist == ["m1"]
    assert snap.row_level_restricted is True
    assert len(snap.expiring_soon) == 1
    assert "read" in snap.allowed_actions


async def test_my_permissions_filters_expired_grants() -> None:
    viewer = FakeUser(uid=1)
    # 授权人须为平台管理员（新越权防护 P0），快照主体仍用 viewer
    svc, repo, _ = _svc(FakeUser(uid=9, role="platform_admin"))
    row = await svc.grant(_payload(expires_at=_SOON), actor_id=9)
    row.expires_at = _PAST
    snap = await svc.my_permissions(viewer)  # type: ignore[arg-type]
    assert snap.grants == []
    assert snap.granted_domains == []


# ------------------------------------------------------------- PII 复核


async def test_pii_review_approve_sets_compliance_flag() -> None:
    svc, repo, events = _svc(FakeUser(uid=2, role="compliance_officer"))
    repo.metrics["m1"] = FakeMetric(metric_code="m1", owner_id=5)
    result = await svc.pii_review(
        PiiReviewRequest(metric_code="m1", decision="APPROVE", comment="脱敏后可发布"),
        reviewer=FakeUser(uid=2, role="compliance_officer"),  # type: ignore[arg-type]
    )
    assert result.compliance_reviewed is True
    assert result.masking_policy == "hash"
    assert repo.metrics["m1"].compliance_reviewed is True
    assert repo.metrics["m1"].pii_flag is True
    assert "pii.reviewed" in events.types()


async def test_pii_review_reject_keeps_flag_false() -> None:
    svc, repo, _ = _svc()
    repo.metrics["m1"] = FakeMetric(metric_code="m1", owner_id=5, compliance_reviewed=True)
    result = await svc.pii_review(
        PiiReviewRequest(
            metric_code="m1",
            decision="REJECT",
            sensitivity_level=SensitivityLevel.CONFIDENTIAL,
            comment="脱敏策略不足",
        ),
        reviewer=FakeUser(uid=2, role="compliance_officer"),  # type: ignore[arg-type]
    )
    assert result.compliance_reviewed is False
    assert result.masking_policy == "mask"
    assert repo.metrics["m1"].compliance_reviewed is False


async def test_pii_review_rejects_self_review() -> None:
    """职责分离：指标 Owner 不得复核自己的指标。"""
    svc, repo, _ = _svc()
    repo.metrics["m1"] = FakeMetric(metric_code="m1", owner_id=2)
    with pytest.raises(ValidationError):
        await svc.pii_review(
            PiiReviewRequest(metric_code="m1", decision="APPROVE", comment="放行"),
            reviewer=FakeUser(uid=2, role="compliance_officer"),  # type: ignore[arg-type]
        )


async def test_pii_review_unknown_metric() -> None:
    svc, _, _ = _svc()
    with pytest.raises(NotFoundError):
        await svc.pii_review(
            PiiReviewRequest(metric_code="nope", decision="APPROVE", comment="x"),
            reviewer=FakeUser(uid=2),  # type: ignore[arg-type]
        )


async def test_pii_review_rejects_unknown_sensitivity() -> None:
    """UNKNOWN 是分级引擎降级标记（0109 后两表枚举均已含），不可作为人工复核赋值。

    人工复核须给真实级别或 NEEDS_REVIEW，避免治理页把「未知/降级」当真实级别写回资产。
    """
    svc, repo, _ = _svc()
    repo.metrics["m1"] = FakeMetric(metric_code="m1", owner_id=5)
    with pytest.raises(ValidationError):
        await svc.pii_review(
            PiiReviewRequest(
                metric_code="m1",
                decision="APPROVE",
                sensitivity_level=SensitivityLevel.UNKNOWN,
                comment="测试：不应允许赋值 UNKNOWN",
            ),
            reviewer=FakeUser(uid=2, role="compliance_officer"),  # type: ignore[arg-type]
        )


# --------------------------------------------------------------- 分级重扫


async def test_classification_rescan_detects_pii_and_updates_catalog() -> None:
    svc, repo, events = _svc()
    repo.catalogs = [
        FakeCatalog(1, "dwd.user", {"columns": [{"name": "id_card"}]}, "INTERNAL"),
        FakeCatalog(2, "dwd.order", {"columns": [{"name": "amount"}]}, "INTERNAL"),
    ]
    result = await svc.classification_rescan(ClassificationRescanRequest())
    assert result.scanned == 2
    assert result.pii_found == 1
    assert result.changed == 1
    assert repo.catalog_updates == [(1, "PII")]
    assert len(repo.classifications) == 2
    assert "classification.changed" in events.types()
    assert "classification.done" in events.types()


async def test_classification_rescan_filters_by_source_ids() -> None:
    """重扫支持 source_ids 多选：只扫指定数据源的资产（前端多选）。"""
    svc, repo, _ = _svc()
    repo.catalogs = [
        FakeCatalog(1, "a", {"columns": [{"name": "phone"}]}, "INTERNAL", source_id="ds1"),
        FakeCatalog(2, "b", {"columns": [{"name": "gmv"}]}, "INTERNAL", source_id="ds2"),
    ]
    result = await svc.classification_rescan(ClassificationRescanRequest(source_ids=["ds1"]))
    assert result.scanned == 1
    assert result.items[0].catalog_id == 1


async def test_classification_rescan_filters_by_ids() -> None:
    svc, repo, _ = _svc()
    repo.catalogs = [
        FakeCatalog(1, "a", {"columns": [{"name": "phone"}]}, "INTERNAL"),
        FakeCatalog(2, "b", {"columns": [{"name": "gmv"}]}, "INTERNAL"),
    ]
    result = await svc.classification_rescan(ClassificationRescanRequest(catalog_ids=[2]))
    assert result.scanned == 1
    assert result.items[0].catalog_id == 2


# ----------------------------------------------------------------- PDP


async def test_check_permission_denies_unreviewed_pii_metric() -> None:
    user = FakeUser(uid=1, role="domain_admin", domain="sales")
    svc, repo, _ = _svc(user)
    repo.metrics["m1"] = FakeMetric(
        metric_code="m1", domain="sales", pii_flag=True, compliance_reviewed=False
    )
    result = await svc.check_permission(
        PermissionCheckRequest(user_id=1, action="read", metric_code="m1")
    )
    assert result.allow is False
    assert result.error_code == "FORBIDDEN_PII"


async def test_check_permission_allows_same_domain_admin() -> None:
    user = FakeUser(uid=1, role="domain_admin", domain="sales")
    svc, repo, _ = _svc(user)
    repo.metrics["m1"] = FakeMetric(metric_code="m1", domain="sales", compliance_reviewed=True)
    result = await svc.check_permission(
        PermissionCheckRequest(user_id=1, action="write", metric_code="m1")
    )
    assert result.allow is True


async def test_check_permission_unknown_metric() -> None:
    svc, _, _ = _svc(FakeUser(uid=1, role="domain_admin", domain="sales"))
    with pytest.raises(NotFoundError):
        await svc.check_permission(
            PermissionCheckRequest(user_id=1, action="read", metric_code="ghost")
        )


async def test_check_permission_domain_only_denied_by_default() -> None:
    svc, _, _ = _svc(FakeUser(uid=1, role="viewer", domain="hr"))
    result = await svc.check_permission(
        PermissionCheckRequest(user_id=1, action="read", domain="sales")
    )
    assert result.allow is False
    assert result.error_code == "FORBIDDEN"


# ------------------------------------------------------ PII 血缘传播 (US13)


class _FakeLineageEdge:
    def __init__(self, source: str, target: str, inherited: bool = False) -> None:
        self.source_node = source
        self.target_node = target
        self.pii_inherited = inherited
        self.deleted_at = None


class _FakeDb:
    """模拟 self._db.execute 返回 lineage 边。"""

    def __init__(self, edges: list[_FakeLineageEdge]) -> None:
        self._edges = edges

    async def execute(self, stmt: Any) -> Any:
        class _Result:
            def __init__(self, edges: list[_FakeLineageEdge]) -> None:
                self._edges = edges

            def scalars(self) -> _Result:
                return self

            def all(self) -> list[_FakeLineageEdge]:
                return self._edges

        return _Result(self._edges)


async def test_propagate_pii_from_upstream_columns_sets_metric_flag() -> None:
    svc, repo, events = _svc()
    metric = FakeMetric(metric_code="m1", pii_flag=False, definition_json={})
    repo.metrics["m1"] = metric
    svc._db = _FakeDb([])

    changed = await svc.propagate_pii_to_metric(
        "m1",
        upstream_source_columns=[{"column": "phone", "pii": True}],
    )
    assert changed is True
    assert metric.pii_flag is True
    assert metric.definition_json.get("pii") is True
    assert "pii.propagated" in events.types()


async def test_propagate_pii_no_upstream_returns_false() -> None:
    svc, repo, events = _svc()
    metric = FakeMetric(metric_code="m1", pii_flag=False, definition_json={})
    repo.metrics["m1"] = metric
    svc._db = _FakeDb([])

    changed = await svc.propagate_pii_to_metric(
        "m1",
        upstream_source_columns=[{"column": "name", "pii": False}],
    )
    assert changed is False
    assert metric.pii_flag is False


async def test_propagate_pii_inherits_via_lineage_edge() -> None:
    svc, repo, events = _svc()
    metric = FakeMetric(metric_code="m1", pii_flag=False, definition_json={})
    # 上游指标带 PII，且存在 lineage 边 m_src -> m1
    repo.metrics["m1"] = metric
    repo.metrics["m_src"] = FakeMetric(metric_code="m_src", pii_flag=True)
    svc._db = _FakeDb([_FakeLineageEdge(source="m_src", target="m1")])

    changed = await svc.propagate_pii_to_metric("m1")
    assert changed is True
    assert metric.pii_flag is True


async def test_propagate_pii_marks_lineage_edge_inherited() -> None:
    svc, repo, events = _svc()
    metric = FakeMetric(metric_code="m1", pii_flag=False, definition_json={})
    repo.metrics["m1"] = metric
    edge = _FakeLineageEdge(source="m_src", target="m1", inherited=False)
    svc._db = _FakeDb([edge])

    # 通过上游 columns 直接触发（不依赖边 PII 判定，但最终需标记边的 pii_inherited）
    changed = await svc.propagate_pii_to_metric(
        "m1",
        upstream_source_columns=[{"column": "phone", "pii": True}],
    )
    assert changed is True
    assert edge.pii_inherited is True


async def test_propagate_pii_metric_not_found_raises() -> None:
    svc, _, _ = _svc()
    with pytest.raises(NotFoundError):
        await svc.propagate_pii_to_metric("nonexistent")


# ---------- check_metric_permission（PDP 决策入口，供 semantic 写操作调用） ----------


async def test_check_metric_permission_allows_same_domain_owner() -> None:
    svc, repo, _ = _svc()
    repo.metrics["m1"] = FakeMetric(metric_code="m1", domain="sales", owner_id=5)
    decision = await svc.check_metric_permission(
        metric_code="m1", action="write", user_id=5, role="metric_owner", user_domains=["sales"]
    )
    assert decision.allow is True


async def test_check_metric_permission_blocks_cross_domain() -> None:
    svc, repo, _ = _svc()
    repo.metrics["m1"] = FakeMetric(metric_code="m1", domain="sales", owner_id=5)
    decision = await svc.check_metric_permission(
        metric_code="m1", action="write", user_id=5, role="metric_owner", user_domains=["finance"]
    )
    assert decision.allow is False
    assert decision.error_code == "FORBIDDEN"


async def test_check_metric_permission_blocks_unreviewed_pii() -> None:
    svc, repo, _ = _svc()
    repo.metrics["m1"] = FakeMetric(
        metric_code="m1", domain="sales", owner_id=5, pii_flag=True, compliance_reviewed=False
    )
    decision = await svc.check_metric_permission(
        metric_code="m1", action="read", user_id=5, role="metric_owner", user_domains=["sales"]
    )
    assert decision.allow is False
    assert decision.error_code == "FORBIDDEN_PII"


async def test_check_metric_permission_metric_not_found() -> None:
    svc, _, _ = _svc()
    with pytest.raises(NotFoundError):
        await svc.check_metric_permission(
            metric_code="nonexistent", action="write", user_id=1, role="platform_admin"
        )


async def test_check_metric_permission_skip_pii_gate_allows_submit_flow() -> None:
    """死锁修复：skip_pii_gate=True 时未复核 PII 指标可提交审核（进入 REVIEW）。"""
    svc, repo, _ = _svc()
    repo.metrics["m1"] = FakeMetric(
        metric_code="m1", domain="sales", owner_id=5, pii_flag=True, compliance_reviewed=False
    )
    # 默认（不跳过）→ PII 门禁拦截
    blocked = await svc.check_metric_permission(
        metric_code="m1", action="write", user_id=5, role="metric_owner", user_domains=["sales"]
    )
    assert blocked.allow is False
    assert blocked.error_code == "FORBIDDEN_PII"
    # 跳过 PII 门禁 → 域/角色校验放行（提交审核入口）
    allowed = await svc.check_metric_permission(
        metric_code="m1",
        action="write",
        user_id=5,
        role="metric_owner",
        user_domains=["sales"],
        skip_pii_gate=True,
    )
    assert allowed.allow is True
    # 跨域仍被拦截（skip_pii_gate 只豁免 PII，不豁免域）
    cross = await svc.check_metric_permission(
        metric_code="m1",
        action="write",
        user_id=5,
        role="metric_owner",
        user_domains=["finance"],
        skip_pii_gate=True,
    )
    assert cross.allow is False


# ------------------------------------------- RBAC 可配置化（角色权限点覆盖）


async def test_list_role_permissions_default_view() -> None:
    """默认（无覆盖）：全部角色显示默认动作、custom=None、effective=默认。"""
    svc, _repo, _ = _svc()
    items = await svc.list_role_permissions()
    by_role = {i["role"]: i for i in items}
    assert "platform_admin" in by_role and "viewer" in by_role and "analyst" in by_role
    viewer = by_role["viewer"]
    assert viewer["custom_actions"] is None
    assert viewer["effective_actions"] == ["read"]
    assert viewer["protected"] is False
    assert by_role["platform_admin"]["protected"] is True


async def test_set_role_permissions_overrides_effective_and_decision() -> None:
    """覆盖 reviewer 追加 export：effective 变化 + check_metric_permission 本域可导出。"""
    svc, repo, _ = _svc()
    repo.metrics["m1"] = FakeMetric(
        metric_code="m1", domain="sales", owner_id=1, compliance_reviewed=True
    )

    # 默认 reviewer 不可导出（基线无 export）
    denied = await svc.check_metric_permission(
        metric_code="m1", action="export", user_id=1, role="reviewer", user_domains=["sales"]
    )
    assert denied.allow is False
    # 覆盖 reviewer 追加 export
    item = await svc.set_role_permissions("reviewer", ["read", "approve", "export"])
    assert item["custom_actions"] == ["approve", "export", "read"]
    assert item["effective_actions"] == ["approve", "export", "read"]

    # 覆盖后 reviewer 本域可导出（load_role_actions 合并生效）
    allowed = await svc.check_metric_permission(
        metric_code="m1", action="export", user_id=1, role="reviewer", user_domains=["sales"]
    )
    assert allowed.allow is True


async def test_set_role_permissions_revokes_action() -> None:
    """覆盖将 domain_admin 的 write 收窄后，本域写被回收。"""
    svc, repo, _ = _svc()
    repo.metrics["m1"] = FakeMetric(
        metric_code="m1", domain="sales", owner_id=1, compliance_reviewed=True
    )
    await svc.set_role_permissions("domain_admin", ["read", "approve", "export"])
    d = await svc.check_metric_permission(
        metric_code="m1", action="write", user_id=1, role="domain_admin", user_domains=["sales"]
    )
    assert d.allow is False
    # 本域 read 仍放行
    d2 = await svc.check_metric_permission(
        metric_code="m1", action="read", user_id=1, role="domain_admin", user_domains=["sales"]
    )
    assert d2.allow is True


async def test_set_role_permissions_rejects_unknown_action() -> None:
    """未知权限点 → ValidationError（ROLE_PERMISSION_INVALID）。"""
    svc, _repo, _ = _svc()
    with pytest.raises(ValidationError) as exc:
        await svc.set_role_permissions("viewer", ["read", "sudo"])
    assert exc.value.error_code == "ROLE_PERMISSION_INVALID"


async def test_set_role_permissions_accepts_many_actions() -> None:
    """覆盖角色权限点时支持超过 8 个动作点（对齐 action-registry 规模，不再被过时上限拦截）。"""
    svc, repo, _ = _svc()
    actions = [
        "dashboard:view",
        "todo:view",
        "catalog:view",
        "assetmap:view",
        "lineage:view",
        "quality:view",
        "query:view",
        "ai:view",
        "dimensions:view",
        "glossary:view",
        "data-sources:view",
        "catalogs:view",
        "metric:create",
        "metric:edit",
        "metric:review",
        "metric:export",
    ]
    item = await svc.set_role_permissions("analyst", actions)
    # 资源级动词（read/write/approve/export/review）→ custom_actions；
    # UI 权限点（模块:功能，如 metric:create）→ ui_custom_actions；均完整保留
    assert item["custom_actions"] is None
    assert item["ui_custom_actions"] == sorted(actions)
    assert len(item["ui_custom_actions"]) == 16


async def test_set_role_permissions_resave_same_actions() -> None:
    """同一批动作点二次保存（角色已有覆盖行）不冲突、不重复。

    回归：修复前 replace 用软删（update deleted_at），(role, action) 唯一索引被
    软删行占用，二次保存触发 Duplicate entry 1062 → 500；现为物理删除+插入。
    """
    svc, repo, _ = _svc()
    actions = ["ai:view", "metric:create", "catalog:view", "dashboard:view"]
    first = await svc.set_role_permissions("analyst", actions)
    assert first["ui_custom_actions"] == sorted(actions)
    # 二次保存同一批（等价于用户再次点击保存）
    second = await svc.set_role_permissions("analyst", actions)
    assert second["ui_custom_actions"] == sorted(actions)
    # 覆盖行只保留一份（无重复追加）
    perms = [rp for rp in repo.role_permissions if rp.role == "analyst"]
    assert len(perms) == len(actions)


def test_role_permission_update_accepts_many_actions() -> None:
    """schema 层：超过 8 个动作点可通过校验（max_length 放宽到 256）。"""
    actions = [f"module:action{i}" for i in range(20)]
    payload = RolePermissionUpdate(actions=actions)
    assert payload.actions == actions


async def test_set_role_permissions_rejects_protected_role() -> None:
    """platform_admin 受保护：配置其权限点 → ValidationError（ROLE_PERMISSION_PROTECTED）。"""
    svc, _repo, _ = _svc()
    with pytest.raises(ValidationError) as exc:
        await svc.set_role_permissions("platform_admin", ["read"])
    assert exc.value.error_code == "ROLE_PERMISSION_PROTECTED"


async def test_reset_role_permissions_restores_default() -> None:
    """重置覆盖后恢复默认基线。"""
    svc, repo, _ = _svc()
    await svc.set_role_permissions("reviewer", ["read", "write"])
    item = await svc.reset_role_permissions("reviewer")
    assert item["custom_actions"] is None
    assert item["effective_actions"] == ["approve", "read"]
    assert repo.role_permissions == []


async def test_remind_expiring_grants_notifies_and_dedupes() -> None:
    """授权到期提醒（grant.expiring_soon）：即将到期授权定向通知被授权人，二次调用去重。"""
    from unittest.mock import AsyncMock, patch

    svc, repo, _ = _svc(FakeUser(role="platform_admin"))
    await svc.grant(_payload(expires_at=_SOON), actor_id=9)

    fake_cm = AsyncMock()
    fake_cm.__aenter__.return_value = fake_cm
    fake_cm.execute.return_value.scalars.return_value.first.return_value = None
    notify = AsyncMock()

    with (
        patch("app.db.mysql.async_session_factory", return_value=fake_cm),
        patch(
            "app.services.notify.service.NotifyService.notify_user",
            new=notify,
        ),
    ):
        first = await svc.remind_expiring_grants()
        second = await svc.remind_expiring_grants()

    assert first == 1  # 本轮提醒 1 条
    assert second == 0  # 已标记提醒，二次调用去重
    assert notify.await_count == 1
    assert notify.await_args.kwargs["event_type"] == "grant.expiring_soon"
    # 标记去重：FakeRepo 记录 expiring_reminded_at
    reminded = [g for g in repo.grants if g.expiring_reminded_at is not None]
    assert len(reminded) == 1


# --------------------------------------------- 自定义角色（方案 A）与动作点注册表


async def test_create_custom_role_ok() -> None:
    """创建自定义角色：写入 role 表（is_custom=True），权限点默认为空集。"""
    svc, repo, _ = _svc()
    role = await svc.create_custom_role("data_analyst", "数据分析员")
    assert str(role.name) == "data_analyst"
    assert getattr(role, "is_custom", False) is True
    assert repo.roles and repo.roles[0] is role
    # 列表含自定义角色且 is_custom=True
    items = await svc.list_role_permissions()
    by_role = {i["role"]: i for i in items}
    assert "data_analyst" in by_role
    assert by_role["data_analyst"]["is_custom"] is True
    assert by_role["data_analyst"]["ui_effective_actions"] == []


async def test_list_role_options_returns_builtin_and_custom() -> None:
    """授权下拉数据源：内置登记 + 自定义角色并集（与角色管理页一致）。"""
    svc, repo, _ = _svc()
    # 预置内置角色登记行（等价迁移 0118 种入）+ 一个自定义角色
    repo.roles = [
        Role(name=RoleName.PLATFORM_ADMIN, is_custom=False),
        Role(name=RoleName.VIEWER, is_custom=False),
    ]
    await svc.create_custom_role("data_analyst", "数据分析员")
    options = await svc.list_role_options()
    by_name = {o["name"]: o for o in options}
    assert set(by_name) == {"platform_admin", "viewer", "data_analyst"}
    assert by_name["platform_admin"]["is_custom"] is False
    assert by_name["data_analyst"]["is_custom"] is True
    assert all("id" in o and "name" in o and "is_custom" in o for o in options)


async def test_create_custom_role_rejects_reserved_and_invalid() -> None:
    """自定义角色名与内置角色重名 / 格式非法 → ValidationError。"""
    svc, _repo, _ = _svc()
    with pytest.raises(ValidationError) as exc1:
        await svc.create_custom_role("platform_admin", "x")
    assert exc1.value.error_code == "ROLE_NAME_RESERVED"
    with pytest.raises(ValidationError) as exc2:
        await svc.create_custom_role("Bad Role!", "x")
    assert exc2.value.error_code == "ROLE_NAME_INVALID"


async def test_delete_custom_role_ok() -> None:
    """删除自定义角色：软删，角色从列表消失。"""
    svc, repo, _ = _svc()
    await svc.create_custom_role("temp_role", "临时")
    await svc.delete_custom_role("temp_role")
    items = await svc.list_role_permissions()
    assert "temp_role" not in {i["role"] for i in items}


async def test_delete_custom_role_in_use_rejected() -> None:
    """自定义角色仍被用户占用 → ROLE_IN_USE。"""
    svc, repo, _ = _svc()
    await svc.create_custom_role("busy_role", "占用")
    repo.user_role_counts = {"busy_role": 3}
    with pytest.raises(ValidationError) as exc:
        await svc.delete_custom_role("busy_role")
    assert exc.value.error_code == "ROLE_IN_USE"


async def test_delete_builtin_role_rejected() -> None:
    """内置角色不可删除（ROLE_NAME_RESERVED）。"""
    svc, _repo, _ = _svc()
    with pytest.raises(ValidationError) as exc:
        await svc.delete_custom_role("viewer")
    assert exc.value.error_code == "ROLE_NAME_RESERVED"


async def test_action_registry_returns_grouped_items() -> None:
    """动作点注册表：含 module/label/description，按模块分组排序。"""
    svc, _repo, _ = _svc()
    items = await svc.action_registry()
    assert len(items) >= 40  # 按钮级权限点足够细
    keys = {i["action"] for i in items}
    assert "metric:create" in keys and "user:disable" in keys and "catalog:view" in keys
    assert all(i["module"] and i["label"] for i in items)
    # 排序：按 (module, action)
    modules = [i["module"] for i in items]
    assert modules == sorted(modules)


async def test_my_permissions_includes_ui_actions() -> None:
    """权限快照含 ui_actions（前端 usePermission 消费）；viewer 默认只读。"""
    svc, _repo, _ = _svc()
    snap = await svc.my_permissions(FakeUser(uid=1, role="viewer"))  # type: ignore[arg-type]
    assert "catalog:view" in snap.ui_actions
    assert "metric:create" not in snap.ui_actions
    assert "read" in snap.allowed_actions  # 资源级动词保持兼容


async def test_my_permissions_ui_action_meta_chinese_labels() -> None:
    """权限快照附带 ui_action_meta 中文元数据：module/label/description 对齐注册表。"""
    svc, _repo, _ = _svc()
    snap = await svc.my_permissions(FakeUser(uid=1, role="viewer"))  # type: ignore[arg-type]
    by_action = {m.action: m for m in snap.ui_action_meta}
    assert by_action["catalog:view"].label == "查看指标目录"
    assert by_action["catalog:view"].module == "指标"
    assert "访问指标目录列表" in by_action["catalog:view"].description
    # ui_actions 与 ui_action_meta 一一对应
    assert {m.action for m in snap.ui_action_meta} == set(snap.ui_actions)
    # 未知/自定义权限点降级为 action 本身、模块归「其他」
    custom = UiActionMeta(
        action="custom:probe",
        module="其他",
        label="custom:probe",
        description="自定义权限点",
    )
    assert custom.label == "custom:probe"


async def test_my_permissions_multi_role_union() -> None:
    """方案 A 多角色：主角色 viewer + 扩展 reviewer → UI 权限点与资源级动词取并集。"""
    svc, _repo, _ = _svc()
    snap = await svc.my_permissions(
        FakeUser(uid=1, role="viewer", extra_roles=["reviewer"])  # type: ignore[arg-type]
    )
    # reviewer 独有权限点出现在并集中
    assert "metric:approve" in snap.ui_actions
    # viewer 基础只读保留
    assert "catalog:view" in snap.ui_actions
    # 资源级动词并集：viewer(read) ∪ reviewer(read, approve)
    assert "read" in snap.allowed_actions
    assert "approve" in snap.allowed_actions
    # roles 字段含全部角色（主角色在前）
    assert snap.roles == ["viewer", "reviewer"]


async def test_custom_role_ui_actions_via_override() -> None:
    """自定义角色经 set_role_permissions 配置 UI 权限点后，快照生效动作随之更新。"""

    svc, repo, _ = _svc()
    await svc.create_custom_role("data_analyst", "数据分析员")
    await svc.set_role_permissions("data_analyst", ["catalog:view", "metric:create", "query:view"])
    item = await svc.list_role_permissions()
    by_role = {i["role"]: i for i in item}
    assert by_role["data_analyst"]["ui_effective_actions"] == [
        "catalog:view",
        "metric:create",
        "query:view",
    ]
    snap = await svc.my_permissions(FakeUser(uid=1, role="data_analyst"))  # type: ignore[arg-type]
    assert snap.ui_actions == ["catalog:view", "metric:create", "query:view"]
    # 自定义角色未配置资源级动词 → PDP 默认拒绝（fail-closed）
    assert snap.allowed_actions == []


async def test_user_direct_permissions_merged_in_snapshot() -> None:
    """用户直挂按钮权限点与角色继承 ui_actions 做并集返回（直挂为辅叠加）。"""
    svc, repo, _ = _svc()
    await repo.replace_user_ui_permissions(1, ["metric:create", "metric:deprecate"], 99, "专项授权")
    snap = await svc.my_permissions(FakeUser(uid=1, role="viewer"))  # type: ignore[arg-type]
    # viewer 基线含 catalog:view 等查看类；直挂叠加 metric:create/deprecate
    assert "catalog:view" in snap.ui_actions
    assert "metric:create" in snap.ui_actions
    assert "metric:deprecate" in snap.ui_actions


async def test_get_user_ui_permissions_matrix() -> None:
    """按用户授权矩阵：返回角色继承 / 直挂 / 并集三态，供前端回显勾选。"""
    svc, repo, _ = _svc()
    await repo.replace_user_ui_permissions(1, ["metric:create"], 99, None)
    data = await svc.get_user_ui_permissions(1)
    assert data["user_id"] == 1
    assert data["role"] == "viewer"
    assert "catalog:view" in data["role_actions"]  # viewer 基线
    assert data["direct_actions"] == ["metric:create"]
    assert "metric:create" in data["effective_actions"]
    assert "catalog:view" in data["effective_actions"]


async def test_set_user_ui_permissions_unknown_rejected() -> None:
    """直挂未知权限点 → USER_PERMISSION_INVALID（fail-closed）。"""
    from app.core.exceptions import ValidationError

    svc, _repo, _ = _svc()
    with pytest.raises(ValidationError) as ei:
        await svc.set_user_ui_permissions(1, ["ghost:perm"], 99)
    assert ei.value.error_code == "USER_PERMISSION_INVALID"


async def test_set_user_ui_permissions_empty_clears() -> None:
    """空列表清空直挂，回退为仅角色继承（effective == role_actions）。"""
    svc, repo, _ = _svc()
    await repo.replace_user_ui_permissions(1, ["metric:create"], 99, None)
    data = await svc.set_user_ui_permissions(1, [], 99, None)
    assert data["direct_actions"] == []
    assert data["effective_actions"] == data["role_actions"]


async def test_user_deny_overrides_role_inheritance() -> None:
    """用户级负向收窄（deny）优先于角色继承：deny 角色基线权限点后 effective 与快照均不含。"""
    svc, repo, _ = _svc()
    # viewer 基线含 catalog:view；用户级 deny catalog:view
    await repo.replace_user_ui_permissions(1, [], 99, None, deny_actions=["catalog:view"])
    data = await svc.get_user_ui_permissions(1)
    assert "catalog:view" in data["role_actions"]  # 角色继承仍存在
    assert "catalog:view" in data["deny_actions"]  # 直挂 deny 回显
    assert "catalog:view" not in data["effective_actions"]  # deny 优先于 grant
    # my_permissions 快照同样被收窄
    snap = await svc.my_permissions(FakeUser(uid=1, role="viewer"))  # type: ignore[arg-type]
    assert "catalog:view" not in snap.ui_actions


async def test_user_deny_can_be_recovered() -> None:
    """deny 可恢复：清空 deny 后角色继承权限点重新生效。"""
    svc, repo, _ = _svc()
    await repo.replace_user_ui_permissions(1, [], 99, None, deny_actions=["catalog:view"])
    data = await svc.set_user_ui_permissions(1, [], 99, None)  # deny_actions 缺省=清空
    assert data["deny_actions"] == []
    assert "catalog:view" in data["effective_actions"]


# ------------------------------------------------------- 误报反馈（COMPL-3）


class _FPFakeDB:
    """误报反馈专用 FakeDB：按语句区分 catalog / system_dict 查询。"""

    def __init__(self, catalog: FakeCatalog) -> None:
        self.catalog = catalog
        self.existing_dict: dict[str, object] = {}
        self.added: list[object] = []

    async def execute(self, stmt: Any) -> Any:
        text = str(stmt)

        class _Scalars:
            def __init__(self, owner: _FPFakeDB) -> None:
                self._owner = owner

            def all(self) -> list[object]:
                return list(self._owner.existing_dict.values())

        class _R:
            def __init__(self, owner: _FPFakeDB) -> None:
                self._owner = owner

            def scalar_one_or_none(self) -> object:
                if "db_catalog" in text:
                    return self._owner.catalog
                if "system_dict" in text:
                    # 返回已存在的词表项（首次查询为 None → 触发 add）
                    return self._owner.existing_dict.get("exempt_field")
                return None

            def scalars(self) -> _Scalars:
                return _Scalars(self._owner)

        return _R(self)

    def add(self, obj: object) -> None:
        self.added.append(obj)
        # 新增的 SystemDict 记录纳入后续查询（load_pii_vocab 合并豁免词）
        code = getattr(obj, "code", None)
        if code:
            self.existing_dict[code] = obj

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


async def test_classification_false_positive_writes_vocab_and_downgrades() -> None:
    """误报反馈：字段写入 exempt_field 词表 + 实体重算降级 + 审计留痕。"""
    from unittest.mock import AsyncMock, patch

    from app.services.collector.classifier import PiiVocab

    svc, repo, events = _svc()
    cat = FakeCatalog(
        5,
        "wedw_tmp.village_department_count_dp_tmp1",
        {"columns": [{"name": "name", "comment": "村名"}, {"name": "cnt", "comment": "数量"}]},
        "PII",
    )
    fake_db = _FPFakeDB(cat)
    svc._db = fake_db  # type: ignore[assignment]
    # 豁免词表已含 name（模拟此前误报反馈），重算时 name 不再判 PII
    vocab = PiiVocab(exempt_fields=frozenset({"name"}))

    with (
        patch(
            "app.services.governance.service.write_audit",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.collector.rules.load_pii_vocab",
            new=AsyncMock(return_value=vocab),
        ),
    ):
        result = await svc.classification_false_positive(
            catalog_id=5,
            column="name",
            scope="field",
            reason="村名不是个人姓名",
            actor_id=2,
        )

    assert result.entity_name == "wedw_tmp.village_department_count_dp_tmp1"
    assert result.exempted_as == "name"
    assert result.sensitivity_after == "INTERNAL"
    assert result.remaining_pii_columns == []
    # 豁免词表写入（SystemDict add）
    assert len(fake_db.added) == 1
    added = fake_db.added[0]
    assert added.dict_type == "pii_vocab"
    assert added.code == "exempt_field"
    assert added.description == "name"
    # 重算落库 + 敏感级降级
    assert repo.catalog_updates == [(5, "INTERNAL")]
    assert len(repo.classifications) == 1


async def test_classification_false_positive_prefix_scope() -> None:
    """前缀豁免：village_phone → village_ 前缀写入 exempt_prefix。"""
    from unittest.mock import AsyncMock, patch

    from app.services.collector.classifier import PiiVocab

    svc, repo, _ = _svc()
    cat = FakeCatalog(
        7,
        "dwd.org",
        {"columns": [{"name": "village_phone", "comment": "村电话"}]},
        "PII",
    )
    fake_db = _FPFakeDB(cat)
    svc._db = fake_db  # type: ignore[assignment]
    vocab = PiiVocab(exempt_prefixes=("village_",))

    with (
        patch(
            "app.services.governance.service.write_audit",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.collector.rules.load_pii_vocab",
            new=AsyncMock(return_value=vocab),
        ),
    ):
        result = await svc.classification_false_positive(
            catalog_id=7,
            column="village_phone",
            scope="prefix",
            reason="机构/地点字段",
            actor_id=2,
        )

    assert result.exempted_as == "village_"
    assert result.scope == "prefix"
    added = fake_db.added[0]
    assert added.code == "exempt_prefix"
    assert added.description == "village_"
