"""governance 服务编排（TD §12.5 / FR-11）。

职责：

1. 角色与授权（域 + 指标白名单 + 行级开关 + 临时授权 TTL 自动回收）
2. PII 合规门禁（COMPL-1）：合规官复核后方可置 ``metric.compliance_reviewed=true``
3. 分级重扫（COMPL-2）：规则引擎重算敏感级 → 落 ``classification`` + 回写 ``db_catalog``
4. 权限快照与 PDP 决策入口（供 consume/semantic 调用）
5. PII 血缘传播（P2: US13）：register_catalog/create_metric 时检查上游字段 PII 标记，
   自动设置 metric.definition_json.pii=True 并标记 lineage_edge.pii_inherited=True

安全基线：授权范围不得为空（防越权全量放权）、复核禁止自审、批量操作有上限且失败即回滚。
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.base_service import BaseService
from app.core.exceptions import AuthError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.audit import AuditLog
from app.models.erasure import ErasureRequest, ErasureStatus
from app.models.governance import (
    Grant,
    GrantStatus,
    Role,
    RoleName,
    SensitivityLevel,
)
from app.models.user import User
from app.services.governance import policy
from app.services.governance.events import GovernanceEventPublisher
from app.services.governance.repository import GovernanceRepository
from app.services.governance.schemas import (
    ClassificationFalsePositiveResult,
    ClassificationItem,
    ClassificationRescanRequest,
    ClassificationRescanResult,
    GrantBatchItemResult,
    GrantBatchRequest,
    GrantBatchResult,
    GrantCreate,
    GrantListParams,
    GrantResponse,
    PermissionCheckRequest,
    PermissionCheckResult,
    PermissionSnapshot,
    PiiReviewRequest,
    PiiReviewResult,
    PiiSecondaryValidationResult,
    RoleCreate,
)

logger = get_logger("unisense.governance.service")


def _role_to_str(role: Any) -> str:
    """将 user.role（DB 枚举成员或字符串）统一为字符串值。

    说明：User.role 列是 SQLAlchemy 字符串 Enum，从真实 MySQL 加载后为普通 enum.Enum
    成员（非 StrEnum）。若直接用于 ``RoleName(...)`` 会抛 ValueError，用
    ``ROLE_ACTIONS.get(...)`` 会命中空集合；统一转为字符串值以保证 PDP 与角色网关
    在真实 DB 下正确工作。StrEnum 成员也会被正确取出其 .value。
    """
    value = role.value if isinstance(role, enum.Enum) else role
    return str(value)


def _grant_to_dict(g: Grant) -> dict[str, Any]:
    """将 Grant ORM 行转为 PDP ``Subject.grants`` 元组项。

    ``policy.decide`` / ``policy._match_grant`` 消费该字典（status 须为 "ACTIVE" 字符串、
    grant_type 为 READ/WRITE/READ_WRITE、expires_at 支持 datetime/None/ISO 字符串）。
    """
    return {
        "id": g.id,
        "domain": g.domain,
        "metric_whitelist": [str(x) for x in (g.metric_whitelist or [])],
        "grant_type": str(g.grant_type),
        "status": str(g.status),
        "row_level": g.row_level,
        "expires_at": g.expires_at,
    }


#: 授权到期提醒窗口（天）——快照中标记 expiring_soon，驱动「一键续期」待办（OP-02）。
EXPIRING_WINDOW_DAYS = 7


class GovernanceService(BaseService):
    """权限与合规治理编排。"""

    def __init__(self, db: AsyncSession, events: GovernanceEventPublisher | None = None) -> None:
        super().__init__(db)
        self._db = db
        self._repo = GovernanceRepository(db)
        self._legacy_events = events or GovernanceEventPublisher()

    async def _safe_publish(self, event: dict[str, Any]) -> None:
        """事件发布为 best-effort：通知服务不可达时静默降级，不阻断主流程。

        P3: 优先使用 EventBus.publish，保留 legacy_events 兼容。
        """
        # 1. 使用统一 EventBus 发布
        try:
            event_type = event.get("event_type", "governance.unknown")
            await self._publish_event(event_type, event, actor_id="")
        except Exception as exc:  # noqa: BLE001
            logger.warning("governance EventBus 发布失败: %s", exc)

        # 2. 兼容：仍发送到 legacy 事件发布器
        try:
            await self._legacy_events.publish(event)
        except Exception as exc:  # noqa: BLE001 - 事件降级，不向上抛
            logger.warning("governance 事件发布失败（best-effort 跳过）：%s", exc)

    # ------------------------------------------------------------------ role

    async def create_role(self, payload: RoleCreate) -> Role:
        """创建角色；同名角色幂等返回既有记录。"""
        existing = await self._repo.get_role_by_name(payload.name)
        if existing is not None:
            return existing
        return await self._repo.create_role(
            Role(name=payload.name, description=payload.description)
        )

    # -------------------------------------------------------- custom role CRUD

    async def create_custom_role(self, name: str, description: str | None) -> Role:
        """创建自定义角色（方案 A：自定义角色名写入 role 表 + user.role 字符串承载）。

        校验规则：
        - 名称须为 ``[a-z][a-z0-9_]{1,31}``（小写，32 位内）；
        - 不得与内置角色同名（内置角色名不可被覆盖为自定义）；
        - 同名角色已存在时幂等返回既有记录。

        Raises:
            ValidationError: 名称格式非法 / 与内置角色重名。
        """
        if name in policy.ROLE_ACTIONS or name in policy.ROLE_UI_ACTIONS:
            raise ValidationError(
                f"角色名 {name} 为内置角色，不可创建自定义角色", error_code="ROLE_NAME_RESERVED"
            )
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", name):
            raise ValidationError(
                f"自定义角色名非法: {name}（须小写字母开头，含小写字母/数字/下划线，2-32 位）",
                error_code="ROLE_NAME_INVALID",
                ctx={"name": name},
            )
        existing = await self._repo.get_role_by_name(name)
        if existing is not None:
            return existing
        return await self._repo.create_role(
            Role(name=name, description=description, is_custom=True)
        )

    async def delete_custom_role(self, name: str) -> None:
        """删除自定义角色（软删）。

        保护：
        - 内置角色不可删除（须先放开平台管理的该角色权限点覆盖？不——内置角色由系统管理）；
        - 仍被用户占用的角色不可删除（先改派用户或迁移权限）。

        Raises:
            ValidationError: 内置角色 / 仍被用户占用。
            NotFoundError: 自定义角色不存在。
        """
        if name in policy.ROLE_ACTIONS or name in policy.ROLE_UI_ACTIONS:
            raise ValidationError(
                f"角色 {name} 为内置角色，不可删除", error_code="ROLE_NAME_RESERVED"
            )
        row = await self._repo.get_role_by_name(name)
        if row is None:
            raise NotFoundError("角色不存在", ctx={"role": name})
        if not getattr(row, "is_custom", False):
            raise ValidationError(
                f"角色 {name} 非自定义角色，不可删除", error_code="ROLE_NAME_RESERVED"
            )
        used = await self._repo.count_users_by_role(name)
        if used > 0:
            raise ValidationError(
                f"角色 {name} 仍被 {used} 个用户使用，请先改派用户角色",
                error_code="ROLE_IN_USE",
                ctx={"role": name, "users": used},
            )
        await self._repo.delete_role(row)

    async def list_custom_roles(self) -> list[Role]:
        """列出全部自定义角色（用户管理角色下拉数据源）。"""
        return await self._repo.list_custom_roles()

    async def list_role_options(self) -> list[dict[str, Any]]:
        """列出全部角色行（内置登记 + 自定义），供授权下拉 id→name 映射。

        Returns:
            每项含 ``id``（grants.role_id 关联键）/ ``name`` / ``is_custom``。
        """
        roles = await self._repo.list_all_roles()
        return [
            {"id": r.id, "name": str(r.name), "is_custom": bool(r.is_custom)} for r in roles
        ]

    async def action_registry(self) -> list[dict[str, str]]:
        """动作点注册表（前端角色管理可视化配置数据源）。

        Returns:
            每项含 ``action``（权限点键）/ ``module``（分组）/ ``label``（中文名）/
            ``description``（说明），按模块名分组排序。
        """
        items = [
            {
                "action": action,
                "module": meta["module"],
                "label": meta["label"],
                "description": meta["description"],
            }
            for action, meta in policy.UI_ACTION_REGISTRY.items()
        ]
        items.sort(key=lambda i: (i["module"], i["action"]))
        return items

    # --------------------------------------------- role permission (RBAC 可配置化)

    async def list_role_permissions(self) -> list[dict[str, Any]]:
        """角色 × 权限点配置列表（默认基线 + ``role_permission`` 表覆盖的合并视图）。

        Returns:
            每项含 ``role`` / ``default_actions`` / ``custom_actions``（未覆盖为 None）/
            ``effective_actions``（生效动作）/ ``protected``（受保护角色不可配置），
            以及 UI 权限点三态（``ui_default_actions`` / ``ui_custom_actions`` /
            ``ui_effective_actions``）与 ``is_custom``（自定义角色标记）。
        """
        overrides = await self._repo.list_role_permissions()
        by_role: dict[str, set[str]] = {}
        for row in overrides:
            by_role.setdefault(row.role, set()).add(row.action)
        custom_roles = await self._repo.list_custom_roles()
        roles = sorted(
            set(policy.ROLE_ACTIONS.keys())
            | set(policy.ROLE_UI_ACTIONS.keys())
            | set(by_role.keys())
            | {str(r.name) for r in custom_roles},
            key=lambda r: (r not in policy.PROTECTED_ROLES, r),  # 受保护角色（平台管理员）置顶
        )
        items: list[dict[str, Any]] = []
        for r in roles:
            default_actions = sorted(policy.ROLE_ACTIONS.get(r, frozenset()))
            ui_default_actions = sorted(policy.ROLE_UI_ACTIONS.get(r, frozenset()))
            raw_custom = by_role.get(r, set())
            custom_actions = sorted(a for a in raw_custom if not policy.is_ui_action(a)) or None
            ui_custom_actions = sorted(a for a in raw_custom if policy.is_ui_action(a)) or None
            items.append(
                {
                    "role": r,
                    "default_actions": default_actions,
                    "custom_actions": custom_actions,
                    "effective_actions": custom_actions or default_actions,
                    "ui_default_actions": ui_default_actions,
                    "ui_custom_actions": ui_custom_actions,
                    "ui_effective_actions": ui_custom_actions or ui_default_actions,
                    "protected": r in policy.PROTECTED_ROLES,
                    "is_custom": r in {str(c.name) for c in custom_roles},
                }
            )
        return items

    async def load_role_actions(self) -> dict[str, frozenset[str]]:
        """合并默认基线 + 覆盖，产出供 ``policy.decide`` 的 ``role_actions`` 映射。

        仅合并**资源级动词**（read/write/approve/export/review）覆盖；UI 权限点
        （``模块:功能``）经 ``load_ui_role_actions`` 独立合并，避免 UI 配置误伤 PDP 判定。
        每次决策前调用（低频配置查询，RBAC 配置化场景可接受）；被覆盖的角色以
        ``role_permission`` 表为准，未覆盖的沿用 ``policy.ROLE_ACTIONS`` 默认。

        P10（性能审查）：consume dry-run/execute 等高频路径每次 PDP 决策都全表扫
        ``role_permission``——加进程内短 TTL 缓存（60s），配置变更在 TTL 内自然收敛。
        """
        from app.services.governance.cache import get_role_actions_cached

        return await get_role_actions_cached(self._load_role_actions_uncached)

    async def _load_role_actions_uncached(self) -> dict[str, frozenset[str]]:
        merged = dict(policy.ROLE_ACTIONS)
        by_role: dict[str, set[str]] = {}
        for row in await self._repo.list_role_permissions():
            by_role.setdefault(row.role, set()).add(row.action)
        for role, actions in by_role.items():
            resource_actions = frozenset(a for a in actions if not policy.is_ui_action(a))
            if resource_actions:
                merged[role] = resource_actions
        return merged

    async def load_ui_role_actions(self) -> dict[str, frozenset[str]]:
        """合并默认基线 + 覆盖，产出供前端 ``usePermission`` 消费的 UI 权限点映射。

        仅合并 UI 权限点（``模块:功能``）覆盖；自定义角色无默认基线，生效动作
        完全来自 ``role_permission`` 覆盖（未配置即为空集，fail-closed）。

        P10：与 load_role_actions 同款进程内短 TTL 缓存（60s）。
        """
        from app.services.governance.cache import get_ui_role_actions_cached

        return await get_ui_role_actions_cached(self._load_ui_role_actions_uncached)

    async def _load_ui_role_actions_uncached(self) -> dict[str, frozenset[str]]:
        merged = dict(policy.ROLE_UI_ACTIONS)
        by_role: dict[str, set[str]] = {}
        for row in await self._repo.list_role_permissions():
            by_role.setdefault(row.role, set()).add(row.action)
        for role, actions in by_role.items():
            ui_actions = frozenset(a for a in actions if policy.is_ui_action(a))
            if ui_actions:
                merged[role] = ui_actions
        return merged

    async def set_role_permissions(self, role: str, actions: list[str]) -> dict[str, Any]:
        """覆盖某角色的权限点（RBAC 配置化）。

        Raises:
            ValidationError: 角色为受保护角色（platform_admin）或含未知动作。
        """
        if role in policy.PROTECTED_ROLES:
            raise ValidationError(
                "platform_admin 为受保护角色，权限点不可配置（硬编码跨域直通）",
                error_code="ROLE_PERMISSION_PROTECTED",
                ctx={"role": role},
            )
        unknown = sorted(set(actions) - set(policy.all_configurable_actions()))
        if unknown:
            raise ValidationError(
                f"包含未知权限点: {', '.join(unknown)}",
                error_code="ROLE_PERMISSION_INVALID",
                ctx={"role": role, "unknown": unknown},
            )
        await self._repo.replace_role_permissions(role, sorted(set(actions)))
        from app.services.governance.cache import invalidate_role_actions_cache

        invalidate_role_actions_cache()  # P10：写后主动失效进程内缓存
        for item in await self.list_role_permissions():
            if item["role"] == role:
                return item
        raise NotFoundError("角色不存在", ctx={"role": role})

    async def reset_role_permissions(self, role: str) -> dict[str, Any]:
        """清除某角色的权限点覆盖，恢复默认基线。

        Raises:
            ValidationError: 角色为受保护角色（platform_admin）。
        """
        if role in policy.PROTECTED_ROLES:
            raise ValidationError(
                "platform_admin 为受保护角色，权限点不可配置（硬编码跨域直通）",
                error_code="ROLE_PERMISSION_PROTECTED",
                ctx={"role": role},
            )
        await self._repo.reset_role_permissions(role)
        from app.services.governance.cache import invalidate_role_actions_cache

        invalidate_role_actions_cache()  # P10：写后主动失效进程内缓存
        for item in await self.list_role_permissions():
            if item["role"] == role:
                return item
        raise NotFoundError("角色不存在", ctx={"role": role})

    # ----------------------------------------------------- user permission

    async def get_user_ui_permissions(self, user_id: int) -> dict[str, Any]:
        """查询用户按钮权限点（角色继承 + 直挂并集，供「按用户授权」矩阵）。

        Returns:
            dict: ``user_id / role / role_actions / direct_actions / effective_actions``。
        """
        user = await self._ensure_user_exists(user_id)
        role_s = _role_to_str(user.role)
        all_roles = user.roles_all()
        ui_role_actions = await self.load_ui_role_actions()
        role_actions = frozenset().union(
            *(ui_role_actions.get(r, frozenset()) for r in all_roles)
        )
        direct = await self._repo.list_user_ui_permissions(user_id)
        return {
            "user_id": user_id,
            "role": role_s,
            "roles": all_roles,
            "role_actions": sorted(role_actions),
            "direct_actions": sorted(direct),
            "effective_actions": sorted(set(role_actions) | direct),
        }

    async def set_user_ui_permissions(
        self,
        user_id: int,
        actions: list[str],
        actor_id: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """整表替换某用户直挂的按钮权限点（支持清空）。

        Raises:
            ValidationError: 含未知权限点（不在 UI 动作注册表内）。
        """
        unknown = sorted(set(actions) - set(policy.all_configurable_actions()))
        if unknown:
            raise ValidationError(
                f"包含未知权限点: {', '.join(unknown)}",
                error_code="USER_PERMISSION_INVALID",
                ctx={"user_id": user_id, "unknown": unknown},
            )
        await self._repo.replace_user_ui_permissions(
            user_id, sorted(set(actions)), actor_id, reason
        )
        return await self.get_user_ui_permissions(user_id)

    # ----------------------------------------------------------------- grant

    async def grant(self, payload: GrantCreate, actor_id: int) -> Grant:
        """新增/续期授权。

        越权防护（P0）：授权人须具备与被授权范围匹配的域权限——
        ``platform_admin`` 可全局授权；``domain_admin`` 仅可授权 **本域**
        （域为空或跨域一律 fail-closed 拒绝）；其余角色禁止授权。

        Raises:
            ValidationError: 授权范围为空、TTL 已过期或被授权人不存在。
            AuthError: 授权人域权限不足以授予目标范围。
        """
        self._validate_grant_scope(payload)
        actor = await self._ensure_user_exists(actor_id)
        self._assert_grant_scope(actor, payload)
        await self._ensure_user_exists(payload.user_id)

        existing = await self._repo.find_active_grant(
            payload.user_id, payload.role_id, payload.domain, payload.grant_type
        )
        if existing is not None:
            merged = sorted(
                {str(x) for x in (existing.metric_whitelist or [])}
                | set(payload.metric_whitelist or [])
            )
            existing.metric_whitelist = merged or None
            existing.row_level = existing.row_level or payload.row_level
            # 续期语义收敛：仅当显式传入 expires_at 才调整到期时间，
            # 缺省（None）保持既有到期时间，避免「省略 TTL 续期」把临时授权
            # 静默升级为永久授权（fail-closed）。
            if payload.expires_at is not None:
                existing.expires_at = _later(existing.expires_at, payload.expires_at)
            existing.granted_by = actor_id
            if payload.reason:
                existing.reason = payload.reason
            await self._db.flush()
            row = existing
        else:
            row = await self._repo.create_grant(
                Grant(
                    user_id=payload.user_id,
                    role_id=payload.role_id,
                    domain=payload.domain,
                    metric_whitelist=payload.metric_whitelist,
                    row_level=payload.row_level,
                    grant_type=payload.grant_type,
                    status=GrantStatus.ACTIVE,
                    expires_at=payload.expires_at,
                    granted_by=actor_id,
                    reason=payload.reason,
                )
            )

        await self._safe_publish(
            {
                "event_type": "grant.granted",
                "grant_id": row.id,
                "user_id": row.user_id,
                "domain": row.domain,
                "grant_type": str(row.grant_type),
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            }
        )
        return row

    def _assert_grant_scope(self, actor: User, payload: GrantCreate) -> None:
        """授权越权防护（P0）：授予范围必须收敛到授权人的管理域。

        与 ``_assert_revoke_scope`` 对称，弥补原 grant 无 actor 范围校验的提权漏洞：

        - ``platform_admin``：可全局授权；
        - ``domain_admin``：仅可授予 **本域**（``payload.domain == actor.domain``）；
          未指定域（``domain is None``）或跨域一律拒绝（fail-closed）；
        - 其余角色：禁止授权（fail-closed，纵深防御）。

        Raises:
            AuthError: 授权人域权限不足以授予目标范围。
        """
        role = _role_to_str(actor.role)
        if role == "platform_admin":
            return
        if role == "domain_admin":
            if payload.domain and payload.domain == actor.domain:
                return
            raise AuthError(
                "域管理员仅可授予本域授权",
                error_code="FORBIDDEN",
                ctx={
                    "grant_domain": payload.domain,
                    "actor_domain": actor.domain,
                    "user_id": payload.user_id,
                },
            )
        raise AuthError(
            "当前角色无授权权限（仅平台管理员/本域管理员可授权）",
            error_code="FORBIDDEN",
            ctx={"actor_id": actor.id, "role": role},
        )

    def _validate_grant_scope(self, payload: GrantCreate) -> None:
        """授权范围必须收敛：域与白名单不可同时为空（否则等价于全量放权）。"""
        if not payload.domain and not payload.metric_whitelist:
            raise ValidationError(
                "授权范围不能为空：domain 与 metric_whitelist 至少提供一项",
                ctx={"user_id": payload.user_id},
            )
        if payload.expires_at is not None:
            expires = payload.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= datetime.now(UTC):
                raise ValidationError(
                    "expires_at 必须晚于当前时间", ctx={"expires_at": expires.isoformat()}
                )

    async def _ensure_user_exists(self, user_id: int) -> User:
        stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
        user = (await self._db.execute(stmt)).scalar_one_or_none()
        if user is None:
            raise NotFoundError("被授权用户不存在", ctx={"user_id": user_id})
        return user

    async def batch(
        self, payload: GrantBatchRequest, actor_id: int, dry_run: bool
    ) -> GrantBatchResult:
        """批量授权/回收（R3-07）。

        ``dry_run=True`` 仅计算影响面不落库；``dry_run=False`` 逐条执行，
        任一条目失败即抛出异常，由 API 层回滚整批（全成功或全不生效）。
        """
        items: list[GrantBatchItemResult] = []
        affected_users: set[int] = set()
        affected_metrics: set[str] = set()
        succeeded = 0
        failed = 0

        for item in payload.items:
            affected_users.add(item.user_id)
            affected_metrics.update(item.metric_whitelist or [])
            if dry_run:
                ok, detail = await self._preview_item(payload.operation, item)
                succeeded += int(ok)
                failed += int(not ok)
                items.append(
                    GrantBatchItemResult(
                        user_id=item.user_id,
                        domain=item.domain,
                        action=payload.operation,
                        ok=ok,
                        detail=detail,
                    )
                )
                continue

            if payload.operation == "grant":
                row = await self.grant(item, actor_id)
                detail = f"grant#{row.id}"
            else:
                revoked = await self._revoke_matching(item, actor_id)
                detail = f"revoked={revoked}"
            succeeded += 1
            items.append(
                GrantBatchItemResult(
                    user_id=item.user_id,
                    domain=item.domain,
                    action=payload.operation,
                    ok=True,
                    detail=detail,
                )
            )

        return GrantBatchResult(
            dry_run=dry_run,
            operation=payload.operation,
            affected_users=len(affected_users),
            affected_metrics=len(affected_metrics),
            succeeded=succeeded,
            failed=failed,
            items=items,
        )

    async def _preview_item(self, operation: str, item: GrantCreate) -> tuple[bool, str]:
        """dry-run 单条预检：不写库，返回 (是否可执行, 说明)。"""
        try:
            self._validate_grant_scope(item)
            await self._ensure_user_exists(item.user_id)
        except (ValidationError, NotFoundError) as exc:
            return False, exc.message
        if operation == "revoke":
            existing = await self._repo.find_active_grant(
                item.user_id, item.role_id, item.domain, item.grant_type
            )
            if existing is None:
                return False, "无匹配的 ACTIVE 授权可回收"
            return True, f"将回收 grant#{existing.id}"
        existing = await self._repo.find_active_grant(
            item.user_id, item.role_id, item.domain, item.grant_type
        )
        return True, ("将续期/合并既有授权" if existing else "将新建授权")

    async def _revoke_matching(self, item: GrantCreate, actor_id: int) -> int:
        existing = await self._repo.find_active_grant(
            item.user_id, item.role_id, item.domain, item.grant_type
        )
        if existing is None:
            raise NotFoundError(
                "无匹配的 ACTIVE 授权可回收",
                ctx={"user_id": item.user_id, "domain": item.domain},
            )
        actor = await self._ensure_user_exists(actor_id)
        self._assert_revoke_scope(actor, existing)
        await self._repo.set_grant_status(existing, GrantStatus.REVOKED, item.reason)
        await self._safe_publish(
            {
                "event_type": "grant.revoked",
                "grant_id": existing.id,
                "user_id": existing.user_id,
                "operator_id": actor_id,
            }
        )
        return 1

    def _assert_revoke_scope(self, actor: User, grant: Grant) -> None:
        """回收授权范围校验（D10 §3.5 缺口补齐）。

        回收权限须收敛到授权目标归属/域，防止越权回收：

        - ``platform_admin``：全局可回收；
        - ``domain_admin``：仅可回收 **本域** 授权（``grant.domain == actor.domain``）；
          无域归属的授权（``domain is None``）视为跨域，须由平台管理员回收（fail-closed）；
        - 其它角色（analyst / metric_owner / reviewer / compliance_officer / viewer）：
          仅可回收 **本人** 授权（``grant.user_id == actor.id``）。

        默认 fail-closed：任何不满足上述条件的回收一律 ``FORBIDDEN``。
        """
        role = _role_to_str(actor.role)
        if role == "platform_admin":
            return
        if role == "domain_admin":
            if grant.domain and grant.domain == actor.domain:
                return
            raise AuthError(
                "域管理员仅可回收本域授权",
                error_code="FORBIDDEN",
                ctx={"grant_domain": grant.domain, "actor_domain": actor.domain},
            )
        # 非管理员：仅可回收本人授权
        if grant.user_id == actor.id:
            return
        raise AuthError(
            "无权回收该授权（仅平台管理员/本域管理员/授权本人可操作）",
            error_code="FORBIDDEN",
            ctx={"grant_user_id": grant.user_id, "actor_id": actor.id},
        )

    async def revoke(self, grant_id: int, actor_id: int, reason: str | None = None) -> Grant:
        """按 ID 回收授权。"""
        row = await self._repo.get_grant(grant_id)
        if row is None:
            raise NotFoundError("授权不存在", ctx={"grant_id": grant_id})
        if row.status is not GrantStatus.ACTIVE:
            raise ValidationError(
                f"仅 ACTIVE 授权可回收，当前状态 {row.status}", ctx={"grant_id": grant_id}
            )
        actor = await self._ensure_user_exists(actor_id)
        self._assert_revoke_scope(actor, row)
        await self._repo.set_grant_status(row, GrantStatus.REVOKED, reason)
        await self._safe_publish(
            {
                "event_type": "grant.revoked",
                "grant_id": row.id,
                "user_id": row.user_id,
                "operator_id": actor_id,
            }
        )
        return row

    async def list_grants(self, params: GrantListParams) -> tuple[list[Grant], int]:
        return await self._repo.list_grants(
            params.user_id, params.domain, params.status, params.page, params.page_size
        )

    async def expire_due_grants(self) -> int:
        """到期授权自动回收（TD §12.5 定时 Worker，每 5 分钟）。"""
        rows = await self._repo.expire_due_grants()
        for row in rows:
            await self._safe_publish(
                {
                    "event_type": "grant.expired",
                    "grant_id": row.id,
                    "user_id": row.user_id,
                    "domain": row.domain,
                }
            )
        # 顺带触发「即将到期」提醒（同 Worker 入口，能力闭环）
        await self.remind_expiring_grants()
        return len(rows)

    async def remind_expiring_grants(self, window: timedelta | None = None) -> int:
        """授权到期提醒（TD §5.5 grant.expiring_soon，定向通知被授权人）。

        扫描 ``EXPIRING_WINDOW_DAYS``（默认 7 天）内到期且未提醒的授权，
        定向通知被授权人（IN_APP，不依赖订阅偏好），随后批量标记提醒时间去重。
        best-effort：单条通知失败不阻断其余；返回本轮提醒条数。
        """
        win = window or timedelta(days=EXPIRING_WINDOW_DAYS)
        rows = await self._repo.list_expiring_grants(win)
        if not rows:
            return 0
        from app.db.mysql import async_session_factory
        from app.services.notify.service import NotifyService

        reminded: list[int] = []
        for row in rows:
            async with async_session_factory() as session:
                try:
                    await NotifyService(session).notify_user(
                        user_id=row.user_id,
                        event_type="grant.expiring_soon",
                        title="权限即将到期",
                        payload={
                            "grant_id": row.id,
                            "domain": row.domain or "",
                            "expires_at": (
                                row.expires_at.isoformat() if row.expires_at else ""
                            ),
                        },
                    )
                    reminded.append(row.id)
                except Exception as exc:  # noqa: BLE001 - best-effort 不阻断
                    logger.warning(
                        "grant_expiring_notify_failed grant=%s user=%s err=%s",
                        row.id,
                        row.user_id,
                        exc,
                    )
        await self._repo.mark_expiring_reminded(reminded)
        return len(reminded)

    # ----------------------------------------------------------- permissions

    async def my_permissions(self, user: User) -> PermissionSnapshot:
        """当前用户权限快照（``GET /me/permissions``）。"""
        grants = await self._repo.active_grants_for_user(user.id)
        effective = [g for g in grants if policy.is_grant_effective(g.expires_at)]
        deadline = datetime.now(UTC) + timedelta(days=EXPIRING_WINDOW_DAYS)
        expiring = [
            g for g in effective if g.expires_at is not None and _as_utc(g.expires_at) <= deadline
        ]
        whitelist: set[str] = set()
        domains: set[str] = set()
        for g in effective:
            whitelist.update(str(x) for x in (g.metric_whitelist or []))
            if g.domain:
                domains.add(g.domain)
        role_s = _role_to_str(user.role)
        all_roles = user.roles_all()
        role_actions = await self.load_role_actions()
        ui_role_actions = await self.load_ui_role_actions()
        direct_actions = await self._repo.list_user_ui_permissions(user.id)
        # 方案 A 多角色：所有角色（主角色 + user_role 扩展）的权限点取并集，
        # 避免多角色用户只按单角色查 ui_actions 导致其它角色权限缺失。
        ui_actions: set[str] = set(direct_actions)
        allowed_actions: set[str] = set()
        for r in all_roles:
            ui_actions |= set(ui_role_actions.get(r, frozenset()))
            allowed_actions |= set(role_actions.get(r, frozenset()))
        return PermissionSnapshot(
            user_id=user.id,
            role=role_s,
            roles=all_roles,
            home_domain=user.domain,
            allowed_actions=sorted(allowed_actions),
            ui_actions=sorted(ui_actions),
            granted_domains=sorted(domains),
            metric_whitelist=sorted(whitelist),
            row_level_restricted=any(g.row_level for g in effective),
            grants=[GrantResponse.model_validate(g) for g in effective],
            expiring_soon=[GrantResponse.model_validate(g) for g in expiring],
        )

    async def check_permission(self, req: PermissionCheckRequest) -> PermissionCheckResult:
        """PDP 决策入口（默认拒绝）。"""
        user = await self._ensure_user_exists(req.user_id)
        grants = await self._repo.active_grants_for_user(user.id)
        subject = policy.Subject(
            user_id=user.id,
            role=_role_to_str(user.role),
            domain=user.domain,
            grants=tuple(_grant_to_dict(g) for g in grants),
            roles=tuple(user.roles_all()),
        )
        resource = policy.Resource(domain=req.domain, metric_code=req.metric_code)
        if req.metric_code:
            metric = await self._repo.get_metric_by_code(req.metric_code)
            if metric is None:
                raise NotFoundError("指标不存在", ctx={"metric_code": req.metric_code})
            resource = policy.Resource(
                domain=req.domain or metric.domain,
                metric_code=metric.metric_code,
                sensitivity=(
                    SensitivityLevel.PII.value
                    if metric.pii_flag
                    else SensitivityLevel.INTERNAL.value
                ),
                compliance_reviewed=bool(metric.compliance_reviewed),
                owner_id=metric.owner_id,
            )
        decision = policy.decide(
            subject, req.action, resource, role_actions=await self.load_role_actions()
        )
        return PermissionCheckResult(
            allow=decision.allow,
            reason=decision.reason,
            error_code=decision.error_code,
            restricted=decision.restricted,
            masking=decision.masking,
        )

    async def check_metric_permission(
        self,
        metric_code: str,
        action: str,
        user_id: int,
        role: str,
        user_domain: str | None = None,
        *,
        skip_pii_gate: bool = False,
    ) -> policy.Decision:
        """PDP 决策入口——供 semantic 等服务调用。

        构建 Subject 时不加载 grants（semantic 操作仅需角色+域校验，
        grants 由 consume 的接入方鉴权路径处理）。

        Args:
            metric_code: 指标编码。
            action: read/write/approve/export/review。
            user_id: 操作人 ID。
            role: 操作人角色字符串。
            user_domain: 操作人所属域。
            skip_pii_gate: 跳过 PII 合规门禁（仅用于提交审核等 PII 合规流程入口，
                否则未复核的 PII 指标永远无法进入 REVIEW 状态形成死锁）。

        Returns:
            policy.Decision。

        Raises:
            NotFoundError: 指标不存在。
        """
        metric = await self._repo.get_metric_by_code(metric_code)
        if metric is None:
            raise NotFoundError("指标不存在", ctx={"metric_code": metric_code})
        resource = policy.Resource(
            domain=metric.domain,
            metric_code=metric.metric_code,
            sensitivity=(
                SensitivityLevel.PII.value if metric.pii_flag else SensitivityLevel.INTERNAL.value
            ),
            # skip_pii_gate=True 时视为已复核，绕过 PII 门禁但保留域/角色校验
            compliance_reviewed=bool(metric.compliance_reviewed) or skip_pii_gate,
            owner_id=metric.owner_id,
        )
        subject = policy.Subject(
            user_id=user_id,
            role=role,
            domain=user_domain,
            grants=(),
        )
        return policy.decide(
            subject, action, resource, role_actions=await self.load_role_actions()
        )

    async def check_internal_read_permission(
        self,
        user: User,
        metric_code: str,
    ) -> tuple[policy.Decision, dict[str, Any] | None]:
        """PDP 决策入口——内部登录用户只读查询（consume internal 路径，TD §12.5）。

        与 ``check_metric_permission``（grants=()，语义/资产管理场景）不同：
        本方法加载该用户的跨域授权（``active_grants_for_user``）再构建 Subject，
        使 metric_whitelist / domain / row_level 授权在内部查询路径真正生效。

        Returns:
            (Decision, 命中的授权字典)。``restricted`` 授权命中时返回对应 grant
            （供调用方按 metric_whitelist 做行级过滤），其余情况返回 ``None``。

        Raises:
            NotFoundError: 指标不存在。
        """
        metric = await self._repo.get_metric_by_code(metric_code)
        if metric is None:
            raise NotFoundError("指标不存在", ctx={"metric_code": metric_code})
        grants = await self._repo.active_grants_for_user(user.id)
        subject = policy.Subject(
            user_id=user.id,
            role=_role_to_str(user.role),
            domain=user.domain,
            grants=tuple(_grant_to_dict(g) for g in grants),
            roles=tuple(user.roles_all()),
        )
        resource = policy.Resource(
            domain=metric.domain,
            metric_code=metric.metric_code,
            sensitivity=(
                SensitivityLevel.PII.value if metric.pii_flag else SensitivityLevel.INTERNAL.value
            ),
            compliance_reviewed=bool(metric.compliance_reviewed),
            owner_id=metric.owner_id,
        )
        decision = policy.decide(
            subject, "read", resource, role_actions=await self.load_role_actions()
        )
        matched = (
            policy._match_grant(subject.grants, "read", resource)
            if decision.allow and decision.restricted
            else None
        )
        return decision, matched

    # ------------------------------------------------------------ PII review

    async def pii_review(self, payload: PiiReviewRequest, reviewer: User) -> PiiReviewResult:
        """合规官复核（COMPL-1）。

        Raises:
            NotFoundError: 指标不存在。
            ValidationError: 复核人自审（职责分离）。
        """
        metric = await self._repo.get_metric_by_code(payload.metric_code)
        if metric is None:
            raise NotFoundError("指标不存在", ctx={"metric_code": payload.metric_code})
        if metric.owner_id == reviewer.id:
            raise ValidationError(
                "职责分离：合规复核人不得为指标 Owner",
                ctx={"metric_code": payload.metric_code, "reviewer_id": reviewer.id},
            )
        if payload.sensitivity_level is SensitivityLevel.UNKNOWN:
            # UNKNOWN 是分级引擎降级标记（0109 后两表枚举均已含，但仅降级路径使用），
            # 不可作为人工复核赋值的敏感级别——人工须给真实级别或 NEEDS_REVIEW。
            raise ValidationError(
                "敏感级别不可为 UNKNOWN（降级标记），请选择真实级别或 NEEDS_REVIEW",
                ctx={"metric_code": payload.metric_code},
            )

        approved = payload.decision == "APPROVE"
        masking = payload.masking_policy or policy.masking_for(payload.sensitivity_level)
        metric.pii_flag = payload.sensitivity_level is SensitivityLevel.PII
        await self._repo.set_compliance_reviewed(metric, approved)
        reviewed_at = datetime.now(UTC)

        await self._safe_publish(
            {
                "event_type": "pii.reviewed",
                "metric_code": metric.metric_code,
                "decision": payload.decision,
                "sensitivity_level": payload.sensitivity_level.value,
                "masking_policy": masking,
                "reviewer_id": reviewer.id,
                "comment": payload.comment,
            }
        )
        return PiiReviewResult(
            metric_code=metric.metric_code,
            decision=payload.decision,
            compliance_reviewed=approved,
            sensitivity_level=payload.sensitivity_level.value,
            masking_policy=masking,
            reviewer_id=reviewer.id,
            reviewed_at=reviewed_at,
            secondary_validation=await self.validate_pii_masking(
                metric.metric_code, pii_columns=payload.pii_columns
            ),
        )

    async def validate_pii_masking(
        self, metric_code: str, pii_columns: list[str] | None = None
    ) -> PiiSecondaryValidationResult:
        """PII 字段级脱敏二次校验（落库外 / 查询侧补强）。

        在 DB 落库脱敏之外，再次核验：
        1. 指标须通过合规复核（``compliance_reviewed``）；
        2. 字段级脱敏策略须已生效（PII 对应策略非 ``none``）；
        3. 口径定义中不得出现 PII 字段明文暴露。

        非 PII 指标直接判定通过（无字段级脱敏义务）。

        Raises:
            NotFoundError: 指标不存在。
        """
        from app.services.governance.schemas import PiiSecondaryValidationResult

        metric = await self._repo.get_metric_by_code(metric_code)
        if metric is None:
            raise NotFoundError("指标不存在", ctx={"metric_code": metric_code})

        # 非 PII 指标：无字段级脱敏义务，二次校验直接通过
        if not metric.pii_flag:
            return PiiSecondaryValidationResult(
                metric_code=metric_code,
                passed=True,
                masking_policy=policy.masking_for(SensitivityLevel.INTERNAL),
                checked_columns=[],
                findings=[],
            )

        findings: list[str] = []
        masking = policy.masking_for(SensitivityLevel.PII)

        # ① 合规复核门禁（COMPL-1）
        if not metric.compliance_reviewed:
            findings.append("PII 指标未通过合规复核(compliance_reviewed=False)，禁止对外服务")
        # ② 字段级脱敏策略须生效
        if masking in ("none", "NONE"):
            findings.append("PII 指标未配置字段级脱敏策略(mask_policy=none)")

        # ③ 口径定义明文暴露校验：先剔除脱敏函数调用（hash/mask/...）内的引用，
        # 剩余文本若仍含 PII 字段名，视为明文暴露。
        columns = list(pii_columns or (metric.definition_json or {}).get("pii_fields") or [])
        definition_text = json.dumps(metric.definition_json or {}, ensure_ascii=False)
        exposed_text = _strip_masking_calls(definition_text)
        for col in columns:
            if col and re.search(rf"\b{re.escape(col)}\b", exposed_text):
                findings.append(f"PII 字段 {col} 在口径定义中明文暴露，字段级脱敏二次校验未通过")

        return PiiSecondaryValidationResult(
            metric_code=metric_code,
            passed=len(findings) == 0,
            masking_policy=masking,
            checked_columns=columns,
            findings=findings,
        )

    # -------------------------------------------------------- classification

    async def classification_rescan(
        self, payload: ClassificationRescanRequest
    ) -> ClassificationRescanResult:
        """分级重扫（COMPL-2）。

        规则引擎对单个资产失败时按 TD §5.5 降级：标记 ``UNKNOWN`` 并继续，不阻断整批。
        """
        catalogs = await self._repo.list_catalog(
            payload.source_id, payload.catalog_ids, payload.limit, payload.source_ids
        )
        # 使用 DB 可配置规则（合并内置）——敏感规则配置台改规则后重扫即时生效
        from app.services.collector.classifier import SensitivityClassifier
        from app.services.collector.rules import load_pii_rules

        pii_rules, conf_rules = await load_pii_rules(self._db)
        classifier = SensitivityClassifier(rules=pii_rules, confidential_rules=conf_rules)
        items: list[ClassificationItem] = []
        changed = 0
        pii_found = 0
        degraded_cnt = 0

        for cat in catalogs:
            before = str(cat.sensitivity_level)
            try:
                hits = policy.detect_pii_columns(
                    cat.schema_json or {}, classifier=classifier
                )
                after = policy.infer_sensitivity(hits, current=before)
                degraded = False
            except Exception as exc:  # noqa: BLE001 - 单资产失败降级，不阻断整批
                logger.warning("分级引擎失败，资产 %s 标记 UNKNOWN：%s", cat.id, exc)
                hits = []
                after = SensitivityLevel.UNKNOWN
                degraded = True
                degraded_cnt += 1

            pii_cols = [
                {
                    "column": h.column,
                    "rule": h.rule,
                    "confidence": h.confidence,
                    "matched_by": h.matched_by,
                }
                for h in hits
            ]
            await self._repo.upsert_classification(
                catalog_id=cat.id,
                level=after,
                pii_columns=pii_cols,
                classified_by="rule_engine",
                model_version=policy.RULES_VERSION,
            )
            if after is SensitivityLevel.PII:
                pii_found += 1
            if after is not SensitivityLevel.UNKNOWN and after.value != before:
                await self._repo.update_catalog_sensitivity(cat.id, after)
                changed += 1
                await self._safe_publish(
                    {
                        "event_type": "classification.changed",
                        "catalog_id": cat.id,
                        "entity_name": cat.entity_name,
                        "before": before,
                        "after": after.value,
                    }
                )
            items.append(
                ClassificationItem(
                    catalog_id=cat.id,
                    entity_name=cat.entity_name,
                    sensitivity_before=before,
                    sensitivity_after=after.value,
                    pii_columns=pii_cols,
                    degraded=degraded,
                )
            )

        await self._safe_publish(
            {
                "event_type": "classification.done",
                "scanned": len(catalogs),
                "changed": changed,
                "pii_found": pii_found,
                "degraded": degraded_cnt,
                "model_version": policy.RULES_VERSION,
            }
        )
        return ClassificationRescanResult(
            scanned=len(catalogs),
            changed=changed,
            pii_found=pii_found,
            degraded=degraded_cnt,
            model_version=policy.RULES_VERSION,
            items=items,
        )

    async def classification_false_positive(
        self,
        catalog_id: int,
        column: str,
        scope: str,
        reason: str,
        actor_id: int,
    ) -> ClassificationFalsePositiveResult:
        """误报反馈（COMPL-3）：把误判字段/前缀写入 pii_vocab 豁免词表并重算实体。

        治理者在资产地图待复核明细发现误判后一键反馈：字段名/前缀写入
        ``pii_vocab`` 字典的 ``exempt_field``（精确）/``exempt_prefix``（前缀），
        再用含豁免词表的分类器重算该实体（pii_columns 剔除、敏感级降级）。
        全链路审计（谁、何时、豁免了什么、原因）。

        Args:
            catalog_id: 采集目录实体 ID。
            column: 被误判为 PII 的字段名。
            scope: field=精确字段名；prefix=字段名前缀（首词 + 下划线）。
            reason: 误报原因（审计留痕）。
            actor_id: 操作者用户 ID。

        Returns:
            处理结果（豁免词、重算前后敏感级、剩余 PII 字段）。

        Raises:
            NotFoundError: 实体不存在。
            ValidationError: 参数非法（空字段/非法 scope）。
        """
        from sqlalchemy import select

        from app.models.data_source import DBCatalog
        from app.models.system_dict import SystemDict
        from app.services.collector.classifier import SensitivityClassifier
        from app.services.collector.rules import load_pii_rules, load_pii_vocab

        col = column.strip()
        if not col:
            raise ValidationError("column 不能为空")
        if scope not in ("field", "prefix"):
            raise ValidationError("scope 必须是 field 或 prefix")
        if not reason.strip():
            raise ValidationError("reason 不能为空")
        cat = (
            await self._db.execute(
                select(DBCatalog).where(
                    DBCatalog.id == catalog_id,
                    DBCatalog.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if cat is None:
            raise NotFoundError(f"采集目录实体不存在: {catalog_id}")

        # 豁免词：精确字段名 或 首词前缀（village_phone → village_）
        if scope == "field":
            exempt_entry = col
            dict_code = "exempt_field"
        else:
            head = col.split("_", 1)[0].strip("_")
            exempt_entry = f"{head}_" if head else col
            dict_code = "exempt_prefix"

        # 幂等 upsert 豁免词表（同 code 多词逗号连接，去重排序）
        existing = (
            await self._db.execute(
                select(SystemDict).where(
                    SystemDict.dict_type == "pii_vocab",
                    SystemDict.code == dict_code,
                    SystemDict.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        words = set()
        if existing and (existing.description or "").strip():
            words.update(
                p.strip()
                for p in str(existing.description).replace("\n", ",").split(",")
                if p.strip()
            )
        words.add(exempt_entry)
        merged = ",".join(sorted(words))
        if existing is None:
            self._db.add(
                SystemDict(
                    dict_type="pii_vocab",
                    code=dict_code,
                    label="误报豁免字段（精确）" if dict_code == "exempt_field" else "误报豁免前缀",
                    sort_order=0,
                    status="active",
                    description=merged,
                )
            )
        else:
            existing.description = merged

        # 用含豁免词表的分类器重算该实体
        pii_rules, conf_rules = await load_pii_rules(self._db)
        vocab = await load_pii_vocab(self._db)
        classifier = SensitivityClassifier(
            rules=pii_rules, confidential_rules=conf_rules, vocab=vocab
        )
        before = str(cat.sensitivity_level)
        hits = policy.detect_pii_columns(cat.schema_json or {}, classifier=classifier)
        # 误报是「人工确认」的处置：不走 infer_sensitivity 的只升不降保护
        # （current=PII 时无命中也保持 PII），允许按豁免后真实命中降级。
        after = policy.infer_sensitivity(hits, current=None)
        pii_cols = [
            {
                "column": h.column,
                "rule": h.rule,
                "confidence": h.confidence,
                "matched_by": h.matched_by,
            }
            for h in hits
        ]
        await self._repo.upsert_classification(
            catalog_id=cat.id,
            level=after,
            pii_columns=pii_cols,
            classified_by="false_positive_feedback",
            model_version=policy.RULES_VERSION,
        )
        if after.value != before:
            await self._repo.update_catalog_sensitivity(cat.id, after)
            await self._safe_publish(
                {
                    "event_type": "classification.changed",
                    "catalog_id": cat.id,
                    "entity_name": cat.entity_name,
                    "before": before,
                    "after": after.value,
                    "reason": "false_positive_feedback",
                }
            )
        # 审计：误报豁免（PII 处置留痕）
        await write_audit(
            self._db,
            actor_id=actor_id,
            action="classification.false_positive",
            entity_type="catalog",
            entity_id=str(cat.id),
            detail={
                "entity_name": cat.entity_name,
                "column": col,
                "scope": scope,
                "exempted_as": exempt_entry,
                "sensitivity_before": before,
                "sensitivity_after": after.value,
                "reason": reason,
            },
            pii_access=True,
        )
        return ClassificationFalsePositiveResult(
            catalog_id=cat.id,
            entity_name=cat.entity_name,
            column=col,
            scope=scope,
            exempted_as=exempt_entry,
            sensitivity_before=before,
            sensitivity_after=after.value,
            remaining_pii_columns=[h.column for h in hits],
        )

    # ---------------------------------------------------------- right to erasure

    async def execute_erasure(
        self, subject_user_id: int, operator_id: int, reason: str | None = None
    ) -> ErasureRequest:
        """执行被遗忘权（R7-09③）：覆写脱敏命中主体的审计行 PII。

        WORM 约束下审计行**物理删除被禁止**，本方法以覆写实现去标识化：

        - 将 ``actor_id == subject_user_id`` 的审计行 ``ip`` 置为脱敏令牌；
        - 将 ``detail_json`` 中出现的主体标识（user id / 邮箱 / IPv4）替换为令牌；
        - 写入一条 ``action=PII_ANONYMIZED`` 审计留存（操作本身可追溯）；
        - 落一条 ``erasure_request`` 台账。

        事务由调用方（API）在方法返回后 ``commit``；审计与台账同会话、同事务。
        """
        token = "ANONYMIZED_" + hashlib.sha256(str(subject_user_id).encode()).hexdigest()[:16]

        rows = (
            (await self._db.execute(select(AuditLog).where(AuditLog.actor_id == subject_user_id)))
            .scalars()
            .all()
        )

        affected = 0
        for row in rows:
            row.ip = token
            if row.detail_json:
                row.detail_json = _scrub_pii(row.detail_json, subject_user_id, token)
            affected += 1

        erasure = ErasureRequest(
            subject_user_id=subject_user_id,
            requested_by=operator_id,
            status=ErasureStatus.COMPLETED,
            token=token,
            affected_rows=affected,
            reason=reason,
        )
        self._db.add(erasure)
        await write_audit(
            self._db,
            actor_id=operator_id,
            action="erasure.anonymize",
            entity_type="erasure_request",
            entity_id=str(subject_user_id),
            detail={"token_prefix": token[:12], "affected_rows": affected},
            trace_id="",
        )
        return erasure

    # ------------------------------------------------------ PII 血缘传播 (US13)

    async def propagate_pii_to_metric(
        self,
        metric_code: str,
        *,
        upstream_source_columns: list[dict[str, Any]] | None = None,
    ) -> bool:
        """PII 血缘传播：检查上游字段 PII 标记，自动传播到下游指标。

        当任一上游 source_column 含 pii=True 时：
        1. 自动设置 metric.definition_json.pii=True
        2. 设置 metric.pii_flag=True
        3. 标记关联 lineage_edge.pii_inherited=True

        Args:
            metric_code: 目标指标编码。
            upstream_source_columns: 上游字段列表，每个元素含 pii 标记。
                格式: [{"column": "phone", "pii": True}, ...]

        Returns:
            是否触发了 PII 传播。
        """
        from app.models.lineage import LineageEdge

        metric = await self._repo.get_metric_by_code(metric_code)
        if metric is None:
            raise NotFoundError("指标不存在", ctx={"metric_code": metric_code})

        # 检查上游是否含 PII：优先显式传入的 source_column 标记，
        # 其次检查通过 lineage_edge 指向本指标的上游节点是否带 PII。
        has_upstream_pii = False
        if upstream_source_columns:
            for col in upstream_source_columns:
                if col.get("pii", False):
                    has_upstream_pii = True
                    break

        if not has_upstream_pii:
            # 也检查通过 lineage_edge 传入的 PII 继承
            stmt = select(LineageEdge).where(
                LineageEdge.target_node == metric_code,
                LineageEdge.deleted_at.is_(None),
            )
            edges = (await self._db.execute(stmt)).scalars().all()
            for _edge in edges:
                # 检查 source_node 对应的 catalog 是否有 PII
                upstream_metric = await self._repo.get_metric_by_code(_edge.source_node)
                if upstream_metric and upstream_metric.pii_flag:
                    has_upstream_pii = True
                    break

        if not has_upstream_pii:
            return False

        # 传播 PII 标记
        changed = False
        if not metric.pii_flag:
            metric.pii_flag = True
            changed = True

        # 更新 definition_json 中的 pii 标记
        definition = dict(metric.definition_json or {})
        if not definition.get("pii"):
            definition["pii"] = True
            metric.definition_json = definition
            changed = True

        # 标记 lineage_edge.pii_inherited=True（列已存在于 0017 迁移，R10-04/0019）
        stmt = select(LineageEdge).where(
            LineageEdge.target_node == metric_code,
            LineageEdge.deleted_at.is_(None),
        )
        edges = (await self._db.execute(stmt)).scalars().all()
        for _edge in edges:
            if not _edge.pii_inherited:
                _edge.pii_inherited = True
                changed = True

        if changed:
            await self._safe_publish(
                {
                    "event_type": "pii.propagated",
                    "metric_code": metric_code,
                    "propagation_source": "upstream_lineage",
                }
            )
            logger.info("pii_propagated", metric_code=metric_code)

        return changed


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _scrub_text(value: str, subject_user_id: int, token: str) -> str:
    """对单个字符串值做标识替换（主体 id / 邮箱 / IPv4）。"""
    text = value.replace(str(subject_user_id), token)
    text = _EMAIL_RE.sub(token, text)
    text = _IPV4_RE.sub(token, text)
    return text


def _scrub_pii(detail: Any, subject_user_id: int, token: str) -> Any:
    """递归抹除 detail_json 中的个人标识（主体 user id / 邮箱 / IPv4）。

    在已解析的 JSON 结构上遍历替换，**避免对数值型 user id 做裸字符串替换**
    破坏 JSON 结构（如 ``{"uid": 42}`` → ``{"uid": "<token>"}``）。

    返回去标识化后的对象；结构保持 JSON 合法（token 仅含字母数字与下划线）。
    """
    if detail is None:
        return None
    if isinstance(detail, dict):
        return {k: _scrub_pii(v, subject_user_id, token) for k, v in detail.items()}
    if isinstance(detail, list):
        return [_scrub_pii(v, subject_user_id, token) for v in detail]
    if isinstance(detail, bool):
        return detail
    if isinstance(detail, int):
        # 数值型主体 id 直接命中 → 以 token 字符串替换（保持 JSON 合法）
        return token if detail == subject_user_id else detail
    if isinstance(detail, float):
        return detail
    if isinstance(detail, str):
        return _scrub_text(detail, subject_user_id, token)
    # 其它类型（datetime 等）原样保留
    return detail


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# 字段级脱敏函数名（用于 PII 二次校验时剔除已脱敏引用）
_MASKING_FNS = (
    "hash",
    "sha256",
    "sha1",
    "md5",
    "mask",
    "substr",
    "substring",
    "regexp_replace",
    "left",
    "right",
    "encrypt",
    "desensitize",
    "aes_encrypt",
)


def _strip_masking_calls(text: str) -> str:
    """剔除脱敏函数调用 ``fn(...)``，返回剩余文本用于明文暴露判定。

    仅处理非嵌套的简单调用；嵌套脱敏场景由治理复核人工兜底。
    """
    cleaned = text
    for fn in _MASKING_FNS:
        cleaned = re.sub(rf"\b{fn}\s*\([^()]*\)", " ", cleaned, flags=re.IGNORECASE)
    return cleaned


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    """取更晚的到期时间；任一为 ``None``（永久）则结果为 ``None``。"""
    if a is None or b is None:
        return None
    return max(_as_utc(a), _as_utc(b))


__all__ = ["EXPIRING_WINDOW_DAYS", "GovernanceService", "RoleName"]
