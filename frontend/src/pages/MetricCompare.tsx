import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Button, Card, Empty, Spin, Table, Tag } from "antd";
import { compareMetrics } from "../api";
import type { MetricCompareDeps, MetricCompareField, MetricCompareResult } from "../types";
import { ObjectView } from "../utils/display";

const FIELD_LABELS: Record<string, string> = {
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

export function MetricCompare() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const codeA = searchParams.get("a") || "";
  const codeB = searchParams.get("b") || "";
  const [result, setResult] = useState<MetricCompareResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    if (!codeA || !codeB) {
      setError("请从指标目录勾选两个指标后对比");
      setLoading(false);
      return;
    }
    setLoading(true);
    compareMetrics(codeA, codeB)
      .then((res) => {
        if (alive) setResult(res);
      })
      .catch((err) => {
        if (alive) setError(err instanceof Error ? err.message : "对比失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [codeA, codeB]);

  const rows = result
    ? Object.entries(result.fields).map(([key, field]) => {
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
      })
    : [];

  const columns = [
    {
      title: "字段",
      dataIndex: "label",
      key: "label",
      width: 130,
      render: (v: string) => <strong>{FIELD_LABELS[v] ?? v}</strong>,
    },
    { title: codeA || "指标 A", dataIndex: "a", key: "a", render: (v: unknown) => renderValue(v) },
    {
      title: "差异",
      dataIndex: "level",
      key: "level",
      width: 90,
      render: (v: keyof typeof DIFF_META) => <Tag color={DIFF_META[v]?.color}>{DIFF_META[v]?.label ?? v}</Tag>,
    },
    { title: codeB || "指标 B", dataIndex: "b", key: "b", render: (v: unknown) => renderValue(v) },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Assets / Compare</div>
          <h2>指标对比</h2>
          <p>
            <span className="mono">{codeA}</span>
            <span style={{ margin: "0 8px" }}>↔</span>
            <span className="mono">{codeB}</span>
            <span style={{ margin: "0 8px" }}>·</span>
            关键字段并排 diff
          </p>
        </div>
        <Button type="link" onClick={() => navigate("/catalog")}>← 返回目录</Button>
      </div>

      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin />
        </div>
      ) : error ? (
        <Card>
          <Empty description={error} />
        </Card>
      ) : result ? (
        <Table
          dataSource={rows}
          columns={columns}
          rowKey="key"
          size="middle"
          pagination={false}
          bordered
        />
      ) : null}
    </div>
  );
}
