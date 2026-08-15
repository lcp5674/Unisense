import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Empty, Input, Select, Spin, Table, Tag } from "antd";
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
  /** 布局策略：auto=检测到真环用力导向否则分层；hierarchy=分层（DAG）；force=力导向。默认 auto */
  layout?: "auto" | "hierarchy" | "force";
}

const TYPE_LABEL: Record<string, string> = {
  metric: "指标",
  table: "表 / 视图",
  field: "字段",
  unknown: "未知",
};

const TYPE_OPTIONS = Object.entries(TYPE_LABEL).map(([value, label]) => ({ value, label }));

// 业务域配色：12 色高饱和浓烈色板（Material Design 700 段 + Tailwind 600/700）。
// 选 MD/Tailwind 700 段而非 500 段的原因：500 段偏亮（如翠绿 #10b981），白字在上面对比度仅 ~2:1，
// 节点名称几乎不可读；700 段在白色画布上视觉"鲜艳浓烈"、色相饱和度极高，
// 且与文字的对比度普遍 >4:1（WCAG AA）。同时配合 labelBackground 白底 pill 兜底可读性。
const DOMAIN_PALETTE = [
  "#1976d2", // 蓝（blue 700）
  "#00897b", // 青绿（teal 700）
  "#43a047", // 绿（green 700）
  "#fb8c00", // 橙（orange 700）
  "#e53935", // 红（red 600）
  "#8e24aa", // 紫（purple 700）
  "#039be5", // 天蓝（light blue 700）
  "#d81b60", // 粉（pink 700）
  "#f57c00", // 橙黄（orange 800）
  "#3949ab", // 靛蓝（indigo 700）
  "#7b1fa2", // 深紫（purple 800）
  "#00acc1", // 青（cyan 700）
];

// 节点类型兜底色（节点 domain 缺失时使用，按类型区分保证视觉差异）
const TYPE_FALLBACK_COLOR: Record<string, string> = {
  metric: "#7b1fa2", // 紫（指标）
  table: "#1976d2", // 蓝（表/视图）
  field: "#00897b", // 青绿（字段）
  unknown: "#546e7a", // 中性灰蓝
};

// 边类型配色：偏亮深灰蓝，不同类型区分（血缘总览里 DERIVED_FROM 占绝大多数）
const EDGE_PALETTE: Record<string, string> = {
  DERIVED_FROM: "#94a3b8",
  CONSUMED_BY: "#60a5fa",
};

// 全局域-色映射表：按域首次出现顺序分配色板中的颜色。
// 在小样本（2-3 个域）下，hash 几乎必然撞色（生日问题），导致"一片同色"。
// 改用确定性分配保证"每个域得到不同颜色"（在域数 ≤ 色数时）。
// Module-level 状态，跨组件实例共享；多次渲染稳定。
const _assignedDomainColors = new Map<string, string>();
let _nextDomainColorIdx = 0;
function _allocateDomainColor(domain: string): string {
  const cached = _assignedDomainColors.get(domain);
  if (cached) return cached;
  const color = DOMAIN_PALETTE[_nextDomainColorIdx % DOMAIN_PALETTE.length];
  _assignedDomainColors.set(domain, color);
  _nextDomainColorIdx += 1;
  return color;
}

function domainColor(n?: { domain?: string; type?: string }): string {
  if (n?.domain) {
    // 确定性按出现顺序分配色（同域恒定同色，不同域不同色——在小数据集下更友好）
    return _allocateDomainColor(n.domain);
  }
  // 无 domain 时按类型 fallback，避免一片灰
  return TYPE_FALLBACK_COLOR[n?.type ?? "unknown"] ?? TYPE_FALLBACK_COLOR.unknown;
}

function edgeColor(type?: string): string {
  if (type && EDGE_PALETTE[type]) return EDGE_PALETTE[type];
  return "#94a3b8";
}

function trimLabel(label: string, max = 14): string {
  return label.length > max ? `${label.slice(0, max)}…` : label;
}

/** 图边的展示数据（在 AssetGraphEdge 之上叠加渲染语义字段）。 */
interface RenderEdge extends AssetGraphEdge {
  /** 双向边（A↔B 合并后置 true，单箭头改双箭头） */
  bidirectional?: boolean;
  /** 属于真实循环（SCC 尺寸>2）的边 */
  inCycle?: boolean;
}

