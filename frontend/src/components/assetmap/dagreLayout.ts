import dagre from "dagre";

/** 布局输入节点：id + G6 渲染尺寸（number=直径 / [宽, 高]，与 nodeStyleCache 语义一致）。 */
export interface DagreLayoutNode {
  id: string;
  /** 节点渲染尺寸；缺省时按 10（与 G6 antv-dagre 的 defaultNodeSize 一致）。 */
  size?: number | [number, number];
}

/** 分层布局参数（与既有 layoutConfig 的 antv-dagre 配置一一对应）。 */
export interface DagreLayoutParams {
  /** 分层方向：TB=自上而下 / LR=从左到右（与 G6 rankdir 语义一致）。 */
  rankdir: "TB" | "LR";
  /** 同层节点间距（dagre nodesep 语义）。 */
  nodesep: number;
  /** 相邻层间距（dagre ranksep 语义）。 */
  ranksep: number;
  /** 层内对齐：TB 用 DL / LR 用 UL（与 layoutConfig 既有取值一致）。 */
  align?: "UL" | "UR" | "DL" | "DR";
}

export interface DagrePosition {
  x: number;
  y: number;
}

function parseSize(size?: number | [number, number]): [number, number] {
  if (Array.isArray(size)) return [size[0] ?? 10, size[1] ?? 10];
  const v = typeof size === "number" && size > 0 ? size : 10;
  return [v, v];
}

/**
 * 用原生 dagre 复刻 G6「antv-dagre」布局的坐标计算（Web Worker 化/预设坐标渲染的核心）。
 *
 * 复刻要点（与 @antv/layout AntVDagreLayout 保持一致，保证预设坐标与小图内建布局观感一致）：
 * 1. 节点盒 = 渲染尺寸 + 2×sep 边距——TB 时水平补 nodesep、垂直补 ranksep；LR 时互换
 *    （antv 的 horisep/vertisep 逻辑），dagre 在加宽的盒子上再按 nodesep/ranksep 排布；
 * 2. ranker='tight-tree' + acyclicer='greedy'（antv 同款：环边翻转保证分层不散架）；
 * 3. align 透传（TB=DL / LR=UL）。
 *
 * 纯函数、无 DOM/G6 依赖——主线程同步兜底与 Web Worker 内共用同一实现，保证两条路径坐标一致。
 * 返回 Map<id, 节点中心坐标>（dagre 输出即中心点，可直接写入 G6 节点 style.x/y）。
 */
export function computeDagrePositions(
  nodes: DagreLayoutNode[],
  edges: { source: string; target: string }[],
  params: DagreLayoutParams,
): Map<string, DagrePosition> {
  const { rankdir = "TB", nodesep, ranksep, align } = params;
  const horizontal = rankdir === "LR";
  // antv 的 spacing 方向：TB 时水平间距=nodesep/垂直间距=ranksep；LR/RL 时互换
  const hSep = horizontal ? ranksep : nodesep;
  const vSep = horizontal ? nodesep : ranksep;
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir, nodesep, ranksep, align, ranker: "tight-tree", acyclicer: "greedy" });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) {
    const [w, h] = parseSize(n.size);
    g.setNode(n.id, { width: w + 2 * hSep, height: h + 2 * vSep });
  }
  for (const e of edges) {
    // 仅连两端都存在的边——泳道锚定边/折叠边在传给布局前已保证端点存在，此处防御性过滤
    if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  }
  dagre.layout(g);
  const out = new Map<string, DagrePosition>();
  for (const n of nodes) {
    const pos = g.node(n.id);
    if (pos) out.set(n.id, { x: pos.x, y: pos.y });
  }
  return out;
}
