import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { setConsumeToken, getConsumeToken, clearConsumeToken, consumeQuery } from "../api";

// 消费令牌生命周期回归：request() 的 consumeAuth 分支对 401/403 的处理差异。
// 根因（2026-09）：此前 401/403 一律 clearConsumeToken，而 403（FORBIDDEN_DOMAIN/
// FORBIDDEN_METRIC/FORBIDDEN_PII）是「鉴权通过、授权拒绝」，令牌本身有效——误清后
// 下次请求无 Bearer，后端回落 X-Api-Key 报「需要消费令牌」，形成死循环（用户看到
// 『指标不在授权域』403 后反复提示『需要消费令牌』）。修复：仅 401 清除消费令牌。
describe("api request consumeAuth 401/403 令牌处理", () => {
  const originalFetch = globalThis.fetch;

  function mockFetch(status: number, code: string, message: string) {
    globalThis.fetch = vi.fn(async () => {
      return new Response(
        JSON.stringify({ code, message, data: null, trace_id: "trace-test" }),
        { status, headers: { "Content-Type": "application/json" } },
      ) as Response;
    }) as typeof fetch;
  }

  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it("403（授权拒绝）不清除消费令牌——令牌有效，仅指标无权限", async () => {
    setConsumeToken("valid-consume-jwt");
    mockFetch(403, "FORBIDDEN_DOMAIN", "指标不在接入方授权域内");

    await expect(
      consumeQuery({ metric_code: "gmv_net", dimensions: [], date_range: "" }),
    ).rejects.toThrow("指标不在接入方授权域内");

    expect(getConsumeToken()).toBe("valid-consume-jwt"); // 令牌保留
  });

  it("401（令牌无效/过期）清除消费令牌", async () => {
    setConsumeToken("expired-consume-jwt");
    mockFetch(401, "AUTH_APIKEY_INVALID", "X-Api-Key 格式应为 client_id:secret");

    await expect(
      consumeQuery({ metric_code: "gmv_net", dimensions: [], date_range: "" }),
    ).rejects.toThrow("X-Api-Key 格式应为 client_id:secret");

    expect(getConsumeToken()).toBeNull(); // 令牌清除
  });

  it("401 清除令牌后派发变更事件（UI 可同步回到『需要消费令牌』）", async () => {
    setConsumeToken("expired-consume-jwt");
    mockFetch(401, "AUTH_APIKEY_INVALID", "invalid");

    // 监听变更事件：request 清除令牌后应派发，使 QueryWorkspace 实时同步
    let notified = false;
    const listener = () => {
      notified = true;
    };
    window.addEventListener("unisense:consume-token-changed", listener);

    await expect(
      consumeQuery({ metric_code: "gmv_net", dimensions: [], date_range: "" }),
    ).rejects.toThrow();

    expect(notified).toBe(true);
    window.removeEventListener("unisense:consume-token-changed", listener);
    clearConsumeToken();
  });
});
