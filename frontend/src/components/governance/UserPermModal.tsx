import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Checkbox,
  Input,
  Modal,
  Space,
  Tabs,
  Tag,
  Tooltip,
  message,
} from "antd";
import { getUserPermissions, listActionRegistry, setUserPermissions, UnisenseApiError } from "../../api";
import type { ActionRegistryItem, UserPermissionResponse } from "../../types";

/** 动作点注册表按模块分组（可视化配置弹窗渲染用）。 */
export function groupRegistry(
  registry: ActionRegistryItem[],
): Array<{ module: string; items: ActionRegistryItem[] }> {
  const byModule = new Map<string, ActionRegistryItem[]>();
  for (const item of registry) {
    const arr = byModule.get(item.module) ?? [];
    arr.push(item);
    byModule.set(item.module, arr);
  }
  return Array.from(byModule.entries())
    .map(([module, moduleItems]) => ({
      module,
      items: moduleItems.sort((a, b) => a.action.localeCompare(b.action)),
    }))
    .sort((a, b) => a.module.localeCompare(b.module));
}

/** 权限点动作类型分类（按钮级配置「先模块后类型」筛选用）。 */
export type UiCategory = "all" | "view" | "write" | "export" | "llm" | "other";

export const UI_CATEGORIES: Array<{ key: UiCategory; label: string }> = [
  { key: "all", label: "全部" },
  { key: "view", label: "查看" },
  { key: "write", label: "写操作" },
  { key: "export", label: "导出" },
  { key: "llm", label: "LLM 推断" },
  { key: "other", label: "其他" },
];

export function categoryOf(action: string): Exclude<UiCategory, "all"> {
  if (action.endsWith(":view")) return "view";
  if (action.includes("export")) return "export";
  if (action.includes("infer") || action.includes("nl2sql")) return "llm";
  const writeRe =
    /(create|edit|delete|deprecate|manage|assign|review|arbitrate|escalate|close|reopen|revoke|config|rescan|collect|test-connection|disable|batch-status|instantiate|reconcile|mapping|publish|reset-password|validate|execute|pii)/;
  if (writeRe.test(action)) return "write";
  return "other";
}

/** 「按用户授权」按钮权限矩阵：角色已含（只读）+ 可直挂叠加（可勾选）。
 *
 * 授权者无需记忆按钮名——可搜索按钮名/权限点、按动作类型筛选，快速定位后勾选；
 * 角色已含项（``role_actions``）为灰色只读（由角色管理配置，直挂不可收窄角色）；
 * 其余按钮为「直挂叠加」区，保存仅写 ``user_permission`` 表（角色继承不受影响）。
 */
export function UserPermModal({
  userId,
  userName,
  open,
  onClose,
}: {
  userId: number;
  userName: string;
  open: boolean;
  onClose: () => void;
}) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [snap, setSnap] = useState<UserPermissionResponse | null>(null);
  const [registry, setRegistry] = useState<ActionRegistryItem[]>([]);
  // 直挂草稿（可勾选集合；角色已含项不可改，仅叠加）
  const [directDraft, setDirectDraft] = useState<Set<string>>(new Set());
  const [uiSearch, setUiSearch] = useState("");
  const [uiCategory, setUiCategory] = useState<UiCategory>("all");

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    Promise.all([getUserPermissions(userId), listActionRegistry()])
      .then(([snapRes, reg]) => {
        setSnap(snapRes);
        setRegistry(reg);
        setDirectDraft(new Set(snapRes.direct_actions));
      })
      .catch((err) => {
        message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
      })
      .finally(() => setLoading(false));
  }, [open, userId]);

  const roleSet = useMemo(() => new Set(snap?.role_actions ?? []), [snap]);
  const grouped = useMemo(() => {
    const kw = uiSearch.trim().toLowerCase();
    return groupRegistry(registry)
      .map((g) => ({
        module: g.module,
        items: g.items.filter((it) => {
          if (uiCategory !== "all" && categoryOf(it.action) !== uiCategory) return false;
          if (
            kw &&
            !it.label.toLowerCase().includes(kw) &&
            !it.action.toLowerCase().includes(kw)
          ) {
            return false;
          }
          return true;
        }),
      }))
      .filter((g) => g.items.length > 0);
  }, [registry, uiSearch, uiCategory]);

  function toggleDirect(action: string, checked: boolean) {
    setDirectDraft((prev) => {
      const next = new Set(prev);
      if (checked) next.add(action);
      else next.delete(action);
      return next;
    });
  }

  async function handleSave() {
    if (!snap) return;
    setSaving(true);
    try {
      await setUserPermissions(snap.user_id, {
        actions: [...directDraft],
        reason: "按用户授权矩阵配置",
      });
      message.success(`已更新「${userName}」的直挂按钮权限`);
      onClose();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title={`按用户授权：${userName}${snap ? `（角色 ${snap.role}）` : ""}`}
      open={open}
      onCancel={onClose}
      onOk={handleSave}
      okText="保存"
      okButtonProps={{ loading: saving }}
      width={840}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="矩阵展示该用户生效的按钮权限点：灰色勾选=角色已含（由角色管理配置，不可在此收窄）；其余为「直挂叠加」区，勾选保存后即时生效（后端写接口仍按内置角色强制兜底）。"
      />
      <Space direction="vertical" style={{ width: "100%", marginBottom: 12 }} size={8}>
        <Input.Search
          placeholder="搜索按钮名称或权限点（如：导出 / audit:export）"
          allowClear
          value={uiSearch}
          onChange={(e) => setUiSearch(e.target.value)}
        />
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <Tabs
            size="small"
            activeKey={uiCategory}
            onChange={(k) => setUiCategory(k as UiCategory)}
            items={UI_CATEGORIES.map((c) => ({ key: c.key, label: c.label }))}
            tabBarStyle={{ marginBottom: 0, flex: 1 }}
          />
          <span className="muted" style={{ whiteSpace: "nowrap", fontSize: 12 }}>
            直挂已选 {directDraft.size} 项
          </span>
        </div>
      </Space>
      <div style={{ maxHeight: 440, overflow: "auto" }}>
        {loading ? (
          <div className="muted" style={{ textAlign: "center", padding: 24 }}>加载中…</div>
        ) : grouped.length === 0 ? (
          <div className="muted" style={{ textAlign: "center", padding: 24 }}>无匹配的按钮权限点</div>
        ) : (
          grouped.map((g) => (
            <div key={g.module} style={{ marginBottom: 14 }}>
              <div style={{ fontWeight: 600, marginBottom: 6 }}>{g.module}</div>
              <Space wrap>
                {g.items.map((it) =>
                  roleSet.has(it.action) ? (
                    <Tooltip key={it.action} title={`${it.description}（${it.action}）`}>
                      <Checkbox checked disabled>
                        {it.label} <Tag style={{ marginLeft: 2 }}>角色</Tag>
                      </Checkbox>
                    </Tooltip>
                  ) : (
                    <Tooltip key={it.action} title={`${it.description}（${it.action}）`}>
                      <Checkbox
                        checked={directDraft.has(it.action)}
                        onChange={(e) => toggleDirect(it.action, e.target.checked)}
                      >
                        {it.label}
                      </Checkbox>
                    </Tooltip>
                  ),
                )}
              </Space>
            </div>
          ))
        )}
      </div>
    </Modal>
  );
}
