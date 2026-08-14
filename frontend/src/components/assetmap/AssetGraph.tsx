import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Empty, Table, Tag } from "antd";
import { FullscreenOutlined } from "@ant-design/icons";
import { Graph as G6Graph } from "@antv/g6";
import type { GraphData, IElementEvent, NodeData } from "@antv/g6";

/** 资产地图图谱节点（后端 /assetmap/graph 的 nodes 元素）。 */
export interface AssetGraphNode extends Record<string, unknown> {
  id: string;
  type: string;
  label: string;
  /** db_catalog 主键（仅表/视图节点有值，用于实体详情下钻） */
  entity_id?: number;
  pii?: boolean;
  domain?: string;
  owner?: string;
}

export interface AssetGraphEdge extends Record<string, unknown> {
  source: string;
  target: string;
  type: string;
}

interface AssetGraphProps {
  nodes: AssetGraphNode[];
  edges: AssetGraphEdge[];
  height?: number;
  onNodeClick?: (node: AssetGraphNode) => void;
}

const TYPE_LABEL: Record<string, string> = {
  metric: "指标",
  table: "表 / 视图",
  field: "字段",
  unknown: "未知",
};

// 业务域配色：确定性 hash 取色（同域恒定同色，避免渲染抖动）
const DOMAIN_PALETTE = [
  "#5b8ff9",
  "#5ad8a6",
  "#5d7092",
  "#f6bd16",
  "#e8684a",
  "#6dc8ec",
  "#9270ca",
  "#ff9d4d",
  "#269a99",
  "#ff99c3",
];

function domainColor(domain?: string): string {
  if (!domain) return "#c4c4c4";
  let h = 0;
  for (const ch of domain) h = (h * 31 + ch.charCodeAt(0)) % 9973;
  return DOMAIN_PALETTE[h % DOMAIN_PALETTE.length];
}

function trimLabel(label: string, max = 26): string {
  return label.length > max ? `${label.slice(0, max)}…` : label;
}

// 节点渲染上限：节点过多时力导向图会失去可读性（挤成一团、标签不可辨）。
// 超出后按优先级保留核心节点：指标 > 表/视图 > 字段，同一优先级按血缘度降序。
const MAX_RENDER_NODES = 160;

function nodeRank(n: AssetGraphNode): number {
  if (n.type === "metric") return 0;
  if (n.type === "table") return 1;
  return 2; // field 及未知类型
}

/** 按优先级 + 血缘度截断节点，返回可见节点集与仅含两端可见的边。 */
function pickVisible(
  nodes: AssetGraphNode[],
  edges: AssetGraphEdge[],
  showAll: boolean,
): { visible: AssetGraphNode[]; visibleEdges: AssetGraphEdge[]; hidden: number } {
  if (showAll || nodes.length <= MAX_RENDER_NODES) {
    const ids = new Set(nodes.map((n) => n.id));
    return {
      visible: nodes,
      visibleEdges: edges.filter((e) => ids.has(e.source) && ids.has(e.target)),
      hidden: 0,
    };
  }
  const degree = new Map<string, number>();
  for (const e of edges) {
    degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
    degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
  }
  const sorted = [...nodes].sort(
    (a, b) => nodeRank(a) - nodeRank(b) || (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0),
  );
  const visible = sorted.slice(0, MAX_RENDER_NODES);
  const ids = new Set(visible.map((n) => n.id));
  return {
    visible,
    visibleEdges: edges.filter((e) => ids.has(e.source) && ids.has(e.target)),
    hidden: nodes.length - MAX_RENDER_NODES,
  };
}

/**
 * 资产地图力导向图（方案 A 主视图）。
 *
 * - 节点：按业务域着色成簇、PII 红色描边、按血缘度编码大小
 * - 交互：拖拽画布 / 滚轮缩放 / 拖拽节点 / 悬停邻域高亮 / 点击节点回调 / 重置视图
 * - 可读性：节点过多时按优先级限流渲染 + 提示筛选；节点数较少时保留完整标签
 * - 兜底：canvas 不可用（jsdom/弱环境）时降级为表格，保证数据可浏览
 */
