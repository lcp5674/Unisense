import { useState } from "react";
import { Button, Card, Input, Select, Space, Table, Tag, message, Tabs, Alert } from "antd";
import { SearchOutlined, CodeOutlined, ApartmentOutlined } from "@ant-design/icons";
import { lineageImpact, lineageEdges, lineageImpactPreview, parseLineage, UnisenseApiError } from "../api";
import type { LineageEdge } from "../types";
import { useTracking } from "../hooks/useTracking";
import { enumLabel, GRANULARITY_LABEL } from "../utils/enums";

const RISK_LEVEL_LABEL: Record<string, string> = {
  low: "低",
  medium: "中",
  high: "高",
  critical: "严重",
};

const EDGE_TYPE_LABEL: Record<string, string> = {
  DERIVED_FROM: "派生自",
  CONSUMED_BY: "被消费",
};

type Direction = "upstream" | "downstream" | "both";

function ImpactTab() {
  const [node, setNode] = useState("");
  const [direction, setDirection] = useState<Direction>("downstream");
  const [edges, setEdges] = useState<LineageEdge[]>([]);
  const [total, setTotal] = useState(0);
  const [risk, setRisk] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const { track } = useTracking();

  async function loadImpact() {
    if (!node.trim()) {
      message.warning("请输入节点（指标编码或表名）");
      return;
    }
    setLoading(true);
    try {
      const data =
        direction === "downstream"
          ? await lineageImpact({ node: node.trim(), direction, max_hops: 5 })
          : await lineageEdges({ node: node.trim(), direction });
      setEdges(data.items ?? data);
      setTotal(data.total ?? (Array.isArray(data) ? data.length : 0));
      track("lineage_query", node.trim(), "node");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "查询失败");
      setEdges([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }

  async function previewImpact() {
    if (!node.trim()) {
      message.warning("请输入指标编码");
      return;
    }
    setLoading(true);
    try {
      const p = await lineageImpactPreview(node.trim(), "schema_drift");
      setRisk(
        `受影响指标 ${p.affected_metrics.length} · 物理表 ${p.affected_tables.length} · 消费方 ${p.affected_consumers.length} · 风险等级 ${RISK_LEVEL_LABEL[p.risk_level] ?? p.risk_level}`,
      );
      track("lineage_preview", node.trim(), "node");
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "预览失败");
    } finally {
      setLoading(false);
    }
  }

  const columns = [
    { title: "源", dataIndex: "source_node", key: "source", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "目标", dataIndex: "target_node", key: "target", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
    { title: "类型", dataIndex: "edge_type", key: "type", render: (v: string) => <Tag>{EDGE_TYPE_LABEL[v] ?? v}</Tag> },
    { title: "粒度", dataIndex: "granularity", key: "granularity", width: 100, render: (v: string) => enumLabel(GRANULARITY_LABEL, v) },
    { title: "置信度", dataIndex: "confidence", key: "confidence", width: 90, render: (v: number) => `${(v * 100).toFixed(0)}%` },
    { title: "PII", dataIndex: "pii_inherited", key: "pii", width: 70, render: (v?: boolean) => (v ? <Tag color="red">PII</Tag> : null) },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          placeholder="节点（指标编码 / 表名）"
          value={node}
          onChange={(e) => setNode(e.target.value)}
          onPressEnter={loadImpact}
          prefix={<SearchOutlined />}
          className="mono"
          style={{ width: 300 }}
        />
        <Select
          value={direction}
          onChange={(v) => setDirection(v)}
          style={{ width: 140 }}
          options={[
            { value: "downstream", label: "下游影响" },
            { value: "upstream", label: "上游来源" },
            { value: "both", label: "双向" },
          ]}
        />
        <Button type="primary" onClick={loadImpact} loading={loading}>
          查询
        </Button>
        <Button onClick={previewImpact} loading={loading}>
          变更影响预览
        </Button>
      </Space>

      {risk && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="变更影响预览（what-if）"
          description={risk}
        />
      )}

      {edges.length > 0 ? (
        <Table
          dataSource={edges}
          columns={columns}
          rowKey="id"
          pagination={false}
          size="small"
          footer={() => `共 ${total} 条血缘边`}
        />
      ) : (
        !loading && (
          <p className="muted" style={{ textAlign: "center", padding: 24 }}>
            输入节点后查询血缘关系
          </p>
        )
      )}
    </div>
  );
}

function ParseTab() {
  const [sql, setSql] = useState("");
  const [dialect, setDialect] = useState("mysql");
  const [result, setResult] = useState<{ table_edges: number; field_edges: number; graph_written: boolean } | null>(null);
  const [loading, setLoading] = useState(false);
  const { track } = useTracking();

  async function handleParse() {
    if (!sql.trim()) {
      message.warning("请输入 SQL");
      return;
    }
    setLoading(true);
    try {
      const res = await parseLineage(sql, dialect);
      setResult(res);
      message.success("血缘解析完成");
      track("lineage_parse", undefined, "sql", { dialect });
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "解析失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          value={dialect}
          onChange={setDialect}
          style={{ width: 160 }}
          options={["mysql", "postgres", "hive", "spark", "clickhouse", "duckdb"].map((v) => ({ value: v, label: v }))}
        />
        <Button type="primary" icon={<CodeOutlined />} onClick={handleParse} loading={loading}>
          解析血缘
        </Button>
      </Space>
      <Input.TextArea
        rows={10}
        className="mono"
        value={sql}
        onChange={(e) => setSql(e.target.value)}
        placeholder="-- 粘贴 SQL，解析表级/字段级血缘并写入图谱&#10;SELECT order_id, user_id, amount FROM dwd_finance_order WHERE dt = '2026-08-01'"
        style={{ fontSize: 13 }}
      />
      {result && (
        <Alert
          type="success"
          showIcon
          style={{ marginTop: 12 }}
          message="解析结果"
          description={`表级边 ${result.table_edges} · 字段级边 ${result.field_edges} · 图谱写入 ${result.graph_written ? "成功" : "未写入"}`}
        />
      )}
    </div>
  );
}

export function LineageView() {
  const tabItems = [
    { key: "impact", label: <span><ApartmentOutlined /> 血缘查询 / 影响分析</span>, children: <ImpactTab /> },
    { key: "parse", label: <span><CodeOutlined /> SQL 血缘解析</span>, children: <ParseTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Lineage / Impact</div>
          <h2>血缘视图</h2>
          <p>上下游血缘查询、what-if 变更影响预览、SQL 血缘解析入库。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 16 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
