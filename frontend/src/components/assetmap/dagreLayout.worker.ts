/**
 * dagre 分层布局 Web Worker——把全量血缘的大图布局从主线程挪到独立线程。
 *
 * G6 内建 antv-dagre 布局在 render() 内同步执行：1763+ 节点（含泳道锚点后近翻倍）
 * 的 dagre 计算实测阻塞主线程数十秒（LineageView.tsx:1197-1200 注释自证 66s），
 * 点「全量血缘」即卡死。本 Worker 在独立线程跑同一坐标算法（computeDagrePositions），
 * 算完把节点中心坐标回传主线程，主线程以「预设坐标」渲染（G6 不配 layout 时按
 * 数据 style.x/y 落位），UI 全程可交互。
 */
import { computeDagrePositions } from "./dagreLayout";
import type { DagreLayoutNode, DagreLayoutParams, DagrePosition } from "./dagreLayout";

export interface DagreLayoutRequest {
  /** 请求序号：主线程递增生成，响应携带原样返回，用于丢弃过期结果（快速切换竞态）。 */
  jobId: number;
  nodes: DagreLayoutNode[];
  edges: { source: string; target: string }[];
  params: DagreLayoutParams;
}

export interface DagreLayoutResponse {
  jobId: number;
  /** 坐标条目数组（Map 的 entries 快照）——postMessage 结构化克隆对 Map 兼容但数组更稳。 */
  positions: [string, DagrePosition][];
  /** 布局耗时（ms），仅用于诊断，不参与业务。 */
  ms: number;
}

const scope = self as unknown as {
  postMessage(msg: DagreLayoutResponse): void;
  onmessage: ((ev: MessageEvent<DagreLayoutRequest>) => void) | null;
};

scope.onmessage = (ev: MessageEvent<DagreLayoutRequest>) => {
  const { jobId, nodes, edges, params } = ev.data;
  const t0 = performance.now();
  let positions: [string, DagrePosition][] = [];
  try {
    positions = [...computeDagrePositions(nodes, edges, params).entries()];
  } catch (err) {
    // 布局异常不应让 worker 崩溃（后续 job 还需复用）；回传空表，主线程按兜底处理
    console.error("[dagreLayout.worker] 布局失败", err);
  }
  scope.postMessage({ jobId, positions, ms: performance.now() - t0 });
};

export {};
