/**
 * 侧边栏入口授权视图（按前端侧边栏菜单形态配置 view 权限点）。
 *
 * 渲染 ``NAV_GROUPS`` 菜单树，每个入口映射 ``ROUTE_PERM[path]`` 的 view 权限点——
 * 管理员按「菜单入口是否可见」勾选，等价于勾选/取消对应 ``xxx:view`` 权限点，
 * 比平铺的权限点矩阵更直观（无需记忆「哪个权限点对应哪个菜单」）。
 *
 * 交互：勾选=该角色/用户可见该侧边栏入口（写入 view 权限点）；取消=隐藏。
 * 无独立权限点映射的入口（如 /search、/api-docs、/approval 聚合）显示为只读说明。
 *
 * 增强（需传 ``registry`` 注册表）：
 * - 取消入口时若该入口所属模块（registry.module）下仍有其它已勾选按钮权限点，
 *   弹确认「是否一并停用」——避免「入口关了但按钮还开着」的困惑；
 * - 模块头「全部收起/展开」——按侧边栏分组一键停用/恢复该分组对应模块的全部权限点。
 */

import { Checkbox, Modal, Space, Tooltip } from "antd";
import type { ActionRegistryItem } from "../../types";
import { NAV_GROUPS } from "../Layout";
import { ROUTE_PERM } from "../../hooks/usePermission";

/** 聚合入口：需要任一相关权限点才可见（与 Layout 菜单过滤逻辑一致）。 */
const AGGREGATE_ENTRIES: Record<string, string[]> = {
  "/approval": ["metric:review", "master-data:review", "review:view"],
};

/** 侧边栏分组内全部入口对应的权限点（view + create/review 等 ROUTE_PERM 值）。 */
function groupEntryActions(g: (typeof NAV_GROUPS)[number]): string[] {
  return g.children
    .map((c) => ROUTE_PERM[c.key])
    .filter((p): p is string => Boolean(p));
}

/** 侧边栏分组对应的 registry 模块集合（通过入口权限点反查 module）。 */
function groupModules(g: (typeof NAV_GROUPS)[number], registry: ActionRegistryItem[]): Set<string> {
  const entryActions = new Set(groupEntryActions(g));
  return new Set(
    registry.filter((r) => entryActions.has(r.action)).map((r) => r.module),
  );
}

/** 侧边栏分组对应的全部权限点（所属 registry 模块并集）。 */
function groupAllActions(g: (typeof NAV_GROUPS)[number], registry: ActionRegistryItem[]): string[] {
  const mods = groupModules(g, registry);
  return registry.filter((r) => mods.has(r.module)).map((r) => r.action);
}

/** 侧边栏入口自身对应的权限点（ROUTE_PERM 值，如 /create→metric:create）。
 *
 * 这类权限点由「入口复选框」独立控制——「一并停用」只应联动纯按钮权限点，
 * 若把入口权限点也纳入候选，会误取消同模块其它侧边栏入口（如取消 /catalogs
 * 却连带取消 /create）。故联动候选须排除全部入口权限点。
 */
const SIDEBAR_GATE_ACTIONS = new Set(Object.values(ROUTE_PERM));

/** 某权限点所属 registry 模块下、除自身外且非侧边栏入口的其它权限点（联动停用候选）。 */
function siblingActions(action: string, registry: ActionRegistryItem[]): string[] {
  const mod = registry.find((r) => r.action === action)?.module;
  if (!mod) return [];
  return registry
    .filter(
      (r) =>
        r.module === mod &&
        r.action !== action &&
        !SIDEBAR_GATE_ACTIONS.has(r.action),
    )
    .map((r) => r.action);
}

