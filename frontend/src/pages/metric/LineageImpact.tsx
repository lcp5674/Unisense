import { useEffect, useState } from "react";
import { Alert, Button, Collapse, Empty, Segmented, Select, Space, Table, Tag, message } from "antd";
import { lineageImpact, lineageImpactPreview } from "../../api";
import type { ImpactPreview, LineageEdge, LineageNodeInfo } from "../../types";
import { AssetGraph, AssetGraphEdge, AssetGraphNode } from "../../components/assetmap/AssetGraph";
import { LINEAGE_EDGE_TYPE_LABEL } from "../../utils/enums";
import { CodeValue } from "../../components/CodeValue";

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

const RISK_LEVEL_LABEL: Record<string, string> = {
  low: "低",
  medium: "中",
  high: "高",
  critical: "严重",
};

const RISK_LEVEL_COLOR: Record<string, string> = {
  low: "green",
  medium: "orange",
  high: "red",
  critical: "volcano",
};

// 跳数选项：与后端 /lineage/impact max_hops(ge=1, le=10) 对齐
const HOPS_OPTIONS = [1, 2, 3, 5, 8, 10].map((v) => ({ value: v, label: `${v} 跳` }));

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
  const [direction, setDirection] = useState<"upstream" | "downstream" | "both">("downstream");
  const [maxHops, setMaxHops] = useState(5);
  const [preview, setPreview] = useState<ImpactPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load(dir: "upstream" | "downstream" | "both", hops: number) {
    setLoading(true);
    setError(null);
    try {
      const res = await lineageImpact({ node: `metric:${metricCode}`, direction: dir, max_hops: hops, page_size: 50 });
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
    load(direction, maxHops);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [metricCode, direction, maxHops]);

  /** 变更影响预览（what-if）：schema 漂移场景下受影响指标/物理表/消费方 + 风险等级。 */
  async function previewImpact() {
    setLoading(true);
    try {
      const p: ImpactPreview = await lineageImpactPreview(`metric:${metricCode}`, "schema_drift");
      setPreview(p);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "变更影响预览失败");
    } finally {
      setLoading(false);
    }
  }

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
      title: direction === "downstream" ? "下游节点" : direction === "upstream" ? "上游节点" : "关联节点",
      key: "node",
      render: (_: unknown, e: LineageEdge) => {
        // 节点 ID 形如 metric:/table:/field:，剥离前缀展示编码本体（等宽窄列下保留更多省略空间）；
        // 完整 ID 仍存于 aria-label，hover 可查
        const node = direction === "downstream" ? e.target_node : e.source_node;
        const display = node.replace(/^(metric|table|field):/, "");
        return <CodeValue value={node} displayValue={display} code maxWidth={280} maxChars={34} />;
      },
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
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
        <Space wrap>
          <Segmented
            value={direction}
            onChange={(v) => setDirection(v as "upstream" | "downstream" | "both")}
            options={[
              { label: "下游影响", value: "downstream" },
              { label: "上游依赖", value: "upstream" },
              { label: "双向", value: "both" },
            ]}
          />
          <Select showSearch
            size="small"
            style={{ width: 84 }}
            value={maxHops}
            onChange={setMaxHops}
            options={HOPS_OPTIONS}
            title="血缘展开跳数（1-10）"
          />
          <Button size="small" onClick={previewImpact} loading={loading}>
            变更影响预览
          </Button>
        </Space>
        <Button
          type="link"
          size="small"
          onClick={() => window.open(`/lineage?node=${encodeURIComponent(`metric:${metricCode}`)}`, "_blank")}
        >
          在图谱中查看 →
        </Button>
      </div>

      {preview && (
        <Alert
          type={preview.risk_level === "low" ? "info" : preview.risk_level === "medium" ? "warning" : "error"}
          showIcon
          closable
          onClose={() => setPreview(null)}
          style={{ marginBottom: 12 }}
          message="变更影响预览（what-if）"
          description={
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <Space size={18} wrap>
                <span>受影响指标 {preview.affected_metrics.length}</span>
                <span>物理表 {preview.affected_tables.length}</span>
                <span>消费方 {preview.affected_consumers.length}</span>
                <Tag color={RISK_LEVEL_COLOR[preview.risk_level] ?? "default"}>
                  风险等级 {RISK_LEVEL_LABEL[preview.risk_level] ?? preview.risk_level}
                </Tag>
              </Space>
              {(preview.affected_metrics.length > 0 ||
                preview.affected_tables.length > 0 ||
                preview.affected_consumers.length > 0) && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                  {preview.affected_metrics.slice(0, 12).map((m) => (
                    <Tag key={m.metric_code} color="purple" title="受影响指标">
                      {m.metric_code}
                    </Tag>
                  ))}
                  {preview.affected_metrics.length > 12 && (
                    <Tag>…等 {preview.affected_metrics.length} 个指标</Tag>
                  )}
                  {preview.affected_tables.slice(0, 16).map((t) => (
                    <Tag key={t} color="blue" title="受影响物理表">
                      {t.replace(/^table:/, "")}
                    </Tag>
                  ))}
                  {preview.affected_tables.length > 16 && (
                    <Tag>…等 {preview.affected_tables.length} 张表</Tag>
                  )}
                  {preview.affected_consumers.slice(0, 8).map((c) => (
                    <Tag key={c} color="green" title="受影响消费方">
                      {c.replace(/^consumer:/, "")}
                    </Tag>
                  ))}
                </div>
              )}
            </div>
          }
        />
      )}

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
