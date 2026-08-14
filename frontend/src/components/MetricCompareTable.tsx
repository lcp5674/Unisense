import { Table } from "antd";
import type { MetricCompareDeps, MetricCompareField, MetricCompareResult } from "../types";
import { DEF_FIELD_LABEL, DefinitionView, ObjectView } from "../utils/display";

export const COMPARE_FIELD_LABELS: Record<string, string> = {
  granularity: "粒度",
  unit: "单位",
  currency: "币种",
  aggregation: "聚合",
  time_semantics: "时间语义",
  additivity: "可加性",
  dw_layer: "数仓分层",
  metric_tier: "分级",
  serving_mode: "服务模式",
  freshness: "新鲜度",
  definition: "口径定义",
  dependencies: "依赖指标",
};

// 差异等级 → 视觉：行底色 + 圆点 pill。色值用「仪表盘调色板」以契合设计语言。
const DIFF_META: Record<string, { bg: string; pillBg: string; pillFg: string; label: string }> = {
  identical: { bg: "transparent", pillBg: "rgba(46,158,91,0.10)", pillFg: "#2e9e5b", label: "一致" },
  similar: { bg: "rgba(232,134,45,0.06)", pillBg: "rgba(232,134,45,0.14)", pillFg: "#c77700", label: "相似" },
  different: { bg: "rgba(214,69,69,0.05)", pillBg: "rgba(214,69,69,0.12)", pillFg: "#d64545", label: "不同" },
};

function renderValue(v: unknown) {
  if (v == null) return <span className="muted">—</span>;
  if (typeof v === "object") {
    // 口径定义/依赖对象：用 DEF_FIELD_LABEL 做字段名中文化，避免技术 key 直出
    return <ObjectView data={v as Record<string, unknown>} depth={1} labels={DEF_FIELD_LABEL} />;
  }
  return <span className="mono">{String(v)}</span>;
}

/** 差异等级 pill：圆点 + 文字，密度比 antd 默认 Tag 更紧凑、更具信号灯气质 */
function DiffPill({ level }: { level: keyof typeof DIFF_META }) {
  const m = DIFF_META[level] ?? DIFF_META.identical;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "2px 8px",
        borderRadius: 999,
        background: m.pillBg,
        color: m.pillFg,
        fontSize: 12,
        fontWeight: 600,
        lineHeight: 1.6,
        whiteSpace: "nowrap",
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: "50%",
          background: m.pillFg,
          display: "inline-block",
        }}
      />
      {m.label}
    </span>
  );
}

/** 顶部汇总条：共 N 项 · 一致 X · 相似 Y · 不同 Z */
function DiffSummary({ counts }: { counts: Record<string, number> }) {
  const total = (counts.identical ?? 0) + (counts.similar ?? 0) + (counts.different ?? 0);
  const items: { key: string; label: string; n: number }[] = [
    { key: "identical", label: "一致", n: counts.identical ?? 0 },
    { key: "similar", label: "相似", n: counts.similar ?? 0 },
    { key: "different", label: "不同", n: counts.different ?? 0 },
  ];
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "8px 12px",
        background: "var(--paper)",
        border: "1px solid var(--line-soft)",
        borderRadius: 8,
        marginBottom: 10,
        fontSize: 13,
      }}
    >
      <span style={{ color: "var(--muted)" }}>共 {total} 项</span>
      <span style={{ color: "var(--line)" }}>·</span>
      {items.map((it) => {
        const m = DIFF_META[it.key];
        return (
          <span key={it.key} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: m.pillFg,
                display: "inline-block",
              }}
            />
            <span style={{ color: "var(--text-secondary)" }}>{it.label}</span>
            <span className="mono" style={{ fontWeight: 600 }}>{it.n}</span>
          </span>
        );
      })}
    </div>
  );
}

/**
 * 两指标关键字段并排对比表（TD §12.4 协商面板 / §7.6 对比页共用）。
 * 输入 compareMetrics 的结果，纯展示，无副作用，便于在 Modal/页面复用。
 */
export function MetricCompareTable({
  result,
  codeA,
  codeB,
  size = "middle",
  showSummary = true,
}: {
  result: MetricCompareResult;
  codeA: string;
  codeB: string;
  size?: "middle" | "small";
  showSummary?: boolean;
}) {
  const counts: Record<string, number> = { identical: 0, similar: 0, different: 0 };
  const rows = Object.entries(result.fields).map(([key, field]) => {
    if (!field) {
      counts.identical += 1;
      return { key, label: key, a: undefined, b: undefined, level: "identical" as const };
    }
    if ("difference_level" in field && "a" in field && "b" in field && !("only_a" in field)) {
      const f = field as MetricCompareField;
      counts[f.difference_level] = (counts[f.difference_level] ?? 0) + 1;
      return { key, label: key, a: f.a, b: f.b, level: f.difference_level };
    }
    const d = field as MetricCompareDeps;
    counts[d.difference_level] = (counts[d.difference_level] ?? 0) + 1;
    return {
      key,
      label: key,
      a: { 交集: d.intersection, 仅A: d.only_a },
      b: { 交集: d.intersection, 仅B: d.only_b },
      level: d.difference_level,
    };
  });

  const columns = [
    {
      title: "字段",
      dataIndex: "label",
      key: "label",
      width: 200,
      render: (v: string) => (
        <strong style={{ color: "var(--ink)" }}>{COMPARE_FIELD_LABELS[v] ?? v}</strong>
      ),
    },
    {
      title: codeA || "指标 A",
      dataIndex: "a",
      key: "a",
      width: 330,
      render: (v: unknown, r: { key: string }) => (
        <div style={{ padding: "2px 0" }}>
          {r.key === "definition" && v && typeof v === "object" ? (
            <DefinitionView data={v as Record<string, unknown>} />
          ) : (
            renderValue(v)
          )}
        </div>
      ),
    },
    {
      title: "差异",
      dataIndex: "level",
      key: "level",
      width: 100,
      render: (v: keyof typeof DIFF_META) => <DiffPill level={v} />,
    },
    {
      title: codeB || "指标 B",
      dataIndex: "b",
      key: "b",
      width: 330,
      render: (v: unknown, r: { key: string }) => (
        <div style={{ padding: "2px 0" }}>
          {r.key === "definition" && v && typeof v === "object" ? (
            <DefinitionView data={v as Record<string, unknown>} />
          ) : (
            renderValue(v)
          )}
        </div>
      ),
    },
  ];

  return (
    <div>
      {showSummary && <DiffSummary counts={counts} />}
      <Table
        dataSource={rows}
        columns={columns}
        rowKey="key"
        size={size}
        pagination={false}
        bordered
        tableLayout="fixed"
        rowClassName={(r) => `compare-row compare-row-${r.level}`}
        style={{
          background: "var(--surface)",
          borderRadius: 8,
          overflow: "hidden",
        }}
      />
    </div>
  );
}