export function AssetGraph({ nodes, edges, height = 600, onNodeClick }: AssetGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<G6Graph | null>(null);
  const onNodeClickRef = useRef(onNodeClick);
  const [renderFailed, setRenderFailed] = useState(false);
  const [showAll, setShowAll] = useState(false);
  onNodeClickRef.current = onNodeClick;

  // 限流渲染：优先保留核心节点，超出阈值时默认隐藏附属字段节点
  const {
    visible: visibleNodes,
    visibleEdges,
    hidden,
  } = useMemo(() => pickVisible(nodes, edges, showAll), [nodes, edges, showAll]);

  // 血缘度：节点关联边数 → 编码节点大小
  const degreeMap = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of visibleEdges) {
      m.set(e.source, (m.get(e.source) ?? 0) + 1);
      m.set(e.target, (m.get(e.target) ?? 0) + 1);
    }
    return m;
  }, [visibleEdges]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || visibleNodes.length === 0) return;

    let graph: G6Graph | null = null;
    const nodeData: GraphData = {
      nodes: visibleNodes.map((n) => ({ id: n.id, data: n })),
      edges: visibleEdges.map((e) => ({ source: e.source, target: e.target, data: e })),
    };

    try {
      graph = new G6Graph({
        container,
        autoFit: "view",
        padding: 32,
        data: nodeData,
        node: {
          style: {
            size: (d: NodeData) => Math.min(26, 10 + (degreeMap.get(String(d.id)) ?? 0) * 1.5),
            fill: (d: NodeData) => domainColor((d.data as AssetGraphNode | undefined)?.domain),
            stroke: (d: NodeData) =>
              (d.data as AssetGraphNode | undefined)?.pii ? "#e02020" : "#ffffff",
            lineWidth: (d: NodeData) => ((d.data as AssetGraphNode | undefined)?.pii ? 3 : 1.5),
            labelText: (d: NodeData) =>
              trimLabel((d.data as AssetGraphNode | undefined)?.label ?? String(d.id)),
            labelPlacement: "bottom",
            labelFill: "#4a4a4a",
            labelFontSize: 11,
            cursor: "pointer",
          },
          state: {
            active: { fill: "#faad14", stroke: "#8c6d00", lineWidth: 2 },
            inactive: { opacity: 0.2 },
          },
        },
        edge: {
          style: {
            stroke: "#c9c9c9",
            lineWidth: 1,
            endArrow: true,
          },
        },
        layout: {
          type: "d3-force",
          linkDistance: 60,
          collide: { radius: 22 },
          manyBody: { strength: -320 },
        },
        behaviors: ["drag-canvas", "zoom-canvas", "drag-element"],
      });
      graphRef.current = graph;

      graph.on<IElementEvent>("node:click", (evt) => {
        const raw = evt.target as { id?: string; __data__?: { id?: string } } | undefined;
        const id = raw?.id ?? raw?.__data__?.id;
        if (!id || !graph) return;
        const node = graph.getNodeData(String(id))?.data as AssetGraphNode | undefined;
        if (node) onNodeClickRef.current?.(node);
      });

      // 悬停邻域高亮：相邻节点高亮，其余淡化
      graph.on<IElementEvent>("node:pointerenter", (evt) => {
        const raw = evt.target as { id?: string; __data__?: { id?: string } } | undefined;
        const id = raw?.id ?? raw?.__data__?.id;
        if (!id || !graph) return;
        const neighbors = graph.getNeighborNodesData(String(id));
        const active = new Set<string>([String(id), ...neighbors.map((n) => String(n.id))]);
        for (const n of graph.getNodeData()) {
          void graph.setElementState(
            String(n.id),
            active.has(String(n.id)) ? "active" : "inactive",
          );
        }
      });
      graph.on("node:pointerleave", () => {
        if (!graph) return;
        for (const n of graph.getNodeData()) {
          void graph.setElementState(String(n.id), []);
        }
      });

      graph.render().catch((err: unknown) => {
        console.error("[AssetGraph] G6 render 失败，降级为表格", err);
        setRenderFailed(true);
      });
      setRenderFailed(false);
    } catch (err) {
      console.error("[AssetGraph] G6 初始化失败，降级为表格", err);
      setRenderFailed(true);
    }

    return () => {
      graphRef.current = null;
      graph?.destroy();
    };
  }, [visibleNodes, visibleEdges, degreeMap]);

  if (nodes.length === 0) {
    return <Empty description="暂无图谱数据" />;
  }

  if (renderFailed) {
    return (
      <div>
        <Table
          dataSource={nodes}
          rowKey="id"
          size="small"
          pagination={{ pageSize: 20 }}
          columns={[
            {
              title: "类型",
              dataIndex: "type",
              key: "type",
              width: 100,
              render: (v: string) => (
                <Tag color={v === "metric" ? "purple" : v === "table" ? "blue" : "cyan"}>
                  {TYPE_LABEL[v] ?? v}
                </Tag>
              ),
            },
            { title: "名称", dataIndex: "label", key: "label", ellipsis: true },
            {
              title: "域",
              dataIndex: "domain",
              key: "domain",
              width: 130,
              render: (v: string | undefined) => v ?? <span className="muted">-</span>,
            },
            {
              title: "PII",
              dataIndex: "pii",
              key: "pii",
              width: 70,
              render: (v?: boolean) => (v ? <Tag color="red">PII</Tag> : null),
            },
          ]}
        />
        <Table
          dataSource={edges}
          rowKey={(r) => `${r.source}-${r.target}-${r.type}`}
          size="small"
          style={{ marginTop: 16 }}
          pagination={{ pageSize: 20 }}
          columns={[
            { title: "源", dataIndex: "source", key: "source", ellipsis: true },
            { title: "目标", dataIndex: "target", key: "target", ellipsis: true },
            { title: "类型", dataIndex: "type", key: "type", width: 160 },
          ]}
        />
      </div>
    );
  }

  const domains = [...new Set(nodes.map((n) => n.domain).filter(Boolean))] as string[];
  const typeCounts = nodes.reduce<Record<string, number>>((acc, n) => {
    acc[n.type] = (acc[n.type] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div>
      {hidden > 0 && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "6px 12px",
            marginBottom: 8,
            background: "var(--bg-elevated, #fafafa)",
            borderRadius: 6,
            fontSize: 13,
            color: "var(--text-2)",
          }}
        >
          <span>
            图节点较多（共 {nodes.length} 个），已优先展示 {visibleNodes.length} 个核心节点。
            可切换到全部或使用「域筛选」缩小范围后更清晰。
          </span>
          <Button size="small" type="link" onClick={() => setShowAll(true)}>
            显示全部
          </Button>
        </div>
      )}
      <div ref={containerRef} style={{ height, width: "100%" }} data-testid="asset-graph-canvas" />
      <div
        style={{
          marginTop: 10,
          display: "flex",
          gap: 20,
          flexWrap: "wrap",
          fontSize: 12,
          color: "var(--text-2)",
          alignItems: "center",
        }}
      >
        <div>
          <span className="muted">类型：</span>
          {Object.entries(typeCounts).map(([t, c]) => (
            <Tag key={t} style={{ marginRight: 6 }}>
              {TYPE_LABEL[t] ?? t} {c}
            </Tag>
          ))}
        </div>
        <div>
          <span className="muted">业务域：</span>
          {domains.map((d) => (
            <span key={d} style={{ marginRight: 8 }}>
              <span
                style={{
                  display: "inline-block",
                  width: 10,
                  height: 10,
                  borderRadius: 3,
                  background: domainColor(d),
                  marginRight: 4,
                }}
              />
              {d}
            </span>
          ))}
          {domains.length === 0 && <span className="muted">-</span>}
        </div>
        <div>
          <span
            style={{
              display: "inline-block",
              width: 10,
              height: 10,
              borderRadius: 3,
              background: "#e02020",
              marginRight: 4,
            }}
          />
          <span className="muted">PII 描边 · 节点大小=血缘度</span>
        </div>
        <Button
          size="small"
          icon={<FullscreenOutlined />}
          onClick={() => graphRef.current?.fitView()}
        >
          重置视图
        </Button>
      </div>
    </div>
  );
}
