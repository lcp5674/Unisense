"""指标 P0-3 读路径行级隔离可见性条件单测（semantic/visibility.py）。

保障 list_metrics 明细与 assetmap 指标汇总共用同一可见性口径，杜绝
「列表按可见性过滤、汇总按全量计数」泄露他人 DRAFT/REVIEW 私有计数。
"""

from __future__ import annotations

from sqlalchemy import ColumnElement
from sqlalchemy.dialects import mysql

from app.api.assetmap import _metric_visibility_scope
from app.services.semantic.visibility import metric_visibility_conditions


def _sql(cond: ColumnElement[bool]) -> str:
    """编译为带内联字面量的 SQL（便于断言状态字面量与列引用）。"""
    return str(cond.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))


def test_admin_roles_get_no_filter() -> None:
    """管理角色（platform_admin/domain_admin）不附加可见性过滤（全量治理视角）。"""
    for role in ("platform_admin", "domain_admin"):
        assert metric_visibility_conditions(1, role, "finance") == []
    # 缺少调用者上下文同样不过滤
    assert metric_visibility_conditions(None, "metric_owner", "finance") == []


def test_non_admin_owner_visibility() -> None:
    """metric_owner：仅公开状态 + 本人 Owner/副 Owner 可见。"""
    conds = metric_visibility_conditions(11, "metric_owner", "finance")
    assert len(conds) == 1
    sql = _sql(conds[0])
    # 公开状态三档
    assert "PUBLISHED" in sql and "EXPERIMENTAL" in sql and "DEPRECATED" in sql
    # 本人 Owner / 副 Owner
    assert "owner_id = :" in sql or "metric.owner_id" in sql
    assert "backup_owner_id" in sql


def test_reviewer_visibility_includes_assigned_review() -> None:
    """reviewer：额外放行被指派的 REVIEW 待审项（user 型指定 / domain 型同域）。"""
    conds = metric_visibility_conditions(11, "reviewer", "finance")
    assert len(conds) == 1
    sql = _sql(conds[0])
    assert "REVIEW" in sql
    assert "reviewer_type" in sql
    assert "reviewer_id" in sql
    assert "reviewer_domain" in sql


def test_assetmap_visibility_scope_admin_global() -> None:
    """assetmap 汇总作用域：管理角色返回空（走全局聚合缓存）。"""
    from unittest.mock import MagicMock

    for role in ("platform_admin", "domain_admin"):
        u = MagicMock()
        u.role = role
        assert _metric_visibility_scope(u) == {}


def test_assetmap_visibility_scope_non_admin_scoped() -> None:
    """assetmap 汇总作用域：非管理角色按 actor/role/domain 收敛。"""
    from unittest.mock import MagicMock

    u = MagicMock()
    u.role = "metric_owner"
    u.id = 11
    u.domain = None
    assert _metric_visibility_scope(u) == {
        "actor_id": 11,
        "role": "metric_owner",
        "user_domain": None,
    }
