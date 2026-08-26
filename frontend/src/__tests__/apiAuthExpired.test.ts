import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  AUTH_EXPIRED_EVENT,
  setToken,
  setRefreshToken,
  fetchCurrentUser,
} from "../api";

// S-4（第八轮）：会话中途失效（401 刷新失败）→ 派发 AUTH_EXPIRED_EVENT，App 层
// 监听后回登录页。此前 401 仅清 token 且"交由上层跳登录"但上层只启动时校验，
// 用户滞留页面反复报错。本测试锚定 401 刷失败场景确实派发事件。
describe("api request 401 会话失效事件", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    localStorage.clear();
    // 默认：主请求 401 + refresh 也 401（会话彻底失效）
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      return new Response(JSON.stringify({ detail: "expired" }), {
        status: url.includes("/auth/refresh") ? 401 : 401,
        headers: { "Content-Type": "application/json" },
      }) as Response;
    }) as typeof fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it("401 刷新失败后派发 AUTH_EXPIRED_EVENT", async () => {
    setToken("expired-access-token");
    setRefreshToken("expired-refresh-token");

    const listener = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, listener);

    await expect(fetchCurrentUser()).rejects.toThrow();

    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener(AUTH_EXPIRED_EVENT, listener);
  });
});
