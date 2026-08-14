"""governance 服务编排单元测试（FakeRepo + FakeEvents，无真实 DB）。

覆盖：授权新建/幂等合并/范围校验/TTL、批量 dry-run 与执行、到期回收、
权限快照、PII 复核（含自审拦截）、分级重扫（含引擎降级）、PDP 决策。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.exceptions import AuthError, NotFoundError, ValidationError
from app.models.governance import Grant, GrantStatus, GrantType, Role, RoleName, SensitivityLevel
from app.services.governance.schemas import (
    ClassificationRescanRequest,
    GrantBatchRequest,
    GrantCreate,
    GrantListParams,
    PermissionCheckRequest,
    PiiReviewRequest,
    RoleCreate,
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
    def __init__(self, uid: int = 1, role: str = "viewer", domain: str | None = "hr") -> None:
        self.id = uid
        self.role = role
        self.domain = domain


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
    def __init__(self, cid: int, name: str, schema: dict[str, Any], level: str) -> None:
        self.id = cid
        self.entity_name = name
        self.schema_json = schema
        self.sensitivity_level = level


class FakeRepo:
    def __init__(self) -> None:
        self.grants: list[Grant] = []
        self.roles: list[Role] = []
        self.metrics: dict[str, FakeMetric] = {}
        self.catalogs: list[FakeCatalog] = []
        self.classifications: list[dict[str, Any]] = []
        self.catalog_updates: list[tuple[int, str]] = []
        self._seq = 0

    # role
    async def get_role_by_name(self, name: RoleName) -> Role | None:
        return next((r for r in self.roles if r.name == name), None)

    async def create_role(self, role: Role) -> Role:
        self._seq += 1
        role.id = self._seq
        self.roles.append(role)
        return role

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

    # metric
    async def get_metric_by_code(self, metric_code: str) -> FakeMetric | None:
        return self.metrics.get(metric_code)

    async def set_compliance_reviewed(self, metric: FakeMetric, reviewed: bool) -> FakeMetric:
        metric.compliance_reviewed = reviewed
        return metric

    # catalog / classification
    async def list_catalog(
        self, source_id: str | None, catalog_ids: list[int] | None, limit: int
    ) -> list[FakeCatalog]:
        rows = self.catalogs
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
        metric_code="m1", action="write", user_id=5, role="metric_owner", user_domain="sales"
    )
    assert decision.allow is True


async def test_check_metric_permission_blocks_cross_domain() -> None:
    svc, repo, _ = _svc()
    repo.metrics["m1"] = FakeMetric(metric_code="m1", domain="sales", owner_id=5)
    decision = await svc.check_metric_permission(
        metric_code="m1", action="write", user_id=5, role="metric_owner", user_domain="finance"
    )
    assert decision.allow is False
    assert decision.error_code == "FORBIDDEN"


async def test_check_metric_permission_blocks_unreviewed_pii() -> None:
    svc, repo, _ = _svc()
    repo.metrics["m1"] = FakeMetric(
        metric_code="m1", domain="sales", owner_id=5, pii_flag=True, compliance_reviewed=False
    )
    decision = await svc.check_metric_permission(
        metric_code="m1", action="read", user_id=5, role="metric_owner", user_domain="sales"
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
        metric_code="m1", action="write", user_id=5, role="metric_owner", user_domain="sales"
    )
    assert blocked.allow is False
    assert blocked.error_code == "FORBIDDEN_PII"
    # 跳过 PII 门禁 → 域/角色校验放行（提交审核入口）
    allowed = await svc.check_metric_permission(
        metric_code="m1",
        action="write",
        user_id=5,
        role="metric_owner",
        user_domain="sales",
        skip_pii_gate=True,
    )
    assert allowed.allow is True
    # 跨域仍被拦截（skip_pii_gate 只豁免 PII，不豁免域）
    cross = await svc.check_metric_permission(
        metric_code="m1",
        action="write",
        user_id=5,
        role="metric_owner",
        user_domain="finance",
        skip_pii_gate=True,
    )
    assert cross.allow is False
