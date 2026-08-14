import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Empty, Input, Select, Table, Tag } from "antd";
import { FullscreenOutlined, SearchOutlined } from "@ant-design/icons";
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
  /** 是否展示字段节点（血缘总览等场景默认隐藏，减少视觉噪声）；默认 true */
  showFields?: boolean;
}

const TYPE_LABEL: Record<string, string> = {
  metric: "指标",
  table: "表 / 视图",
  field: "字段",
  unknown: "未知",
};

const TYPE_OPTIONS = Object.entries(TYPE_LABEL).map(([value, label]) => ({ value, label }));

// 业务域配色：确定性 hash 取色（同域恒定同色，避免渲染抖动）。
// 使用饱和深色调（相对旧的浅粉/浅青有更高对比度），在白色画布上更醒目。
const DOMAIN_PALETTE = [
  "#3a6df0", // 蓝
  "#0f9d58", // 绿
  "#7b4dff", // 紫
  "#e8a200", // 琥珀
  "#d64545", // 红
  "#0d9bb0", // 青
  "#b44ee0", // 洋红紫
  "#ef6c00", // 橙
  "#00897b", // 青绿
  "#c2185b", // 玫红
];

// 边类型配色：深灰蓝为主，不同类型区分（血缘总览里 DERIVED_FROM 占绝大多数）。
const EDGE_PALETTE: Record<string, string> = {
  DERIVED_FROM: "#78909c",
  CONSUMED_BY: "#5a8dee",
};

function domainColor(domain?: string): string {
  if (!domain) return "#b0bec5";
  let h = 0;
  for (const ch of domain) h = (h * 31 + ch.charCodeAt(0)) % 9973;
  return DOMAIN_PALETTE[h % DOMAIN_PALETTE.length];
}

function edgeColor(type?: string): string {
  if (type && EDGE_PALETTE[type]) return EDGE_PALETTE[type];
  return "#90a4ae";
}

function trimLabel(label: string, max = 26): string {
  return label.length > max ? `${label.slice(0, max)}…` : label;
}

