import { useEffect, useState } from "react";
import { Button, Collapse, Empty, Segmented, Table, Tag, message } from "antd";
import { lineageImpact } from "../../api";
import type { LineageEdge, LineageNodeInfo } from "../../types";
import { AssetGraph, AssetGraphEdge, AssetGraphNode } from "../../components/assetmap/AssetGraph";
import { LINEAGE_EDGE_TYPE_LABEL } from "../../utils/enums";

const EDGE_COLOR: Record<string, string> = {
  METRIC_DERIVES: "blue",
  METRIC_DEPENDS_ON: "purple",
  TABLE_TO_FIELD: "cyan",
  FIELD_TO_TABLE: "geekblue",
  SQL_PARSE: "default",
  USES_DIMENSION: "gold",
  READS_COLUMN: "magenta",
};

const PROVENANCE_LABEL: Record<string, string> = {
  sqlglot: "SQL 解析",
  manual: "人工登记",
  neo4j: "图谱导入",
};

/**
 * 从血缘边列表构建图谱数据（与血缘影响分析页 edgesToGraphData 语义一致）：
 * 节点 id 去重、label 去类型前缀、type 由前缀推断（table:/metric:/field: → 对应类型，
 * 其余 → other）；合并后端节点元数据（entity_id/domain/owner/pii），使图节点具备
 * 下钻能力并按域/PII 着色。
 */
function edgesToGraphData(
  edges: LineageEdge[],
  nodeMeta?: LineageNodeInfo[],
): { nodes: AssetGraphNode[]; edges: AssetGraphEdge[] } {
  const metaById = new Map((nodeMeta ?? []).map((m) => [m.id, m]));
  const nodeMap = new Map<string, AssetGraphNode>();
  const graphEdges: AssetGraphEdge[] = [];
  const addNode = (id: string) => {
    if (nodeMap.has(id)) return;
    const colon = id.indexOf(":");
    const prefix = colon === -1 ? "" : id.slice(0, colon);
    const label = colon === -1 ? id : id.slice(colon + 1);
    const type =
      prefix === "table" ? "table" : prefix === "metric" ? "metric" : prefix === "field" ? "field" : "other";
    const meta = metaById.get(id);
    nodeMap.set(id, {
      id,
      type,
      label: label || id,
      entity_id: meta?.entity_id ?? undefined,
      pii: meta?.pii,
      domain: meta?.domain ?? undefined,
      owner: meta?.owner ?? undefined,
    });
  };
  for (const e of edges) {
    addNode(e.source_node);
    addNode(e.target_node);
    graphEdges.push({ source: e.source_node, target: e.target_node, type: e.edge_type });
  }
  return { nodes: Array.from(nodeMap.values()), edges: graphEdges };
}

export function LineageImpact({ metricCode }: { metricCode: string }) {
  const [edges, setEdges] = useState<LineageEdge[]>([]);
  const [graphData, setGraphData] = useState<{
    nodes: AssetGraphNode[];
    edges: AssetGraphEdge[];
  } | null>(null);
  const [direction, setDirection] = useState<"upstream" | "downstream">("downstream");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load(dir: "upstream" | "downstream") {
    setLoading(true);
    setError(null);
    try {
      const res = await lineageImpact({ node: `metric:${metricCode}`, direction: dir, max_hops: 5, page_size: 50 });
      setEdges(res.items);
      // 边列表 → 图谱数据（节点元数据合并进图节点，供下钻/着色）
      setGraphData(res.items.length > 0 ? edgesToGraphData(res.items, res.nodes) : null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "血缘加载失败");
      setEdges([]);
      setGraphData(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(direction);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [metricCode, direction]);

  /** 点击图谱节点：指标节点新开详情页查看，表/字段节点提示（不打断当前详情上下文）。 */
  function handleNodeClick(node: AssetGraphNode) {
    if (node.type === "metric") {
      window.open(`/detail/${encodeURIComponent(node.id.replace(/^metric:/, ""))}`, "_blank");
    } else if (node.type === "table") {
      message.info(
        node.entity_id != null
          ? `表「${node.label}」已收录于元数据目录（id=${node.entity_id}）`
          : `表「${node.label}」未在元数据目录中（可能尚未采集），仅展示血缘关系`,
      );
    } else if (node.type === "field") {
      message.info(`字段「${node.label}」的详情请在血缘图谱中查看`);
    } else {
      message.info(`节点「${node.label}」的详情请在血缘图谱中查看`);
    }
  }

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
      render: (v: string) => <Tag color={EDGE_COLOR[v] ?? "default"}>{LINEAGE_EDGE_TYPE_LABEL[v] ?? v}</Tag>,
    },
    { title: "置信度", dataIndex: "confidence", key: "conf", width: 100, render: (v: number) => `${Math.round(v * 100)}%` },
    { title: "来源", dataIndex: "provenance", key: "prov", width: 110, render: (v: string) => PROVENANCE_LABEL[v] ?? v },
    {
      title: "PII 传导",
      dataIndex: "pii_inherited",
      key: "pii",
      width: 100,
      render: (v?: boolean) => (v ? <Tag color="red">PII</Tag> : <span className="muted">—</span>),
    },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
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
      ) : graphData && graphData.nodes.length > 0 ? (
        <>
          <AssetGraph
            nodes={graphData.nodes}
            edges={graphData.edges}
            height={380}
            onNodeClick={handleNodeClick}
            lanes
          />
          <Collapse
            size="small"
            style={{ marginTop: 12 }}
            items={[
              {
                key: "edge-list",
                label: `边明细（${edges.length} 条）`,
                children: (
                  <Table
                    dataSource={edges}
                    columns={columns}
                    rowKey="id"
                    size="small"
                    pagination={false}
                    locale={{ emptyText: "暂无血缘边" }}
                  />
                ),
              },
            ]}
          />
        </>
      ) : (
        <Empty
          style={{ padding: "24px 0" }}
          description={
            loading
              ? "血缘加载中…"
              : "暂无血缘关系（可在血缘视图解析 SQL 建立）"
          }
        />
      )}
    </div>
  );
}
