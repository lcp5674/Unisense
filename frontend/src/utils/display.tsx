// 通用展示辅助 —— 将后端技术对象渲染为中文可读视图，避免裸 JSON 直出。
// 供执行计划 / 元信息 / 口径定义 / Schema 摘要 / 质量阈值等复用。
import type { ReactNode } from "react";
import { Tag } from "antd";
import { enumLabel, DATE_RANGE_LABEL, GRANULARITY_LABEL, METRIC_STATUS_LABEL } from "./enums";

/** 执行计划 / 元信息通用字段名 → 中文 */
export const PLAN_FIELD_LABEL: Record<string, string> = {
  metric_code: "指标编码",
  expression_ast: "口径表达式",
  raw: "原始表达式",
  dialect_sql: "执行 SQL",
  sql_params: "SQL 参数",
  dimensions: "维度过滤",
  date_range: "日期范围",
  granularity: "粒度",
  grain: "口径粒度",
  unit: "单位",
  pii: "含 PII",
  lineage: "依赖指标",
  domain: "业务域",
  status: "状态",
  name: "维度名",
  value: "维度值",
  checks: "校验项",
  total: "总行数",
  elapsed_ms: "耗时",
  from_cache: "来自缓存",
};

/** 口径定义 JSON 字段名 → 中文（对齐 backend definition_json 全集：SQL 模式 / 表达式模式 / seed 数据） */
export const DEF_FIELD_LABEL: Record<string, string> = {
  // SQL 模式
  sql: "口径 SQL",
  etl_sql: "口径 SQL",
  source_tables: "关联数据表",
  source_fields: "来源字段",
  source_columns: "来源字段",
  group_by: "分组维度",
  filters: "过滤条件",
  time_column: "时间字段",
  partition_key: "分区字段",
  measure_columns: "度量列",
  dimensions: "维度",
  measures: "度量",
  columns: "度量字段",
  // 表达式模式
  expression: "表达式",
  expr: "表达式",
  // 依赖与来源
  dependencies: "依赖指标",
  source_table: "源表",
  measure_column: "度量列",
  period: "统计周期",
  grain: "口径粒度",
  unit: "单位",
  // 嵌套对象（measures / source_fields 项）
  name: "名称",
  aggregation: "聚合方式",
  table: "来源表",
  column: "字段",
  pii: "含 PII",
  // 其它元信息
  code: "指标编码",
  metric_code: "指标编码",
};

/** 质量阈值字段名 → 中文 */
export const THRESHOLD_FIELD_LABEL: Record<string, string> = {
  min: "下限",
  max: "上限",
  pct: "偏差百分比",
  expr: "判定表达式",
  window: "统计窗口",
  duration: "时长",
  recent_n: "近期点数",
  days: "天数",
  baseline: "基线值",
  tolerance: "容差",
};

function translateByKey(key: string, raw: string): string {
  if (key === "date_range") return enumLabel(DATE_RANGE_LABEL, raw);
  if (key === "granularity" || key === "grain") return enumLabel(GRANULARITY_LABEL, raw);
  if (key === "status") return enumLabel(METRIC_STATUS_LABEL, raw);
  return raw;
}

function renderValue(key: string, value: unknown, depth: number, labels: Record<string, string>): ReactNode {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return <span className="mono">{String(value)}</span>;
  if (key === "dialect_sql" || key === "sql" || key === "etl_sql") {
    return (
      <pre
        style={{
          margin: 0,
          maxHeight: 200,
          overflow: "auto",
          background: "var(--paper)",
          padding: 8,
          borderRadius: 4,
          fontSize: 12,
        }}
      >
        {String(value)}
      </pre>
    );
  }
  if (key === "expression" || key === "expr") {
    return <code className="mono">{String(value)}</code>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="muted">无</span>;
    const allObjects = value.every((it) => typeof it === "object" && it !== null);
    if (allObjects) {
      return (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {value.map((item, i) => (
            <ObjectView key={i} data={item as Record<string, unknown>} depth={Math.max(depth - 1, 0)} labels={labels} />
          ))}
        </div>
      );
    }
    return (
      <span>
        {value.map((item, i) => (
          <Tag key={i} className="mono" style={{ marginBottom: 2 }}>
            {String(item)}
          </Tag>
        ))}
      </span>
    );
  }
  if (typeof value === "object") {
    if (depth <= 0) return <span className="mono">{JSON.stringify(value)}</span>;
    return <ObjectView data={value as Record<string, unknown>} depth={depth - 1} labels={labels} />;
  }
  return <span className="mono">{translateByKey(key, String(value))}</span>;
}

/** 结构化对象渲染：字段名中文 + 值可读化，嵌套对象递归展开 */
export function ObjectView({
  data,
  depth = 2,
  labels = PLAN_FIELD_LABEL,
}: {
  data: Record<string, unknown>;
  depth?: number;
  labels?: Record<string, string>;
}) {
  const entries = Object.entries(data).filter(([, v]) => v !== null && v !== undefined && v !== "");
  if (!entries.length) return <span className="muted">—</span>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {entries.map(([k, v]) => (
        <div key={k} style={{ display: "flex", flexDirection: "column" }}>
          <span style={{ fontSize: 12, color: "var(--text-secondary)", fontWeight: 600 }}>
            {labels[k] ?? k}
          </span>
          <div style={{ fontSize: 13, lineHeight: 1.8 }}>{renderValue(k, v, depth, labels)}</div>
        </div>
      ))}
    </div>
  );
}

/** 紧凑对象文本：用于表格单元格等窄空间，键→中文、值→可读文本 */
export function kvText(obj: Record<string, unknown>, labels: Record<string, string> = {}): string {
  return Object.entries(obj)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${labels[k] ?? k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(" · ");
}

/** 质量阈值摘要：将阈值 JSON 渲染为「字段：值 · 字段：值」的可读文本 */
export function ThresholdSummary({ threshold }: { threshold: Record<string, unknown> }) {
  const entries = Object.entries(threshold).filter(([, v]) => v !== null && v !== undefined && v !== "");
  if (!entries.length) return <span className="muted">—</span>;
  return (
    <span>
      {entries.map(([k, v], i) => (
        <span key={k}>
          {i > 0 && <span style={{ margin: "0 4px" }}>·</span>}
          <span style={{ fontSize: 12 }}>
            <span style={{ color: "var(--text-secondary)" }}>{THRESHOLD_FIELD_LABEL[k] ?? k}</span>
            <span className="mono" style={{ marginLeft: 4 }}>
              {typeof v === "object" ? JSON.stringify(v) : String(v)}
            </span>
          </span>
        </span>
      ))}
    </span>
  );
}
