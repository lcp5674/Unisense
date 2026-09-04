/**
 * dagre 布局 Worker 客户端：单例 Worker + 按 jobId 分发响应的 Promise 封装。
 *
 * 仅在 `typeof Worker !== "undefined"` 时可用（真实浏览器）；测试/降级环境由
 * GraphCanvas 直接走同步 computeDagrePositions，不经过本模块。
 */
import type { DagreLayoutNode, DagreLayoutParams, DagrePosition } from "./dagreLayout";
import type { DagreLayoutRequest, DagreLayoutResponse } from "./dagreLayout.worker";

let worker: Worker | null = null;
let nextJobId = 0;
const pending = new Map<number, { resolve: (v: Map<string, DagrePosition>) => void }>();

function ensureWorker(): Worker {
  if (worker) return worker;
  // Vite 构建期识别 `new Worker(new URL(...))` 并把 worker 单独打包为独立 chunk
  const w = new Worker(new URL("./dagreLayout.worker.ts", import.meta.url), { type: "module" });
  w.onmessage = (ev: MessageEvent<DagreLayoutResponse>) => {
    const { jobId, positions } = ev.data;
    const p = pending.get(jobId);
    if (!p) return; // 已超时/被丢弃的过期 job
    pending.delete(jobId);
    p.resolve(new Map(positions));
  };
  w.onerror = () => {
    // worker 异常：拒绝全部在途请求，调用方降级处理（可重试/同步兜底）
    for (const [, p] of pending) p.resolve(new Map());
    pending.clear();
    worker?.terminate();
    worker = null;
  };
  worker = w;
  return w;
}

/**
 * 在 Worker 线程计算分层坐标。返回 Map<id, 中心坐标>。
 * 注意：调用方需自行处理竞态（响应按 jobId 匹配，过期结果不会误覆盖——但调用方
 * 仍应比较自身最新请求序号，丢弃旧请求的 resolve）。
 */
export function computeLayoutInWorker(
  nodes: DagreLayoutNode[],
  edges: { source: string; target: string }[],
  params: DagreLayoutParams,
): Promise<Map<string, DagrePosition>> {
  const w = ensureWorker();
  const jobId = ++nextJobId;
  return new Promise<Map<string, DagrePosition>>((resolve) => {
    pending.set(jobId, { resolve });
    const req: DagreLayoutRequest = { jobId, nodes, edges, params };
    w.postMessage(req);
  });
}