/** 合并 A↔B 双向边为一条双箭头边，减少大图的视觉噪声（796/1000 边是双向边）。 */
function mergeBidirectionalEdges(edges: AssetGraphEdge[]): RenderEdge[] {
  const key = (a: string, b: string) => `${a}__${b}`;
  const seen = new Map<string, number>();
  const result: RenderEdge[] = [];
  for (const e of edges) {
    const forward = key(e.source, e.target);
    const backward = key(e.target, e.source);
    // 已存在反向边 → 把它标记为双向（两条合并为一条）
    const existingIdx = seen.get(backward);
    if (existingIdx !== undefined && !result[existingIdx].bidirectional) {
      result[existingIdx] = { ...result[existingIdx], bidirectional: true };
      continue;
    }
    seen.set(forward, result.length);
    result.push({ ...e, bidirectional: false });
  }
  return result;
}

/** Tarjan 强连通分量：返回尺寸>2 的分量（真实循环依赖，区别于双向边 2-cycle）。 */
function findTrueCycles(edges: RenderEdge[], nodeIds: string[]): Set<string> {
  const adj = new Map<string, string[]>();
  for (const id of nodeIds) adj.set(id, []);
  for (const e of edges) {
    if (adj.has(e.source)) adj.get(e.source)!.push(e.target);
  }
  const index = new Map<string, number>();
  const low = new Map<string, number>();
  const onStack = new Set<string>();
  const stack: string[] = [];
  const cycles = new Set<string>();
  let idx = 0;

  const strongconnect = (v: string): void => {
    index.set(v, idx);
    low.set(v, idx);
    idx += 1;
    stack.push(v);
    onStack.add(v);
    for (const w of adj.get(v) ?? []) {
      if (!index.has(w)) {
        strongconnect(w);
        low.set(v, Math.min(low.get(v)!, low.get(w)!));
      } else if (onStack.has(w)) {
        low.set(v, Math.min(low.get(v)!, index.get(w)!));
      }
    }
    if (low.get(v) === index.get(v)) {
      const comp: string[] = [];
      let w: string | undefined;
      do {
        w = stack.pop();
        onStack.delete(w!);
        comp.push(w!);
      } while (w !== v);
      // 尺寸>2 视为真实循环（2 节点分量是双向边，已合并处理）
      if (comp.length > 2) {
        for (const c of comp) cycles.add(c);
      }
    }
  };

  for (const id of nodeIds) {
    if (!index.has(id)) strongconnect(id);
  }
  return cycles;
}

/** 标记属于真环的边：两端都在同一个 SCC>2 分量内的边。 */
function markCycleEdges(edges: RenderEdge[], cycleNodes: Set<string>): RenderEdge[] {
  return edges.map((e) =>
    cycleNodes.has(e.source) && cycleNodes.has(e.target)
      ? { ...e, inCycle: true }
      : e,
  );
}

// G6 状态更新安全封装：图在数据重载/销毁过渡期，节点可能暂不存在——
// setElementState 会同步抛错或异步 rejection，统一吞掉避免未捕获异常污染控制台/打断交互。
function safeSetElementState(
  graph: G6Graph | null | undefined,
  id: string,
  state: string | string[],
) {
  if (!graph || graph.destroyed) return;
  try {
    void graph.setElementState(id, state).catch(() => {});
  } catch {
    // 忽略瞬时异常
  }
}

