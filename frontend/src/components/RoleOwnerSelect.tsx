import { useState } from "react";
import { Select } from "antd";
import type { SelectProps } from "antd";

/** 责任方值：平台用户（id）或外部人员（name）。id 可解析时优先展示平台用户。 */
export interface RoleOwnerValue {
  id?: number | null;
  name?: string | null;
}

interface RoleOwnerSelectProps
  extends Omit<
    SelectProps,
    "value" | "onChange" | "options" | "labelRender" | "onSearch" | "filterOption"
  > {
  /** 当前值（antd Form.Item 受控注入） */
  value?: RoleOwnerValue | null;
  onChange?: (v: RoleOwnerValue | undefined) => void;
  /** 平台用户清单（listUsers() 或精简子集）——仅用 id/username/display_name */
  users: ReadonlyArray<{ id: number; username: string; display_name?: string | null }>;
  placeholder?: string;
}

// 责任方选择器：优先选平台用户，无匹配用户时可自由输入外部人员名称。
// 值内部用 token 字符串（user:{id} / text:{名称}）承载，labelRender 解析展示——
// 避免"外部人员"被显示成原始 token、也避免对象值在 Form 中触发无谓重渲染。
export default function RoleOwnerSelect({
  value,
  onChange,
  users,
  placeholder,
  ...rest
}: RoleOwnerSelectProps) {
  const [search, setSearch] = useState("");

  const currentId = value?.id ?? null;
  const currentName = value?.name || null;
  // 有 id 用 id 解析（平台用户权威）；仅 name 时按文本展示（外部人员兜底）
  const currentToken =
    currentId != null ? `user:${currentId}` : currentName ? `text:${currentName}` : undefined;

  function parseToken(token?: string): RoleOwnerValue | undefined {
    if (token == null || token === "") return undefined;
    if (token.startsWith("user:")) {
      const id = Number(token.slice(5));
      return { id: Number.isFinite(id) ? id : null, name: null };
    }
    if (token.startsWith("text:")) {
      const name = token.slice(5).trim();
      return name ? { id: null, name } : undefined;
    }
    return undefined; // 非法 token 兜底视为空
  }

  const userOptions = users.map((u) => ({
    value: `user:${u.id}`,
    label: `${u.display_name || u.username}（${u.id}）`,
  }));

  // 搜索词非空、且未命中任何平台用户、且与当前外部人员名不同 → 提供"外部人员"自由输入项
  const trimmedSearch = search.trim();
  const searchHitsUser = userOptions.some((o) =>
    String(o.label).toLowerCase().includes(trimmedSearch.toLowerCase()),
  );
  const canCreateText =
    trimmedSearch.length > 0 && !searchHitsUser && trimmedSearch !== currentName;

  return (
    <Select
      {...rest}
      allowClear
      showSearch
      placeholder={placeholder ?? "选择平台用户或输入外部人员名称"}
      value={currentToken}
      options={[
        ...userOptions,
        ...(canCreateText
          ? [{ value: `text:${trimmedSearch}`, label: `外部人员：${trimmedSearch}` }]
          : []),
      ]}
      filterOption={(input, option) =>
        String(option?.label ?? "").toLowerCase().includes(input.toLowerCase())
      }
      onSearch={(v) => setSearch(v)}
      onChange={(v) => onChange?.(parseToken(v as string | undefined))}
      labelRender={({ value: token }) => {
        const t = String(token ?? "");
        if (t.startsWith("user:")) {
          const id = Number(t.slice(5));
          const u = users.find((x) => x.id === id);
          return u ? `${u.display_name || u.username}` : "未知用户";
        }
        if (t.startsWith("text:")) return t.slice(5);
        return t;
      }}
    />
  );
}
