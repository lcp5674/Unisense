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
    user_domain: str | None,
) -> list[ColumnElement[bool]]:
    """构造 P0-3 读路径行级隔离条件（返回空列表 = 不加过滤，全量可见）。

    - 管理角色（platform_admin/domain_admin）或未提供调用者上下文 → 空列表；
    - 其余角色：公开状态（PUBLISHED/EXPERIMENTAL/DEPRECATED）+ 本人 Owner/副 Owner
      （DRAFT/REVIEW 私有工作区）+ reviewer 额外放行被指派的 REVIEW 待审项
      （reviewer_type=user 指定 reviewer_id / reviewer_type=domain 同域评审组，
      未指派由域管理员兜底，reviewer 不可见）。
    """
    if actor_id is None or role is None or role in ("platform_admin", "domain_admin"):
        return []
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
                        Metric.reviewer_domain == user_domain,
                    ),
                ),
            )
        )
    return [or_(*visibility)]
