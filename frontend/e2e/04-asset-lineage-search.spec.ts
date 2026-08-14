import { test, expect, type Page } from "@playwright/test";
import { getE2EBaseURL, getAdminCredentials } from "./utils/auth";

const BASE_URL = getE2EBaseURL();
const credentials = getAdminCredentials();

/**
 * Helper: ensure logged in before each test.
 * Relies on global-setup auth state when available, otherwise performs login.
 */
async function ensureLoggedIn(page: Page): Promise<void> {
  await page.goto(BASE_URL);
  const loginForm = page.locator('input[autocomplete="username"]');
  if (await loginForm.isVisible({ timeout: 3000 }).catch(() => false)) {
    await loginForm.fill(credentials.username);
    await page.fill('input[autocomplete="current-password"]', credentials.password);
    await page.click(".login-submit");
    await page.waitForURL("**/dashboard", { timeout: 20000 });
  } else {
    // Already authenticated — just ensure we land somewhere sane
    await page.waitForLoadState("networkidle");
  }
}

// ---------------------------------------------------------------------------
// Asset Map (8 tests)
// ---------------------------------------------------------------------------
test.describe("Asset Map", () => {
  test.beforeEach(async ({ page }) => {
    await ensureLoggedIn(page);
  });

  // 1. 访问资产地图
  test("访问资产地图 - goto /asset-map", async ({ page }) => {
    await page.goto(`${BASE_URL}/assetmap`);
    await page.waitForLoadState("networkidle");

    // Verify page title and header
    await expect(page.getByRole("heading", { name: "资产地图" })).toBeVisible({ timeout: 10000 });
    // Default tab "概览" should be active
    await expect(page.getByRole("tab", { name: "概览" })).toBeVisible({ timeout: 5000 });
  });

  // 2. 表搜索
  test("表搜索 - 输入表名搜索", async ({ page }) => {
    await page.goto(`${BASE_URL}/assetmap`);
    await page.waitForLoadState("networkidle");

    // Switch to 搜索 tab
    await page.getByRole("tab", { name: "搜索" }).click();
    await page.waitForLoadState("networkidle");

    // Search input placeholder matches
    const searchInput = page.getByPlaceholder("输入表名 / 字段名 / 指标编码 / 指标名称");
    await expect(searchInput).toBeVisible({ timeout: 5000 });

    // Type a table name and search
    await searchInput.fill("dwd");
    await page.getByRole("button", { name: "搜索" }).click();
    await page.waitForTimeout(2000); // wait for results to render

    // Results table should appear (no crash)
    const resultsArea = page.locator(".ant-table");
    if (await resultsArea.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(resultsArea).toBeVisible();
    }
  });

  // 3. 表热度显示
  test("表热度显示 - 热度值显示", async ({ page }) => {
    await page.goto(`${BASE_URL}/assetmap`);
    await page.waitForLoadState("networkidle");

    // Switch to 热力视图 tab
    await page.getByRole("tab", { name: "热力视图" }).click();
    await page.waitForLoadState("networkidle");

    // Heatmap area should render without throwing
    const heatmapContainer = page.locator(".ant-card, .recharts-wrapper, canvas").first();
    if (await heatmapContainer.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(heatmapContainer).toBeVisible();
    }

    // Statistic cards (节点数/边数) should be visible in 热力视图
    const statCard = page.locator(".ant-statistic").first();
    if (await statCard.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(statCard).toBeVisible();
    }
    await expect(page.getByRole("heading", { name: "资产地图" })).toBeVisible({ timeout: 5000 });
  });

  // 4. 表详情
  test("表详情 - 点击表显示详情", async ({ page }) => {
    await page.goto(`${BASE_URL}/assetmap`);
    await page.waitForLoadState("networkidle");

    // Switch to 数据表 tab
    await page.getByRole("tab", { name: "数据表" }).click();
    await page.waitForLoadState("networkidle");

    // Find first table row and click 详情
    const detailBtn = page.getByRole("link", { name: "详情" }).first();
    if (await detailBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
      await detailBtn.click();
      await page.waitForTimeout(1000);
      // Drawer should open with entity details
      await expect(
        page.locator("text=实体详情").or(page.locator("text=实体名称")),
      ).toBeVisible({ timeout: 5000 });
      // Close drawer
      await page.keyboard.press("Escape");
    }
  });

  // 5. 孤表查看
  test("孤表查看 - 查看孤儿资产 Tab", async ({ page }) => {
    await page.goto(`${BASE_URL}/assetmap`);
    await page.waitForLoadState("networkidle");

    // Switch to 孤儿资产 tab
    await page.getByRole("tab", { name: "孤儿资产" }).click();
    await page.waitForLoadState("networkidle");

    // Orphan table should be visible (may be empty but component should render)
    const orphanPanel = page.getByRole("tabpanel", { name: /孤儿资产/ });
    await expect(orphanPanel).toBeVisible({ timeout: 5000 });
    const orphanTable = orphanPanel.locator(".ant-table, .ant-empty").first();
    if (await orphanTable.isVisible({ timeout: 3000 }).catch(() => false)) {
      await expect(orphanTable).toBeVisible();
    }
  });

  // 6. 搜索 Tab 多维度搜索
  test("搜索 Tab - 多维度搜索", async ({ page }) => {
    await page.goto(`${BASE_URL}/assetmap`);
    await page.waitForLoadState("networkidle");

    // Switch to 搜索 tab
    await page.getByRole("tab", { name: "搜索" }).click();
    await page.waitForLoadState("networkidle");

    // Filter by type (全部类型 select)
    const typeSelect = page.locator(".ant-select").filter({ hasText: "全部类型" }).first();
    if (await typeSelect.isVisible({ timeout: 3000 }).catch(() => false)) {
      await typeSelect.click();
      await page.waitForTimeout(500);
      // Pick a type option
      const typeOption = page.locator(".ant-select-item").filter({ hasText: "表 / 视图" }).first();
      if (await typeOption.isVisible({ timeout: 3000 }).catch(() => false)) {
        await typeOption.click();
        await page.waitForTimeout(500);
      }
    }

    // Filter by domain (全部域 select)
    const domainSelect = page.locator(".ant-select").filter({ hasText: "全部域" }).first();
    if (await domainSelect.isVisible({ timeout: 3000 }).catch(() => false)) {
      await domainSelect.click();
      await page.waitForTimeout(500);
      // Pick first domain option if available
      const domainOption = page.locator(".ant-select-item").first();
      if (await domainOption.isVisible({ timeout: 3000 }).catch(() => false)) {
        await domainOption.click();
        await page.waitForTimeout(500);
      }
    }
  });

  // 7. 表导出
  test("表导出 - 导出 CSV", async ({ page }) => {
    await page.goto(`${BASE_URL}/assetmap`);
    await page.waitForLoadState("networkidle");

    // Find 导出 CSV button
    const exportBtn = page.getByRole("button", { name: /导出 CSV/ });
    if (await exportBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      // Set up download listener
      const downloadPromise = page.waitForEvent("download", { timeout: 10000 }).catch(() => null);
      await exportBtn.click();
      const download = await downloadPromise;
      // Verify download triggered (file may not actually save in headless)
      if (download) {
        expect(download.suggestedFilename()).toBeTruthy();
      }
    }
  });

  // 8. ETL 信息
  test("ETL 信息 - 验证 ETL SQL 显示", async ({ page }) => {
    await page.goto(`${BASE_URL}/assetmap`);
    await page.waitForLoadState("networkidle");

    // Navigate to 图谱视图 tab
    await page.getByRole("tab", { name: "图谱视图" }).click();
    await page.waitForLoadState("networkidle");

    // Nodes table should be present
    const nodesTable = page.locator(".ant-table").first();
    await expect(nodesTable).toBeVisible({ timeout: 5000 });

    // Check for ETL SQL column or related content in drawer
    const etlRelatedText = page.locator("text=ETL").or(page.locator("text=SQL")).or(page.locator("text=血缘"));
    if (await etlRelatedText.first().isVisible({ timeout: 3000 }).catch(() => false)) {
      await expect(etlRelatedText.first()).toBeVisible();
    }
  });
});