function safeFocusElement(graph: G6Graph | null | undefined, id: string) {
  if (!graph || graph.destroyed) return;
  try {
    void graph.focusElement(id).catch(() => {});
  } catch {
    // 个别环境不可用时不阻断
  }
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

/** 布局配置：分层（DAG 自上而下）或力导向（环/交互定位）。 */
function layoutConfig(layoutMode: "hierarchy" | "force") {
  if (layoutMode === "hierarchy") {
    // 分层布局：血缘 DAG 自上而下（表→指标），节点多时比力导向清晰得多
    // nodesep/ranksep 加大以容纳放节点内部的标签 + 给边留呼吸空间
    return {
      type: "antv-dagre",
      rankdir: "TB",
      align: "DL",
      nodesep: 50,
      ranksep: 72,
    };
  }
  // 力导向：环图/交互定位用（对循环依赖天然容忍，节点自然分布）
  // collide 加大避免小图密集时节点圆形互相穿透
  return {
    type: "d3-force",
    linkDistance: 110,
    collide: { radius: 48 },
    manyBody: { strength: -300 },
  };
}

interface GraphCanvasProps {
  nodes: AssetGraphNode[];
  edges: RenderEdge[];
  layoutMode: "hierarchy" | "force";
  height: number;
  searchText: string;
  onNodeClick: (node: AssetGraphNode) => void;
  /** 图渲染完成回调（父组件用于清除布局切换 loading） */
  onReady: () => void;
}

/**
 * 图渲染子组件：持有 G6 实例，独立挂载/卸载。
 *
 * 关键设计：父组件用 `key={layoutMode-tick}` 渲染本组件——**每次布局切换都卸载旧容器、
 * 挂载全新容器与全新 G6 实例**。此前在同一 DOM 容器上「destroy 旧实例 + new 新实例 + render」
 * 会触发 G6 v5 内部 bug（render promise 永不 resolve，图永久空白、切换后无反馈）。
 * 全新容器 + 全新实例彻底绕开该问题，保证布局切换后图必然重渲染。
 */
function GraphCanvas({
  nodes,
  edges,
  layoutMode,
  height,
  searchText,
  onNodeClick,
  onReady,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<G6Graph | null>(null);
  const onNodeClickRef = useRef(onNodeClick);
  onNodeClickRef.current = onNodeClick;
  const onReadyRef = useRef(onReady);
  onReadyRef.current = onReady;
  const [renderFailed, setRenderFailed] = useState(false);
  // 图是否渲染完成（state 驱动搜索高亮 effect 在重挂载后自动重跑）
  const [graphReady, setGraphReady] = useState(false);

  // 血缘度与环检测：style 回调通过 ref 读取最新值（避免闭包捕获旧值）
  const degreeMap = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of edges) {
      m.set(e.source, (m.get(e.source) ?? 0) + 1);
      m.set(e.target, (m.get(e.target) ?? 0) + 1);
    }
    return m;
  }, [edges]);
  const cycleNodes = useMemo(
    () => findTrueCycles(edges, nodes.map((n) => n.id)),
    [edges, nodes],
  );
  const degreeMapRef = useRef(degreeMap);
  degreeMapRef.current = degreeMap;
  const cycleNodesRef = useRef(cycleNodes);
  cycleNodesRef.current = cycleNodes;

  // 图实例创建：仅挂载时执行（布局切换由父组件 key 强制重挂载本组件，因此无需依赖 layoutMode）
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
              // 最小半径 24 + 血缘度缩放：节点需容纳「白底 pill + 12px 深字」的标签组合
              const r = Math.max(24, 20 + (degreeMapRef.current.get(String(d.id)) ?? 0) * 1.4);
              if (t === "table") return [r * 2.0, r * 1.2];
              if (t === "field") return [r * 1.4, r * 0.8];
              return r;
            },
            fill: (d: NodeData) => {
              const n = d.data as AssetGraphNode | undefined;
              // 环节点：橙色填充淡出提示（不覆盖域色，仅叠加暖色倾向）
              if (n && cycleNodesRef.current.has(String(d.id))) return "#ff8a80";
              return domainColor(n);
            },
            stroke: (d: NodeData) => {
              const n = d.data as AssetGraphNode | undefined;
              // 环节点：橙色粗描边（区别于 PII 红色）；PII 红色优先保留语义
              if (n && cycleNodesRef.current.has(String(d.id))) return "#e65100";
              return n?.pii ? "#c62828" : "#ffffff";
            },
            lineWidth: (d: NodeData) => {
              const n = d.data as AssetGraphNode | undefined;
              if (n && cycleNodesRef.current.has(String(d.id))) return 3.5;
              return n?.pii ? 3 : 2;
            },
            // 投影让节点从画布上"浮起"，减少平铺感；环节点用橙色投影强调
            shadowColor: (d: NodeData) =>
              cycleNodesRef.current.has(String(d.id))
                ? "rgba(230,81,0,0.5)"
                : "rgba(0,0,0,0.28)",
            shadowBlur: (d: NodeData) =>
              cycleNodesRef.current.has(String(d.id)) ? 14 : 8,
            shadowOffsetY: 3,
            labelText: (d: NodeData) =>
              trimLabel((d.data as AssetGraphNode | undefined)?.label ?? String(d.id)),
            // 标签放节点内部：彻底解决 dagre 横向同 rank 节点标签相互挤压重叠
            labelPlacement: "center",
            // 节点内「白底 pill + 深字」：绝对清晰可读，不受节点填充色深浅影响。
            // 此前去掉 pill 后即便 luminance 自适应，11px 字号 + 节点色叠加仍模糊不清。
            labelBackground: true,
            labelBackgroundFill: "#ffffff",
            labelBackgroundLineWidth: 1,
            labelBackgroundPadding: 4,
            labelFill: "#1f2937", // 深字（白底上对比度最高，不受节点色影响）
            labelFontSize: 12,
            labelFontWeight: 600,
            cursor: "pointer",
          },
          state: {
            active: { fill: "#faad14", stroke: "#8c6d00", lineWidth: 2 },
            inactive: { opacity: 0.2 },
          },
        },
        edge: {
          style: {
            stroke: (e) =>
              (e.data as RenderEdge | undefined)?.inCycle
                ? "#e53935" // 真环：红色虚线醒目提示
                : edgeColor((e.data as AssetGraphEdge | undefined)?.type),
            lineWidth: (e) => ((e.data as RenderEdge | undefined)?.inCycle ? 2.4 : 1.3),
            strokeOpacity: (e) =>
              (e.data as RenderEdge | undefined)?.inCycle ? 1 : 0.72,
            lineDash: (e) => ((e.data as RenderEdge | undefined)?.inCycle ? [6, 4] : undefined),
            endArrow: true,
            startArrow: (e) => ((e.data as RenderEdge | undefined)?.bidirectional ? true : false),
            radius: 10,
          },
        },
        layout: layoutConfig(layoutMode),
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

      // 悬停邻域高亮：相邻节点高亮，其余淡化（图销毁/渲染过渡期的在途事件一律忽略）
      graph.on<IElementEvent>("node:pointerenter", (evt) => {
        if (!graph || graph.destroyed || !graphReady) return;
        const raw = evt.target as { id?: string; __data__?: { id?: string } } | undefined;
        const id = raw?.id ?? raw?.__data__?.id;
        if (!id) return;
        try {
          const neighbors = graph.getNeighborNodesData(String(id));
          const active = new Set<string>([String(id), ...neighbors.map((n) => String(n.id))]);
          for (const n of graph.getNodeData()) {
            safeSetElementState(
              graph,
              String(n.id),
              active.has(String(n.id)) ? "active" : "inactive",
            );
          }
        } catch {
          // 高亮为装饰性交互，过渡期失败静默忽略
        }
      });
      graph.on("node:pointerleave", () => {
        if (!graph || graph.destroyed || !graphReady) return;
        try {
          for (const n of graph.getNodeData()) {
            safeSetElementState(graph, String(n.id), []);
          }
        } catch {
          // 忽略过渡期状态清理失败
        }
      });
    } catch (err) {
      console.error("[AssetGraph] G6 初始化失败，降级为表格", err);
      setRenderFailed(true);
      onReadyRef.current();
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
    // 布局切换由父组件 key 强制重挂载，实例只随挂载/卸载创建销毁
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 数据渲染：挂载后首次 + 数据变化时复用实例 setData+render
  useEffect(() => {
    const graph = graphRef.current;
    if (!graph || graph.destroyed) return;
    setGraphReady(false);
    const data: GraphData =
      nodes.length === 0
        ? { nodes: [], edges: [] }
        : {
            nodes: nodes.map((n) => ({ id: n.id, data: n })),
            edges: edges.map((e) => ({ source: e.source, target: e.target, data: e })),
          };
    graph.setData(data);
    graph
      .render()
      .then(() => {
        if (graph.destroyed) return;
        setGraphReady(true);
        onReadyRef.current();
        // 图就绪后强制 fitView（应对布局切换后位置变化），让图充满画布
        try {
          graph.fitView({ when: "always" });
        } catch {
          /* fitView 偶尔在过渡期失败 */
        }
      })
      .catch((err) => {
        console.error("[AssetGraph] G6 render 失败，降级为表格", err);
        setRenderFailed(true);
        onReadyRef.current();
      });
    setRenderFailed(false);
  }, [nodes, edges]);

  // 搜索定位：匹配 label 的节点高亮 + 聚焦首个匹配；清空时恢复全量状态。
  // graphReady 变化（含重挂载后重新渲染完成）时自动重跑，保证布局切换后搜索仍生效。
  useEffect(() => {
    const graph = graphRef.current;
    if (!graph || graph.destroyed || !graphReady) return;
    try {
      const allNodes = graph.getNodeData?.() as unknown;
      const nodeList = Array.isArray(allNodes) ? allNodes : [];
      if (!searchText.trim()) {
        for (const n of nodeList) safeSetElementState(graph, String(n.id), []);
        return;
      }
      const kw = searchText.trim().toLowerCase();
      const matchIds = new Set(
        nodes.filter((n) => n.label.toLowerCase().includes(kw)).map((n) => n.id),
      );
      for (const n of nodeList) {
        safeSetElementState(
          graph,
          String(n.id),
          matchIds.has(String(n.id)) ? "active" : "inactive",
        );
      }
      if (matchIds.size > 0) {
        safeFocusElement(graph, [...matchIds][0]);
      }
    } catch {
      // 图状态变化导致的瞬时异常（如数据重载中）静默忽略，避免崩溃
    }
  }, [searchText, graphReady, nodes]);

  // G6 内部 setData 的异步 batch 在数据过渡期可能抛 `Node not found` 未处理 rejection——
  // 这是库在节点被替换时的已知瞬时噪音，不影响渲染结果，作用域内抑制以免污染控制台。
  useEffect(() => {
    const handler = (e: PromiseRejectionEvent) => {
      const reason = e.reason instanceof Error ? e.reason.message : String(e.reason);
      if (reason.includes("Node not found")) e.preventDefault();
    };
    window.addEventListener("unhandledrejection", handler);
    return () => window.removeEventListener("unhandledrejection", handler);
  }, []);

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

  return (
    <div style={{ position: "relative", width: "100%" }} data-testid="asset-graph-wrap">
      <div ref={containerRef} style={{ height, width: "100%" }} data-testid="asset-graph-canvas" />
      <div style={{ marginTop: 6, textAlign: "right" }}>
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

/**
 * 资产地图/血缘图。
 *
 * - 节点：按业务域着色（饱和深色）、按类型区分形状（指标=圆 / 表=圆角矩形 /
 *   字段=椭圆）、PII 红色描边、按血缘度编码大小；标签放节点内白底 pill 提升可读性。
 * - 边：深灰蓝 + 弧线 + 按类型着色，避免浅灰线条在密集图中杂乱无章。
 * - 交互：拖拽画布 / 滚轮缩放 / 拖拽节点 / 悬停邻域高亮 / 点击节点回调 / 重置视图。
 * - 布局切换：GraphCanvas 用 key 强制重挂载（全新容器+实例），根治 G6 同容器重建空白 bug。
 * - 可读性：节点过多时按优先级限流渲染 + 提示筛选；``showFields=false`` 时隐藏字段节点。
 * - 兜底：canvas 不可用（jsdom/弱环境）时降级为表格，保证数据可浏览。
 */
export function AssetGraph({
  nodes,
  edges,
  height = 600,
  onNodeClick,
  showFields = true,
  layout = "auto",
}: AssetGraphProps) {
  const onNodeClickRef = useRef(onNodeClick);
  onNodeClickRef.current = onNodeClick;
  const [showAll, setShowAll] = useState(false);
  // 前端筛选：按节点类型过滤 + 按 label 搜索定位（不重新请求后端）
  const [typeFilter, setTypeFilter] = useState<string[]>([]);
  const [searchText, setSearchText] = useState("");
  // 布局手动覆盖：undefined=跟随 auto 检测；否则强制指定
  const [layoutOverride, setLayoutOverride] = useState<"hierarchy" | "force" | undefined>(undefined);
  // 布局代际计数器：用户每次点 Select 就 +1（即使选了同一项，也强制重挂载 GraphCanvas）。
  // 解决"hierarchy 默认下用户点了一次 分层布局 没反应"的问题。
  const [layoutTick, setLayoutTick] = useState(0);
  // 布局切换瞬态标记：用户点击切换 Select 后立即置 true，GraphCanvas 重挂载并完成 render 后置 false，
  // 期间叠加 Spin 让用户明确感知到"正在重新计算布局"，避免在节点多时误以为"点了没反应"
  const [layoutSwitching, setLayoutSwitching] = useState(false);

  // 布局切换处理：更新 override + 触发代际 + 显示 loading（GraphCanvas onReady 后自动清除）
  const handleLayoutChange = (v: "hierarchy" | "force" | undefined) => {
    setLayoutOverride(v);
    setLayoutTick((t) => t + 1);
    setLayoutSwitching(true);
  };

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

  // 环检测 + 双向边合并：A↔B 合并为双箭头减少视觉噪声；SCC>2 的真环单独标记
  const mergedEdges = useMemo(() => mergeBidirectionalEdges(visibleEdges), [visibleEdges]);
  const cycleNodes = useMemo(
    () => findTrueCycles(mergedEdges, visibleNodes.map((n) => n.id)),
    [mergedEdges, visibleNodes],
  );
  const renderEdges = useMemo(
    () => markCycleEdges(mergedEdges, cycleNodes),
    [mergedEdges, cycleNodes],
  );
  // 布局策略：auto=有真环用力导向（环图 dagre 渲染异常），无环用分层；手动覆盖优先
  const layoutMode = useMemo<"hierarchy" | "force">(() => {
    if (layoutOverride) return layoutOverride;
    if (layout === "force") return "force";
    if (layout === "hierarchy") return "hierarchy";
    return cycleNodes.size > 0 ? "force" : "hierarchy";
  }, [layout, layoutOverride, cycleNodes]);

  if (nodes.length === 0) {
    return <Empty description="暂无图谱数据" />;
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
      {cycleNodes.size > 0 && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "6px 12px",
            marginBottom: 8,
            background: "rgba(229,57,53,0.08)",
            border: "1px solid rgba(229,57,53,0.35)",
            borderRadius: 6,
            fontSize: 13,
            color: "#b71c1c",
          }}
          data-testid="asset-graph-cycle-banner"
        >
          <span>
            检测到 <b>{cycleNodes.size}</b> 个节点存在<b>循环依赖</b>。
            <b style={{ color: "#e65100" }}> 橙色描边</b>为环节点、
            <b style={{ color: "#e53935" }}> 红色虚线</b>为环边（见下图例）。
            这通常是 ETL 回流或配置错误，请检查相关表的加工链。
          </span>
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
        <Select
          allowClear
          placeholder="布局：自动"
          style={{ minWidth: 130 }}
          value={layoutOverride}
          onChange={handleLayoutChange}
          data-testid="asset-graph-layout"
          options={[
            { value: "hierarchy", label: "分层布局" },
            { value: "force", label: "力导向布局" },
          ]}
        />
      </div>
      <div style={{ position: "relative", width: "100%" }}>
        <GraphCanvas
          key={`${layoutMode}-${layoutTick}`}
          nodes={visibleNodes}
          edges={renderEdges}
          layoutMode={layoutMode}
          height={height}
          searchText={searchText}
          onNodeClick={(n) => onNodeClickRef.current?.(n)}
          onReady={() => setLayoutSwitching(false)}
        />
        {layoutSwitching && (
          <div
            style={{
              position: "absolute",
              inset: 0,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              background: "rgba(255,255,255,0.55)",
              backdropFilter: "blur(2px)",
              pointerEvents: "none",
              transition: "opacity 0.2s",
            }}
            data-testid="asset-graph-switching"
          >
            <Spin tip="正在重新计算布局…" />
          </div>
        )}
      </div>
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
                  background: domainColor({ domain: d }),
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
        <div>
          <span
            style={{
              display: "inline-block",
              width: 10,
              height: 10,
              borderRadius: "50%",
              border: "3px solid #e65100",
              background: "#ff8a80",
              marginRight: 4,
              verticalAlign: "middle",
            }}
          />
          <span className="muted" style={{ color: "#e65100" }}>
            环节点（橙色描边）
          </span>
        </div>
        <div>
          <span
            style={{
              display: "inline-block",
              width: 18,
              height: 0,
              borderTop: "2px dashed #e53935",
              marginRight: 4,
              verticalAlign: "middle",
            }}
          />
          <span className="muted" style={{ color: "#b71c1c" }}>
            环边（红色虚线）
          </span>
        </div>
      </div>
    </div>
  );
}
