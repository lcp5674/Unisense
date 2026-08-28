import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { listMetrics } from "../api";

// 回归锚点：指标目录回收站曾出现「已删指标不显示、未删 PUBLISHED 反而显示」——
// 根因是 listMetrics 内部构造 query string 时漏传 deleted 参数（参数声明有、
// 但没进 pageQs），后端收到缺省 False 一直查正常列表。本测试直接测真实
// listMetrics（不经组件 mock），锚定 deleted 确实进入请求 URL。
describe("listMetrics query string 透传（回收站 deleted 参数）", () => {
  const originalFetch = globalThis.fetch;
  let capturedUrl = "";

  beforeEach(() => {
    localStorage.clear();
    capturedUrl = "";
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      capturedUrl = String(input);
      return new Response(
        JSON.stringify({
          code: "OK",
          data: { items: [], total: 0, page: 1, page_size: 20 },
          message: "ok",
          trace_id: "t",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ) as Response;
    }) as typeof fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it("回收站视图 listMetrics 携带 deleted=true", async () => {
    await listMetrics({ deleted: true, page: 1, page_size: 20 });
    expect(capturedUrl).toContain("deleted=true");
  });

  it("正常列表不携带 deleted（缺省不污染 URL）", async () => {
    await listMetrics({ page: 1, page_size: 20 });
    expect(capturedUrl).not.toContain("deleted");
  });
});
