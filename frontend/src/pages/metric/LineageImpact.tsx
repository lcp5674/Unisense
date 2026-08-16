import { useEffect, useState } from "react";
import { Button, Empty, Segmented, Table, Tag } from "antd";
import { lineageImpact } from "../../api";
import type { LineageEdge } from "../../types";

const EDGE_COLOR: Record<string, string> = {
  METRIC_DERIVES: "blue",
  METRIC_DEPENDS_ON: "purple",
  TABLE_TO_FIELD: "cyan",
  FIELD_TO_TABLE: "geekblue",
  SQL_PARSE: "default",
};

const EDGE_TYPE_LABEL: Record<string, string> = {
  DERIVED_FROM: "派生自",
  LINEAGE_UP: "上游依赖",
  LINEAGE_DOWN: "下游影响",
  CONSUMED_BY: "被消费",
  EXTERNAL_BREAK: "外部断链",
  METRIC_DERIVES: "指标派生",
  METRIC_DEPENDS_ON: "指标依赖",
  TABLE_TO_FIELD: "表到字段",
  FIELD_TO_TABLE: "字段到表",
  SQL_PARSE: "SQL 解析",
};

const PROVENANCE_LABEL: Record<string, string> = {
  sqlglot: "SQL 解析",
  manual: "人工登记",
  neo4j: "图谱导入",
};

export function LineageImpact({ metricCode }: { metricCode: string }) {
  const [edges, setEdges] = useState<LineageEdge[]>([]);
  const [direction, setDirection] = useState<"upstream" | "downstream">("downstream");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load(dir: "upstream" | "downstream") {
    setLoading(true);
    setError(null);
    try {
      const res = await lineageImpact({ node: `metric:${metricCode}`, direction: dir, max_hops: 5, page_size: 50 });
      setEdges(res.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "血缘加载失败");
      setEdges([]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(direction);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [metricCode, direction]);

  const columns = [
    {
      title: direction === "downstream" ? "下游节点" : "上游节点",
      key: "node",
      render: (_: unknown, e: LineageEdge) => (
        <span className="mono">{direction === "downstream" ? e.target_node : e.source_node}</span>
      ),
    },
    {
      title: "关系",
      dataIndex: "edge_type",
      key: "edge_type",
      width: 160,
      render: (v: string) => <Tag color={EDGE_COLOR[v] ?? "default"}>{EDGE_TYPE_LABEL[v] ?? v}</Tag>,
    },
    { title: "置信度", dataIndex: "confidence", key: "conf", width: 100, render: (v: number) => `${Math.round(v * 100)}%` },
    { title: "来源", dataIndex: "provenance", key: "prov", width: 110, render: (v: string) => PROVENANCE_LABEL[v] ?? v },
    { title: "PII 传导", dataIndex: "pii_inherited", key: "pii", width: 100, render: (v?: boolean) => v ? <Tag color="red">PII</Tag> : <span className="muted">—</span> },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        <Segmented
          value={direction}
          onChange={(v) => setDirection(v as "upstream" | "downstream")}
          options={[
            { label: "下游影响（被谁消费）", value: "downstream" },
            { label: "上游依赖（来自哪里）", value: "upstream" },
          ]}
        />
        <Button
          type="link"
          size="small"
          onClick={() => window.open(`/lineage?node=${encodeURIComponent(`metric:${metricCode}`)}`, "_blank")}
        >
          在图谱中查看 →
        </Button>
      </div>
      {error ? (
        <Empty description={error} />
      ) : (
        <Table
          dataSource={edges}
          columns={columns}
          rowKey="id"
          size="small"
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          loading={loading}
          locale={{ emptyText: "暂无血缘关系（可在血缘视图解析 SQL 建立）" }}
        />
      )}
    </div>
  );
}