/** 图例中的节点形状示意（指标=圆 / 表=圆角矩形 / 字段=椭圆）。 */
function ShapeSwatch({ type }: { type: string }) {
  const common = { stroke: "#607d8b", fill: "none", strokeWidth: 1.3 };
  if (type === "table") {
    return (
      <svg width={12} height={10} viewBox="0 0 12 10" aria-hidden>
        <rect x={1} y={1} width={10} height={8} rx={1.5} {...common} />
      </svg>
    );
  }
  if (type === "field") {
    return (
      <svg width={14} height={8} viewBox="0 0 14 8" aria-hidden>
        <ellipse cx={7} cy={4} rx={6} ry={3} {...common} />
      </svg>
    );
  }
  return (
    <svg width={10} height={10} viewBox="0 0 10 10" aria-hidden>
      <circle cx={5} cy={5} r={4} {...common} />
    </svg>
  );
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
 * 资产地图/血缘力导向图。
 *
 * - 节点：按业务域着色（饱和深色）、按类型区分形状（指标=圆 / 表=圆角矩形 /
 *   字段=椭圆）、PII 红色描边、按血缘度编码大小；标签带白底 pill 提升可读性。
 * - 边：深灰蓝 + 弧线 + 按类型着色，避免浅灰线条在密集图中杂乱无章。
 * - 交互：拖拽画布 / 滚轮缩放 / 拖拽节点 / 悬停邻域高亮 / 点击节点回调 / 重置视图。
 * - 可读性：节点过多时按优先级限流渲染 + 提示筛选；``showFields=false`` 时隐藏字段节点。
 * - 兜底：canvas 不可用（jsdom/弱环境）时降级为表格，保证数据可浏览。
 */
export function AssetGraph({
  nodes,
  edges,
  height = 600,
  onNodeClick,
  showFields = true,
}: AssetGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<G6Graph | null>(null);
  const onNodeClickRef = useRef(onNodeClick);
  const [renderFailed, setRenderFailed] = useState(false);
  const [showAll, setShowAll] = useState(false);
  // 前端筛选：按节点类型过滤 + 按 label 搜索定位（不重新请求后端）
  const [typeFilter, setTypeFilter] = useState<string[]>([]);
  const [searchText, setSearchText] = useState("");
  onNodeClickRef.current = onNodeClick;

  // 类型筛选（空 = 全部）；showFields=false 时剔除字段节点（血缘总览降噪）
  const filteredNodes = useMemo(() => {
    let list = typeFilter.length === 0 ? nodes : nodes.filter((n) => typeFilter.includes(n.type));
    if (showFields === false) list = list.filter((n) => n.type !== "field");
    return list;
  }, [nodes, typeFilter, showFields]);

  // 限流渲染：优先保留核心节点，超出阈值时默认隐藏附属字段节点
  const {
    visible: visibleNodes,
    visibleEdges,
    hidden,
  } = useMemo(() => pickVisible(filteredNodes, edges, showAll), [filteredNodes, edges, showAll]);

  // 血缘度：节点关联边数 → 编码节点大小
  const degreeMap = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of visibleEdges) {
      m.set(e.source, (m.get(e.source) ?? 0) + 1);
      m.set(e.target, (m.get(e.target) ?? 0) + 1);
    }
    return m;
  }, [visibleEdges]);
  // 图实例仅创建一次，节点大小回调需通过 ref 读取最新度图（避免闭包捕获旧值）
  const degreeMapRef = useRef(degreeMap);
  degreeMapRef.current = degreeMap;
  // G6 图实例在首次 render() 的异步 prepare 中才初始化 context.element——
  // 在此之前调用 setElementState 会因 context.element 为 undefined 崩溃。此标志标记「图已就绪」。
  const graphReadyRef = useRef(false);

  // 图实例创建（仅一次）：销毁只发生在组件卸载。
  // 复用实例避免「每次数据变化都 destroy + 重建」——G6 d3-force 仿真在 destroy 后
  // 仍可能派发在途 tick/事件，访问已被清空的 context 会抛 `Cannot read ... of undefined (reading 'draw')`。
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let graph: G6Graph | null = null;
    try {
      graph = new G6Graph({
        container,
        autoFit: "view",
        padding: 32,
        data: { nodes: [], edges: [] },
        node: {
          // 形状按类型区分：指标=圆 / 表=圆角矩形 / 字段=椭圆
          type: (d: NodeData) => {
            const t = (d.data as AssetGraphNode | undefined)?.type;
            if (t === "table") return "rect";
            if (t === "field") return "ellipse";
            return "circle";
          },
          style: {
            size: (d: NodeData) => {
              const t = (d.data as AssetGraphNode | undefined)?.type;
              const r = Math.max(14, 12 + (degreeMapRef.current.get(String(d.id)) ?? 0) * 1.2);
              if (t === "table") return [r * 1.9, r * 1.0];
              if (t === "field") return [r * 1.3, r * 0.7];
              return r;
            },
            fill: (d: NodeData) => domainColor((d.data as AssetGraphNode | undefined)?.domain),
            stroke: (d: NodeData) =>
              (d.data as AssetGraphNode | undefined)?.pii ? "#c62828" : "#ffffff",
            lineWidth: (d: NodeData) =>
              ((d.data as AssetGraphNode | undefined)?.pii ? 3 : 1.5),
            // 投影让节点从画布上"浮起"，减少平铺感
            shadowColor: "rgba(0,0,0,0.28)",
            shadowBlur: 8,
            shadowOffsetY: 3,
            labelText: (d: NodeData) =>
              trimLabel((d.data as AssetGraphNode | undefined)?.label ?? String(d.id)),
            labelPlacement: "bottom",
            labelFill: "#263238",
            labelFontSize: 11,
            labelFontWeight: 600,
            // 标签白底 pill：在密集边与任何填充色上都清晰可读
            labelBackground: true,
            labelBackgroundFill: "rgba(255,255,255,0.86)",
            labelBackgroundRadius: 4,
            labelBackgroundPadding: [2, 5],
            cursor: "pointer",
          },
          state: {
            active: { fill: "#faad14", stroke: "#8c6d00", lineWidth: 2 },
            inactive: { opacity: 0.2 },
          },
        },
        edge: {
          style: {
            stroke: (e) => edgeColor((e.data as AssetGraphEdge | undefined)?.type),
            lineWidth: 1.3,
            strokeOpacity: 0.72,
            endArrow: true,
            radius: 10,
          },
        },
        layout: {
          type: "d3-force",
          linkDistance: 90,
          collide: { radius: 30 },
          manyBody: { strength: -380 },
        },
        behaviors: ["drag-canvas", "zoom-canvas", "drag-element"],
      });
      graphRef.current = graph;

      graph.on<IElementEvent>("node:click", (evt) => {
        if (!graph || graph.destroyed) return;
        const raw = evt.target as { id?: string; __data__?: { id?: string } } | undefined;
        const id = raw?.id ?? raw?.__data__?.id;
        if (!id) return;
        const node = graph.getNodeData(String(id))?.data as AssetGraphNode | undefined;
        if (node) onNodeClickRef.current?.(node);
      });

      // 悬停邻域高亮：相邻节点高亮，其余淡化（图销毁后的在途事件一律忽略）
      graph.on<IElementEvent>("node:pointerenter", (evt) => {
        if (!graph || graph.destroyed) return;
        const raw = evt.target as { id?: string; __data__?: { id?: string } } | undefined;
        const id = raw?.id ?? raw?.__data__?.id;
        if (!id) return;
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
        if (!graph || graph.destroyed) return;
        for (const n of graph.getNodeData()) {
          void graph.setElementState(String(n.id), []);
        }
      });
    } catch (err) {
      console.error("[AssetGraph] G6 初始化失败，降级为表格", err);
      setRenderFailed(true);
    }

    return () => {
      // 用闭包 graph（而非 graphRef.current）销毁：即使 destroy 抛异常也确保引用置空，避免留下僵尸实例
      try {
        graph?.destroy();
      } catch {
        // 销毁异常不阻断卸载
      }
      if (graphRef.current === graph) graphRef.current = null;
    };
  }, []);

  // 数据更新：复用图实例 setData + render（不再销毁重建，从根上消除竞态）
  useEffect(() => {
    const graph = graphRef.current;
    if (!graph || graph.destroyed) return;
    const markReady = () => {
      graphReadyRef.current = true;
    };
    if (visibleNodes.length === 0) {
      // 空数据：清空画布，避免残留旧图
      try {
        graph.setData({ nodes: [], edges: [] });
        void graph.render().then(markReady).catch(() => undefined);
      } catch {
        // 空数据渲染失败可忽略（保留空画布）
      }
      return;
    }
    const nodeData: GraphData = {
      nodes: visibleNodes.map((n) => ({ id: n.id, data: n })),
      edges: visibleEdges.map((e) => ({ source: e.source, target: e.target, data: e })),
    };
    try {
      graph.setData(nodeData);
      graph
        .render()
        .then(markReady)
        .catch((err: unknown) => {
          console.error("[AssetGraph] G6 render 失败，降级为表格", err);
          setRenderFailed(true);
        });
      setRenderFailed(false);
    } catch (err) {
      console.error("[AssetGraph] G6 数据更新失败，降级为表格", err);
      setRenderFailed(true);
    }
  }, [visibleNodes, visibleEdges, degreeMap]);

  // 搜索定位：匹配 label 的节点高亮 + 聚焦首个匹配；清空时恢复全量状态。
  // 图未就绪（context.element 尚未初始化）时跳过——否则 setElementState 会崩溃
  useEffect(() => {
    const graph = graphRef.current;
    if (!graph || graph.destroyed || !graphReadyRef.current) return;
    const allNodes = graph.getNodeData?.() as unknown;
    const nodeList = Array.isArray(allNodes) ? allNodes : [];
    if (!searchText.trim()) {
      for (const n of nodeList) void graph.setElementState(String(n.id), []);
      return;
    }
    const kw = searchText.trim().toLowerCase();
    const matchIds = new Set(
      visibleNodes.filter((n) => n.label.toLowerCase().includes(kw)).map((n) => n.id),
    );
    for (const n of nodeList) {
      void graph.setElementState(String(n.id), matchIds.has(String(n.id)) ? "active" : "inactive");
    }
    if (matchIds.size > 0) {
      try {
        void graph.focusElement([...matchIds][0]);
      } catch {
        // focusElement 在个别环境不可用时不阻断搜索高亮
      }
    }
  }, [searchText, visibleNodes]);

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
      <div
        style={{
          display: "flex",
          gap: 12,
          flexWrap: "wrap",
          marginBottom: 10,
          alignItems: "center",
        }}
      >
        <Select
          mode="multiple"
          allowClear
          placeholder="按类型筛选"
          style={{ minWidth: 180 }}
          value={typeFilter}
          onChange={setTypeFilter}
          options={TYPE_OPTIONS}
          maxTagCount="responsive"
          data-testid="asset-graph-type-filter"
        />
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜索节点（名称）…"
          style={{ width: 240 }}
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          data-testid="asset-graph-search"
        />
      </div>
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
        <div style={{ display: "flex", gap: 14, alignItems: "center" }}>
          <span className="muted">类型：</span>
          <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
            <ShapeSwatch type="metric" /> 指标 {typeCounts.metric ?? 0}
          </span>
          <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
            <ShapeSwatch type="table" /> 表 / 视图 {typeCounts.table ?? 0}
          </span>
          <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
            <ShapeSwatch type="field" /> 字段 {typeCounts.field ?? 0}
          </span>
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
              background: "#c62828",
              marginRight: 4,
            }}
          />
          <span className="muted">PII 描边 · 节点大小=血缘度 · 圆形/矩形/椭圆=指标/表/字段</span>
        </div>
        <Button
          size="small"
          icon={<FullscreenOutlined />}
          onClick={() => {
            const g = graphRef.current;
            if (g && !g.destroyed) g.fitView();
          }}
        >
          重置视图
        </Button>
      </div>
    </div>
  );
}
