import { Table, Tag } from "antd";
import type { MetricCompareDeps, MetricCompareField, MetricCompareResult } from "../types";
import { ObjectView } from "../utils/display";

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

const DIFF_META: Record<string, { color: string; label: string }> = {
  identical: { color: "green", label: "一致" },
  similar: { color: "blue", label: "相似" },
  different: { color: "orange", label: "不同" },
};

function renderValue(v: unknown) {
  if (v == null) return <span className="muted">—</span>;
  if (typeof v === "object") {
    return <ObjectView data={v as Record<string, unknown>} depth={1} />;
  }
  return <span className="mono">{String(v)}</span>;
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
}: {
  result: MetricCompareResult;
  codeA: string;
  codeB: string;
  size?: "middle" | "small";
}) {
  const rows = Object.entries(result.fields).map(([key, field]) => {
    if (!field) return { key, label: key, a: undefined, b: undefined, level: "identical" as const };
    if ("difference_level" in field && "a" in field && "b" in field && !("only_a" in field)) {
      const f = field as MetricCompareField;
      return { key, label: key, a: f.a, b: f.b, level: f.difference_level };
    }
    const d = field as MetricCompareDeps;
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
      width: 120,
      render: (v: string) => <strong>{COMPARE_FIELD_LABELS[v] ?? v}</strong>,
    },
    { title: codeA || "指标 A", dataIndex: "a", key: "a", render: (v: unknown) => renderValue(v) },
    {
      title: "差异",
      dataIndex: "level",
      key: "level",
      width: 80,
      render: (v: keyof typeof DIFF_META) => (
        <Tag color={DIFF_META[v]?.color}>{DIFF_META[v]?.label ?? v}</Tag>
      ),
    },
    { title: codeB || "指标 B", dataIndex: "b", key: "b", render: (v: unknown) => renderValue(v) },
  ];

  return (
    <Table
      dataSource={rows}
      columns={columns}
      rowKey="key"
      size={size}
      pagination={false}
      bordered
    />
  );
}
