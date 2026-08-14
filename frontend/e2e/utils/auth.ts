import type { Page } from "@playwright/test";

// 运行栈由 Docker Compose 提供：unisense-frontend → 8180，unisense-backend → 8100。
// 开发态（vite dev）则在 5173，并由 vite.config.ts 代理 /api → 8100。
const LIVE_FRONTEND_URL = "http://localhost:8180";
const DEV_FRONTEND_URL = "http://localhost:5173";

// 登录态 token 的 localStorage 键名，需与 src/api.ts 中 TOKEN_KEY 保持一致。
const TOKEN_KEY = "unisense_token";

export function getE2EBaseURL(): string {
  const fromEnv = process.env.PLAYWRIGHT_BASE_URL?.replace(/\/$/, "");
  if (fromEnv) return fromEnv;
  return LIVE_FRONTEND_URL;
}

export interface AdminCredentials {
  username: string;
  password: string;
}

export function getAdminCredentials(): AdminCredentials {
  return {
    username: process.env.UNISENSE_E2E_ADMIN_USER ?? "admin",
    password: process.env.UNISENSE_E2E_ADMIN_PASSWORD ?? "changeme123",
  };
}

// Unisense 登录页（src/App.tsx LoginPage）：
// - 用户名输入框 autocomplete="username"
// - 密码输入框 autocomplete="current-password"
// - 提交按钮 .login-submit
// 登录成功后 token 写入 localStorage[TOKEN_KEY] 并跳转 /dashboard。
export async function loginAsAdmin(
  page: Page,
  credentials: AdminCredentials = getAdminCredentials(),
): Promise<void> {
  await page.goto("/");
  await page.waitForSelector('input[autocomplete="username"]', {
    state: "visible",
    timeout: 20000,
  });
  await page.fill('input[autocomplete="username"]', credentials.username);
  await page.fill('input[autocomplete="current-password"]', credentials.password);
  await page.click(".login-submit");
  await page.waitForFunction(
    (key) => localStorage.getItem(key) !== null,
    TOKEN_KEY,
    { timeout: 20000 },
  );
  await page.waitForURL("**/dashboard", { timeout: 20000 });
}
