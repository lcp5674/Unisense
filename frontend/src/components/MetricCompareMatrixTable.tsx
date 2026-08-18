import { Table, Tag } from "antd";
import type {
  MetricCompareMatrixDeps,
  MetricCompareMatrixField,
  MetricCompareMatrixResult,
} from "../types";
import { DEF_FIELD_LABEL, ObjectView } from "../utils/display";
import { COMPARE_FIELD_LABELS } from "./MetricCompareTable";

// 行级差异等级 → 视觉：圆点 pill。语义：全部一致 / 部分不同 / 全部不同
const MATRIX_DIFF_META: Record<
  string,
  { pillBg: string; pillFg: string; label: string }
> = {
  all_identical: { pillBg: "rgba(46,158,91,0.10)", pillFg: "#2e9e5b", label: "全部一致" },
  partial: { pillBg: "rgba(232,134,45,0.14)", pillFg: "#c77700", label: "部分不同" },
  all_different: { pillBg: "rgba(214,69,69,0.12)", pillFg: "#d64545", label: "全部不同" },
};

function MatrixDiffPill({ level }: { level: string }) {
  const m = MATRIX_DIFF_META[level] ?? MATRIX_DIFF_META.all_identical;
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

function renderValue(v: unknown) {
  if (v == null) return <span className="muted">—</span>;
  if (typeof v === "object") {
    return <ObjectView data={v as Record<string, unknown>} depth={1} labels={DEF_FIELD_LABEL} />;
  }
  return <span className="mono">{String(v)}</span>;
}

// 指标状态 → 中文标签（对齐 MetricDetail 口径）
const STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  EXPERIMENTAL: "实验",
  REVIEW: "审核中",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
  DATA_SOURCE_DROPPED: "数据源下线",
};

/**
 * 治理字段友好渲染（P2-14）：PII/合规复核/状态/版本/责任人 需可读标签而非裸 bool/数字，
 * 否则「责任人不同、敏感分级不同」等高价值治理信号被埋在 true/1/2 里。
 */
function renderGovernanceValue(
  key: string,
  v: unknown,
  ownerNames: Record<number, string> | undefined,
) {
  switch (key) {
    case "pii_flag":
      return v ? <Tag color="red">PII</Tag> : <Tag>非 PII</Tag>;
    case "compliance_reviewed":
      return v ? <Tag color="green">已复核</Tag> : <Tag color="orange">未复核</Tag>;
    case "status":
      return <Tag>{STATUS_LABEL[String(v)] ?? String(v)}</Tag>;
    case "version":
      return <span className="mono">v{String(v)}</span>;
    case "owner_id":
      return ownerNames && typeof v === "number" && ownerNames[v]
        ? <span>{ownerNames[v]}</span>
        : renderValue(v);
    default:
      return renderValue(v);
  }
}

/**
 * 多指标矩阵对比表（对比页专用）：每行一个字段、每列一个指标，行级汇总差异等级。
 * 输入 compareMetricsMatrix 的结果，纯展示，无副作用。
 */
export function MetricCompareMatrixTable({ result }: { result: MetricCompareMatrixResult }) {
  const metrics = result.metrics;
  const counts: Record<string, number> = { all_identical: 0, partial: 0, all_different: 0 };

  type Row = {
    key: string;
    values: Record<string, unknown>;
    level: string;
    only?: Record<string, string[]>;
  };
  const rows: Row[] = Object.entries(result.fields).map(([key, field]) => {
    if (!field) {
      counts.all_identical += 1;
      return { key, values: {}, level: "all_identical" };
    }
    if ("only" in field) {
      const d = field as MetricCompareMatrixDeps;
      counts[d.difference_level] = (counts[d.difference_level] ?? 0) + 1;
      return { key, values: d.values as Record<string, unknown>, level: d.difference_level, only: d.only };
    }
    const f = field as MetricCompareMatrixField;
    counts[f.difference_level] = (counts[f.difference_level] ?? 0) + 1;
    return { key, values: f.values, level: f.difference_level };
  });

  // 顶部汇总条
  const total = metrics.length > 0 ? Object.keys(result.fields).length : 0;

  const columns = [
    {
      title: "字段",
      dataIndex: "key",
      key: "key",
      width: 150,
      render: (v: string) => <strong style={{ color: "var(--ink)" }}>{COMPARE_FIELD_LABELS[v] ?? v}</strong>,
    },
    {
      title: "汇总",
      dataIndex: "level",
      key: "level",
      width: 110,
      render: (v: string) => <MatrixDiffPill level={v} />,
    },
    ...metrics.map((code) => ({
      title: <span className="mono">{code}</span>,
      dataIndex: "values",
      key: code,
      width: 260,
      render: (values: Record<string, unknown>, row: Row) => {
        if (row.key === "definition" && values[code] && typeof values[code] === "object") {
          return <ObjectView data={values[code] as Record<string, unknown>} depth={1} labels={DEF_FIELD_LABEL} />;
        }
        if (row.key === "dependencies" && row.only) {
          const deps = (values[code] as string[]) ?? [];
          const only = row.only[code] ?? [];
          return (
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {deps.length ? (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                  {deps.map((d) => (
                    <Tag key={d} style={{ marginInlineEnd: 0 }}>{d}</Tag>
                  ))}
                </div>
              ) : (
                <span className="muted">—</span>
              )}
              {only.length > 0 && (
                <span style={{ fontSize: 12, color: "#c77700" }}>
                  仅本指标: {only.join(", ")}
                </span>
              )}
            </div>
          );
        }
        return (
          <div style={{ padding: "2px 0" }}>{renderGovernanceValue(row.key, values[code], result.owner_names)}</div>
        );
      },
    })),
  ];

  return (
    <div>
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
        <span style={{ color: "var(--muted)" }}>
          共 {total} 项字段 · {metrics.length} 个指标
        </span>
        <span style={{ color: "var(--line)" }}>·</span>
        {Object.entries(counts).map(([key, n]) => {
          const m = MATRIX_DIFF_META[key];
          return (
            <span key={key} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span
                style={{
                  width: 6,
                  height: 6,
                  borderRadius: "50%",
                  background: m.pillFg,
                  display: "inline-block",
                }}
              />
              <span style={{ color: "var(--text-secondary)" }}>{m.label}</span>
              <span className="mono" style={{ fontWeight: 600 }}>{n}</span>
            </span>
          );
        })}
      </div>
      <Table
        dataSource={rows}
        columns={columns}
        rowKey="key"
        size="middle"
        pagination={false}
        bordered
        tableLayout="fixed"
        scroll={{ x: 150 + 110 + metrics.length * 260 }}
        style={{
          background: "var(--surface)",
          borderRadius: 8,
          overflow: "hidden",
        }}
      />
    </div>
  );
}