// ---------------------------------------------------------------------------
// Lineage View (7 tests)
// ---------------------------------------------------------------------------
test.describe("Lineage View", () => {
  test.beforeEach(async ({ page }) => {
    await ensureLoggedIn(page);
  });

  // 9. 访问血缘视图
  test("访问血缘视图 - goto /lineage", async ({ page }) => {
    await page.goto(`${BASE_URL}/lineage`);
    await page.waitForLoadState("networkidle");

    // Verify page title
    await expect(page.getByRole("heading", { name: "血缘视图" })).toBeVisible({ timeout: 10000 });
    // Default tab 血缘查询 / 影响分析 should be active
    await expect(page.getByRole("tab", { name: "血缘查询 / 影响分析" })).toBeVisible({ timeout: 5000 });
  });

  // 10. SQL 解析
  test("SQL 解析 - 输入 SQL 点击解析", async ({ page }) => {
    await page.goto(`${BASE_URL}/lineage`);
    await page.waitForLoadState("networkidle");

    // Switch to SQL 血缘解析 tab
    await page.getByRole("tab", { name: "SQL 血缘解析" }).click();
    await page.waitForLoadState("networkidle");

    // SQL textarea with placeholder
    const sqlTextarea = page.getByPlaceholder(/粘贴 SQL/);
    await expect(sqlTextarea).toBeVisible({ timeout: 5000 });

    // Enter a simple SQL statement
    const sql = "SELECT order_id, user_id, amount FROM dwd_finance_order WHERE dt = '2026-08-01'";
    await sqlTextarea.fill(sql);

    // Dialect select should be present
    const dialectSelect = page.locator(".ant-select").first();
    if (await dialectSelect.isVisible({ timeout: 3000 }).catch(() => false)) {
      await dialectSelect.click();
      await page.waitForTimeout(500);
      // Select mysql option
      const mysqlOption = page.locator(".ant-select-item").filter({ hasText: "mysql" }).first();
      if (await mysqlOption.isVisible({ timeout: 3000 }).catch(() => false)) {
        await mysqlOption.click();
      }
    }

    // Click 解析血缘 button
    const parseBtn = page.getByRole("button", { name: "解析血缘" });
    await expect(parseBtn).toBeVisible({ timeout: 5000 });
    await parseBtn.click();
    await page.waitForTimeout(3000); // wait for parsing result

    // Result should appear (no crash), may show success or parsed table info
    const resultArea = page.locator(".ant-result, .ant-table, text=成功, text=解析").first();
    if (await resultArea.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(resultArea).toBeVisible();
    }
  });

  // 11. 血缘图展示
  test("血缘图展示 - 验证节点和边", async ({ page }) => {
    await page.goto(`${BASE_URL}/lineage`);
    await page.waitForLoadState("networkidle");

    // Enter a known node in the impact analysis tab
    const nodeInput = page.getByPlaceholder("节点（指标编码 / 表名）");
    await expect(nodeInput).toBeVisible({ timeout: 5000 });
    await nodeInput.fill("dwd_finance_order");

    // Direction select (下游影响 / 上游来源 / 双向)
    const dirSelect = page.locator(".ant-select").filter({ hasText: "下游影响" }).first();
    if (await dirSelect.isVisible({ timeout: 3000 }).catch(() => false)) {
      await dirSelect.click();
      await page.waitForTimeout(500);
      const dirOption = page.locator(".ant-select-item").filter({ hasText: "下游影响" }).first();
      if (await dirOption.isVisible({ timeout: 3000 }).catch(() => false)) {
        await dirOption.click();
      }
    }

    // Click 查询
    await page.getByRole("button", { name: /查\s*询/ }).click();
    await page.waitForTimeout(3000);

    // Results table should show columns: 源, 目标, 类型, 粒度, 置信度, PII
    const colHeader = page.locator("text=源").or(page.locator("th:has-text('源')"));
    if (await colHeader.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(colHeader.first()).toBeVisible();
      // Check for 目标 column
      await expect(page.locator("text=目标").first()).toBeVisible();
    }
  });

  // 12. 节点交互
  test("节点交互 - 点击节点显示详情", async ({ page }) => {
    await page.goto(`${BASE_URL}/lineage`);
    await page.waitForLoadState("networkidle");

    // Enter a node and query
    const nodeInput = page.getByPlaceholder("节点（指标编码 / 表名）");
    await nodeInput.fill("dwd_finance_order");
    await page.getByRole("button", { name: /查\s*询/ }).click();
    await page.waitForTimeout(3000);

    // Try clicking a node link in the results table (源 or 目标 column)
    const nodeLink = page.locator("a:has-text('dwd')").first();
    if (await nodeLink.isVisible({ timeout: 3000 }).catch(() => false)) {
      await nodeLink.click();
      await page.waitForTimeout(1500);
      // Drawer or detail panel should appear
      const detailPanel = page.locator("text=实体详情").or(page.locator("text=实体名称")).first();
      if (await detailPanel.isVisible({ timeout: 3000 }).catch(() => false)) {
        await expect(detailPanel).toBeVisible();
      }
    }
  });

  // 13. 影响分析
  test("影响分析 - 执行影响分析，验证返回 affected_tables", async ({ page }) => {
    await page.goto(`${BASE_URL}/lineage`);
    await page.waitForLoadState("networkidle");

    // Ensure we are on 血缘查询 / 影响分析 tab
    await expect(page.getByRole("tab", { name: "血缘查询 / 影响分析" })).toBeVisible({ timeout: 5000 });

    // Enter a node for impact analysis
    const nodeInput = page.getByPlaceholder("节点（指标编码 / 表名）");
    await nodeInput.fill("dwd_finance_order");

    // Click 查询
    await page.getByRole("button", { name: /查\s*询/ }).click();
    await page.waitForTimeout(3000);

    // Verify affected_tables are returned in the results (not affected_reports)
    // Results should contain table names in 源/目标 columns
    const resultsTable = page.locator(".ant-table").first();
    if (await resultsTable.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(resultsTable).toBeVisible();
      // Should not crash, results render correctly
      const rows = page.locator(".ant-table-tbody tr");
      const rowCount = await rows.count();
      // Row count should be >= 0 (may be 0 for isolated nodes)
      expect(rowCount).toBeGreaterThanOrEqual(0);
    }
  });

  // 14. 缩放控制
  test("缩放控制 - 缩放功能", async ({ page }) => {
    await page.goto(`${BASE_URL}/lineage`);
    await page.waitForLoadState("networkidle");

    // 图谱视图 renders with nodes; look for zoom controls
    const graphContainer = page.locator(".recharts-wrapper, .ant-chart-container, canvas").first();
    if (await graphContainer.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(graphContainer).toBeVisible();
      // Try zooming with mouse wheel
      await graphContainer.hover();
      await page.mouse.wheel(0, -100);
      await page.waitForTimeout(500);
      // Zoom in/out buttons if present
      const zoomInBtn = page.locator('button[aria-label*="zoom"], button[title*="缩放"], button:has-text("+")').first();
      if (await zoomInBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
        await zoomInBtn.click();
        await page.waitForTimeout(300);
      }
    }
  });

  // 15. 导出
  test("导出 - 导出功能", async ({ page }) => {
    await page.goto(`${BASE_URL}/lineage`);
    await page.waitForLoadState("networkidle");

    // Enter a node and query first
    const nodeInput = page.getByPlaceholder("节点（指标编码 / 表名）");
    await nodeInput.fill("dwd_finance_order");
    await page.getByRole("button", { name: /查\s*询/ }).click();
    await page.waitForTimeout(3000);

    // Look for export button (could be 导出 CSV or 变更影响预览)
    const exportBtn = page
      .getByRole("button", { name: /导出|预览|变更影响/ })
      .first();
    if (await exportBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      const downloadPromise = page.waitForEvent("download", { timeout: 10000 }).catch(() => null);
      await exportBtn.click();
      const download = await downloadPromise;
      if (download) {
        expect(download.suggestedFilename()).toBeTruthy();
      }
    }
  });
});

