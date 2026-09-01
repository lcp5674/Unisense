import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  apiLogin,
  clearAuthTokens,
  fetchCurrentUser,
  getRefreshToken,
  getToken,
  setRefreshToken,
  setToken,
  UnisenseApiError,
} from "../api";

// P0 令牌无感续期：401 → refresh token 换新 → 重放原请求。
// 这些测试直接 import 真实的 api.ts（不 mock 模块），mock 全局 fetch 验证 request() 真实行为。

const REFRESH_PATH = "/api/v1/auth/refresh";
const ME_PATH = "/api/v1/auth/me";

function envelope<T>(data: T) {
  return { code: "OK", message: "success", data, trace_id: "t" };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function okResponse<T>(data: T): Response {
  return jsonResponse(200, envelope(data));
}

const refreshSuccessBody = () => ({
  access_token: "new-access",
  refresh_token: "new-refresh",
  token_type: "bearer",
});

const loginBody = () => ({
  access_token: "access-1",
  refresh_token: "refresh-1",
  token_type: "bearer",
});

const meData = { id: 1, username: "admin", role: "platform_admin", domain: "fin" };

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("apiLogin 存储 access + refresh", () => {
  it("登录成功同时持久化 access_token 与 refresh_token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => okResponse(loginBody())),
    );
    const res = await apiLogin("admin", "secret");
    expect(res.access_token).toBe("access-1");
    expect(res.totp_required).toBeFalsy();
    expect(getToken()).toBe("access-1");
    expect(getRefreshToken()).toBe("refresh-1");
  });
});

describe("request() 401 无感续期", () => {
  it("401 → 调 /auth/refresh → 用新 token 重放原请求成功", async () => {
    setToken("expired-access");
    setRefreshToken("refresh-1");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(401, { code: "AUTH_TOKEN_EXPIRED", message: "expired" })) // 原请求 401
      .mockResolvedValueOnce(okResponse(refreshSuccessBody())) // refresh 成功
      .mockResolvedValueOnce(okResponse(meData)); // 重放成功

    vi.stubGlobal("fetch", fetchMock);

    const me = await fetchCurrentUser();
    expect(me.username).toBe("admin");
    // 令牌已轮换
    expect(getToken()).toBe("new-access");
    expect(getRefreshToken()).toBe("new-refresh");
    // 调用序列：me(401) → refresh → me(200)
    const urls = fetchMock.mock.calls.map((c) => c[0]);
    expect(urls).toEqual([ME_PATH, REFRESH_PATH, ME_PATH]);
    // refresh 请求体携带 refresh_token
    const refreshCall = fetchMock.mock.calls[1];
    expect(JSON.parse(refreshCall[1].body)).toEqual({ refresh_token: "refresh-1" });
  });

  it("刷新失败 → 清空 access+refresh，原错误向上抛", async () => {
    setToken("expired-access");
    setRefreshToken("refresh-1");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(401, { code: "AUTH_TOKEN_EXPIRED", message: "expired" }))
      .mockResolvedValueOnce(jsonResponse(401, { code: "AUTH_REFRESH_REVOKED", message: "revoked" }));

    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchCurrentUser()).rejects.toBeInstanceOf(UnisenseApiError);
    expect(getToken()).toBeNull();
    expect(getRefreshToken()).toBeNull();
  });

  it("无 refresh token → 不发起刷新，直接抛错并清空", async () => {
    setToken("expired-access");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(401, { code: "AUTH_TOKEN_EXPIRED", message: "expired" })),
    );
    await expect(fetchCurrentUser()).rejects.toBeInstanceOf(UnisenseApiError);
    expect(getToken()).toBeNull();
  });

  it("403 是权限拒绝，不触发刷新", async () => {
    setToken("valid-access");
    setRefreshToken("refresh-1");
    const fetchMock = vi.fn(async (_url: string) => jsonResponse(403, { code: "FORBIDDEN", message: "no" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchCurrentUser()).rejects.toBeInstanceOf(UnisenseApiError);
    // 未调用 refresh
    const urls = fetchMock.mock.calls.map((c) => c[0]);
    expect(urls).toEqual([ME_PATH]);
  });

  it("并发 401 共享同一次刷新（单飞）", async () => {
    setToken("expired-access");
    setRefreshToken("refresh-1");
    let refreshCalls = 0;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      console.log("SF", url, JSON.stringify(init?.headers ?? {}));
      if (url === REFRESH_PATH) {
        refreshCalls += 1;
        return okResponse(refreshSuccessBody());
      }
      if (init?.headers && JSON.stringify(init.headers).includes("new-access")) {
        // 已换新 token 的重放成功
        return okResponse(meData);
      }
      return jsonResponse(401, { code: "AUTH_TOKEN_EXPIRED", message: "expired" });
    });
    vi.stubGlobal("fetch", fetchMock);

    const [a, b] = await Promise.all([fetchCurrentUser(), fetchCurrentUser()]);
    expect(a.username).toBe("admin");
    expect(b.username).toBe("admin");
    expect(refreshCalls).toBe(1); // 只刷新一次
  });
});

describe("clearAuthTokens", () => {
  it("清空 access + refresh", () => {
    setToken("a");
    setRefreshToken("r");
    clearAuthTokens();
    expect(getToken()).toBeNull();
    expect(getRefreshToken()).toBeNull();
  });
});
