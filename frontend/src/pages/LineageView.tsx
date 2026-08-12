import { useState } from "react";
import { Button, Card, Input, Select, Space, Table, Tag, message } from "antd";
import { SearchOutlined } from "@ant-design/icons";
import { lineageImpact, lineageEdges, lineageImpactPreview, UnisenseApiError } from "../api";
import type { LineageEdge } from "../types";
import { useTracking } from "../hooks/useTracking";

type Direction = "upstream" | "downstream" | "both";

export function LineageView() {
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
      message.error(
        err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "查询失败",
      );
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
        `受影响指标 ${p.affected_metrics.length} · 表 ${p.affected_reports.length} · 消费方 ${p.affected_consumers.length} · 风险 ${p.risk_level}`,
      );
      track("lineage_preview", node.trim(), "node");
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "预览失败",
      );
    } finally {
      setLoading(false);
    }
  }

  const columns = [
    { title: "源", dataIndex: "source_node", key: "source" },
    { title: "目标", dataIndex: "target_node", key: "target" },
    { title: "类型", dataIndex: "edge_type", key: "type" },
    { title: "粒度", dataIndex: "granularity", key: "granularity" },
    {
      title: "置信度",
      dataIndex: "confidence",
      key: "confidence",
      render: (v: number) => `${(v * 100).toFixed(0)}%`,
    },
    {
      title: "PII",
      dataIndex: "pii_inherited",
      key: "pii",
      render: (v?: boolean) =>
        v ? <Tag color="red">PII</Tag> : null,
    },
  ];

  return (
    <Card title="血缘视图">
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          placeholder="节点（指标编码 / 表名）"
          value={node}
          onChange={(e) => setNode(e.target.value)}
          onPressEnter={loadImpact}
          prefix={<SearchOutlined />}
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
        <p style={{ marginBottom: 12 }}>
          <Tag color={risk.includes("high") ? "red" : risk.includes("medium") ? "orange" : "green"}>
            影响预览
          </Tag>{" "}
          {risk}
        </p>
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
          <p style={{ color: "#999", textAlign: "center" }}>输入节点后查询血缘关系</p>
        )
      )}
    </Card>
  );
}