export function SidebarPermPanel({
  checked,
  onChange,
  disabled = false,
  registry = [],
}: {
  /** 当前已勾选的权限点集合（view 权限点）。 */
  checked: string[];
  /** 勾选变更回调（传入最新权限点集合）。 */
  onChange: (next: string[]) => void;
  disabled?: boolean;
  /** 权限点注册表（用于模块联动停用/收起；缺省仅做入口显隐，无联动）。 */
  registry?: ActionRegistryItem[];
}) {
  const checkedSet = new Set(checked);

  function apply(next: Set<string>) {
    onChange([...next]);
  }

  /** 勾选/取消单个入口：取消时若同模块有已勾选按钮权限点，弹确认是否一并停用。 */
  function toggle(action: string, on: boolean, label: string) {
    const next = new Set(checkedSet);
    if (on) {
      next.add(action);
      apply(next);
      return;
    }
    next.delete(action);
    const siblings = siblingActions(action, registry).filter((a) => checkedSet.has(a));
    if (siblings.length === 0 || disabled) {
      apply(next);
      return;
    }
    const labels = registry
      .filter((r) => siblings.includes(r.action))
      .slice(0, 3)
      .map((r) => r.label)
      .join("、");
    Modal.confirm({
      title: `取消侧边栏入口「${label}」？`,
      content: `该入口将隐藏。其所在模块下仍有 ${siblings.length} 个按钮权限点已勾选（如 ${labels}…），是否一并停用？`,
      okText: "一并停用",
      cancelText: "仅隐藏入口",
      onOk: () => {
        const removed = new Set(next);
        for (const s of siblings) removed.delete(s);
        apply(removed);
      },
      onCancel: () => apply(next),
    });
  }

  /** 模块级一键收起/展开：收起=移除该分组对应模块的全部权限点，展开=全部恢复勾选。 */
  function toggleGroup(g: (typeof NAV_GROUPS)[number]) {
    const all = groupAllActions(g, registry);
    if (all.length === 0) return;
    const allOn = all.every((a) => checkedSet.has(a));
    const next = new Set(checkedSet);
    if (allOn) {
      for (const a of all) next.delete(a);
    } else {
      for (const a of all) next.add(a);
    }
    apply(next);
  }

  return (
    <div>
      {NAV_GROUPS.map((g) => {
        const children = g.children.filter((c) => ROUTE_PERM[c.key] || AGGREGATE_ENTRIES[c.key]);
        if (children.length === 0) return null;
        const groupAll = groupAllActions(g, registry);
        const allOn = groupAll.length > 0 && groupAll.every((a) => checkedSet.has(a));
        return (
          <div key={g.label} style={{ marginBottom: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <span style={{ fontWeight: 600 }}>{g.label}</span>
              {groupAll.length > 0 && (
                <ButtonLink
                  disabled={disabled}
                  onClick={() => toggleGroup(g)}
                  text={allOn ? "全部收起" : "全部展开"}
                  count={groupAll.length}
                />
              )}
            </div>
            <Space direction="vertical" size={4} style={{ width: "100%" }}>
              {children.map((c) => {
                const perm = ROUTE_PERM[c.key];
                const agg = AGGREGATE_ENTRIES[c.key];
                if (!perm) {
                  // 聚合入口：无单一权限点，展示占位说明（相关权限点在按钮矩阵中配置）
                  return (
                    <div key={c.key} className="muted" style={{ fontSize: 12, padding: "2px 0" }}>
                      <Checkbox disabled>
                        {c.label}
                      </Checkbox>
                      <span style={{ marginLeft: 4 }}>
                        （{agg?.length ?? 0} 个相关权限点，见按钮矩阵）
                      </span>
                    </div>
                  );
                }
                return (
                  <Tooltip key={c.key} title={`${perm}——${checkedSet.has(perm) ? "已勾选，该入口对授权对象可见" : "未勾选，该入口对授权对象隐藏"}`}>
                    <Checkbox
                      checked={checkedSet.has(perm)}
                      disabled={disabled}
                      onChange={(e) => toggle(perm, e.target.checked, c.label)}
                    >
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                        {c.label}
                        <code style={{ fontSize: 11, color: "#999" }}>{perm}</code>
                      </span>
                    </Checkbox>
                  </Tooltip>
                );
              })}
            </Space>
          </div>
        );
      })}
      <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
        当前可见入口 {checked.filter((a) => Object.values(ROUTE_PERM).includes(a)).length} 个
      </div>
    </div>
  );
}

/** 模块头「全部收起/展开」链接按钮（antd Button type="link" 内联写法）。 */
function ButtonLink({
  disabled,
  onClick,
  text,
  count,
}: {
  disabled: boolean;
  onClick: () => void;
  text: string;
  count: number;
}) {
  return (
    <a
      style={{ fontSize: 12, color: "#1677ff", opacity: disabled ? 0.5 : 1, cursor: disabled ? "not-allowed" : "pointer" }}
      onClick={(e) => {
        e.stopPropagation();
        if (!disabled) onClick();
      }}
    >
      {text}（{count} 项）
    </a>
  );
}
