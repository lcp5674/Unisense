import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Button, Empty, Input, InputNumber, Modal, Select, Spin, Table, Tag } from "antd";
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
  /** 数仓分层（后端返回小写：ods/dwd/dws/ads/dm；指标取 Metric.dw_layer，表侧未来采集登记）。 */
  dw_layer?: string;
  /** 语义泳道的隐藏锚点节点（渲染为不可见、不响应交互） */
  anchor?: boolean;
}

export interface AssetGraphEdge extends Record<string, unknown> {
  source: string;
  target: string;
  type: string;
  /** 字段级血缘边在连线上展示的加工标注（如「空值兜底 COALESCE(id,…)」）；
   *  仅字段钻取等携带表达式信息的边设置，表级主图边不设 → 不渲染 label，零影响。 */
  edgeLabel?: string;
  /** 完整加工表达式（用于边 label 截断后的 hover 完整查看）。 */
  fullExpr?: string;
}

interface AssetGraphProps {
  nodes: AssetGraphNode[];
  edges: AssetGraphEdge[];
  height?: number;
  onNodeClick?: (node: AssetGraphNode) => void;
  /** 是否展示字段节点（血缘总览等场景默认隐藏，减少视觉噪声）；默认 true */
  showFields?: boolean;
  /** 悬停路径高亮时是否把「非血缘链节点」压暗（inactive 半透明）。
   *  小图（字段级钻取、聚焦子图）节点密集、hover 链短，压暗会让其他节点几乎不可见，
   *  应关闭（只高亮链上、不压暗其余）；大图默认 true 保持「从哪来/流向哪」的聚焦效果。 */
  dimOnHover?: boolean;
  /** 是否默认渲染全部节点（true=跳过 160 节点上限、画所有传入节点；false=LOD 限流 +「显示全部」按钮）。
   *  适用于调用方已确定节点数可全画的场景（如血缘图谱 1763 节点要全部可见）。默认 false。
   *  showAll=true 时 LOD 提示横幅与「显示全部」按钮自动隐藏（节点不再被裁剪）。
   */
  defaultShowAll?: boolean;
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
  /** 分层布局方向：TB=自上而下（数仓源头在上、字段在下）；LR=从左到右（源头在左、字段在右）。
   *  仅影响 hierarchy 布局，force/radial 无方向概念。默认 "TB"（保持既有行为）；可在工具栏手动切换。 */
  direction?: "TB" | "LR";
  /** 初始泳道折叠集合（结构概览模式：父组件传入全层折叠，进入即显示各层聚合带 + 层间主干边）。
   *  仅作为 collapsedLayers 的初值（内部仍可点击聚合节点展开/工具栏调整/全部展开）。
   *  父组件切换模式时应通过 key 强制重挂载以应用新初值。默认 []（不折叠，保持既有行为）。 */
  defaultCollapsedLayers?: string[];
}

const TYPE_LABEL: Record<string, string> = {
  metric: "指标",
  table: "表 / 视图",
  field: "字段",
  unknown: "未知",
};

const TYPE_OPTIONS = Object.entries(TYPE_LABEL).map(([value, label]) => ({ value, label }));

// 业务域配色：12 色现代低饱和色板（Tailwind 600 段），与「校准仪表」设计系统协调
// （深蓝底盘 --ink + 数据青 --data + 信号橙 --signal）。600 段是专为白字设计的
// 深色档（对比度普遍 >4:1，WCAG AA），同时比 700 段更柔和、有高级感；
// 节点填充再加径向渐变（中心提亮→主色）后层次更立体。labelBackground 白底 pill 兜底可读性。
const DOMAIN_PALETTE = [
  "#3b82f6", // 蓝（blue 600）
  "#0e7490", // 青（cyan 700）
  "#059669", // 绿（emerald 600）
  "#d97706", // 琥珀（amber 600）
  "#dc2626", // 红（red 600）
  "#7c3aed", // 紫（violet 600）
  "#0284c7", // 天蓝（sky 600）
  "#db2777", // 玫红（pink 600）
  "#ea580c", // 橙（orange 600）
  "#4f46e5", // 靛蓝（indigo 600）
  "#9333ea", // 紫罗兰（purple 600）
  "#0d9488", // 青绿（teal 600）
];

// 节点类型兜底色（节点 domain 缺失时使用，按类型区分保证视觉差异）
const TYPE_FALLBACK_COLOR: Record<string, string> = {
  metric: "#7c3aed", // 紫（指标）
  table: "#3b82f6", // 蓝（表/视图）
  field: "#0d9488", // 青绿（字段）
  unknown: "#64748b", // 中性灰蓝
};

