"""指标 P0-3 读路径行级隔离可见性条件单测（semantic/visibility.py）。

保障 list_metrics 明细与 assetmap 指标汇总共用同一可见性口径，杜绝
「列表按可见性过滤、汇总按全量计数」泄露他人 DRAFT/REVIEW 私有计数。
权限域为并集（团队继承 ∪ 显式指定，User.domains_all()）。
"""

from __future__ import annotations

from sqlalchemy import ColumnElement
from sqlalchemy.dialects import mysql

from app.api.assetmap import _metric_visibility_scope
from app.services.semantic.visibility import metric_visibility_conditions


def _sql(cond: ColumnElement[bool]) -> str:
    """编译为带内联字面量的 SQL（便于断言状态字面量与列引用）。"""
    return str(cond.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))


def test_platform_admin_gets_no_filter() -> None:
    """platform_admin 不附加可见性过滤（平台级全域治理视角）。"""
    assert metric_visibility_conditions(1, "platform_admin", ["finance"]) == []
    # 缺少调用者上下文同样不过滤
    assert metric_visibility_conditions(None, "metric_owner", ["finance"]) == []


def test_domain_admin_bound_domain_scoped() -> None:
    """domain_admin 绑定域：治理范围收敛为权限域并集 + 本人负责（不再全域可见）。"""
    conds = metric_visibility_conditions(1, "domain_admin", ["finance", "medical_fee"])
    assert len(conds) == 1
    sql = _sql(conds[0])
    assert "domain IN ('finance', 'medical_fee')" in sql
    assert "owner_id" in sql and "backup_owner_id" in sql


def test_domain_admin_unbound_domain_personal() -> None:
    """domain_admin 未绑定域：退化为个人视角（公开 + 本人负责），不再全域可见。"""
    conds = metric_visibility_conditions(1, "domain_admin", None)
    assert len(conds) == 1
    sql = _sql(conds[0])
    assert "PUBLISHED" in sql and "EXPERIMENTAL" in sql and "DEPRECATED" in sql
    assert "owner_id" in sql and "backup_owner_id" in sql
    # 个人视角不含「本域」条件（无域可收敛）
    assert "metric.domain = " not in sql and "domain = '" not in sql


def test_non_admin_owner_visibility() -> None:
    """metric_owner：仅公开状态 + 本人 Owner/副 Owner 可见。"""
    conds = metric_visibility_conditions(11, "metric_owner", ["finance"])
    assert len(conds) == 1
    sql = _sql(conds[0])
    # 公开状态三档
    assert "PUBLISHED" in sql and "EXPERIMENTAL" in sql and "DEPRECATED" in sql
    # 本人 Owner / 副 Owner
    assert "owner_id = :" in sql or "metric.owner_id" in sql
    assert "backup_owner_id" in sql


def test_reviewer_visibility_includes_assigned_review() -> None:
    """reviewer：额外放行被指派的 REVIEW 待审项（user 型指定 / domain 型同域并集）。"""
    conds = metric_visibility_conditions(11, "reviewer", ["finance", "outpatient"])
    assert len(conds) == 1
    sql = _sql(conds[0])
    assert "REVIEW" in sql
    assert "reviewer_type" in sql
    assert "reviewer_id" in sql
    assert "reviewer_domain" in sql


def test_assetmap_visibility_scope_admin_global() -> None:
    """assetmap 汇总作用域：platform_admin 返回空（走全局聚合缓存）。"""
    from unittest.mock import MagicMock

    u = MagicMock()
    u.role = "platform_admin"
    assert _metric_visibility_scope(u) == {}


def test_assetmap_visibility_scope_domain_admin_scoped() -> None:
    """assetmap 汇总作用域：domain_admin 返回作用域（按权限域并集收敛，不再全局）。"""
    from unittest.mock import MagicMock

    u = MagicMock()
    u.role = "domain_admin"
    u.id = 1
    u.domains_all = lambda: ["finance", "medical_fee"]
    assert _metric_visibility_scope(u) == {
        "actor_id": 1,
        "role": "domain_admin",
        "user_domains": ["finance", "medical_fee"],
    }


def test_assetmap_visibility_scope_non_admin_scoped() -> None:
    """assetmap 汇总作用域：非管理角色按 actor/role/domains 收敛。"""
    from unittest.mock import MagicMock

    u = MagicMock()
    u.role = "metric_owner"
    u.id = 11
    u.domains_all = lambda: None
    assert _metric_visibility_scope(u) == {
        "actor_id": 11,
        "role": "metric_owner",
        "user_domains": None,
    }
