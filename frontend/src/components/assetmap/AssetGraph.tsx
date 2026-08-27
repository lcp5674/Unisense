import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Button, Empty, Input, Select, Spin, Table, Tag } from "antd";
import { FullscreenOutlined, FullscreenExitOutlined, SearchOutlined } from "@ant-design/icons";
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
  /** 语义泳道的隐藏锚点节点（渲染为不可见、不响应交互） */
  anchor?: boolean;
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
  /** 布局策略：auto=检测到真环用力导向否则分层；hierarchy=分层（DAG）；force=力导向；radial=血缘度同心圆（依赖引用数高者居中）。默认 auto */
  layout?: "auto" | "hierarchy" | "force" | "radial";
  /**
   * 语义泳道：把节点按类型锚进三条语义带（表带在上、指标带中、字段带下，与血缘方向一致——
   * 表→指标→字段 的数据流），通过「隐藏锚点 + 锚定边」让 dagre 分层自然聚带；
   * 开启后即使检测到真环也强制分层（dagre acyclic 翻转环边 + 环标记），不再整图回退力导向。
   * 默认 false（保持既有行为）；血缘图谱/资产地图/影响分析等大图建议开启。
   */
  lanes?: boolean;
  /** 是否显示数仓分层徽标（表节点按 ods_/dwd_/dws_/ads_/dm_ 前缀描边着色）。默认 true */
  layerBadges?: boolean;
  /** 是否提供「全屏」按钮（图谱全屏展示）。默认 true */
  fullscreenable?: boolean;
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
  BASED_ON: "#a78bfa",
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

function trimLabel(label: string, max = 40): string {
  // 底部标签要"完整展示"，仅对极长名称（>40 字符）截断，一般血缘节点名均完整显示
  return label.length > max ? `${label.slice(0, max)}…` : label;
}

/**
 * 自适应节点基准半径：按图规模动态缩放。
 * 聚焦视图（如从指标目录跳转 ?node= 只看 1-3 个节点的上下游）节点少，
 * 若仍用全景的大半径（24）会显得图标硕大突兀；节点越少半径越小，越多越大。
 * 同时保留血缘度缩放，且下限 14 保证「中央图标 + 底部完整标签」仍可容纳。
 */
export function adaptiveBaseRadius(nodeCount: number): number {
  if (nodeCount <= 3) return 16; // 聚焦视图：精致小节点
  if (nodeCount <= 10) return 18;
  if (nodeCount <= 30) return 20;
  return 24; // 全景大图：维持可读性优先
}

// 颜色提亮：给定 hex 色，向白色方向提亮 amt（0-255），用于渐变高光
function lightenHex(hex: string, amt: number): string {
  const n = parseInt(hex.slice(1), 16);
  if (Number.isNaN(n)) return hex;
  const r = Math.min(255, ((n >> 16) & 255) + amt);
  const g = Math.min(255, ((n >> 8) & 255) + amt);
  const b = Math.min(255, (n & 255) + amt);
  return `#${((1 << 24) | (r << 16) | (g << 8) | b).toString(16).slice(1)}`;
}

// 节点类型图标（emoji 渲染在节点中心，标签在节点下方，互不干扰）
function nodeIconText(type?: string): string {
  if (type === "table") return "🗂️";
  if (type === "field") return "🔖";
  return "📈"; // metric
}

/** 图边的展示数据（在 AssetGraphEdge 之上叠加渲染语义字段）。 */
interface RenderEdge extends AssetGraphEdge {
  /** 双向边（A↔B 合并后置 true，单箭头改双箭头） */
  bidirectional?: boolean;
  /** 属于真实循环（SCC 尺寸>2）的边 */
  inCycle?: boolean;
  /** 语义泳道的隐藏锚定边（锚点间连线 / 锚点→真实节点挂载边，渲染时不可见） */
  anchorEdge?: boolean;
}

// —— 数仓分层徽标 ——
// 表节点按命名前缀推断数仓分层（ods→dwd→dws→ads），加工链的"层级"通过描边色一眼可见。
// 优先级低于 PII（红）与环（橙）：仅当两者都不命中时用层色描边。
const LAYER_STROKE: Record<string, string> = {
  ods: "#2e7d32", // 操作数据层（绿）
  dwd: "#1565c0", // 明细数据层（蓝）
  dws: "#6a1b9a", // 汇总数据层（紫）
  ads: "#ef6c00", // 应用数据层（橙）
  dm: "#00695c", // 数据集市（青）
};

/** 推断节点数仓分层：表按名称前缀，指标按 dw_layer 属性（后端可选返回）。 */
export function layerOf(n: AssetGraphNode): string | null {
  if (n.type === "table") {
    const name = (n.label || n.id).toLowerCase();
    for (const layer of Object.keys(LAYER_STROKE)) {
      if (name.startsWith(`${layer}_`) || name.startsWith(`${layer}.`)) return layer;
    }
    return null;
  }
  if (n.type === "metric") {
    const l = (n as { dw_layer?: unknown }).dw_layer;
    if (typeof l === "string" && LAYER_STROKE[l]) return l;
    return null;
  }
  return null;
}

