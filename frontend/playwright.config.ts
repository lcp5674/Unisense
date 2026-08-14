import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  // 只跑我们自己写的 spec 文件，不跑旧的 placeholder
  testMatch: [
    "e2e/01-auth-metrics.spec.ts",
    "e2e/02-conflict-consume.spec.ts",
    "e2e/03-quality-governance.spec.ts",
    "e2e/04-asset-lineage-search.spec.ts",
    "e2e/05-other-modules.spec.ts",
  ],
  fullyParallel: false, // 避免并发时 session 混乱
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  // 关键：指定 chromium 路径（Python playwright 捆绑的）
  webServer: undefined,
  use: {
    baseURL: "http://localhost:8180",
    // 不依赖 globalSetup，每个测试自行登录
    // storageState 注释掉，改用测试内登录
    trace: "on-first-retry",
    // 强制使用 Python playwright 的 chromium
    launchOptions: {
      executablePath:
        "/Users/lcp/Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    },
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
      },
    },
  ],
});
