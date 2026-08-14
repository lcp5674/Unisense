import { chromium } from "@playwright/test";
import path from "node:path";
import fs from "node:fs";

const AUTH_FILE = path.resolve(__dirname, "../.auth/admin.json");

async function globalSetup() {
  const browser = await chromium.launch();
  const context = await browser.newContext();
  const page = await context.newPage();

  // Navigate to login page
  await page.goto("http://localhost:5173/login");

  // Fill in admin credentials
  await page.getByLabel("用户名").fill("admin");
  await page.getByLabel("密码").fill("admin123");
  await page.getByRole("button", { name: "登录" }).click();

  // Wait for navigation after login
  await page.waitForURL("http://localhost:5173/", { timeout: 30000 });

  // Save auth state
  const authDir = path.dirname(AUTH_FILE);
  if (!fs.existsSync(authDir)) {
    fs.mkdirSync(authDir, { recursive: true });
  }
  await context.storageState({ path: AUTH_FILE });

  await browser.close();
}

export default globalSetup;
