import { useEffect, useRef, useState } from "react";
import { Select, Tag } from "antd";
import { lineageNodes } from "../../api";
import type { LineageNode } from "../../types";

/** 血缘候选节点类型标签（对齐 LineageView 的 EDGE_NODE_TYPE_TAG 语义）。 */
const NODE_TYPE_TAG: Record<string, { color: string; label: string }> = {
  metric: { color: "purple", label: "指标" },
  table: { color: "blue", label: "表" },
  field: { color: "cyan", label: "字段" },
  column: { color: "cyan", label: "字段" },
  dimension: { color: "geekblue", label: "维度" },
  consumer: { color: "green", label: "消费方" },
  external: { color: "default", label: "外部" },
  other: { color: "default", label: "节点" },
};

/** 无关键词预加载的候选缓存：治理中心多个选择器共享同一份 top-N，避免重复请求。 */
let preloadCache: LineageNode[] | null = null;

interface LineageNodePickerProps {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  width?: number | string;
  allowClear?: boolean;
  /** 输入关键词后无完全匹配时，是否提供「使用输入值」兜底（默认 true，可自由指定节点）。 */
  allowCustomInput?: boolean;
  /** 按回车时回调（如直接触发查询）。 */
  onPressEnter?: () => void;
  /** 聚焦是否展开下拉（默认 true）。 */
  openOnFocus?: boolean;
}

/**
 * 血缘节点可搜索选择器（治理中心/影响分析共用）：
 * 无关键词时加载 top-N 预加载候选，输入后 300ms 防抖远程检索；
 * 选中返回带前缀的规范节点 id（metric:/table:/field:），用户无需感知技术前缀。
 */
export function LineageNodePicker({
  value,
  onChange,
  placeholder = "选择或搜索节点（表 / 指标 / 字段）",
  width = 300,
  allowClear = true,
  allowCustomInput = true,
  onPressEnter,
  openOnFocus = true,
}: LineageNodePickerProps) {
  const [options, setOptions] = useState<LineageNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchWord, setSearchWord] = useState("");
  const [open, setOpen] = useState(false);
  const timer = useRef<number | null>(null);

  /** 加载候选：无关键词走共享缓存（预加载），有关键词直查后端。 */
  async function load(kw?: string) {
    if (!kw && preloadCache) {
      setOptions(preloadCache);
      return;
    }
    setLoading(true);
    try {
      const list = await lineageNodes(kw || undefined, 50);
      setOptions(list);
      if (!kw) preloadCache = list;
    } catch {
      // 候选加载失败不阻断：仍可手动输入/使用自定义值
    } finally {
      setLoading(false);
    }
  }

  /** 进入组件即预加载候选（首次挂载一次）。 */
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function loadDebounced(kw?: string) {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => void load(kw), 300);
  }

  useEffect(() => {
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, []);

  const hasExact = options.some((n) => n.id === searchWord.trim());
  const customOption: LineageNode[] =
    allowCustomInput && searchWord.trim() && !hasExact
      ? [{ id: searchWord.trim(), label: `使用「${searchWord.trim()}」`, count: 0, type: "other" }]
      : [];

  return (
    <Select
      showSearch
      allowClear={allowClear}
      loading={loading}
      value={value || undefined}
      placeholder={placeholder}
      style={{ width }}
      className="mono"
      open={open}
      onDropdownVisibleChange={(o) => setOpen(o)}
      onFocus={() => {
        // 聚焦即展开已有候选（空关键词预加载已就绪，像选项框一样可直接点选）
        if (openOnFocus) setOpen(true);
        if (!options.length) void load();
      }}
      filterOption={(input, opt) => {
        const raw = String(opt?.value ?? "").toLowerCase();
        const label = String(opt?.label ?? "").toLowerCase();
        return raw.includes(input.toLowerCase()) || label.includes(input.toLowerCase());
      }}
      onSearch={(v) => {
        setSearchWord(v);
        loadDebounced(v);
      }}
      onChange={(v) => onChange(v ?? "")}
      onKeyDown={(e) => {
        if (e.key === "Enter" && onPressEnter) onPressEnter();
      }}
      options={[...options, ...customOption].map((n) => ({
        value: n.id,
        label: (
          <span>
            <Tag style={{ marginRight: 6 }} color={NODE_TYPE_TAG[n.type]?.color}>
              {NODE_TYPE_TAG[n.type]?.label ?? n.type}
            </Tag>
            <span>{n.label}</span>
            <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
              {n.count} 边
            </span>
          </span>
        ),
      }))}
    />
  );
}
