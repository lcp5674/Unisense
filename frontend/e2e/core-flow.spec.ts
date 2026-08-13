/** E2E 测试骨架（TEST-09: 端到端测试 — 登录→指标创建→发布→查询）。
 *
 * 框架：Playwright + Docker Compose
 * 对齐 R&D-07: Playwright 原生支持多浏览器、网络拦截、自动等待。
 */

import { test, expect } from '@playwright/test';

// E2E 测试需要完整运行环境（Docker Compose 全启动），
// CI 成本较高。占位文件，后续迭代完善。

test.describe('核心用户流程', () => {
  test.skip('登录→创建指标→发布→查询', async ({ page }) => {
    // 占位：需要完整运行环境
  });
});
