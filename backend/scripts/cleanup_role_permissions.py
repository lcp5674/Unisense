"""清理内置角色权限点覆盖（基线错位对齐的数据清理）。

背景
----
「权限基线错位对齐」（09bf5a64）调整了 ``policy.ROLE_UI_ACTIONS`` 默认基线：

- domain_admin **收窄**：移除 ``org:create`` / ``org:edit`` / ``org:disable`` /
  ``dict:create`` / ``sensitive-rules:edit``（后端仅 platform_admin/compliance_officer 可写）；
- metric_owner **收窄**：移除 ``review:close`` / ``review:reopen``（冲突当事方不授予裁决动作）；
- compliance_officer **新增**：``review:close`` / ``review:reopen``。

但 ``role_permission`` 表（RBAC 覆盖）的语义是「整表替换」：某内置角色只要在角色管理
保存过一次，其生效权限点就是「当时的快照」而非当前基线——基线收窄后，越权项
（如 domain_admin 的 ``org:create``）仍残留，导致「角色管理里已去掉、前端按钮仍显示 /
点了 403 摆设」。而基线新增项（如 compliance_officer 的 ``review:close``）若快照缺失，
该角色生效集合也缺它（覆盖不合并基线）。

本脚本把**内置角色**（``RoleName`` 枚举）的 ``role_permission`` 覆盖收敛为「当前基线
全集」：

- 删除：不在 ``ROLE_UI_ACTIONS[role]`` / ``ROLE_ACTIONS[role]`` 的越权/孤儿行；
- 补插：当前基线有、但覆盖快照缺失的权限点。

不触碰：
- 自定义角色（``Role.is_custom``）：其权限完全来自覆盖，无基线可收敛；
- platform_admin（受保护角色）：不应有覆盖行，历史残留一律清除（基线即全集，行为不变）。

用法
----
.. code-block:: bash

    python -m scripts.cleanup_role_permissions                     # dry-run（只报告）
    python -m scripts.cleanup_role_permissions --apply             # 写库
    python -m scripts.cleanup_role_permissions --role domain_admin # 限单角色（dry-run）
    python -m scripts.cleanup_role_permissions --role domain_admin --apply

注：写库后生效存在 60s 进程内缓存 TTL（``governance.cache``），或重启 backend 立即生效。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

# 将 backend/ 加入 sys.path，确保 CLI 直接执行时也能 import app
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from sqlalchemy import delete, select  # noqa: E402

from app.db.mysql import async_session_factory, engine  # noqa: E402
from app.models.governance import RoleName, RolePermission  # noqa: E402
from app.services.governance import policy  # noqa: E402


def _baseline_for(role: str) -> tuple[set[str], set[str]]:
    """当前基线全集（UI 权限点 + 资源级动作）。"""
    return (
        set(policy.ROLE_UI_ACTIONS.get(role, frozenset())),
        set(policy.ROLE_ACTIONS.get(role, frozenset())),
    )


async def _clean_role(session: Any, role: str, apply: bool) -> dict[str, Any] | None:
    """收敛单个角色的覆盖；无覆盖行返回 None。"""
    rows = list(
        (
            await session.execute(
                select(RolePermission).where(RolePermission.role == role)
            )
        ).scalars().all()
    )
    if not rows:
        return None
    existing = {r.action for r in rows}

    # 受保护角色（platform_admin）：覆盖语义对其不生效，历史残留一律清除
    if role in policy.PROTECTED_ROLES:
        if apply:
            await session.execute(
                delete(RolePermission).where(RolePermission.role == role)
            )
        return {
            "role": role,
            "rows": len(rows),
            "delete": sorted(existing),
            "insert": [],
            "note": "受保护角色覆盖已清除（基线即全集）",
        }

    baseline_ui, baseline_res = _baseline_for(role)
    ui_existing = {a for a in existing if policy.is_ui_action(a)}
    res_existing = existing - ui_existing

    to_delete: set[str] = set()
    to_insert: set[str] = set()
    # 仅当该角色存在对应类别的覆盖行时才收敛——未覆盖类别保持走基线（不新增显式行）
    if ui_existing:
        to_delete |= ui_existing - baseline_ui
        to_insert |= baseline_ui - ui_existing
    if res_existing:
        to_delete |= res_existing - baseline_res
        to_insert |= baseline_res - res_existing

    if not to_delete and not to_insert:
        return {"role": role, "rows": len(rows), "delete": [], "insert": [], "note": "已与基线一致"}

    if apply:
        if to_delete:
            await session.execute(
                delete(RolePermission).where(
                    RolePermission.role == role,
                    RolePermission.action.in_(sorted(to_delete)),
                )
            )
        for action in sorted(to_insert):
            session.add(RolePermission(role=role, action=action))
    return {
        "role": role,
        "rows": len(rows),
        "delete": sorted(to_delete),
        "insert": sorted(to_insert),
    }


async def run(apply: bool, role_filter: str | None) -> int:
    async with async_session_factory() as session:
        builtin = [r.value for r in RoleName]
        if role_filter:
            if role_filter not in builtin:
                print(f"[WARN] {role_filter} 非内置角色（RoleName），按约定不处理自定义角色，退出")
                return 2
            roles = [role_filter]
        else:
            roles = builtin

        reports: list[dict[str, Any]] = []
        for role in roles:
            report = await _clean_role(session, role, apply)
            if report:
                reports.append(report)
        if apply:
            await session.commit()

        total_delete = sum(len(r["delete"]) for r in reports)
        total_insert = sum(len(r["insert"]) for r in reports)
        print(f"role_permission 内置角色收敛报告（apply={apply}）：")
        print(f"  涉及角色 {len(reports)} 个 / 删除 {total_delete} 项 / 补插 {total_insert} 项")
        for r in reports:
            print(f"  [{r['role']}] 原有 {r['rows']} 行"
                  f" -> 删除 {len(r['delete'])}：{', '.join(r['delete']) or '-'}"
                  f" | 补插 {len(r['insert'])}：{', '.join(r['insert']) or '-'}"
                  + (f"（{r['note']}）" if r.get("note") else ""))
        if not apply:
            print("\n[dry-run] 未写库；加 --apply 执行。"
                  "写库后 60s 缓存 TTL 内生效，或重启 backend。")
    await engine.dispose()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="清理内置角色权限点覆盖（收敛为当前基线全集）")
    parser.add_argument("--apply", action="store_true", help="真正写库（缺省 dry-run）")
    parser.add_argument("--role", default=None, help="仅处理指定内置角色（缺省全部内置角色）")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.apply, args.role)))


if __name__ == "__main__":
    main()