// 边类型配色：柔和雾蓝/淡紫/淡青，低饱和不抢节点视觉（血缘总览里 DERIVED_FROM 占绝大多数）
const EDGE_PALETTE: Record<string, string> = {
  DERIVED_FROM: "#a3b3c9",
  BASED_ON: "#b9a8ef",
  CONSUMED_BY: "#8ec5f6",
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
 * 字段节点 label 折行：完整「库.表.列」按点分段、贪婪累积到每行不超过 maxChars 字符时折行。
 * 字段级血缘图的长表列名（如 wedw_mid.jwy_anhao_population_history_tag_df.tag_code）若单行展示
 * 会与邻节点底部标签互相压字、布局拥挤——折行后每行短、完整内容不丢，悬停 tooltip 仍可全文。
 * 换行尽量以「.」为界（不切断库/表/列名）；单段超长（如无点长表名）才在段内硬切兜底。
 */
export function wrapFieldLabel(label: string, maxChars = 24): string {
  const src = String(label ?? "").replace(/\s+/g, "");
  if (!src) return "";
  if (src.length <= maxChars) return src;
  const lines: string[] = [];
  const n = src.length;
  let i = 0;
  while (i < n) {
    if (n - i <= maxChars) {
      lines.push(src.slice(i));
      break;
    }
    const winEnd = i + maxChars;
    // 优先在窗口内最后一个分隔符（. 或 _）处断行，断点保留分隔符——折行只换行不丢任何字符，
    // 还原时 join("\n").replace("\n","") 即原文；窗口内无分隔符（超长无点无下划线段）才硬切兜底。
    let br = -1;
    for (let k = i; k < winEnd; k++) {
      if (src[k] === "." || src[k] === "_") br = k;
    }
    if (br > i) {
      lines.push(src.slice(i, br + 1));
      i = br + 1;
    } else {
      lines.push(src.slice(i, winEnd));
      i = winEnd;
    }
  }
  return lines.join("\n");
}

/**
 * 边 label（「加工方式：表达式」）折行：加工表达式是带空格的 SQL/函数文本，按空白切词
 * 贪婪累积到每行不超过 maxChars 字符时折行；单词超长（无空格的超长列名/常量串）才硬切
 * 兜底。与 wrapFieldLabel 的差异：表达式保留空格语义（不能像节点名那样去空白），且折行
 * 只换行不丢任何字符——join("\n") 去掉换行即原文（仅连续空白规整为单空格，不影响 SQL
 * 语义）。字段级血缘图的边标注不再截断成省略号，完整表达式常驻边旁多行展示。
 */
export function wrapEdgeExpr(label: string, maxChars = 40): string {
  const src = String(label ?? "").trim();
  if (!src) return "";
  if (src.length <= maxChars) return src;
  const words = src.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let line = "";
  const pushLine = (s: string) => {
    lines.push(s);
    line = "";
  };
  for (const w of words) {
    let piece = w;
    while (piece.length > maxChars) {
      if (line) pushLine(line);
      lines.push(piece.slice(0, maxChars));
      piece = piece.slice(maxChars);
    }
    if (!piece) continue;
    const candidate = line ? `${line} ${piece}` : piece;
    if (!line || candidate.length <= maxChars) {
      line = candidate;
    } else {
      pushLine(line);
      line = piece;
    }
  }
  if (line) lines.push(line);
  return lines.join("\n");
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

// 颜色压暗：给定 hex 色，向黑色方向压暗 amt（0-255），用于渐变边缘加深
function darkenHex(hex: string, amt: number): string {
  const n = parseInt(hex.slice(1), 16);
  if (Number.isNaN(n)) return hex;
  const r = Math.max(0, ((n >> 16) & 255) - amt);
  const g = Math.max(0, ((n >> 8) & 255) - amt);
  const b = Math.max(0, (n & 255) - amt);
  return `#${((1 << 24) | (r << 16) | (g << 8) | b).toString(16).slice(1)}`;
}

// —— 节点类型图标（Lucide 风格 24x24 线性 SVG，深色半透明圆衬底让白色线条在任意
// 节点填充色上（深蓝/紫/红/橙/绿）都清晰可辨；端点圆点+圆角+1.5px 精线条提升高级感）——
// 三档衬底透明度：metric（枢纽）最不透明，field（叶子）最淡——视觉层次对应节点重要度。
// 模块加载时预编码一次，iconSrc 回调仅查表，无每帧计算成本。
function svgDataUri(svg: string): string {
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}
function iconWithBackdrop(iconBody: string, backdropOpacity: number): string {
  // 24x24 viewBox：圆衬底 r=11 居中，rgba(15,23,42) 是 slate-900 调半透明，节点色透过来仍是高对比白线条
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="11" fill="rgba(15,23,42,${backdropOpacity})" stroke="rgba(255,255,255,0.18)" stroke-width="0.4"/><g fill="none" stroke="#ffffff" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${iconBody}</g></svg>`;
}
const NODE_ICON_SRC: Record<string, string> = {
  // 表格：表头分割线 + 两根列分隔线 + 三个列头小圆点（Lucide Table 风格，更精致）
  table: svgDataUri(
    iconWithBackdrop(
      `<rect x="3" y="5" width="18" height="14" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="9" y1="10" x2="9" y2="19"/><line x1="15" y1="10" x2="15" y2="19"/><circle cx="6" cy="7.5" r="0.55" fill="#fff" stroke="none"/><circle cx="12" cy="7.5" r="0.55" fill="#fff" stroke="none"/><circle cx="18" cy="7.5" r="0.55" fill="#fff" stroke="none"/>`,
      0.42,
    ),
  ),
  // 字段：竖列矩形 + 三段横线 + 段头圆点（Lucide ListTree 风格，列结构清晰）
  field: svgDataUri(
    iconWithBackdrop(
      `<rect x="5" y="3" width="14" height="18" rx="2"/><line x1="5" y1="9" x2="19" y2="9"/><line x1="5" y1="15" x2="19" y2="15"/><circle cx="8" cy="6" r="0.7" fill="#fff" stroke="none"/><circle cx="8" cy="12" r="0.7" fill="#fff" stroke="none"/><circle cx="8" cy="18" r="0.7" fill="#fff" stroke="none"/>`,
      0.32,
    ),
  ),
  // 指标：折线图 + 端点圆 + 坐标轴（Lucide LineChart 风格，数据感强烈）
  metric: svgDataUri(
    iconWithBackdrop(
      `<line x1="3" y1="20" x2="21" y2="20"/><line x1="3" y1="3" x2="3" y2="20"/><polyline points="7,15 11,11 15,13 20,6"/><circle cx="7" cy="15" r="0.9" fill="#fff" stroke="none"/><circle cx="11" cy="11" r="0.9" fill="#fff" stroke="none"/><circle cx="15" cy="13" r="0.9" fill="#fff" stroke="none"/><circle cx="20" cy="6" r="0.9" fill="#fff" stroke="none"/>`,
      0.52,
    ),
  ),
};
function nodeIconSrc(type?: string): string {
  return NODE_ICON_SRC[type ?? "metric"] ?? NODE_ICON_SRC.metric;
}

/** 图边的展示数据（在 AssetGraphEdge 之上叠加渲染语义字段）。 */
interface RenderEdge extends AssetGraphEdge {
  /** 双向边（A↔B 合并后置 true，单箭头改双箭头） */
  bidirectional?: boolean;
  /** 属于真实循环（SCC 尺寸>2）的边 */
  inCycle?: boolean;
  /** 语义泳道的隐藏锚定边（锚点间连线 / 锚点→真实节点挂载边，渲染时不可见） */
  anchorEdge?: boolean;
  /** 两端节点的泳道跨度（|层差|），≥2 为跨层长边（线团主因，样式降噪用） */
  layerSpan?: number;
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

/** 推断节点数仓分层：表优先用节点携带的分层字段（后端/采集登记），否则按名称前缀；
 *  指标按 dw_layer 属性（后端返回小写）。大小写归一化兜底（兼容 ODS/DWS 大写写法）。 */
export function layerOf(n: AssetGraphNode): string | null {
  const carried = (n as { dw_layer?: unknown }).dw_layer;
  if (typeof carried === "string" && carried) {
    const l = carried.toLowerCase();
    if (LAYER_STROKE[l]) return l;
  }
  if (n.type === "table") {
    const name = (n.label || n.id).toLowerCase();
    for (const layer of Object.keys(LAYER_STROKE)) {
      if (name.startsWith(`${layer}_`) || name.startsWith(`${layer}.`)) return layer;
    }
    return null;
  }
  return null;
}

// —— 语义泳道共享常量 ——
// 泳道顺序与血缘方向一致：数仓分层（源头→应用）→ 其他表 → 指标 → 字段。
// applyLanes / collapseLayers / laneOfNode / 跨层边捆绑 共用同一顺序，保证"层"判定一致。
const LANE_ORDER = ["ods", "dwd", "dws", "ads", "dm", "table", "metric", "field"] as const;
const LANE_LABEL: Record<string, string> = {
  ods: "ODS 贴源层",
  dwd: "DWD 明细层",
  dws: "DWS 汇总层",
  ads: "ADS 应用层",
  dm: "DM 集市层",
  table: "未分层表",
  metric: "指标层",
  field: "字段层",
};

/** 节点所属泳道：折叠聚合节点按其 collapsedLayer 归位；表按数仓前缀；指标/字段按语义带；
 *  其余类型（other/unknown/上游中心节点）返回 ""（自由参与分层，不挂锚）。 */
function laneOfNode(n: AssetGraphNode): string {
  if ((n as { collapsedLayer?: string }).collapsedLayer) {
    return (n as { collapsedLayer?: string }).collapsedLayer as string;
  }
  if (n.type === "table") {
    const l = layerOf(n);
    if (l) return l;
    return "table";
  }
  if (n.type === "metric") return "metric";
  if (n.type === "field" || String(n.type).indexOf("column") === 0) return "field";
  return "";
}

/** 泳道序号（用于跨层跨度计算）；未知层返回 -1。 */
function laneIndexOf(n: AssetGraphNode): number {
  const lane = laneOfNode(n);
  return LANE_ORDER.indexOf(lane as (typeof LANE_ORDER)[number]);
}

/** 折叠聚合节点的层标记读取/判断：节点是否为泳道折叠聚合节点。 */
function collapsedLayerOf(n?: AssetGraphNode): string | undefined {
  return (n as { collapsedLayer?: string } | undefined)?.collapsedLayer;
}

/**
 * 数仓分层泳道：为 dagre 分层插入隐藏锚点节点与锚定边，把节点按「数仓分层 + 语义带」
 * 聚进多条泳道（血缘方向：ODS → DWD → DWS → ADS/DM → 其他表 → 指标 → 字段）。
 * - 表节点按其名称前缀/指标 dw_layer 推断分层（复用 layerOf）；未识别层级的表归入
 *   ``table`` 带（放在应用层之下、指标之上，避免未分层表打散加工链）；
 * - 锚点链按血缘方向连锚定边，dagre 按 rankdir 排布：TB 时源（ODS）放最上、字段放最下，
 *   LR 时源（ODS）放最左、字段放最右（方向由用户/父组件切换，锚点链本身与方向无关）；
 * - 每类真实节点经「锚 → 节点」挂载边锚到对应泳道（同层带内仍按血缘边纵向分层，
 *   表→表加工链在层带内保留）；
 * - other/unknown 类型不挂锚（自由参与分层，如上游依赖图的中心节点保持在最上方）。
 * 锚点与锚定边由 GraphCanvas 按 anchor/anchorEdge 标记渲染为不可见、不响应交互。
 */
export function applyLanes(
  nodes: AssetGraphNode[],
  edges: RenderEdge[],
): { nodes: AssetGraphNode[]; edges: RenderEdge[] } {
  const order = LANE_ORDER;
  const present = order.filter((l) => nodes.some((n) => laneOfNode(n) === l));
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
      if (laneOfNode(n) === t) {
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

/** 泳道折叠聚合节点在 AssetGraphNode 上扩展的字段（供样式/交互识别）。 */
interface CollapsedNodeMeta {
  /** 被折叠的泳道（ods/dwd/dws/ads/dm/table/metric/field） */
  collapsedLayer?: string;
  /** 被折叠的真实节点数 */
  collapsedCount?: number;
}

/**
 * 子图折叠（泳道折叠）：把某一数仓分层/语义带的全部节点收成一个「聚合节点」，
 * 减少"中间层几十个表堆在一起"造成的视觉噪声。
 * - 每个被折叠泳道生成一个聚合节点（id `__fold_{layer}__`，标记 collapsedLayer/collapsedCount，
 *   归位到原泳道）；层内节点从图中移除；
 * - 聚合节点与外部节点之间的边被保留并**按 (源,目标,类型) 去重**（多个层内节点连同一外部
 *   节点 → 合并为一条）；两端都在被折叠层内的边（内部边）丢弃；
 * - 已折叠聚合节点不再递归折叠；锚点（anchor）不受折叠影响（调用方在 applyLanes 前折叠）。
 * 返回折叠后的 nodes/edges 与折叠统计（供工具栏显示"已折叠 N 层/M 节点"）。
 */
export function collapseLayers(
  nodes: AssetGraphNode[],
  edges: AssetGraphEdge[],
  collapsed: readonly string[],
): {
  nodes: AssetGraphNode[];
  edges: AssetGraphEdge[];
  collapsedCount: number;
} {
  const wanted = new Set(collapsed);
  if (wanted.size === 0) return { nodes, edges, collapsedCount: 0 };

  // 按泳道分组被折叠节点（排除锚点与已折叠聚合节点）
  const membersByLayer = new Map<string, AssetGraphNode[]>();
  for (const n of nodes) {
    if ((n as { anchor?: boolean }).anchor || collapsedLayerOf(n)) continue;
    const lane = laneOfNode(n);
    if (wanted.has(lane)) {
      const list = membersByLayer.get(lane) ?? [];
      list.push(n);
      membersByLayer.set(lane, list);
    }
  }
  if (membersByLayer.size === 0) return { nodes, edges, collapsedCount: 0 };

  // 折叠映射：memberId → 聚合节点 id
  const foldId = (lane: string) => `__fold_${lane}__`;
  const memberToFold = new Map<string, string>();
  const aggregateNodes: AssetGraphNode[] = [];
  let collapsedCount = 0;
  const typeFor = (lane: string): string => (lane === "metric" || lane === "field" ? lane : "table");

  for (const [lane, members] of membersByLayer) {
    collapsedCount += members.length;
    const aggId = foldId(lane);
    for (const m of members) memberToFold.set(String(m.id), aggId);
    // 聚合节点：域取该层出现最多的域（无则取第一个非空），保持聚合节点也按域着色统一观感
    const domainCount = new Map<string, number>();
    for (const m of members) {
      if (m.domain) domainCount.set(m.domain, (domainCount.get(m.domain) ?? 0) + 1);
    }
    let domain: string | undefined;
    let max = -1;
    for (const [d, c] of domainCount) {
      if (c > max) {
        max = c;
        domain = d;
      }
    }
    const agg: AssetGraphNode & CollapsedNodeMeta = {
      id: aggId,
      type: typeFor(lane),
      label: `${LANE_LABEL[lane] ?? lane}（${members.length}）`,
      domain,
      collapsedLayer: lane,
      collapsedCount: members.length,
    };
    aggregateNodes.push(agg as AssetGraphNode);
  }

  // 边重定向 + 去重 + 内部边丢弃
  const remaining = nodes.filter((n) => !memberToFold.has(String(n.id)));
  const seen = new Set<string>();
  const outEdges: AssetGraphEdge[] = [];
  for (const e of edges) {
    const ns = memberToFold.get(String(e.source)) ?? e.source;
    const nt = memberToFold.get(String(e.target)) ?? e.target;
    if (ns === nt) continue; // 折叠层内部边丢弃
    const key = `${ns}__${nt}__${e.type}`;
    if (seen.has(key)) continue; // 聚合后重复边去重
    seen.add(key);
    outEdges.push({ ...e, source: ns, target: nt });
  }
  return { nodes: [...remaining, ...aggregateNodes], edges: outEdges, collapsedCount };
}

/** 跨层边跨度标记：计算每条边两端节点的泳道跨度（|层差|），供"跨层长边"样式降噪。 */
export function markLaneSpan(
  edges: RenderEdge[],
  nodes: AssetGraphNode[],
): RenderEdge[] {
  const idxById = new Map<string, number>();
  for (const n of nodes) idxById.set(String(n.id), laneIndexOf(n));
  const getIdx = (id: string): number => {
    const i = idxById.get(id);
    if (i !== undefined && i >= 0) return i;
    // 节点不在当前列表（锚点/外部）——给一个中性值，不参与"跨层"判定
    return -1;
  };
  return edges.map((e) => {
    const a = getIdx(e.source);
    const b = getIdx(e.target);
    if (a < 0 || b < 0) return { ...e, layerSpan: 0 };
    return { ...e, layerSpan: Math.abs(a - b) };
  });
}

// —— 路径高亮（替代 1 跳邻域淡化）——
// hover 节点时点亮它的"血缘链"：沿边的两个方向各向外 K 跳收集上下游节点与路径边，
// 其余节点/边压暗——用户一眼看出"这个节点从哪来、流向哪、和谁同链"，比 1 跳邻域更有信息量。
const PATH_MAX_DEPTH = 3; // 上游/下游各最多追溯 3 跳
const PATH_MAX_NODES = 120; // 防枢纽节点路径爆炸（超限按 BFS 层截断）

/** 血缘邻接表：source→target 视为数据流方向（上游→下游）。排除锚定边。 */
export interface LineageAdjacency {
  /** nodeId → 上游节点（指向它的 source 集合） */
  up: Map<string, string[]>;
  /** nodeId → 下游节点（它指向的 target 集合） */
  down: Map<string, string[]>;
}

export function buildLineageAdjacency(edges: AssetGraphEdge[]): LineageAdjacency {
  const up = new Map<string, string[]>();
  const down = new Map<string, string[]>();
  for (const e of edges) {
    if ((e as RenderEdge | undefined)?.anchorEdge) continue; // 锚定边不参与血缘链
    const s = String(e.source);
    const t = String(e.target);
    const u = up.get(t) ?? [];
    u.push(s);
    up.set(t, u);
    const d = down.get(s) ?? [];
    d.push(t);
    down.set(s, d);
  }
  return { up, down };
}

/**
 * 从中心节点沿上下游 BFS 收集 K 跳内的路径节点集（含中心）。层序截断保证
 * 超限时保留"离中心最近"的节点（BFS 天然近者优先）。
 */
export function collectPathNodes(
  adj: LineageAdjacency,
  center: string,
  maxDepth = PATH_MAX_DEPTH,
  maxNodes = PATH_MAX_NODES,
): Set<string> {
  const visited = new Set<string>([center]);
  let frontier = [center];
  for (let depth = 0; depth < maxDepth; depth += 1) {
    if (visited.size >= maxNodes) break;
    const next: string[] = [];
    for (const id of frontier) {
      for (const nb of [...(adj.up.get(id) ?? []), ...(adj.down.get(id) ?? [])]) {
        if (!visited.has(nb)) {
          visited.add(nb);
          next.push(nb);
          if (visited.size >= maxNodes) break;
        }
      }
      if (visited.size >= maxNodes) break;
    }
    if (next.length === 0) break;
    frontier = next;
  }
  return visited;
}

/** 路径边 = 两端都在路径节点集内的真实血缘边（锚定边除外）。 */
export function collectPathEdges(
  edges: AssetGraphEdge[],
  pathNodes: Set<string>,
): Set<string> {
  const out = new Set<string>();
  for (const e of edges) {
    if ((e as RenderEdge | undefined)?.anchorEdge) continue;
    if (pathNodes.has(String(e.source)) && pathNodes.has(String(e.target))) {
      out.add(`${String(e.source)}-${String(e.target)}`);
    }
  }
  return out;
}

// —— 搜索聚焦子图 ——
// 搜索框的语义是「我只想看这个节点的上下游」，而非「在 1763 个节点里把命中的标亮」。
// 因此命中后按多源 BFS 沿上下游收集 K 跳子图，只渲染子图内节点/边；其余节点压暗
// （inactive）会让整图发灰、命中节点也不突出，子图过滤才是用户真正想要的「聚焦」。
const SEARCH_FOCUS_MAX_NODES = 240; // 子图节点上限：防枢纽节点 K 跳爆炸，超限按 BFS 层序截断（近者优先）

/**
 * 多源 BFS：从一批中心节点沿上下游收集 maxDepth 跳内的节点（含中心）。
 * 层序扩展 + 全局上限保证超限时保留「离任一命中节点最近」的节点。
 */
export function collectSubgraphNodes(
  adj: LineageAdjacency,
  centers: string[],
  maxDepth: number,
  maxNodes = SEARCH_FOCUS_MAX_NODES,
): Set<string> {
  const visited = new Set<string>(centers);
  let frontier = [...centers];
  for (let depth = 0; depth < maxDepth; depth += 1) {
    if (visited.size >= maxNodes) break;
    const next: string[] = [];
    for (const id of frontier) {
      for (const nb of [...(adj.up.get(id) ?? []), ...(adj.down.get(id) ?? [])]) {
        if (visited.has(nb)) continue;
        visited.add(nb);
        next.push(nb);
        if (visited.size >= maxNodes) break;
      }
      if (visited.size >= maxNodes) break;
    }
    if (next.length === 0) break;
    frontier = next;
  }
  return visited;
}

/**
 * 围绕命中节点裁剪出上下游 K 跳子图（自包含：仅保留两端都在子图内的边）。
 * 邻接基于传入 nodes 范围内的边构建——类型/字段筛选先收窄候选，聚焦在其之上展开，
 * 保证「按类型筛选 + 搜索」组合语义正确（勾选只看指标时不会把表带回来）。
 */
export function buildSearchFocus(
  nodes: AssetGraphNode[],
  edges: AssetGraphEdge[],
  matchIds: Set<string>,
  hops: number,
  maxNodes = SEARCH_FOCUS_MAX_NODES,
): { nodes: AssetGraphNode[]; edges: AssetGraphEdge[]; truncated: boolean } {
  if (matchIds.size === 0 || hops <= 0) {
    return { nodes, edges, truncated: false };
  }
  const scoped = new Set(nodes.map((n) => n.id));
  const scopedEdges = edges.filter(
    (e) => scoped.has(String(e.source)) && scoped.has(String(e.target)),
  );
  const adj = buildLineageAdjacency(scopedEdges);
  const keep = collectSubgraphNodes(adj, [...matchIds], hops, maxNodes);
  for (const id of matchIds) keep.add(id); // 命中节点必留（BFS 已含，双保险）
  const subNodes = nodes.filter((n) => keep.has(n.id));
  const subEdges = scopedEdges.filter(
    (e) => keep.has(String(e.source)) && keep.has(String(e.target)),
  );
  return { nodes: subNodes, edges: subEdges, truncated: keep.size >= maxNodes };
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
    void graph.focusElement(id, { duration: 600, easing: "ease-out" }).catch(() => {});
  } catch {
    // 个别环境不可用时不阻断
  }
}

/**
 * 边流动动画（rAF 直接驱动 lineDashOffset）。
 *
 * G6 v5.1.1 的动画系统无法对 lineDashOffset 做**持续**流动：enter/update 的 keyframes
 * 起止值取自元素当前 attributes（与目标相同），会被 preprocessKeyframes 过滤成无动画；
 * update stage 又只在数据变更时触发一次。因此改用 requestAnimationFrame 直接驱动
 * key shape 的 lineDashOffset——@antv/g 的 style 是响应式的，改动后自动重绘。
 *
 * 只驱动有 lineDash 的边（骨干/次骨干/环边），数量有限、每帧仅改标量属性，开销可忽略；
 * 防御式实现：找不到元素/无 getShape 时静默跳过（降级为静态虚线，不影响功能）。
 * 返回取消函数，组件卸载/数据重载时应调用。
 */
function startEdgeFlow(graph: G6Graph | null | undefined, edgeIds: string[]) {
  if (!graph || graph.destroyed || edgeIds.length === 0) return () => {};
  const canvas = graph.getCanvas?.();
  const doc = (canvas?.document ?? undefined) as
    | { id?: string; children?: unknown[]; getShape?: (name: string) => unknown }
    | undefined;
  if (!doc) return () => {};
  const idSet = new Set(edgeIds);
  const found: Array<{
    id?: string;
    children?: unknown[];
    getShape?: (name: string) => unknown;
  }> = [];
  const walk = (node: unknown): void => {
    const n = node as { id?: string; children?: unknown[] } | undefined;
    if (!n) return;
    if (n.id && idSet.has(String(n.id))) found.push(n as never);
    const kids = Array.isArray(n.children) ? n.children : [];
    for (const k of kids) walk(k);
  };
  walk(doc);
  if (found.length === 0) return () => {};
  let raf = 0;
  const tick = () => {
    for (const el of found) {
      const shape = (el.getShape?.("key") ??
        (Array.isArray(el.children) ? el.children[0] : undefined)) as
        | { style?: Record<string, unknown> }
        | undefined;
      if (shape?.style) {
        const cur = typeof shape.style.lineDashOffset === "number" ? shape.style.lineDashOffset : 0;
        shape.style.lineDashOffset = (cur + 0.5) % 14;
      }
    }
    raf = requestAnimationFrame(tick);
  };
  tick();
  return () => cancelAnimationFrame(raf);
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
// 大数据量下「缩到不可读」反而更糟——信息可见优先于极致帧率。
// 阈值 0.35 对应 12px 标签约 4px（小但能辨），低于此再隐藏重装饰层（halo/阴影/icon/badge）。
// 由下方 fitView 最小缩放下限保证大图首屏 zoom ≥ 0.35，compact 通常只在用户主动
// 滚轮缩小到很小时才触发。
const LOD_COMPACT_ZOOM = 0.35;
const LOD_LARGE_GRAPH = 200;

function nodeRank(n: AssetGraphNode): number {
  if (n.type === "metric") return 0;
  if (n.type === "table") return 1;
  return 2; // field 及未知类型
}

/** 字段节点独立上限：showFields 时叠加（不占核心额度），避免大图被成百上千字段挤爆 */
const FIELD_RENDER_CAP = 400;

/** 按优先级 + 血缘度截断节点，返回可见节点集与仅含两端可见的边。
 *
 * field 处理：showFields=true 时，字段节点**不占用核心 MAX_RENDER_NODES 额度**，
 * 而是按血缘度排序后叠加（独立上限 FIELD_RENDER_CAP）。否则大图 >160 时
 * field 因 nodeRank=2 最低会被截断挤掉，用户点「显示字段」无可见变化。
 */
function pickVisible(
  nodes: AssetGraphNode[],
  edges: AssetGraphEdge[],
  showAll: boolean,
  showFields: boolean,
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
  // 分离核心（metric/table/未知）与字段：核心按 rank 截断；字段 showFields 时按血缘度叠加
  const core = nodes.filter((n) => n.type !== "field");
  const fields = nodes.filter((n) => n.type === "field");
  const sortedCore = [...core].sort(
    (a, b) => nodeRank(a) - nodeRank(b) || (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0),
  );
  const visibleCore = sortedCore.slice(0, MAX_RENDER_NODES);
  const visibleFields = showFields
    ? [...fields]
        .sort((a, b) => (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0))
        .slice(0, FIELD_RENDER_CAP)
    : [];
  const visible = [...visibleCore, ...visibleFields];
  const ids = new Set(visible.map((n) => n.id));
  return {
    visible,
    visibleEdges: edges.filter((e) => ids.has(e.source) && ids.has(e.target)),
    hidden: nodes.length - visible.length,
  };
}

/** 布局配置：分层（DAG 自上而下或从左到右）｜力导向（环/交互定位）｜血缘度径向（同心圆，依赖引用数高者居中）。
 *  label：节点与边 label 的综合折行度量（{maxLines, maxLineChars}，无长标注图为 {1,0}）——
 *  分层布局据其放大 ranksep/nodesep：多行标签/边表达式纵向需要更高层距、长行横向需要更大
 *  节点距，保证完整「库.表.列」与边旁完整加工表达式不被邻节点/相邻层/其他标注压字
 *  （字段级血缘图“布局紧凑、表列名/表达式看不全”的根因）。
 */
function layoutConfig(
  layoutMode: "hierarchy" | "force" | "radial",
  direction: "TB" | "LR" = "TB",
  label?: { maxLines: number; maxLineChars: number },
) {
  if (layoutMode === "hierarchy") {
    // 分层布局：血缘 DAG 自上而下（表→指标）或从左到右，节点多时比力导向清晰得多
    // 紧凑度策略：
    //  - 全景大图（节点多）：ranksep 收紧到 36、nodesep 40，让 160 节点大图高度控制在 ~400px，
    //    fitView 缩放后能均匀铺满画布中央（不再堆底部）；文字压在最小需求以上即可。
    //  - 折行图（字段/长标签）：ranksep/nodesep 按 maxLines/maxLineChars 放宽，跨层不压字。
    const hasLongLabel = (label?.maxLineChars ?? 0) > 20;
    const nodeGap = hasLongLabel
      ? Math.min(240, Math.max(90, (label?.maxLineChars ?? 0) * 6.2 + 40))
      : direction === "LR"
        ? 50
        : 40;
    const extraRank = hasLongLabel ? Math.max(0, (label?.maxLines ?? 1) - 1) * 16 : 0;
    return {
      type: "antv-dagre",
      rankdir: direction,
      align: direction === "LR" ? "UL" : "DL",
      nodesep: direction === "LR" ? Math.max(50, nodeGap) : Math.max(40, nodeGap),
      ranksep: direction === "LR" ? Math.max(50, 50 + extraRank) : Math.max(36, 36 + extraRank),
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
  /** 分层布局方向（仅 hierarchy 生效；force/radial 忽略） */
  direction: "TB" | "LR";
  height: number;
  /** 搜索命中的节点 id 集合（空集=无搜索，全部恢复常态）。
   *  父组件计算后传入：图内节点可能已被聚焦为上下游子图，这里只负责「命中即高亮」。 */
  searchMatchIds: Set<string>;
  /** 非命中节点是否压暗（inactive）。
   *  子图聚焦模式为 false——画布上剩下的都是命中节点的上下游，压暗会让整图发灰、
   *  命中节点反而不突出；仅「全图仅标亮」模式为 true（保留旧的高亮定位语义）。 */
  searchDimOthers: boolean;
  onNodeClick: (node: AssetGraphNode) => void;
  /** 图渲染完成回调（父组件用于清除布局切换 loading） */
  onReady: () => void;
  /** 是否启用数仓分层徽标描边（非 PII 非环时按表名前缀用层色描边） */
  layerBadges?: boolean;
  /** 悬停高亮是否压暗非链节点（false=只高亮链上、不压暗其余；见外层 dimOnHover） */
  dimOnHover?: boolean;
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
  direction,
  height,
  searchMatchIds,
  searchDimOthers,
  onNodeClick,
  onReady,
  layerBadges = true,
  dimOnHover = true,
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
  // graphReady 的 ref 镜像：mount effect 内注册的 G6 事件回调（闭包捕获 mount 时初值）
  // 需读最新渲染状态——用 ref 规避闭包过期（修复：旧 hover 回调因捕获 graphReady=false 从不生效）
  const graphReadyRef = useRef(false);
  graphReadyRef.current = graphReady;
  // dimOnHover 的 ref 镜像：mount effect 注册的 hover 回调需读最新值（同 graphReady 规避闭包过期）
  const dimOnHoverRef = useRef(dimOnHover);
  dimOnHoverRef.current = dimOnHover;
  // 「点击聚焦」的当前节点 id：点击节点后该血缘链保持高亮（其余压暗），点击画布空白才清空恢复全亮。
  // 与悬停高亮（pointerenter/leave）并存——悬停是瞬态的（移出即按聚焦态恢复），聚焦是持久的（点击空白才清）。
  const focusedIdRef = useRef<string | null>(null);
  // label 折行度量（节点 + 边）：字段节点「库.表.列」折行、字段血缘边「加工方式：表达式」
  // 折行后纵向变高（多行）、横向变宽（最长行），需同步加大 dagre ranksep（层间距，防层间
  // 压字/压边标注）与 nodesep（节点间距，防同层标签与边标注横向互压）。
  // 经 ref 供 mount effect 构建布局配置时读取最新值（与 dimOnHoverRef 同模式规避闭包过期）。
  const labelMetrics = useMemo(() => {
    let maxLines = 1;
    let maxLineChars = 0;
    const absorb = (lines: string[]) => {
      maxLines = Math.max(maxLines, lines.length);
      for (const ln of lines) maxLineChars = Math.max(maxLineChars, ln.length);
    };
    for (const n of nodes) {
      if ((n as AssetGraphNode).type !== "field") continue;
      absorb(wrapFieldLabel((n as AssetGraphNode).label ?? "", 24).split("\n"));
    }
    for (const e of edges) {
      const edgeLabel = (e as RenderEdge).edgeLabel;
      if (!edgeLabel || !edgeLabel.trim()) continue;
      absorb(wrapEdgeExpr(edgeLabel).split("\n"));
    }
    return { maxLines, maxLineChars };
  }, [nodes, edges]);
  const labelMetricsRef = useRef(labelMetrics);
  labelMetricsRef.current = labelMetrics;
  // 边流动动画的取消函数（render 后启动、数据重载/卸载时取消）
  const edgeFlowCancelRef = useRef<(() => void) | null>(null);

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
  // 血缘邻接表（排除锚定边）：hover 路径高亮用，BFS 收集上下游 K 跳（非 1 跳邻域）
  const adjacency = useMemo(() => buildLineageAdjacency(edges), [edges]);
  const adjacencyRef = useRef(adjacency);
  adjacencyRef.current = adjacency;
  const edgesRef = useRef(edges);
  edgesRef.current = edges;
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
      // 泳道折叠聚合节点：用该泳道层色做填充 + 白色粗描边（可点击展开的视觉暗示），
      // 与普通按域着色节点区分——一眼看出"这是一个折叠层，点击可展开"。
      const cl = collapsedLayerOf(n);
      // 血缘度缩放（与渲染一致性）：原始 r = max(base, 12 + degree*1.2)，表*2.0/字段*1.3
      const r = Math.max(base, 12 + d * 1.2);
      const size = cl
        ? [r * 2.6, r * 1.6] // 聚合节点略大，便于点击展开
        : t === "table"
          ? [r * 2.0, r * 1.2]
          : t === "field"
            ? [r * 1.3, r * 0.7]
            : r;
      const fill = cl
        ? (LAYER_STROKE[cl] ?? "#475569")
        : cyc
          ? "#ff8a80"
          : n?.domain
            ? _allocateDomainColor(n.domain)
            : TYPE_FALLBACK_COLOR[n?.type ?? "unknown"] ?? TYPE_FALLBACK_COLOR.unknown;
      // 径向渐变填充：中心提亮 → 0.55 主色 → 边缘微压暗，节点呈轻微球面感。
      // 提亮/压暗幅度收紧（38/8 而非 62/18）避免小节点下中心过白、边缘过暗导致"脏"或
      // 与白底标签对比变差；@antv/g 的 r(cx,cy,r) 渐变按 shape bbox 归一化，字符串预计算
      // 避免每帧重建（性能与纯色一致）。
      const gradFill = `r(0.5, 0.5, 0.5) 0:${lightenHex(fill, 38)} 0.55:${fill} 1:${darkenHex(fill, 8)}`;
      const stroke = cyc
        ? "#e65100"
        : cl
          ? "#ffffff" // 聚合节点：白描边强调可点击
          : n?.pii
            ? "#c62828"
            : layer
              ? (LAYER_STROKE[layer] ?? "#ffffff")
              : "#ffffff";
      const lineWidth = cyc ? 3.5 : cl ? 3 : n?.pii ? 3 : layer ? 2.5 : 2;
      const hub = !cyc && !cl && d >= 8;
      map.set(id, { size, fill: gradFill, stroke, lineWidth, hub });
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
        // 修复节点堆底部：之前 autoFit:"center" 仅平移不缩放，dagre TB 160 节点 bbox
        // (~640x1400) 被平移到画布中央后，超出画布高度的节点堆在底部溢出区不可见、看起来"节点都暗"。
        // 改 autoFit:"view"：G6 v5 初始化时执行一次完整 fitView（缩放+居中），与后续 render().then
        // 的手动 fitView({when:"always"}) 一致——bbox 计算+适配画布一起做，不会有平移+缩放分裂
        // 导致的部分溢出。
        autoFit: "view",
        padding: [32, 32, 32, 32],
        // 全局 animation 用对象（非 false）激活元素动画管线，使节点/边的 enter/exit 淡入淡出
        // 真正生效（此前 false 会短路所有元素动画，enter/exit 配置实际从未驱动）。
        // 关键：必须显式把 node/edge 的 update/translate 置 false——G6 默认主题给两者配了
        // x/y 位置动画（base.js node.update/translate、edge.translate），全局非 false 时 force
        // 布局迭代会触发位置插值、shape 未就绪即 draw 崩溃（"Cannot read properties of
        // undefined (reading 'draw')"）。覆盖为 false 后数据更新/布局迭代保持瞬时，
        // 仅 enter/exit（透明度）与 hover 状态切换（标量字段，见 node.animation.update）走动画。
        animation: { duration: 300 },
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
                : "rgba(15,23,42,0.22)",
            shadowBlur: (d: NodeData) =>
              (d.data as AssetGraphNode | undefined)?.anchor || cycleNodesRef.current.has(String(d.id))
                ? 0
                : 10,
            shadowOffsetY: (d: NodeData) =>
              (d.data as AssetGraphNode | undefined)?.anchor ? 0 : 3,
            // 柔光 halo：节点填充色提亮版作为外圈，让节点从画布上"发光"、更立体。
            // 枢纽节点（血缘度≥8）halo 略宽更实，形成"骨干发光"层次；幅度收紧以免密集
            // 大图下邻接节点光晕相互侵染、显得"脏乱"。
            halo: (d: NodeData) => !(d.data as AssetGraphNode | undefined)?.anchor,
            haloStroke: (d: NodeData) => {
              const n = d.data as AssetGraphNode | undefined;
              const base = cycleNodesRef.current.has(String(d.id)) ? "#ff8a80" : domainColor(n);
              return lightenHex(base, 90);
            },
            haloLineWidth: (d: NodeData) =>
              (nodeStyleCacheRef.current.get(String(d.id))?.hub as boolean | undefined)
                ? 11
                : 8,
            haloStrokeOpacity: (d: NodeData) =>
              cycleNodesRef.current.has(String(d.id))
                ? 0.5
                : (nodeStyleCacheRef.current.get(String(d.id))?.hub as boolean | undefined)
                  ? 0.45
                  : 0.32,
            // 类型图标：Lucide 风格 24x24 线性 SVG（指标=折线+端点圆 / 表=表格+列头圆点 /
            // 字段=列表+段头圆点），内嵌深色半透明圆衬底让白线条在任意节点填充色上都清晰。
            // 尺寸按节点形状收窄（表节点扁、字段节点更扁），避免溢出节点边界
            icon: (d: NodeData) => !(d.data as AssetGraphNode | undefined)?.anchor,
            iconSrc: (d: NodeData) =>
              nodeIconSrc((d.data as AssetGraphNode | undefined)?.type),
            iconWidth: (d: NodeData) => {
              const t = (d.data as AssetGraphNode | undefined)?.type;
              // 加大 50% 占比：表 20、字段 16、metric 22（接近节点内部直径，比之前 14-16 显著更醒目）
              return t === "table" ? 20 : t === "field" ? 16 : 22;
            },
            iconHeight: (d: NodeData) => {
              const t = (d.data as AssetGraphNode | undefined)?.type;
              return t === "table" ? 16 : t === "field" ? 14 : 22;
            },
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
              // 字段节点 label 为「库.表.列」完整标识（字段级血缘查询/钻取），长度常超
              // 表级默认阈值（40）——放宽到 120 保证完整表列名不被截断；表级/指标节点维持
              // 40（一般血缘节点名完整显示，仅极长名称截断）。
              // 字段节点同时按点折行（每行 ≤24 字符）：完整内容多行展示，避免长 label 与
              // 邻节点压字导致字段级图「布局紧凑、表列名看不全」。
              return n?.type === "field"
                ? wrapFieldLabel(n?.label ?? String(d.id), 24)
                : trimLabel(n?.label ?? String(d.id), 40);
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
            // 悬停/搜索高亮：金色描边 + 增强光晕 + 标签加粗，保留径向渐变填充不覆盖，
            // 高亮节点从图中"点亮"而非被平涂遮盖
            active: {
              stroke: "#f59e0b",
              lineWidth: 3,
              haloLineWidth: 16,
              haloStroke: "#fbbf24",
              haloStrokeOpacity: 0.8,
              labelFontWeight: 700,
              labelFill: "#b45309",
            },
            inactive: { opacity: 0.2 },
            // 大数据量 LOD：用户主动滚轮缩到很小时（zoom < 0.35）隐藏重绘制层
            //（halo/阴影/icon/badge）以保帧率；**标签与白底 pill 始终保留**——
            // 「节点具体信息」是用户最需要看到的，compact 触发时不能丢掉名字。
            compact: {
              iconOpacity: 0,
              haloStrokeOpacity: 0,
              haloOpacity: 0,
              shadowBlur: 0,
              shadowOffsetY: 0,
              badgeOpacity: 0,
            },
          },
          // 节点动画：加载淡入、移除淡出（透明度过渡，真正生效需全局 animation 非 false）。
          // update 配**标量字段**（描边/光晕/标签）——hover 状态切换带动画（见 pointerenter 单节点
          // setElementState(..., true)），视觉上"点亮/熄灭"有 200ms 过渡；不配 x/y 位置字段，
          // 且 translate 显式 false——force 布局迭代时位置由 G6 内部驱动，位置动画会触发
          // 未就绪 shape 的 draw 崩溃（draw undefined），保持瞬时才安全。
          animation: {
            enter: [{ fields: ["opacity"], duration: 350, easing: "ease-out" }],
            exit: [{ fields: ["opacity"], duration: 200 }],
            update: [
              {
                fields: [
                  "stroke",
                  "lineWidth",
                  "haloLineWidth",
                  "haloStrokeOpacity",
                  "labelFontWeight",
                  "labelFill",
                ],
                duration: 200,
                easing: "ease-out",
              },
            ],
            translate: false,
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
            // 粗细与透明度按「两端血缘度总和」分层：骨干边（连接枢纽）清晰突出，
            // 叶子边淡雅退后，形成"主干醒目、枝叶退让"的视觉层次。
            // 跨层长边（layerSpan≥2，泳道间直达、横穿多层）是线团视觉主因：额外降一档
            // 透明度 + 细线，让"跨层束"整体退后为淡色底纹，避免与层内骨干边抢视觉。
            lineWidth: (e) => {
              const d = e.data as RenderEdge | undefined;
              if (d?.anchorEdge) return 0;
              const span = d?.layerSpan ?? 0;
              const total =
                (degreeMapRef.current.get(String(e.source)) ?? 0) +
                (degreeMapRef.current.get(String(e.target)) ?? 0);
              if (d?.inCycle) return 2.4;
              if (span >= 2) return 1.1; // 跨层束：统一细线
              return total >= 10 ? 1.9 : total >= 5 ? 1.5 : 1.2;
            },
            strokeOpacity: (e) => {
              const d = e.data as RenderEdge | undefined;
              if (d?.anchorEdge) return 0;
              if (d?.inCycle) return 1;
              const span = d?.layerSpan ?? 0;
              const total =
                (degreeMapRef.current.get(String(e.source)) ?? 0) +
                (degreeMapRef.current.get(String(e.target)) ?? 0);
              if (span >= 2) return 0.22; // 跨层束：统一淡透明度（层内按血缘度分层）
              return total >= 10 ? 0.92 : total >= 5 ? 0.75 : 0.52;
            },
            // 虚线分层：骨干边（连接枢纽，血缘度总和≥10）用「数据流管道」式虚线 + 流动动画
            //（见 startEdgeFlow），次骨干（≥5）细虚线静态，叶子边实线——与线宽/透明度分层呼应，
            // 形成"主干流动、枝叶静止"的方向感。环边保留红色虚线警示。
            // 跨层长边（span≥2）不再参与虚线分层（保持实线淡色底纹，避免虚线加剧"乱"）。
            lineDash: (e) => {
              const d = e.data as RenderEdge | undefined;
              if (d?.anchorEdge) return undefined;
              if (d?.inCycle) return [6, 4];
              const span = d?.layerSpan ?? 0;
              if (span >= 2) return undefined;
              const total =
                (degreeMapRef.current.get(String(e.source)) ?? 0) +
                (degreeMapRef.current.get(String(e.target)) ?? 0);
              return total >= 10 ? [8, 6] : total >= 5 ? [5, 4] : undefined;
            },
            // 虚线流动起点（rAF 驱动递增取模）；非虚线边该属性无视觉效果
            lineDashOffset: 0,
            endArrow: (e) => !(e.data as RenderEdge | undefined)?.anchorEdge,
            startArrow: (e) => {
              const d = e.data as RenderEdge | undefined;
              if (d?.anchorEdge) return false;
              return d?.bidirectional ? true : false;
            },
            radius: 10,
            // 字段级血缘边的加工标注：携带 edgeLabel 的边（字段钻取子图/字段级血缘查询图）
            // 在连线中点显示「加工方式：表达式」完整原文——长表达式按词折行（wrapEdgeExpr）
            // 成多行完整展示、不再截断省略；表级主图边无 edgeLabel → 空串不渲染。
            labelText: (e) => {
              const d = e.data as RenderEdge | undefined;
              return d?.edgeLabel ? wrapEdgeExpr(d.edgeLabel) : "";
            },
            labelPlacement: "center",
            labelOffset: 8,
            labelBackground: true,
            labelBackgroundFill: "#ffffff",
            labelBackgroundPadding: [2, 6],
            labelBackgroundLineWidth: 1,
            labelBackgroundStroke: "#cbd5e1",
            labelBackgroundRadius: 4,
            labelFill: "#475569",
            labelFontSize: 11,
            labelFontWeight: 500,
          },
          // 路径高亮状态（hover 血缘链）：active=链上边醒目（加亮不加粗，保留方向箭头），
          // inactive=非链边整体压暗到 0.05，让"从哪来/流向哪"一目了然。
          state: {
            active: { strokeOpacity: 0.95 },
            inactive: { strokeOpacity: 0.05 },
          },
          // 边动画：加载淡入、移除淡出。translate 显式 false——force 布局迭代时
          // 边端点位置由 G6 内部驱动，位置动画会触发 draw 崩溃（与节点同理）。
          // 流动效果（lineDashOffset）不用 G6 动画系统（keyframes 起止相同会被过滤），
          // 由渲染完成后的 rAF 手动驱动（见 startEdgeFlow）。
          animation: {
            enter: [{ fields: ["opacity"], duration: 350, easing: "ease-out" }],
            exit: [{ fields: ["opacity"], duration: 200 }],
            update: false,
            translate: false,
          },
        },
        layout: layoutConfig(layoutMode, direction, labelMetricsRef.current),
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
              const div = document.createElement("div");
              div.style.fontSize = "12px";
              div.style.lineHeight = "1.6";
              div.style.maxWidth = "360px";
              div.style.wordBreak = "break-all";
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
              if (id) {
                // 优先节点：展示名称 + 血缘度
                const node = graph?.getNodeData(String(id))?.data as AssetGraphNode | undefined;
                if (node && !node.anchor) {
                  // 字段节点常驻 label 仅列名（表泳道去拥挤：表名只出现于图例），hover 时
                  // 用 node.table 补全「库.表.列」完整标识；无 table（主图字段节点 label 已是
                  // 完整标识）则原样展示。
                  const table = (node as { table?: string }).table;
                  const label = node.type === "field" && table
                    ? `${table}.${node.label ?? ""}`
                    : (node.label ?? String(id));
                  const up = outDegreeMapRef.current.get(String(id)) ?? 0;
                  const down = inDegreeMapRef.current.get(String(id)) ?? 0;
                  const total = up + down;
                  div.innerHTML = `<b>${esc(label)}</b><br/>依赖 ${up} 项（上游）<br/>被 ${down} 项引用（下游）<br/><span style="color:#e65100">血缘度 ${total}</span>`;
                  return div;
                }
                // 兜底边：字段级血缘边——edgeLabel 已是「加工方式：表达式」完整原文（图上
                // 连线中点折行展示），tooltip 放大字号便于核对全文（\n 在 HTML 中折叠为空格）。
                // 无表达式的普通边不弹内容。
                const edge = graph?.getEdgeData(String(id)) as
                  | { data?: RenderEdge }
                  | undefined;
                const ed = edge?.data;
                if (ed?.edgeLabel || ed?.fullExpr) {
                  div.innerHTML = `<b>${esc(ed.edgeLabel ?? "字段映射")}</b>${
                    ed.fullExpr && ed.edgeLabel !== ed.fullExpr
                      ? `<br/><span style="color:#1a73e8">加工表达式：${esc(ed.fullExpr)}</span>`
                      : ""
                  }`;
                  return div;
                }
              }
              return null as unknown as HTMLElement;
            },
          },
        ],
      });
      graphRef.current = graph;

      // —— 血缘链高亮共享逻辑 ——
      /** 沿血缘链点亮 center 上下游各 K 跳节点/边（其余 inactive 压暗）；dimOthers=false 只点亮链上、不压暗。 */
      function applyChainHighlight(center: string, dimOthers: boolean) {
        if (!graph || graph.destroyed) return;
        const pathNodes = collectPathNodes(adjacencyRef.current, center);
        const pathEdges = collectPathEdges(edgesRef.current, pathNodes);
        const nodeRecord: Record<string, string | string[]> = {};
        for (const n of graph.getNodeData()) {
          const nid = String(n.id);
          if (nid === center) continue; // 中心节点单独动画点亮
          if (dimOthers) {
            nodeRecord[nid] = pathNodes.has(nid)
              ? stateWithCompact("active")
              : stateWithCompact("inactive");
          } else if (pathNodes.has(nid)) {
            nodeRecord[nid] = stateWithCompact("active");
          }
        }
        const edgeRecord: Record<string, string | string[]> = {};
        for (const e of edgesRef.current) {
          if ((e as RenderEdge | undefined)?.anchorEdge) continue;
          const eid = `${String(e.source)}-${String(e.target)}`;
          if (dimOthers) {
            edgeRecord[eid] = pathEdges.has(eid) ? "active" : "inactive";
          } else if (pathEdges.has(eid)) {
            edgeRecord[eid] = "active";
          }
        }
        void graph.setElementState(nodeRecord, false).catch(() => {});
        void graph.setElementState(edgeRecord, false).catch(() => {});
        void graph.setElementState(center, stateWithCompact("active"), true).catch(() => {});
      }

      /** 清空全部节点/边状态（回落到 style 默认值 → 全亮）。 */
      function clearAllStates() {
        if (!graph || graph.destroyed) return;
        const nodeRecord: Record<string, string | string[]> = {};
        for (const n of graph.getNodeData()) nodeRecord[String(n.id)] = stateWithCompact([]);
        const edgeRecord: Record<string, string | string[]> = {};
        for (const e of edgesRef.current) {
          if ((e as RenderEdge | undefined)?.anchorEdge) continue;
          edgeRecord[`${String(e.source)}-${String(e.target)}`] = [];
        }
        void graph.setElementState(nodeRecord, false).catch(() => {});
        void graph.setElementState(edgeRecord, false).catch(() => {});
      }

      graph.on<IElementEvent>("node:click", (evt) => {
        if (!graph || graph.destroyed) return;
        const raw = evt.target as { id?: string; __data__?: { id?: string } } | undefined;
        const id = raw?.id ?? raw?.__data__?.id;
        if (!id) return;
        const node = graph.getNodeData(String(id))?.data as AssetGraphNode | undefined;
        if (node && !node.anchor) {
          // 泳道锚点不响应点击；点击真实节点 = 持久聚焦其血缘链（其余压暗），
          // 悬停移出不会清空，点击画布空白（canvas:click）才清空恢复全亮——避免「整图暗无亮节点」卡死。
          focusedIdRef.current = String(id);
          try {
            applyChainHighlight(String(id), dimOnHoverRef.current);
          } catch {
            // 高亮为装饰性交互，过渡期失败静默忽略
          }
          onNodeClickRef.current?.(node);
        }
      });

      // 悬停路径高亮：沿血缘边上下游各 K 跳收集"血缘链"节点与路径边，链上节点/边高亮、
      // 其余全部压暗——一眼看出节点从哪来、流向哪、与谁同链。
      // 性能优化：pointerenter 高频触发（跨节点移动），用 rAF 节流到每帧只处理最后一次；
      // 节点与边各用**批量 record**（单次调用），替代逐节点循环。
      // 中心节点单独 setElementState(..., true) 走 update 标量动画（描边/光晕 200ms 过渡）。
      // 与点击聚焦共存：悬停是瞬态（移出后若有点击聚焦则恢复聚焦链高亮、否则清空恢复全亮）。
      let hoverRaf = 0;
      graph.on<IElementEvent>("node:pointerenter", (evt) => {
        if (!graph || graph.destroyed || !graphReadyRef.current) return;
        const raw = evt.target as { id?: string; __data__?: { id?: string } } | undefined;
        const id = raw?.id ?? raw?.__data__?.id;
        if (!id) return;
        cancelAnimationFrame(hoverRaf);
        hoverRaf = requestAnimationFrame(() => {
          if (!graph || graph.destroyed) return;
          try {
            applyChainHighlight(String(id), dimOnHoverRef.current);
          } catch {
            // 高亮为装饰性交互，过渡期失败静默忽略
          }
        });
      });
      graph.on("node:pointerleave", () => {
        if (!graph || graph.destroyed || !graphReadyRef.current) return;
        cancelAnimationFrame(hoverRaf);
        try {
          // 有「点击聚焦」：离开悬停节点后恢复聚焦链高亮（保持聚焦态，不因移动而清空）；
          // 无聚焦：清空全部节点/边状态（回落到 style 函数默认值 → 全亮）。
          if (focusedIdRef.current) {
            applyChainHighlight(focusedIdRef.current, dimOnHoverRef.current);
          } else {
            clearAllStates();
          }
        } catch {
          // 忽略过渡期状态清理失败
        }
      });

      // 点击画布空白：清空「点击聚焦」并恢复全亮（targetType='node' 说明是节点点击冒泡，忽略不打断聚焦）。
      // 修复：此前点击节点后其余节点被压暗，再点空白无任何处理器恢复 → 整图暗沉无亮节点的卡死观感。
      graph.on<IElementEvent>("canvas:click", (evt) => {
        if (!graph || graph.destroyed || !graphReadyRef.current) return;
        if ((evt as { targetType?: string }).targetType === "node") return;
        if (!focusedIdRef.current) return;
        focusedIdRef.current = null;
        clearAllStates();
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
        edgeFlowCancelRef.current?.();
        edgeFlowCancelRef.current = null;
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
      .then(async () => {
        if (graph.destroyed) return;
        setGraphReady(true);
        onReadyRef.current();
        // 按节点规模自适应 fitView（带平滑缩放进入动画）：
        //  - 节点多（全景）→ always 适配填满画布，但大图 always 会把 zoom 压到 <0.2，
        //    节点缩成亚像素、信息全丢；故叠加最小缩放下限 0.35 保证标签可读，
        //    用户可手动滚轮继续缩（<0.35 触发 compact 隐藏重装饰层，标签仍保留）；
        //  - 节点少（聚焦视图）→ overflow 仅在内容超出视口时裁剪，不把少量节点放大填满画布。
        // 相机方法走 viewport 变换路径，与 force 布局的 shape draw 解耦，动画安全。
        try {
          // 修复节点堆底部：fitView 用 duration:800 动画时，dagre 布局还在过渡中，
          // fitView 拿到的是动画中间态的 bbox（节点全在底部 rank），居中后图仍偏下。
          // 改为 duration:0 立即 fit；初始化时的 padding  [32,32,32,32] 已保证四周留白。
          if (nodeCountRef.current > 5) {
            await graph.fitView({ when: "always" }, { duration: 0 });
            if (typeof graph.getZoom === "function" && graph.getZoom() < 0.35) {
              await graph.zoomTo?.(0.35, { duration: 0 });
            }
          } else {
            await graph.fitView({ when: "overflow" }, { duration: 0 });
          }
        } catch {
          /* fitView 偶尔在过渡期失败 */
        }
        // 大数据量 LOD：fitView 缩放下限 0.35 后，初始首屏通常不再触发 compact；
        // 此处保留 applyLod 仅用于用户后续滚轮缩到 < 0.35 时切换重装饰层（标签仍保留）。
        applyLod();
        // 边流动动画：只驱动有 lineDash 的边（骨干/次骨干/环边）。数据重载时先取消旧的再重启，
        // 避免上一批元素 shape 引用失效后仍被驱动。
        edgeFlowCancelRef.current?.();
        edgeFlowCancelRef.current = startEdgeFlow(
          graph,
          edges
            .filter((e) => {
              const d = e as RenderEdge | undefined;
              if (d?.anchorEdge) return false;
              if (d?.inCycle) return true;
              const total =
                (degreeMapRef.current.get(String(e.source)) ?? 0) +
                (degreeMapRef.current.get(String(e.target)) ?? 0);
              return total >= 5;
            })
            .map((e) => `${e.source}-${e.target}`),
        );
      })
      .catch((err) => {
        console.error("[AssetGraph] G6 render 失败，降级为表格", err);
        setRenderFailed(true);
        onReadyRef.current();
      });
    setRenderFailed(false);
  }, [nodes, edges]);

  // 搜索定位：命中节点高亮 + 聚焦首个匹配；清空时恢复全量状态。
  // graphReady 变化（含重挂载后重新渲染完成）时自动重跑，保证布局切换后搜索仍生效。
  // 命中集合由父组件计算（基于筛选后的候选集），此处只负责上色——子图聚焦模式下
  // 传入的 nodes 已是命中节点的上下游子图，故非命中节点保持常态（不压暗）。
  useEffect(() => {
    const graph = graphRef.current;
    if (!graph || graph.destroyed || !graphReady) return;
    try {
      const allNodes = graph.getNodeData?.() as unknown;
      const nodeList = Array.isArray(allNodes) ? allNodes : [];
      if (searchMatchIds.size === 0) {
        for (const n of nodeList) safeSetElementState(graph, String(n.id), stateWithCompact([]));
        return;
      }
      for (const n of nodeList) {
        const id = String(n.id);
        safeSetElementState(
          graph,
          id,
          searchMatchIds.has(id)
            ? stateWithCompact("active")
            : stateWithCompact(searchDimOthers ? "inactive" : []),
        );
      }
      safeFocusElement(graph, [...searchMatchIds][0]);
    } catch {
      // 图状态变化导致的瞬时异常（如数据重载中）静默忽略，避免崩溃
    }
  }, [searchMatchIds, searchDimOthers, graphReady, nodes]);

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
            if (g && !g.destroyed) g.fitView(undefined, { duration: 600, easing: "ease-out" });
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
  direction = "TB",
  defaultCollapsedLayers,
  dimOnHover = true,
  defaultShowAll = false,
}: AssetGraphProps) {
  const onNodeClickRef = useRef(onNodeClick);
  onNodeClickRef.current = onNodeClick;
  const [showAll, setShowAll] = useState(defaultShowAll);
  // 前端筛选：按节点类型过滤 + 按 label 搜索定位（不重新请求后端）
  const [typeFilter, setTypeFilter] = useState<string[]>([]);
  const [searchText, setSearchText] = useState("");
  // 搜索范围：命中节点向外追溯的跳数（0=「全图仅标亮」，保留旧的高亮定位语义：
  // 不裁剪节点，只在全图上把命中节点标亮、其余压暗）。默认 2 跳——血缘场景「上下游」
  // 多为 1-2 跳可达，过大跳数在枢纽节点上易把整图拉回（等价没搜），控件上限 8。
  const [searchHops, setSearchHops] = useState<number>(2);
  const trimmedSearch = searchText.trim().toLowerCase();
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
  // 分层布局方向：TB=自上而下（默认，数仓源头在上字段在下）；LR=从左到右（源头在左字段在右）。
  // 由工具栏「方向」Select 手动切换；prop direction 作为初始值（父组件可指定首屏方向）。
  const [directionState, setDirectionState] = useState<"TB" | "LR">(direction);
  useEffect(() => setDirectionState(direction), [direction]);
  // 泳道折叠（子图折叠）：把某数仓分层/语义带的全部节点收成一个聚合节点，减少中间层堆叠噪声。
  // collapsedLayers 为当前折叠的泳道集合；工具栏多选切换，点击聚合节点单独展开该层。
  // defaultCollapsedLayers 提供初始折叠集合（结构概览模式：进入即全层聚合、显示层间主干）。
  const [collapsedLayers, setCollapsedLayers] = useState<string[]>(defaultCollapsedLayers ?? []);
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

  // 分层方向切换处理：更新方向 + 触发代际重挂载 + 显示 loading（与布局切换同模式）
  const handleDirectionChange = (v: "TB" | "LR") => {
    if (v === directionState) return; // 同一方向不重挂载
    setDirectionState(v);
    setLayoutTick((t) => t + 1);
    setLayoutSwitching(true);
  };

  // 类型筛选（空 = 全部）；字段折叠（showFieldsOn=false）时剔除字段节点（血缘总览降噪）
  const filteredNodes = useMemo(() => {
    let list = typeFilter.length === 0 ? nodes : nodes.filter((n) => typeFilter.includes(n.type));
    if (showFieldsOn === false) list = list.filter((n) => n.type !== "field");
    return list;
  }, [nodes, typeFilter, showFieldsOn]);

  // 搜索命中：在当前筛选（类型/字段）后的候选集内按 label 模糊匹配（id 也参与，
  // 便于直接粘 `table:db.tbl` 这类完整节点 id 定位）
  const searchMatchIds = useMemo(() => {
    if (!trimmedSearch) return new Set<string>();
    return new Set(
      filteredNodes
        .filter(
          (n) =>
            n.label.toLowerCase().includes(trimmedSearch) ||
            n.id.toLowerCase().includes(trimmedSearch),
        )
        .map((n) => n.id),
    );
  }, [filteredNodes, trimmedSearch]);

  // 搜索聚焦子图：命中节点的上下游 searchHops 跳（hops=0 → 全图仅标亮，不裁剪）。
  // 无命中时返回空子图（而非全量）——搜不到就该明确告知，避免「搜了却还是全图」的错觉。
  const searchFocus = useMemo(() => {
    if (!trimmedSearch || searchHops <= 0) return null;
    if (searchMatchIds.size === 0) return { nodes: [], edges: [], truncated: false };
    return buildSearchFocus(filteredNodes, edges, searchMatchIds, searchHops);
  }, [trimmedSearch, searchHops, searchMatchIds, filteredNodes, edges]);

  // 血缘度筛选（聚焦枢纽）：仅保留依赖引用数 ≥ 阈值的节点。度统计基于完整 edges
  //（含被 typeFilter/字段折叠隐藏的节点），保证「枢纽」判断不被筛选顺序影响。
  // 搜索聚焦时让位——「看这个节点的上下游」是比「只看枢纽」更具体的意图，叠加会把
  // 子图边缘的上下游叶子剪掉，用户看到的就是「搜了却看不到上下游」。
  const degreeFilteredNodes = useMemo(() => {
    if (searchFocus) return searchFocus.nodes;
    if (minDegreeFilter <= 0) return filteredNodes;
    const dm = new Map<string, number>();
    for (const e of edges) {
      dm.set(e.source, (dm.get(e.source) ?? 0) + 1);
      dm.set(e.target, (dm.get(e.target) ?? 0) + 1);
    }
    return filteredNodes.filter((n) => (dm.get(n.id) ?? 0) >= minDegreeFilter);
  }, [searchFocus, filteredNodes, edges, minDegreeFilter]);

  // 聚焦时边集同步裁剪为子图内边；否则用完整边集（由 pickVisible 按可见节点再过滤）
  const scopedEdges = searchFocus ? searchFocus.edges : edges;

  // 限流渲染：核心节点（metric/table）按优先级+血缘度截断到 160；showFields 时
  // 字段节点按血缘度叠加（独立上限 400），不占用核心额度——避免大图按 rank
  // 截断时把 field 挤出可见集（此前用户点「显示字段」看不到字段的根因）
  const {
    visible: visibleNodes,
    visibleEdges,
    hidden,
  } = useMemo(
    // 聚焦子图已按 SEARCH_FOCUS_MAX_NODES 上限收束，不再走 LOD 截断——
    // 否则「搜了某个节点」后 160 上限可能把命中节点本身挤掉，聚焦语义被破坏
    () => pickVisible(degreeFilteredNodes, scopedEdges, showAll || Boolean(searchFocus), showFieldsOn),
    [degreeFilteredNodes, scopedEdges, showAll, showFieldsOn, searchFocus],
  );

  // 泳道折叠（子图折叠）：在环检测/泳道之前把被折叠泳道的节点收成聚合节点。
  // 折叠后节点/边进入后续全部管线（合并双向边→环检测→泳道），保证聚合节点也参与
  // 环/泳道语义（聚合节点按 collapsedLayer 归位原泳道）。
  const folded = useMemo(
    () => collapseLayers(visibleNodes, visibleEdges, collapsedLayers),
    [visibleNodes, visibleEdges, collapsedLayers],
  );
  const foldNodes = folded.nodes;
  const foldEdges = folded.edges;

  // 环检测 + 双向边合并：A↔B 合并为双箭头减少视觉噪声；SCC>2 的真环单独标记
  const mergedEdges = useMemo(() => mergeBidirectionalEdges(foldEdges), [foldEdges]);
  const cycleNodes = useMemo(
    () => findTrueCycles(mergedEdges, foldNodes.map((n) => n.id)),
    [mergedEdges, foldNodes],
  );
  const renderEdges = useMemo(
    () => markCycleEdges(mergedEdges, cycleNodes),
    [mergedEdges, cycleNodes],
  );
  // 跨层跨度标记（层间边束降噪）：基于折叠后节点计算泳道跨度。节点/边变化时重算。
  const spanEdges = useMemo(() => markLaneSpan(renderEdges, foldNodes), [renderEdges, foldNodes]);
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
    if (layoutMode !== "force") return spanEdges;
    const dm = new Map<string, number>();
    for (const e of spanEdges) {
      dm.set(e.source, (dm.get(e.source) ?? 0) + 1);
      dm.set(e.target, (dm.get(e.target) ?? 0) + 1);
    }
    return filterDenseForceEdges(spanEdges, dm, spanEdges.length > MAX_FORCE_DENSE_EDGES);
  }, [layoutMode, spanEdges]);

  // 语义泳道：仅分层布局下插入隐藏锚点 + 锚定边（力导向不需要泳道）
  const laneData = useMemo(() => {
    if (!lanes || layoutMode !== "hierarchy") {
      return { nodes: foldNodes, edges: layoutEdges };
    }
    return applyLanes(foldNodes, layoutEdges);
  }, [lanes, layoutMode, foldNodes, layoutEdges]);

  // 可折叠泳道候选：当前可见节点中节点数 ≥2 的泳道（单节点泳道折叠无意义）。
  // 仅泳道模式（lanes && hierarchy）下提供折叠控件——折叠语义依赖泳道归位。
  const collapsibleOptions = useMemo(() => {
    if (!lanes || layoutMode !== "hierarchy") return [];
    const cnt = new Map<string, number>();
    for (const n of visibleNodes) {
      const lane = laneOfNode(n);
      if (lane) cnt.set(lane, (cnt.get(lane) ?? 0) + 1);
    }
    return [...cnt.entries()]
      .filter(([, c]) => c >= 2)
      .map(([lane, c]) => ({ value: lane, label: `${LANE_LABEL[lane] ?? lane}（${c}）` }));
  }, [lanes, layoutMode, visibleNodes]);

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
          <Button
            size="small"
            type="link"
            onClick={() => {
              const total = nodes.length;
              const heavy = total > 600;
              if (!heavy) {
                setShowAll(true);
                return;
              }
              Modal.confirm({
                title: "全量渲染大图？",
                content: `将一次性布局并渲染全部 ${total} 个节点（含关联边）。节点规模较大时布局与渲染会同步占用页面主线程，可能需要数秒甚至数十秒，期间页面会卡顿无响应。建议先用「域筛选」/搜索收窄范围；确需全量请点「继续」。`,
                okText: "继续全量",
                cancelText: "取消",
                okButtonProps: { danger: true },
                onOk: () => setShowAll(true),
              });
            }}
          >
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
      {folded.collapsedCount > 0 && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "6px 12px",
            marginBottom: 8,
            background: "rgba(124,58,237,0.08)",
            border: "1px solid rgba(124,58,237,0.35)",
            borderRadius: 6,
            fontSize: 13,
            color: "#5b21b6",
          }}
          data-testid="asset-graph-fold-banner"
        >
          <span>
            已折叠 <b>{folded.collapsedCount}</b> 个节点为泳道聚合节点（紫色层色块，点击可展开该层）。
            工具栏「泳道折叠」可调整；跨层长边已弱化为淡色底纹，聚焦核心血缘链更清晰。
          </span>
          <Button
            size="small"
            type="link"
            onClick={() => setCollapsedLayers([])} // 数据 effect 随折叠变化自动 setData 重排
          >
            全部展开
          </Button>
        </div>
      )}
      {searchFocus && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "6px 12px",
            marginBottom: 8,
            background: "rgba(22,119,255,0.08)",
            border: "1px solid rgba(22,119,255,0.35)",
            borderRadius: 6,
            fontSize: 13,
            color: "#0b3d91",
          }}
          data-testid="asset-graph-search-focus-banner"
        >
          {searchFocus.nodes.length === 0 ? (
            <span>
              没有匹配「<b>{searchText.trim()}</b>」的节点——可换更短的关键词，或切「全图仅标亮」在全图核对。
            </span>
          ) : (
            <span>
              已聚焦 <b>{searchMatchIds.size}</b> 个匹配节点的上下游 <b>{searchHops}</b> 跳：
              展示 <b>{searchFocus.nodes.length}</b> 个节点 / <b>{searchFocus.edges.length}</b> 条边
              （全图 {nodes.length} 个节点）。
              {searchFocus.truncated && " 已达子图上限，按距离优先截断——可缩小跳数或改用类型筛选。"}
            </span>
          )}
          <Button size="small" type="link" onClick={() => setSearchText("")}>
            清除搜索看全图
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
        <Select showSearch
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
        <Select showSearch
          allowClear
          placeholder="依赖 ≥ 0"
          style={{ minWidth: 130 }}
          // 搜索聚焦时血缘度筛选让位（见 degreeFilteredNodes 注释），禁用避免「选了却没生效」
          disabled={Boolean(searchFocus)}
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
          style={{ width: 200 }}
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          data-testid="asset-graph-search"
        />
        <InputNumber
          min={1}
          max={8}
          precision={0}
          // 0=「全图仅标亮」模式（保留旧的高亮定位语义：不裁剪节点，只在全图上把命中
          // 节点标亮、其余压暗）。默认 2 跳——血缘场景「上下游」多为 1-2 跳可达，
          // 过大跳数在枢纽节点上易把整图拉回（等价没搜），故上限 8。
          value={searchHops === 0 ? undefined : searchHops}
          onChange={(v) => setSearchHops(v != null && v >= 1 ? Math.min(Math.floor(v), 8) : 2)}
          addonBefore="上下游"
          addonAfter="跳"
          disabled={searchHops === 0}
          style={{ width: 176 }}
          data-testid="asset-graph-search-hops"
        />
        <Button
          size="middle"
          type={searchHops === 0 ? "primary" : "default"}
          onClick={() => setSearchHops(searchHops === 0 ? 2 : 0)}
          data-testid="asset-graph-search-highlight-mode"
        >
          {searchHops === 0 ? "全图标亮中" : "全图仅标亮"}
        </Button>
        <Select showSearch
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
        {layoutMode === "hierarchy" && (
          <Select showSearch
            value={directionState}
            onChange={handleDirectionChange}
            style={{ minWidth: 132 }}
            data-testid="asset-graph-direction"
            options={[
              { value: "TB", label: "方向：自上而下" },
              { value: "LR", label: "方向：从左到右" },
            ]}
          />
        )}
        {collapsibleOptions.length > 0 && (
          <Select
            mode="multiple"
            allowClear
            placeholder="泳道折叠（中间层收束）"
            style={{ minWidth: 210 }}
            value={collapsedLayers}
            onChange={(v: string[]) => setCollapsedLayers(v)}
            options={collapsibleOptions}
            maxTagCount="responsive"
            data-testid="asset-graph-collapse-lanes"
          />
        )}
        {(typeCounts.field ?? 0) > 0 && (
          <Button
            size="middle"
            data-testid="asset-graph-show-fields"
            type={showFieldsOn ? "primary" : "default"}
            onClick={() => setShowFieldsOn((v) => !v)}
          >
            {showFieldsOn ? "隐藏字段" : "显示字段"}
          </Button>
        )}
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
          key={`${layoutMode}-${direction}-${layoutTick}`}
          nodes={laneData.nodes}
          edges={laneData.edges}
          layoutMode={layoutMode}
          direction={directionState}
          height={h}
          searchMatchIds={searchMatchIds}
          searchDimOthers={searchHops === 0}
          onNodeClick={(n) => {
            // 泳道聚合节点：点击=展开该泳道（回到未折叠），不触发外部下钻
            const cl = collapsedLayerOf(n);
            if (cl) {
              setCollapsedLayers((prev) => prev.filter((l) => l !== cl));
              return;
            }
            onNodeClickRef.current?.(n);
          }}
          onReady={() => setLayoutSwitching(false)}
          layerBadges={layerBadges}
          dimOnHover={dimOnHover}
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
      <div className="asset-graph-legend">
        <div className="legend-group">
          <span className="muted">类型：</span>
          <span className="legend-swatch">
            <ShapeSwatch type="metric" /> 指标 {typeCounts.metric ?? 0}
          </span>
          <span className="legend-swatch">
            <ShapeSwatch type="table" /> 表 / 视图 {typeCounts.table ?? 0}
          </span>
          <span className="legend-swatch">
            <ShapeSwatch type="field" /> 字段 {typeCounts.field ?? 0}
          </span>
        </div>
        <div className="legend-group">
          <span className="muted">业务域：</span>
          {domains.map((d) => (
            <span className="legend-swatch" key={d}>
              <span className="legend-dot" style={{ background: domainColor({ domain: d }) }} />
              {d}
            </span>
          ))}
          {domains.length === 0 && <span className="muted">-</span>}
        </div>
        <div className="legend-group">
          <span className="legend-swatch">
            <span className="legend-dot" style={{ background: "#c62828" }} />
            <span className="muted">PII 描边 · 节点大小=血缘度 · 圆形/矩形/椭圆=指标/表/字段 · 右上角数字=依赖引用数</span>
          </span>
        </div>
        {hasLayerNodes && (
          <div className="legend-group">
            <span className="muted">数仓层：</span>
            {Object.entries(LAYER_STROKE).map(([layer, color]) => (
              <span className="legend-swatch" key={layer}>
                <span
                  className="legend-dot"
                  style={{ border: `2.5px solid ${color}`, background: "rgba(0,0,0,0.06)" }}
                />
                {layer.toUpperCase()}
              </span>
            ))}
          </div>
        )}
        <div className="legend-group">
          <span className="legend-swatch">
            <span
              className="legend-dot"
              style={{ borderRadius: "50%", border: "3px solid #e65100", background: "#ff8a80" }}
            />
            <span className="muted" style={{ color: "#e65100" }}>
              环节点（橙色描边）
            </span>
          </span>
        </div>
        <div className="legend-group">
          <span className="legend-swatch">
            <span
              style={{
                display: "inline-block",
                width: 18,
                height: 0,
                borderTop: "2px dashed #e53935",
              }}
            />
            <span className="muted" style={{ color: "#b71c1c" }}>
              环边（红色虚线）
            </span>
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
