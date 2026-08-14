import { chromium, type FullConfig } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { getAdminCredentials, getE2EBaseURL, loginAsAdmin } from "./utils/auth";

// global-setup 在测试套件启动前以 admin 身份登录，
// 将登录态（cookie + localStorage 中的 unisense_token）持久化到 e2e/.auth/admin.json，
// 供各测试项目通过 use.storageState 复用，避免每个用例重复登录。
const STORAGE_STATE_PATH = "e2e/.auth/admin.json";

async function globalSetup(_config: FullConfig): Promise<void> {
  const baseURL = getE2EBaseURL();
  const credentials = getAdminCredentials();

  const browser = await chromium.launch();
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();

  try {
    await loginAsAdmin(page, credentials);
    const outPath = resolve(STORAGE_STATE_PATH);
    await mkdir(dirname(outPath), { recursive: true });
    await context.storageState({ path: outPath });
    console.log(`[global-setup] admin 登录成功，storageState 已保存 → ${outPath}`);
  } finally {
    await browser.close();
  }
}

export default globalSetup;
