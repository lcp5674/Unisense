"""指标读路径行级隔离（P0-3）可见性条件构造（TD §13 治理闭环）。

list_metrics（指标列表）与 assetmap 指标汇总（metric_summary/metric_dimension_summary）
共用同一套可见性条件，防止「列表按可见性过滤、汇总按全量计数」的口径漂移——
资产地图此前对非管理角色泄露他人 DRAFT/REVIEW 私有指标计数即为此类问题。
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, or_

from app.models.metric import Metric


def metric_visibility_conditions(
    actor_id: int | None,
    role: str | None,
    user_domains: list[str] | None,
) -> list[ColumnElement[bool]]:
    """构造 P0-3 读路径行级隔离条件（返回空列表 = 不加过滤，全量可见）。

    - ``platform_admin``（平台级）或未提供调用者上下文 → 空列表（全域治理视角）；
    - ``domain_admin``：按 ``user_domains``（权限域并集：团队继承 ∪ 显式指定）收敛
      治理范围——绑定任一域时本域（全部状态，本域私有 DRAFT/REVIEW 是其治理对象）
      + 本人负责的指标（跨域 Owner/副 Owner）；**无任何权限域时退化为个人视角**
      （公开 + 本人负责），不再全域可见（修复「无域域管理员看到全平台草稿/待审核」）；
    - 其余角色：公开状态（PUBLISHED/EXPERIMENTAL/DEPRECATED）+ 本人 Owner/副 Owner
      （DRAFT/REVIEW 私有工作区）+ reviewer 额外放行被指派的 REVIEW 待审项
      （reviewer_type=user 指定 reviewer_id / reviewer_type=domain 同域评审组，
      未指派由域管理员兜底，reviewer 不可见）。
    """
    if actor_id is None or role is None or role == "platform_admin":
        return []
    if role == "domain_admin":
        if user_domains:
            # 域管理员治理范围 = 权限域（全部状态）+ 本人负责指标（跨域不遗漏）
            return [
                or_(
                    Metric.domain.in_(user_domains),
                    Metric.owner_id == actor_id,
                    Metric.backup_owner_id == actor_id,
                )
            ]
        # 无任何权限域：无治理范围，退化为个人视角
        return _personal_visibility(actor_id, role, user_domains)
    return _personal_visibility(actor_id, role, user_domains)


def _personal_visibility(
    actor_id: int,
    role: str,
    user_domains: list[str] | None,
) -> list[ColumnElement[bool]]:
    """个人视角可见性：公开状态 + 本人 Owner/副 Owner + reviewer 被指派 REVIEW。"""
    visibility: list[ColumnElement[bool]] = [
        Metric.status.in_(("PUBLISHED", "EXPERIMENTAL", "DEPRECATED")),
        Metric.owner_id == actor_id,
        Metric.backup_owner_id == actor_id,
    ]
    if role == "reviewer":
        visibility.append(
            and_(
                Metric.status == "REVIEW",
                or_(
                    and_(
                        Metric.reviewer_type == "user",
                        Metric.reviewer_id == actor_id,
                    ),
                    and_(
                        Metric.reviewer_type == "domain",
                        Metric.reviewer_domain.in_(user_domains or ()),
                    ),
                ),
            )
        )
    return [or_(*visibility)]


def metric_is_visible(
    metric: Metric,
    actor_id: int | None,
    role: str | None,
    user_domains: list[str] | None,
) -> bool:
    """单条指标可见性判定（纯 Python，与 :func:`metric_visibility_conditions` 同源）。

    供单资源端点（详情/挂载详情等）在加载实体后校验当前用户是否可见——
    避免「列表过滤了、详情不校验」的侧门（与列表/汇总口径一致）。
    """
    if actor_id is None or role is None or role == "platform_admin":
        return True
    if role == "domain_admin" and user_domains:
        # 域管理员治理范围 = 权限域 + 本人负责（与列表条件同源）
        return (
            metric.domain in user_domains
            or metric.owner_id == actor_id
            or metric.backup_owner_id == actor_id
        )
    # 无任何权限域：退化为个人视角
    if metric.status in ("PUBLISHED", "EXPERIMENTAL", "DEPRECATED"):
        return True
    if metric.owner_id == actor_id or metric.backup_owner_id == actor_id:
        return True
    if role == "reviewer" and metric.status == "REVIEW":
        if metric.reviewer_type == "user" and metric.reviewer_id == actor_id:
            return True
        if (
            metric.reviewer_type == "domain"
            and metric.reviewer_domain
            and metric.reviewer_domain in (user_domains or [])
        ):
            return True
    return False