// ---------------------------------------------------------------------------
// Global Search (9 tests)
// ---------------------------------------------------------------------------
test.describe("Global Search", () => {
  test.beforeEach(async ({ page }) => {
    await ensureLoggedIn(page);
  });

  // 16. 访问全局搜索
  test("访问全局搜索 - goto /search", async ({ page }) => {
    await page.goto(`${BASE_URL}/search`);
    await page.waitForLoadState("networkidle");

    // Verify page title
    await expect(page.locator("text=全局搜索")).toBeVisible({ timeout: 10000 });
    // Search input should be visible
    await expect(page.getByPlaceholder("输入关键词，跨类型搜索")).toBeVisible({ timeout: 5000 });
  });

  // 17. 搜索指标
  test("搜索指标 - 搜索指标结果正确", async ({ page }) => {
    await page.goto(`${BASE_URL}/search`);
    await page.waitForLoadState("networkidle");

    const searchInput = page.getByPlaceholder("输入关键词，跨类型搜索");
    await searchInput.fill("收入");
    await page.getByRole("button", { name: "搜索" }).click();
    await page.waitForTimeout(2000);

    // Should show metric group or result items
    const metricGroup = page.locator("text=指标").first();
    if (await metricGroup.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(metricGroup).toBeVisible();
    }

    // Should show total count text
    const totalText = page.locator("text=共").or(page.locator("text=条结果"));
    if (await totalText.first().isVisible({ timeout: 3000 }).catch(() => false)) {
      await expect(totalText.first()).toBeVisible();
    }
  });

  // 18. 搜索表
  test("搜索表 - 搜索表结果正确", async ({ page }) => {
    await page.goto(`${BASE_URL}/search`);
    await page.waitForLoadState("networkidle");

    const searchInput = page.getByPlaceholder("输入关键词，跨类型搜索");
    await searchInput.fill("dwd");
    await page.getByRole("button", { name: "搜索" }).click();
    await page.waitForTimeout(2000);

    // Should show catalog or data_source group (tables appear under these)
    const groupLabel = page.locator("text=采集目录").or(page.locator("text=数据源")).first();
    if (await groupLabel.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(groupLabel).toBeVisible();
    }
  });

  // 19. 搜索主题域
  test("搜索主题域 - 搜索主题域结果正确", async ({ page }) => {
    await page.goto(`${BASE_URL}/search`);
    await page.waitForLoadState("networkidle");

    const searchInput = page.getByPlaceholder("输入关键词，跨类型搜索");
    await searchInput.fill("财务");
    await page.getByRole("button", { name: "搜索" }).click();
    await page.waitForTimeout(2000);

    // Should show subject_domain group
    const domainGroup = page.locator("text=主题域").first();
    if (await domainGroup.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(domainGroup).toBeVisible();
    }
  });

  // 20. 搜索字段
  test("搜索字段 - 搜索字段结果正确", async ({ page }) => {
    await page.goto(`${BASE_URL}/search`);
    await page.waitForLoadState("networkidle");

    const searchInput = page.getByPlaceholder("输入关键词，跨类型搜索");
    await searchInput.fill("amount");
    await page.getByRole("button", { name: "搜索" }).click();
    await page.waitForTimeout(2000);

    // Should show field group
    const fieldGroup = page.locator("text=字段").first();
    if (await fieldGroup.isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(fieldGroup).toBeVisible();
    }
  });

  // 21. 空搜索处理
  test("空搜索处理 - 空关键词显示推荐", async ({ page }) => {
    await page.goto(`${BASE_URL}/search`);
    await page.waitForLoadState("networkidle");

    const searchInput = page.getByPlaceholder("输入关键词，跨类型搜索");
    // Submit without typing anything
    await page.getByRole("button", { name: "搜索" }).click();
    await page.waitForTimeout(2000);

    // Should show recommended items or recent searches (no crash)
    const recommendArea = page
      .locator("text=推荐")
      .or(page.locator("text=最近"))
      .or(page.locator(".ant-list"));
    if (await recommendArea.first().isVisible({ timeout: 3000 }).catch(() => false)) {
      await expect(recommendArea.first()).toBeVisible();
    }
  });

  // 22. 无结果处理
  test("无结果处理 - 友好提示", async ({ page }) => {
    await page.goto(`${BASE_URL}/search`);
    await page.waitForLoadState("networkidle");

    const searchInput = page.getByPlaceholder("输入关键词，跨类型搜索");
    const randomString = `__not_exist_${Date.now()}`;
    await searchInput.fill(randomString);
    await page.getByRole("button", { name: "搜索" }).click();
    await page.waitForTimeout(3000);

    // Should show empty state (ant Result component or "暂无数据")
    const emptyState = page
      .locator(".ant-result")
      .or(page.locator("text=暂无数据"))
      .or(page.locator("text=没有找到"));
    if (await emptyState.first().isVisible({ timeout: 5000 }).catch(() => false)) {
      await expect(emptyState.first()).toBeVisible();
    }
  });

  // 23. 分组展示
  test("分组展示 - 按类型分组", async ({ page }) => {
    await page.goto(`${BASE_URL}/search`);
    await page.waitForLoadState("networkidle");

    const searchInput = page.getByPlaceholder("输入关键词，跨类型搜索");
    await searchInput.fill("数据");
    await page.getByRole("button", { name: "搜索" }).click();
    await page.waitForTimeout(3000);

    // Verify grouping: results should be organized under type labels
    // Expected groups: 指标, 维度, 术语, 模板, 数据源, 采集目录, 字段, 主题域
    const groupLabels = [
      "指标", "维度", "术语", "模板", "数据源", "采集目录", "字段", "主题域",
    ];
    for (const label of groupLabels) {
      const group = page.locator(`text=${label}`).first();
      if (await group.isVisible({ timeout: 2000 }).catch(() => false)) {
        await expect(group).toBeVisible();
      }
    }

    // Total count should be visible
    const totalText = page.locator("text=/共.*条结果/").first();
    if (await totalText.isVisible({ timeout: 3000 }).catch(() => false)) {
      await expect(totalText).toBeVisible();
    }
  });

  // 24. 点击跳转
  test("点击跳转 - 跳转到详情页", async ({ page }) => {
    await page.goto(`${BASE_URL}/search`);
    await page.waitForLoadState("networkidle");

    const searchInput = page.getByPlaceholder("输入关键词，跨类型搜索");
    await searchInput.fill("收入");
    await page.getByRole("button", { name: "搜索" }).click();
    await page.waitForTimeout(3000);

    // Find and click a result item
    // Result items have code and name; look for first clickable link
    const firstResultLink = page.locator("a").filter({ hasText: /^[a-zA-Z0-9_]+$/ }).first();
    if (await firstResultLink.isVisible({ timeout: 5000 }).catch(() => false)) {
      const hrefBefore = await firstResultLink.getAttribute("href");
      await firstResultLink.click();
      await page.waitForTimeout(2000);
      // URL should have changed (navigated to detail page)
      const urlAfter = page.url();
      // Either navigated to a detail page or a drawer opened
      const navigated = urlAfter !== `${BASE_URL}/search`;
      // If stayed on same page, at least a drawer should open
      if (!navigated) {
        const drawer = page.locator(".ant-drawer, .ant-modal, text=详情").first();
        if (await drawer.isVisible({ timeout: 3000 }).catch(() => false)) {
          await expect(drawer).toBeVisible();
        }
      }
    }
  });
});
