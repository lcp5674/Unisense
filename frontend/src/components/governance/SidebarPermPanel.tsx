/**
 * 侧边栏入口授权视图（按前端侧边栏菜单形态配置 view 权限点）。
 *
 * 渲染 ``NAV_GROUPS`` 菜单树，每个入口映射 ``ROUTE_PERM[path]`` 的 view 权限点——
 * 管理员按「菜单入口是否可见」勾选，等价于勾选/取消对应 ``xxx:view`` 权限点，
 * 比平铺的权限点矩阵更直观（无需记忆「哪个权限点对应哪个菜单」）。
 *
 * 交互：勾选=该角色/用户可见该侧边栏入口（写入 view 权限点）；取消=隐藏。
 * 无独立权限点映射的入口（如 /search、/api-docs、/approval 聚合）显示为只读说明。
 */

import { Checkbox, Space, Tooltip } from "antd";
import { NAV_GROUPS } from "../Layout";
import { ROUTE_PERM } from "../../hooks/usePermission";

/** 聚合入口：需要任一相关权限点才可见（与 Layout 菜单过滤逻辑一致）。 */
const AGGREGATE_ENTRIES: Record<string, string[]> = {
  "/approval": ["metric:review", "master-data:review", "review:view"],
};

export function SidebarPermPanel({
  checked,
  onChange,
  disabled = false,
}: {
  /** 当前已勾选的权限点集合（view 权限点）。 */
  checked: string[];
  /** 勾选变更回调（传入最新权限点集合）。 */
  onChange: (next: string[]) => void;
  disabled?: boolean;
}) {
  const checkedSet = new Set(checked);

  function toggle(action: string, on: boolean) {
    const next = new Set(checkedSet);
    if (on) next.add(action);
    else next.delete(action);
    onChange([...next]);
  }

  return (
    <div>
      {NAV_GROUPS.map((g) => {
        const children = g.children.filter((c) => ROUTE_PERM[c.key] || AGGREGATE_ENTRIES[c.key]);
        if (children.length === 0) return null;
        return (
          <div key={g.label} style={{ marginBottom: 12 }}>
            <div style={{ fontWeight: 600, marginBottom: 6 }}>{g.label}</div>
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
                      onChange={(e) => toggle(perm, e.target.checked)}
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