/**
 * 数仓分层泳道：为 dagre 分层插入隐藏锚点节点与锚定边，把节点按「数仓分层 + 语义带」
 * 聚进多条泳道（血缘方向：ODS → DWD → DWS → ADS/DM → 其他表 → 指标 → 字段）。
 * - 表节点按其名称前缀/指标 dw_layer 推断分层（复用 layerOf）；未识别层级的表归入
 *   ``table`` 带（放在应用层之下、指标之上，避免未分层表打散加工链）；
 * - 锚点链按血缘方向连锚定边，dagre 自然把源（ODS）放最上、字段放最下；
 * - 每类真实节点经「锚 → 节点」挂载边锚到对应泳道（同层带内仍按血缘边纵向分层，
 *   表→表加工链在层带内保留）；
 * - other/unknown 类型不挂锚（自由参与分层，如上游依赖图的中心节点保持在最上方）。
 * 锚点与锚定边由 GraphCanvas 按 anchor/anchorEdge 标记渲染为不可见、不响应交互。
 */
export function applyLanes(
  nodes: AssetGraphNode[],
  edges: RenderEdge[],
): { nodes: AssetGraphNode[]; edges: RenderEdge[] } {
  // 泳道顺序与血缘方向一致：数仓分层（源头→应用）→ 其他表 → 指标 → 字段
  const order = ["ods", "dwd", "dws", "ads", "dm", "table", "metric", "field"] as const;
  const laneOf = (n: AssetGraphNode): string => {
    if (n.type === "table") {
      const l = layerOf(n);
      if (l) return l;
      return "table";
    }
    if (n.type === "metric") return "metric";
    if (n.type === "field" || String(n.type).indexOf("column") === 0) return "field";
    return "";
  };
  const present = order.filter((l) => nodes.some((n) => laneOf(n) === l));
  // 少于两类时泳道无意义（单类型带内 dagre 已天然分层），直接透传
  if (present.length < 2) return { nodes, edges };

  const anchorIds = present.map((t) => `__lane_${t}__`);
  const anchorNodes: AssetGraphNode[] = present.map((_, i) => ({
    id: anchorIds[i],
    type: "anchor",
    label: "",
    anchor: true,
  }));
  const anchorEdges: RenderEdge[] = [];
  // 锚点链：ODS锚 → DWD锚 → DWS锚 → ADS锚 → DM锚 → 表锚 → 指标锚 → 字段锚（dagre 强制泳道顺序）
  for (let i = 0; i < present.length - 1; i += 1) {
    anchorEdges.push({
      source: anchorIds[i],
      target: anchorIds[i + 1],
      type: "ANCHOR",
      anchorEdge: true,
    });
  }
  // 挂载边：锚 → 该泳道的每个真实节点
  for (const t of present) {
    const anchorId = `__lane_${t}__`;
    for (const n of nodes) {
      if (laneOf(n) === t) {
        anchorEdges.push({ source: anchorId, target: n.id, type: "ANCHOR", anchorEdge: true });
      }
    }
  }
  return { nodes: [...anchorNodes, ...nodes], edges: [...anchorEdges, ...edges] };
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

/**
 * 力导向大图边降采样：节点很多时力导向会因边交叉成网而看不清引用关系。
 * 按「两端血缘度和」降序保留前 MAX_FORCE_DENSE_EDGES 条——优先保留连接枢纽/骨干节点的边，
 * 叶子间低价值边被折叠；被折叠边仍可通过悬停节点邻域高亮临时显现（邻域高亮不受此限）。
 */
const MAX_FORCE_DENSE_EDGES = 600;
function filterDenseForceEdges(
  edges: RenderEdge[],
  degreeMap: Map<string, number>,
  forceDense: boolean,
): RenderEdge[] {
  if (!forceDense || edges.length <= MAX_FORCE_DENSE_EDGES) return edges;
  const weighted = edges.map((e) => ({
    e,
    w: (degreeMap.get(e.source) ?? 0) + (degreeMap.get(e.target) ?? 0),
  }));
  weighted.sort((a, b) => b.w - a.w);
  return weighted.slice(0, MAX_FORCE_DENSE_EDGES).map((x) => x.e);
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

// —— 大数据量 LOD（Level of Detail）——
// G6 交互（缩放/平移）时全量重绘 canvas，标签/图标/柔光/投影是最重的绘制层。
// 缩放低于 LOD_COMPACT_ZOOM 时批量切到 compact 状态（隐藏这些层，只留节点主体+边），
// 放大自动恢复；节点数超过 LOD_LARGE_GRAPH 时初始即 compact，减轻首帧负担。
const LOD_COMPACT_ZOOM = 0.6;
const LOD_LARGE_GRAPH = 200;

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

/** 布局配置：分层（DAG 自上而下）｜力导向（环/交互定位）｜血缘度径向（同心圆，依赖引用数高者居中）。 */
function layoutConfig(layoutMode: "hierarchy" | "force" | "radial") {
  if (layoutMode === "hierarchy") {
    // 分层布局：血缘 DAG 自上而下（表→指标），节点多时比力导向清晰得多
    // nodesep/ranksep 大幅加大以容纳「节点下方的完整标签 pill」，避免同 rank 标签互相压字
    return {
      type: "antv-dagre",
      rankdir: "TB",
      align: "DL",
      nodesep: 110,
      ranksep: 100,
    };
  }
  if (layoutMode === "radial") {
    // 血缘度径向布局（同心圆）：按依赖引用数（degree）排序——引用数高的枢纽节点居中，
    // 低引用数的叶子节点排外围，环间等距、防重叠，让「被谁引用 / 引用谁」的层次关系一目了然。
    // 这是大数据量下比纯力导向清晰得多的替代方案（力导向在几百节点时节点重叠、边交叉成网）。
    return {
      type: "concentric",
      sortBy: "degree",
      preventOverlap: true,
      equidistant: true,
      clockwise: true,
      maxLevelDiff: 10,
    };
  }
  // 力导向：环图/交互定位用（对循环依赖天然容忍，节点自然分布）
  // collide/linkDistance 加大避免底部标签被相邻节点压住
  // 收敛加速：@antv/layout 的 D3ForceLayout.layout() 等待 simulation 'end' 事件
  //（alpha 降到 alphaMin），默认 alphaDecay≈0.0228 / alphaMin=0.001 需约 300 次 tick 才收敛。
  // 调大 alphaDecay + 提高 alphaMin 后约 50 次 tick 触发 'end'，布局切换提速约 5 倍，
  // 力导向是近似布局，视觉差异极小。
  return {
    type: "d3-force",
    linkDistance: 150,
    collide: { radius: 64 },
    manyBody: { strength: -260 },
    alphaDecay: 0.08,
    alphaMin: 0.05,
  };
}

interface GraphCanvasProps {
  nodes: AssetGraphNode[];
  edges: RenderEdge[];
  layoutMode: "hierarchy" | "force" | "radial";
  height: number;
  searchText: string;
  onNodeClick: (node: AssetGraphNode) => void;
  /** 图渲染完成回调（父组件用于清除布局切换 loading） */
  onReady: () => void;
  /** 是否启用数仓分层徽标描边（非 PII 非环时按表名前缀用层色描边） */
  layerBadges?: boolean;
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
  layerBadges = true,
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
  // 依赖引用数拆分：inDegreeMap =「被多少下游引用」（目标端），outDegreeMap =「依赖多少上游」
  //（源端）。血缘边方向 source→target（source 是上游/被依赖方）。badge 显示总血缘度，
  // tooltip 细分「依赖 N 项（上游）/ 被 M 项引用（下游）」，便于用户理解引用关系。
  const inDegreeMap = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of edges) m.set(e.target, (m.get(e.target) ?? 0) + 1);
    return m;
  }, [edges]);
  const outDegreeMap = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of edges) m.set(e.source, (m.get(e.source) ?? 0) + 1);
    return m;
  }, [edges]);
  const inDegreeMapRef = useRef(inDegreeMap);
  inDegreeMapRef.current = inDegreeMap;
  const outDegreeMapRef = useRef(outDegreeMap);
  outDegreeMapRef.current = outDegreeMap;
  const cycleNodes = useMemo(
    () => findTrueCycles(edges, nodes.map((n) => n.id)),
    [edges, nodes],
  );
  const degreeMapRef = useRef(degreeMap);
  degreeMapRef.current = degreeMap;
  const cycleNodesRef = useRef(cycleNodes);
  cycleNodesRef.current = cycleNodes;
  // 数仓分层映射：节点 id → 层名（layerBadges 关闭时为全空）。style 回调经 ref 读取，
  // 使非 PII/环节点按表名前缀用层色描边，加工链层级一眼可见。
  const layerMap = useMemo(() => {
    const m = new Map<string, string>();
    if (layerBadges) {
      for (const n of nodes) {
        const l = layerOf(n);
        if (l) m.set(n.id, l);
      }
    }
    return m;
  }, [nodes, layerBadges]);
  const layerMapRef = useRef(layerMap);
  layerMapRef.current = layerMap;
  // 节点总数：size 回调（G6 style 函数，render 时求值）经 ref 读取最新值，
  // 使聚焦/清除切换（nodes 变化但组件不重挂载时）节点大小随规模自适应
  const nodeCountRef = useRef(nodes.length);
  nodeCountRef.current = nodes.length;

  // —— 节点样式预计算缓存（性能优化）——
  // 原实现：size/fill/stroke/lineWidth/shadow/halo/icon 均为 G6 每帧求值的函数式回调，
  // 大图（160+ 节点）缩放/平移时每帧重算全部样式（含 domainColor/lightenHex 等较重计算），
  // 是 rAF 卡顿的重要来源。此处用 useMemo 预计算为 id → style 的 Map，style 回调改为 O(1) 查表；
  // degreeMap/cycleNodes/layerMap 变化时自动重建。
  const nodeStyleCache = useMemo(() => {
    const base = adaptiveBaseRadius(nodeCountRef.current);
    const map = new Map<string, Record<string, unknown>>();
    for (const n of nodes) {
      const id = String(n.id);
      const t = n?.type;
      const cyc = cycleNodesRef.current.has(id);
      const layer = layerMapRef.current.get(id);
      const d = degreeMapRef.current.get(id) ?? 0;
      // 血缘度缩放（与渲染一致性）：原始 r = max(base, 12 + degree*1.2)，表*2.0/字段*1.3
      const r = Math.max(base, 12 + d * 1.2);
      const size =
        t === "table" ? [r * 2.0, r * 1.2] : t === "field" ? [r * 1.3, r * 0.7] : r;
      const fill = cyc
        ? "#ff8a80"
        : n?.domain
          ? _allocateDomainColor(n.domain)
          : TYPE_FALLBACK_COLOR[n?.type ?? "unknown"] ?? TYPE_FALLBACK_COLOR.unknown;
      const stroke = cyc
        ? "#e65100"
        : n?.pii
          ? "#c62828"
          : layer
            ? (LAYER_STROKE[layer] ?? "#ffffff")
            : "#ffffff";
      const lineWidth = cyc ? 3.5 : n?.pii ? 3 : layer ? 2.5 : 2;
      map.set(id, { size, fill, stroke, lineWidth });
    }
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, degreeMap, cycleNodes, layerMap]);
  const nodeStyleCacheRef = useRef(nodeStyleCache);
  nodeStyleCacheRef.current = nodeStyleCache;

  // —— 大数据量 LOD 状态 ——
  // compactRef：是否处于紧凑模式（隐藏标签/图标/柔光/投影）；大数据量初始即紧凑。
  // lodAppliedRef：已按当前 compact 应用过状态，避免 wheel 高频事件重复 setElementState。
  const compactRef = useRef(nodes.length > LOD_LARGE_GRAPH);
  const lodAppliedRef = useRef(false);

  /** 合并 compact 状态：hover/搜索高亮时保留紧凑模式（否则 setElementState 覆盖会丢失）。 */
  function stateWithCompact(extra: string | string[]) {
    if (!compactRef.current) return extra;
    const arr = Array.isArray(extra) ? extra : [extra];
    return ["compact", ...arr];
  }

  /** 按当前缩放批量切换 compact 状态（跨过阈值才触发，rAF 节流高频 wheel）。 */
  let lodRaf = 0;
  function applyLod() {
    if (lodRaf) return; // 已有待执行帧，忽略本次（rAF 节流）
    lodRaf = requestAnimationFrame(() => {
      lodRaf = 0;
      const graph = graphRef.current;
      if (!graph || graph.destroyed) return;
      // getZoom 缺失时（降级/测试环境）按非紧凑处理，避免抛错触发降级
      const compact =
        typeof graph.getZoom === "function" && graph.getZoom() < LOD_COMPACT_ZOOM;
      if (compact === lodAppliedRef.current) return;
      lodAppliedRef.current = compact;
      compactRef.current = compact;
      const ids = graph.getNodeData().map((n) => String(n.id));
      if (ids.length === 0) return;
      const record: Record<string, string | string[]> = {};
      for (const id of ids) record[id] = compact ? "compact" : [];
      try {
        void graph.setElementState(record, false).catch(() => {});
      } catch {
        // 图过渡期瞬时异常静默忽略
      }
    });
  }

  // 图实例创建：仅挂载时执行（布局切换由父组件 key 强制重挂载本组件，因此无需依赖 layoutMode）
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let graph: G6Graph | null = null;
    try {
      graph = new G6Graph({
        container,
        // autoFit 仅居中不缩放，缩放由 render 完成后的手动 fitView 按节点规模自适应控制：
        //  - 聚焦场景（?node= 跳转 1-3 节点）→ fitView overflow：不放大，按自然尺寸显示
        //    （'view' always 会把少量节点放大填满画布，节点硕大突兀——用户反馈的根因）；
        //  - 全景大图（节点多）→ fitView always：适配填满画布，节点分布均匀。
        autoFit: "center",
        padding: 32,
        // 大数据量性能优化：
        //  - animation:false —— 关闭全局动画，缩放/平移/状态切换不再插帧，交互直接重绘最终帧；
        //  - zoomRange 收窄 —— 限制最小缩放 0.15（避免缩到极小仍全量渲染细节）与最大 8。
        //  配合下方 compact LOD 状态（缩放低于阈值时隐藏标签/图标/柔光/投影），大幅提升交互帧率。
        animation: false,
        zoomRange: [0.15, 8],
        data: { nodes: [], edges: [] },
        node: {
          // 形状按类型区分：指标=圆 / 表=圆角矩形 / 字段=椭圆
          type: (d: NodeData) => {
            const n = d.data as AssetGraphNode | undefined;
            if (n?.anchor) return "circle"; // 泳道锚点：极小圆形，不可见
            const t = n?.type;
            if (t === "table") return "rect";
            if (t === "field") return "ellipse";
            return "circle";
          },
          style: {
            size: (d: NodeData) => {
              const n = d.data as AssetGraphNode | undefined;
              if (n?.anchor) return 1; // 锚点尺寸极小，不撑大 dagre 层间距
              // 预计算缓存查表（性能优化：避免每帧重算血缘度/域色/分层）
              return (nodeStyleCacheRef.current.get(String(d.id))?.size as
                | number
                | [number, number]) ?? 12;
            },
            // 表节点圆角矩形（radius 仅对 rect 生效，circle/ellipse 自动忽略）
            radius: 8,
            fill: (d: NodeData) => {
              const n = d.data as AssetGraphNode | undefined;
              if (n?.anchor) return "transparent"; // 泳道锚点不可见
              return (nodeStyleCacheRef.current.get(String(d.id))?.fill as string) ?? "#94a3b8";
            },
            stroke: (d: NodeData) => {
              const n = d.data as AssetGraphNode | undefined;
              if (n?.anchor) return "transparent";
              return (nodeStyleCacheRef.current.get(String(d.id))?.stroke as string) ?? "#ffffff";
            },
            lineWidth: (d: NodeData) => {
              const n = d.data as AssetGraphNode | undefined;
              if (n?.anchor) return 0;
              return (nodeStyleCacheRef.current.get(String(d.id))?.lineWidth as number) ?? 1.5;
            },
            // 投影让节点从画布上"浮起"，减少平铺感；环节点用橙色投影强调
            shadowColor: (d: NodeData) =>
              cycleNodesRef.current.has(String(d.id))
                ? "rgba(230,81,0,0.5)"
                : "rgba(0,0,0,0.28)",
            shadowBlur: (d: NodeData) =>
              (d.data as AssetGraphNode | undefined)?.anchor || cycleNodesRef.current.has(String(d.id))
                ? 0
                : 8,
            shadowOffsetY: (d: NodeData) =>
              (d.data as AssetGraphNode | undefined)?.anchor ? 0 : 3,
            // 柔光 halo：节点填充色提亮版作为外圈，让节点从画布上"发光"、更立体
            halo: (d: NodeData) => !(d.data as AssetGraphNode | undefined)?.anchor,
            haloStroke: (d: NodeData) => {
              const n = d.data as AssetGraphNode | undefined;
              const base = cycleNodesRef.current.has(String(d.id)) ? "#ff8a80" : domainColor(n);
              return lightenHex(base, 90);
            },
            haloLineWidth: 8,
            haloStrokeOpacity: 0.4,
            // 类型图标：指标 📈 / 表 🗂️ / 字段 🔖，渲染在节点中央
            icon: (d: NodeData) => !(d.data as AssetGraphNode | undefined)?.anchor,
            iconText: (d: NodeData) =>
              nodeIconText((d.data as AssetGraphNode | undefined)?.type),
            iconFontSize: (d: NodeData) =>
              (d.data as AssetGraphNode | undefined)?.type === "field" ? 12 : 16,
            iconFill: "#ffffff",
            // 依赖引用数角标（血缘度 badge）：节点右上角显示该节点被引用的次数，
            // 用户一眼看出哪些是枢纽节点（高血缘度）。compact LOD 模式下隐藏（大图性能）。
            badge: (d: NodeData) => !(d.data as AssetGraphNode | undefined)?.anchor,
            badgeText: (d: NodeData) => {
              const deg = degreeMapRef.current.get(String(d.id)) ?? 0;
              return deg > 0 ? String(deg) : "";
            },
            badgePosition: "right-top",
            badgeFill: (d: NodeData) =>
              (degreeMapRef.current.get(String(d.id)) ?? 0) >= 5
                ? "#e65100" // 高血缘度（≥5）：橙色醒目，标识枢纽
                : (degreeMapRef.current.get(String(d.id)) ?? 0) >= 2
                  ? "#1a73e8" // 中血缘度：蓝
                  : "#546e7a", // 低血缘度：灰蓝
            badgeFontSize: 10,
            badgeTextFill: "#ffffff",
            badgePadding: [2, 4],
            badgeOpacity: 0.95,
            // 标签放节点下方（用户明确要求——放节点中心会遮挡图标且与图例形状语义冲突）。
            // 配合 layoutConfig 加大 nodesep/ranksep/collide 间距，保证底部完整标签不互相压字。
            labelText: (d: NodeData) => {
              const n = d.data as AssetGraphNode | undefined;
              if (n?.anchor) return ""; // 锚点无标签
              return trimLabel(n?.label ?? String(d.id));
            },
            labelPlacement: "bottom",
            labelOffset: 10,
            // 节点下方「白底 pill + 深字」：完整展示节点名，绝对清晰可读，不受节点填充色深浅影响。
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
            // 大数据量 LOD：缩放低于阈值（applyLod）时批量置为 compact——
            // 隐藏标签/图标/柔光/投影/badge，只保留节点主体与边，显著降低 canvas 重绘开销。
            compact: {
              labelOpacity: 0,
              iconOpacity: 0,
              haloStrokeOpacity: 0,
              haloOpacity: 0,
              shadowBlur: 0,
              shadowOffsetY: 0,
              badgeOpacity: 0,
            },
          },
        },
        edge: {
          style: {
            stroke: (e) => {
              const d = e.data as RenderEdge | undefined;
              if (d?.anchorEdge) return "transparent"; // 泳道锚定边不可见
              return d?.inCycle
                ? "#e53935" // 真环：红色虚线醒目提示
                : edgeColor(d?.type);
            },
            lineWidth: (e) => {
              const d = e.data as RenderEdge | undefined;
              if (d?.anchorEdge) return 0;
              return d?.inCycle ? 2.4 : 1.3;
            },
            strokeOpacity: (e) => {
              const d = e.data as RenderEdge | undefined;
              if (d?.anchorEdge) return 0;
              return d?.inCycle ? 1 : 0.72;
            },
            lineDash: (e) => {
              const d = e.data as RenderEdge | undefined;
              if (d?.anchorEdge) return undefined;
              return d?.inCycle ? [6, 4] : undefined;
            },
            endArrow: (e) => !(e.data as RenderEdge | undefined)?.anchorEdge,
            startArrow: (e) => {
              const d = e.data as RenderEdge | undefined;
              if (d?.anchorEdge) return false;
              return d?.bidirectional ? true : false;
            },
            radius: 10,
          },
        },
        layout: layoutConfig(layoutMode),
        behaviors: ["drag-canvas", "zoom-canvas", "drag-element"],
        // 血缘度提示：悬停节点显示「依赖 N 项（上游）/ 被 M 项引用（下游）」，
        // 与右上角 badge 角标互补——badge 快速看总数，tooltip 细分方向。
        plugins: [
          {
            type: "tooltip",
            trigger: "hover",
            getContent: (evt: IElementEvent) => {
              const raw = evt.target as { id?: string; __data__?: { id?: string } } | undefined;
              const id = raw?.id ?? raw?.__data__?.id;
              const node = id
                ? (graph?.getNodeData(String(id))?.data as AssetGraphNode | undefined)
                : undefined;
              const label = node?.label ?? id ?? "";
              const up = id ? (outDegreeMapRef.current.get(String(id)) ?? 0) : 0; // 依赖的上游
              const down = id ? (inDegreeMapRef.current.get(String(id)) ?? 0) : 0; // 被引用的下游
              const total = up + down;
              const div = document.createElement("div");
              div.style.fontSize = "12px";
              div.style.lineHeight = "1.6";
              // P0-4：label 来自 SQL 解析/DP 同步/手动登记，直接插 innerHTML 是存储型
              // XSS 向量（节点名可含 HTML）。先转义再拼接；数字字段天然安全。
              const esc = (s: string) =>
                s.replace(
                  /[&<>"']/g,
                  (c) =>
                    ({
                      "&": "&amp;",
                      "<": "&lt;",
                      ">": "&gt;",
                      '"': "&quot;",
                      "'": "&#39;",
                    })[c] as string,
                );
              div.innerHTML = `<b>${esc(label)}</b><br/>依赖 ${up} 项（上游）<br/>被 ${down} 项引用（下游）<br/><span style="color:#e65100">血缘度 ${total}</span>`;
              return div;
            },
          },
        ],
      });
      graphRef.current = graph;

      graph.on<IElementEvent>("node:click", (evt) => {
        if (!graph || graph.destroyed) return;
        const raw = evt.target as { id?: string; __data__?: { id?: string } } | undefined;
        const id = raw?.id ?? raw?.__data__?.id;
        if (!id) return;
        const node = graph.getNodeData(String(id))?.data as AssetGraphNode | undefined;
        if (node && !node.anchor) onNodeClickRef.current?.(node); // 泳道锚点不响应点击
      });

      // 悬停邻域高亮：相邻节点高亮，其余淡化（图销毁/渲染过渡期的在途事件一律忽略）。
      // 性能优化：pointerenter 高频触发（跨节点移动），用 rAF 节流到每帧只处理最后一次；
      // setElementState 改为**批量 record**（单次调用），替代逐节点循环（160+ 次调用 + 全量重绘
      // 是 rAF 550ms 卡顿的直接来源）。
      let hoverRaf = 0;
      graph.on<IElementEvent>("node:pointerenter", (evt) => {
        if (!graph || graph.destroyed || !graphReady) return;
        const raw = evt.target as { id?: string; __data__?: { id?: string } } | undefined;
        const id = raw?.id ?? raw?.__data__?.id;
        if (!id) return;
        cancelAnimationFrame(hoverRaf);
        hoverRaf = requestAnimationFrame(() => {
          if (!graph || graph.destroyed) return;
          try {
            const neighbors = graph.getNeighborNodesData(String(id));
            const active = new Set<string>([String(id), ...neighbors.map((n) => String(n.id))]);
            const record: Record<string, string | string[]> = {};
            for (const n of graph.getNodeData()) {
              record[String(n.id)] = active.has(String(n.id))
                ? stateWithCompact("active")
                : stateWithCompact("inactive");
            }
            void graph.setElementState(record, false).catch(() => {});
          } catch {
            // 高亮为装饰性交互，过渡期失败静默忽略
          }
        });
      });
      graph.on("node:pointerleave", () => {
        if (!graph || graph.destroyed || !graphReady) return;
        cancelAnimationFrame(hoverRaf);
        try {
          const record: Record<string, string | string[]> = {};
          for (const n of graph.getNodeData()) record[String(n.id)] = stateWithCompact([]);
          void graph.setElementState(record, false).catch(() => {});
        } catch {
          // 忽略过渡期状态清理失败
        }
      });

      // 大数据量 LOD：滚轮缩放跨过阈值时批量切换 compact 状态（隐藏标签/图标/柔光/投影），
      // 提升缩放/平移帧率；放大到阈值以上自动恢复完整样式。applyLod 内部有阈值节流。
      graph.on("canvas:wheel", () => applyLod());
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
        // 按节点规模自适应 fitView：
        //  - 节点多（全景）→ always 适配填满画布，分布均匀；
        //  - 节点少（聚焦视图）→ overflow 仅在内容超出视口时裁剪，不把少量节点放大填满画布。
        try {
          graph.fitView(
            nodeCountRef.current > 5
              ? { when: "always" }
              : { when: "overflow" },
          );
        } catch {
          /* fitView 偶尔在过渡期失败 */
        }
        // 大数据量 LOD：fitView 之后按当前缩放应用 compact——
        // 全景大图会被缩到 zoom<0.6，此时立即隐藏标签/图标/柔光，避免首帧全量绘制卡顿。
        applyLod();
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
        for (const n of nodeList) safeSetElementState(graph, String(n.id), stateWithCompact([]));
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
          matchIds.has(String(n.id))
            ? stateWithCompact("active")
            : stateWithCompact("inactive"),
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
 *   字段=椭圆）、PII 红色描边、按血缘度编码大小；类型图标渲染在节点中央、
 *   名称标签放节点下方白底 pill（完整展示、绝对可读）、halo 柔光让节点更立体。
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
  lanes = false,
  layerBadges = true,
  fullscreenable = true,
}: AssetGraphProps) {
  const onNodeClickRef = useRef(onNodeClick);
  onNodeClickRef.current = onNodeClick;
  const [showAll, setShowAll] = useState(false);
  // 前端筛选：按节点类型过滤 + 按 label 搜索定位（不重新请求后端）
  const [typeFilter, setTypeFilter] = useState<string[]>([]);
  const [searchText, setSearchText] = useState("");
  // 血缘度筛选：仅展示依赖引用数 ≥ 阈值的节点（聚焦枢纽，隐藏低价值叶子）
  const [minDegreeFilter, setMinDegreeFilter] = useState(0);
  // 字段折叠：showFields 作为初始值并受控同步（父组件可动态改），控制条可切换
  const [showFieldsOn, setShowFieldsOn] = useState(showFields);
  useEffect(() => setShowFieldsOn(showFields), [showFields]);
  // 布局手动覆盖：undefined=跟随 auto 检测；否则强制指定
  const [layoutOverride, setLayoutOverride] = useState<
    "hierarchy" | "force" | "radial" | undefined
  >(undefined);
  // 布局代际计数器：用户每次点 Select 就 +1（即使选了同一项，也强制重挂载 GraphCanvas）。
  // 解决"hierarchy 默认下用户点了一次 分层布局 没反应"的问题。
  const [layoutTick, setLayoutTick] = useState(0);
  // 布局切换瞬态标记：用户点击切换 Select 后立即置 true，GraphCanvas 重挂载并完成 render 后置 false，
  // 期间叠加 Spin 让用户明确感知到"正在重新计算布局"，避免在节点多时误以为"点了没反应"
  const [layoutSwitching, setLayoutSwitching] = useState(false);
  // 全屏展示：portal 到 body 的 fixed overlay（复用同一实例 UI 状态，筛选/搜索/布局不丢失）
  const [fullscreenOpen, setFullscreenOpen] = useState(false);

  // 全屏 Esc 退出
  useEffect(() => {
    if (!fullscreenOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFullscreenOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreenOpen]);

  // 布局切换处理：更新 override + 触发代际 + 显示 loading（GraphCanvas onReady 后自动清除）
  const handleLayoutChange = (v: "hierarchy" | "force" | "radial" | undefined) => {
    setLayoutOverride(v);
    setLayoutTick((t) => t + 1);
    setLayoutSwitching(true);
  };

  // 类型筛选（空 = 全部）；字段折叠（showFieldsOn=false）时剔除字段节点（血缘总览降噪）
  const filteredNodes = useMemo(() => {
    let list = typeFilter.length === 0 ? nodes : nodes.filter((n) => typeFilter.includes(n.type));
    if (showFieldsOn === false) list = list.filter((n) => n.type !== "field");
    return list;
  }, [nodes, typeFilter, showFieldsOn]);

  // 血缘度筛选（聚焦枢纽）：仅保留依赖引用数 ≥ 阈值的节点。度统计基于完整 edges
  //（含被 typeFilter/字段折叠隐藏的节点），保证「枢纽」判断不被筛选顺序影响。
  const degreeFilteredNodes = useMemo(() => {
    if (minDegreeFilter <= 0) return filteredNodes;
    const dm = new Map<string, number>();
    for (const e of edges) {
      dm.set(e.source, (dm.get(e.source) ?? 0) + 1);
      dm.set(e.target, (dm.get(e.target) ?? 0) + 1);
    }
    return filteredNodes.filter((n) => (dm.get(n.id) ?? 0) >= minDegreeFilter);
  }, [filteredNodes, edges, minDegreeFilter]);

  // 限流渲染：优先保留核心节点，超出阈值时默认隐藏附属字段节点
  const {
    visible: visibleNodes,
    visibleEdges,
    hidden,
  } = useMemo(
    () => pickVisible(degreeFilteredNodes, edges, showAll),
    [degreeFilteredNodes, edges, showAll],
  );

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
  // 布局策略：泳道模式强制分层（dagre acyclic 翻转环边 + 环标记，环不再毁掉全图秩序）；
  // 否则 auto=有真环用力导向（环图 dagre 渲染异常），无环用分层；手动覆盖优先
  const layoutMode = useMemo<"hierarchy" | "force" | "radial">(() => {
    if (layoutOverride) return layoutOverride;
    if (lanes) return "hierarchy";
    if (layout === "force") return "force";
    if (layout === "hierarchy") return "hierarchy";
    if (layout === "radial") return "radial";
    return cycleNodes.size > 0 ? "force" : "hierarchy";
  }, [layout, layoutOverride, cycleNodes, lanes]);

  // 力导向大图边降采样：仅 force 布局 + 边数超阈值时按「两端血缘度和」降序保留枢纽边。
  // 用「边数」判断（而非节点数）——节点被 160 限流后边可能仍上千，边才是力导向密集的主因。
  const layoutEdges = useMemo(() => {
    if (layoutMode !== "force") return renderEdges;
    const dm = new Map<string, number>();
    for (const e of renderEdges) {
      dm.set(e.source, (dm.get(e.source) ?? 0) + 1);
      dm.set(e.target, (dm.get(e.target) ?? 0) + 1);
    }
    return filterDenseForceEdges(renderEdges, dm, renderEdges.length > MAX_FORCE_DENSE_EDGES);
  }, [layoutMode, renderEdges]);

  // 语义泳道：仅分层布局下插入隐藏锚点 + 锚定边（力导向不需要泳道）
  const laneData = useMemo(() => {
    if (!lanes || layoutMode !== "hierarchy") {
      return { nodes: visibleNodes, edges: layoutEdges };
    }
    return applyLanes(visibleNodes, layoutEdges);
  }, [lanes, layoutMode, visibleNodes, layoutEdges]);

  if (nodes.length === 0) {
    return <Empty description="暂无图谱数据" />;
  }

  const domains = [...new Set(nodes.map((n) => n.domain).filter(Boolean))] as string[];
  const typeCounts = nodes.reduce<Record<string, number>>((acc, n) => {
    acc[n.type] = (acc[n.type] ?? 0) + 1;
    return acc;
  }, {});

  const hasLayerNodes = layerBadges && nodes.some((n) => layerOf(n) !== null);

  // 图谱主体渲染（主视图与全屏视图共用，UI 状态共享——切换全屏不丢失筛选/搜索/布局）
  const renderBody = (h: number) => (
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
        <Select
          allowClear
          placeholder="依赖 ≥ 0"
          style={{ minWidth: 130 }}
          value={minDegreeFilter === 0 ? undefined : minDegreeFilter}
          onChange={(v: number | undefined) => setMinDegreeFilter(v ?? 0)}
          data-testid="asset-graph-min-degree"
          options={[
            { value: 0, label: "依赖 ≥ 0（全部）" },
            { value: 1, label: "依赖 ≥ 1" },
            { value: 2, label: "依赖 ≥ 2" },
            { value: 3, label: "依赖 ≥ 3" },
            { value: 5, label: "依赖 ≥ 5（枢纽）" },
            { value: 10, label: "依赖 ≥ 10" },
          ]}
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
            { value: "radial", label: "血缘度径向" },
          ]}
        />
        <Button
          size="middle"
          data-testid="asset-graph-show-fields"
          type={showFieldsOn ? "primary" : "default"}
          onClick={() => setShowFieldsOn((v) => !v)}
        >
          {showFieldsOn ? "隐藏字段" : "显示字段"}
        </Button>
        {fullscreenable && (
          <Button
            size="middle"
            icon={<FullscreenOutlined />}
            data-testid="asset-graph-fullscreen-btn"
            onClick={() => setFullscreenOpen(true)}
          >
            全屏
          </Button>
        )}
      </div>
      <div style={{ position: "relative", width: "100%" }}>
        <GraphCanvas
          key={`${layoutMode}-${layoutTick}`}
          nodes={laneData.nodes}
          edges={laneData.edges}
          layoutMode={layoutMode}
          height={h}
          searchText={searchText}
          onNodeClick={(n) => onNodeClickRef.current?.(n)}
          onReady={() => setLayoutSwitching(false)}
          layerBadges={layerBadges}
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
          <span className="muted">PII 描边 · 节点大小=血缘度 · 圆形/矩形/椭圆=指标/表/字段 · 右上角数字=依赖引用数</span>
        </div>
        {hasLayerNodes && (
          <div>
            <span className="muted">数仓层：</span>
            {Object.entries(LAYER_STROKE).map(([layer, color]) => (
              <span key={layer} style={{ marginRight: 8 }}>
                <span
                  style={{
                    display: "inline-block",
                    width: 10,
                    height: 10,
                    borderRadius: 3,
                    border: `2.5px solid ${color}`,
                    background: "rgba(0,0,0,0.06)",
                    marginRight: 4,
                    verticalAlign: "middle",
                  }}
                />
                {layer.toUpperCase()}
              </span>
            ))}
          </div>
        )}
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

  // 全屏：portal 到 body 的 fixed overlay，复用同一实例状态（筛选/搜索/布局不丢失）
  if (fullscreenOpen) {
    return createPortal(
      <div
        data-testid="asset-graph-fullscreen"
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 1000,
          background: "#fff",
          padding: 16,
          overflow: "auto",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: 8,
          }}
        >
          <h3 style={{ margin: 0 }}>图谱全屏</h3>
          <Button icon={<FullscreenExitOutlined />} onClick={() => setFullscreenOpen(false)}>
            退出全屏
          </Button>
        </div>
        {renderBody(typeof window !== "undefined" ? Math.max(480, window.innerHeight - 120) : 600)}
      </div>,
      document.body,
    );
  }

  return renderBody(height);
}
