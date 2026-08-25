import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { App as AntApp } from "antd";
import { MetricCreate } from "../pages/MetricCreate";

// 批量注册依赖后端 POST /metric-definitions/batch-register（对齐 FR-030）
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    listDomainTree: vi.fn(),
    listDictItems: vi.fn(),
    listCatalogs: vi.fn(),
    listDimensions: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 }),
    listMetrics: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 }),
    batchRegisterMetrics: vi.fn(),
    batchSubmitMetrics: vi.fn(),
    listUsers: vi.fn().mockResolvedValue([]),
    listMeasureCatalogs: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    autoSuggestMetric: vi.fn(),
    suggestDomain: vi.fn(),
    getDomainDefaults: vi.fn(),
    checkConflict: vi.fn(),
    createMetric: vi.fn(),
  };
});

import { listDomainTree, listDictItems, listCatalogs, batchRegisterMetrics, batchSubmitMetrics, listUsers, autoSuggestMetric, suggestDomain, checkConflict, createMetric, listMetrics, listMeasureCatalogs } from "../api";
import type { DBCatalog, SubjectDomainTreeNode, AutoSuggestResponse, DomainSuggestionResponse } from "../types";

const mockedTree = vi.mocked(listDomainTree);
const mockedDict = vi.mocked(listDictItems);
const mockedCatalogs = vi.mocked(listCatalogs);
const mockedBatch = vi.mocked(batchRegisterMetrics);
const mockedBatchSubmit = vi.mocked(batchSubmitMetrics);
const mockedUsers = vi.mocked(listUsers);
const mockedSuggest = vi.mocked(autoSuggestMetric);
const mockedSuggestDomain = vi.mocked(suggestDomain);
const mockedCheckConflict = vi.mocked(checkConflict);
const mockedCreate = vi.mocked(createMetric);
const mockedMetrics = vi.mocked(listMetrics);

/** 后端 auto-suggest 永不返回 undefined（auto_fill 兜底成完整对象）——"无建议"即空 fields/空 code 的合法响应。 */
const NO_SUGGESTION: AutoSuggestResponse = {
  metric_code_suggestion: null,
  segments: { domain: "", biz_object: null, measure: null, period: null },
  fields: {},
  definition_json: null,
  definition_mode: null,
};

/** 业务域建议默认返回：无法建议（不干扰既有 SQL 推断测试主流程）。 */
const NO_DOMAIN_SUGGESTION: DomainSuggestionResponse = {
  status: "none",
  domain: null,
  candidates: [],
  matched_tables: [],
};

/** 构造完整 DBCatalog（源表搜索 mock 用），仅 entity_name/source_name 参与渲染。 */
function makeCatalog(entityName: string, columns?: { name: string; type?: string }[]): DBCatalog {
  return {
    source_id: "src_test",
    entity_name: entityName,
    entity_type: "TABLE",
    schema_def: columns
      ? { columns: columns.map((c) => ({ name: c.name, type: c.type ?? "", comment: "" })) }
      : {},
    etl_sql: null,
    sensitivity_level: "L2",
    owner_id: null,
    upstream_signature: "",
    content_signature: null,
    schema_incomplete: !columns || columns.length === 0,
    source_name: null,
  };
}

const TREE: SubjectDomainTreeNode[] = [
  {
    id: 1,
    code: "sales",
    name: "销售",
    parent_id: null,
    level: 1,
    sort_order: 0,
    status: "active",
    metric_count: 3,
    children: [
      {
        id: 2,
        code: "sales_order",
        name: "订单",
        parent_id: 1,
        level: 2,
        sort_order: 0,
        status: "active",
        metric_count: 1,
        children: [],
      },
    ],
  },
  {
    id: 3,
    code: "finance",
    name: "财务",
    parent_id: null,
    level: 1,
    sort_order: 1,
    status: "active",
    metric_count: 0,
    children: [],
  },
];

/** 组件依赖 AntApp.useApp() 的 message，渲染时需包 <App> 提供真实 context；页面用 useNavigate 需配路由。 */
function renderPage() {
  return render(
    <AntApp>
      <MemoryRouter initialEntries={["/create"]}>
      <Routes>
        <Route path="/create" element={<MetricCreate />} />
        <Route path="/detail/:code" element={<div>detail</div>} />
      </Routes>
    </MemoryRouter>
    </AntApp>,
  );
}

/** 选择业务域（Cascader 弹出面板点第一层「销售 (sales)」）。模块级复用：粘贴 SQL 与类型级联块均依赖。 */
async function pickDomain() {
  const cascaderInput = document.querySelector(".ant-cascader input") as HTMLInputElement;
  fireEvent.mouseDown(cascaderInput);
  await waitFor(() => {
    const item = document.querySelector(".ant-cascader-menu-item[title='销售 (sales)']");
    expect(item).toBeTruthy();
    if (item) fireEvent.click(item);
  });
}

/** 读取当前向导激活步骤（依据"下一步"按钮文案——Step0/1/2 各有唯一文案，Step3 无下一步）。 */
function currentStepIndex(): number {
  if (screen.queryByRole("button", { name: "下一步：指标定义" })) return 0;
  if (screen.queryByRole("button", { name: "下一步：治理与口径" })) return 1;
  if (screen.queryByRole("button", { name: "下一步：责任方与提交" })) return 2;
  return 3;
}

/** OneData 向导：点击「下一步」前进到目标步骤（检测当前激活步骤，避免重复调用过度推进）。 */
async function goToStep(target: number) {
  let guard = 0;
  while (currentStepIndex() < target && guard < 4) {
    const btn = screen.queryByRole("button", { name: /下一步/ });
    if (!btn) break;
    fireEvent.click(btn);
    // 给 React 并发渲染足够时间推进步骤
    await new Promise((r) => setTimeout(r, 50));
    guard++;
  }
}

/** 打开右上角「SQL 智能推断」抽屉（OneData 向导：SQL 推断收敛为工具抽屉）。 */
async function openSqlInfer() {
  fireEvent.click(screen.getByRole("button", { name: /SQL 智能推断/ }));
  await waitFor(() => {
    expect(document.querySelector(".ant-drawer")).toBeTruthy();
  });
}

/** 打开批量注册弹窗（点击页面右上角「批量注册指标」按钮）。 */
async function openBatchModal() {
  fireEvent.click(await screen.findByText("批量注册指标"));
  const modal = document.querySelector(".ant-modal") as HTMLElement;
  expect(modal).toBeTruthy();
  return modal;
}

/**
 * 在可见下拉中点击指定选项：antd 虚拟列表会渲染同名包裹节点，直接 getByText 会命中多个，
 * 必须点击 .ant-select-item-option 本体（title=选项文本）才能触发选中。
 */
async function clickSelectOption(text: string) {
  await waitFor(() => {
    const dropdown = document.querySelector(
      ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
    ) as HTMLElement | null;
    const option = dropdown?.querySelector(
      `.ant-select-item-option[title="${text}"]`,
    ) as HTMLElement | null;
    expect(option).toBeTruthy();
    if (option) fireEvent.click(option);
  });
}

/** 批量表单公共填充：选域（默认「销售 (sales)」）+ 源表搜索选中 + 填入度量列（tags 逐个 Enter）。 */
async function fillBatchForm(modal: HTMLElement, measureColumns: string) {
  fireEvent.mouseDown(within(modal).getByText("选择所属业务域（须为 active 域）"));
  await clickSelectOption("销售 (sales)");

  // 源表 Select showSearch：id 与主表单重复，需限定在弹窗内查询搜索输入
  const srcInput = modal.querySelector('input[id="source_table"]') as HTMLInputElement;
  fireEvent.mouseDown(srcInput);
  fireEvent.change(srcInput, { target: { value: "dwd" } });
  await clickSelectOption("dwd.sales_detail");

  // 度量列 tags Select：选源表后已自动带出该表列，展开下拉逐个点选（多选，空白行自动过滤）
  const cols = measureColumns
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
  for (const col of cols) {
    const input = modal.querySelector(".ant-select-multiple input") as HTMLInputElement;
    fireEvent.mouseDown(input);
    await clickSelectOption(col);
  }
}

describe("MetricCreate 批量注册指标", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedTree.mockResolvedValue(TREE);
    mockedDict.mockResolvedValue([]);
    mockedSuggest.mockResolvedValue(NO_SUGGESTION);
    mockedCatalogs.mockResolvedValue({
      items: [
        makeCatalog("dwd.sales_detail", [
          { name: "gmv" },
          { name: "order_cnt" },
          { name: "dup" },
        ]),
      ],
      total: 1,
      page: 1,
      page_size: 20,
    });
  });

  it("点击「批量注册指标」打开弹窗，展示批量表单", async () => {
    renderPage();
    const modal = await openBatchModal();
    expect(within(modal).getByText("批量注册指标")).toBeTruthy();
    expect(within(modal).getByText("业务域")).toBeTruthy();
    expect(within(modal).getByText("源表名")).toBeTruthy();
    expect(within(modal).getByText("度量列")).toBeTruthy();
    expect(within(modal).getByText("提交批量注册")).toBeTruthy();
  });

  it("选择源表后，度量列自动带出该表列供选择", async () => {
    renderPage();
    const modal = await openBatchModal();

    fireEvent.mouseDown(within(modal).getByText("选择所属业务域（须为 active 域）"));
    await clickSelectOption("销售 (sales)");

    const srcInput = modal.querySelector('input[id="source_table"]') as HTMLInputElement;
    fireEvent.mouseDown(srcInput);
    fireEvent.change(srcInput, { target: { value: "dwd" } });
    await clickSelectOption("dwd.sales_detail");

    // 展开度量列下拉：应自动带出所选源表的列（而非让用户手输列名）
    const measureInput = modal.querySelector(".ant-select-multiple input") as HTMLInputElement;
    fireEvent.mouseDown(measureInput);
    await waitFor(() => {
      const dropdown = document.querySelector(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
      ) as HTMLElement | null;
      const titles = dropdown
        ? Array.from(dropdown.querySelectorAll(".ant-select-item-option")).map((o) => o.getAttribute("title"))
        : [];
      expect(titles).toContain("gmv");
      expect(titles).toContain("order_cnt");
    });
  });

  it("提交批量注册携带正确 payload（源表/度量列/域/LLM 预填）", async () => {
    mockedBatch.mockResolvedValue({
      batch_id: "batch_abc123",
      candidates: [
        { metric_code: "sales_gmv_day", status: "DRAFT", validation_errors: null },
        { metric_code: "sales_order_cnt_day", status: "DRAFT", validation_errors: null },
      ],
    });
    renderPage();
    const modal = await openBatchModal();

    await fillBatchForm(modal, "gmv\norder_cnt");
    fireEvent.click(within(modal).getByText("提交批量注册"));

    await waitFor(() => {
      expect(mockedBatch).toHaveBeenCalledWith({
        source_table: "dwd.sales_detail",
        measure_columns: ["gmv", "order_cnt"],
        domain: "sales",
        llm_prefill: true,
        dimension_mapping: undefined,
      });
    });

    // 结果区展示批次号与成功明细
    await waitFor(() => {
      expect(screen.getByText(/batch_abc123/)).toBeTruthy();
      expect(screen.getByText("批量注册完成：成功 2 / 失败 0")).toBeTruthy();
      expect(screen.getByText("sales_gmv_day")).toBeTruthy();
      expect(screen.getByText("sales_order_cnt_day")).toBeTruthy();
    });
  });

  it("P3-16: 批量注册成功后点「批量提交评审」调用 batchSubmitMetrics（默认域评审组）", async () => {
    mockedBatch.mockResolvedValue({
      batch_id: "batch_submit1",
      candidates: [
        { metric_code: "sales_gmv_day", status: "DRAFT", validation_errors: null },
        { metric_code: "sales_order_cnt_day", status: "DRAFT", validation_errors: null },
      ],
    });
    mockedBatchSubmit.mockResolvedValue({
      results: [
        { code: "sales_gmv_day", ok: true, message: "" },
        { code: "sales_order_cnt_day", ok: true, message: "" },
      ],
      ok_count: 2,
      fail_count: 0,
    });
    renderPage();
    const modal = await openBatchModal();
    await fillBatchForm(modal, "gmv\norder_cnt");
    fireEvent.click(within(modal).getByText("提交批量注册"));
    await waitFor(() => expect(screen.getByText("sales_gmv_day")).toBeTruthy());
    fireEvent.click(within(modal).getByText("批量提交评审"));
    await waitFor(() => {
      expect(mockedBatchSubmit).toHaveBeenCalledWith([
        { code: "sales_gmv_day", change_reason: "批量注册后提交评审", reviewer_type: "domain", reviewer_id: undefined },
        { code: "sales_order_cnt_day", change_reason: "批量注册后提交评审", reviewer_type: "domain", reviewer_id: undefined },
      ]);
    });
    await waitFor(() => expect(screen.getByText(/批量提交完成：成功 2 \/ 失败 0/)).toBeTruthy());
  });

  it("P3-16: 指定用户评审未选人时前端拦截，不调用 batchSubmitMetrics", async () => {
    mockedBatch.mockResolvedValue({
      batch_id: "batch_submit2",
      candidates: [{ metric_code: "sales_gmv_day", status: "DRAFT", validation_errors: null }],
    });
    mockedUsers.mockResolvedValue([{ id: 7, username: "reviewer", display_name: "评审员", role: "reviewer", domain: "sales", status: "active" }]);
    renderPage();
    const modal = await openBatchModal();
    await fillBatchForm(modal, "gmv");
    fireEvent.click(within(modal).getByText("提交批量注册"));
    await waitFor(() => expect(screen.getByText("sales_gmv_day")).toBeTruthy());
    // 切换到「指定用户」但未选人 → 提交被前端拦截并提示
    fireEvent.click(within(modal).getByText("指定用户"));
    fireEvent.click(within(modal).getByText("批量提交评审"));
    await waitFor(() => expect(screen.getByText(/请先选择评审用户/)).toBeTruthy());
    expect(mockedBatchSubmit).not.toHaveBeenCalled();
  });

  it("维度列映射以可视化键值对填写并组装为对象", async () => {
    mockedBatch.mockResolvedValue({
      batch_id: "batch_map1",
      candidates: [{ metric_code: "sales_gmv_day", status: "DRAFT", validation_errors: null }],
    });
    renderPage();
    const modal = await openBatchModal();

    await fillBatchForm(modal, "gmv");

    // 添加维度映射行：维度名（AutoComplete 可搜平台维度）+ 列名（源表列 Select）
    fireEvent.click(within(modal).getByText("添加维度映射"));
    const dimInput = modal.querySelector('[data-testid="dim-name-auto"] input') as HTMLInputElement;
    expect(dimInput).toBeTruthy();
    fireEvent.change(dimInput, { target: { value: "date" } });
    const colInput = modal.querySelector(".ant-select-multiple input") as HTMLInputElement;
    void colInput;
    // 列名 Select 是普通单选（非 multiple），用其占位符展开下拉选择源表列
    const colSelect = within(modal).getByText("选择源表列");
    fireEvent.mouseDown(colSelect);
    await clickSelectOption("gmv");

    fireEvent.click(within(modal).getByText("提交批量注册"));
    await waitFor(() => {
      expect(mockedBatch).toHaveBeenCalledWith(
        expect.objectContaining({ dimension_mapping: { date: "gmv" } }),
      );
    });
  });

  it("部分失败时展示失败原因明细", async () => {
    mockedBatch.mockResolvedValue({
      batch_id: "batch_fail1",
      candidates: [
        { metric_code: "sales_gmv_day", status: "DRAFT", validation_errors: null },
        { metric_code: "sales_dup_day", status: "VALIDATION_ERROR", validation_errors: "指标编码已存在" },
      ],
    });
    renderPage();
    const modal = await openBatchModal();

    await fillBatchForm(modal, "gmv\ndup");
    fireEvent.click(within(modal).getByText("提交批量注册"));

    await waitFor(() => {
      expect(mockedBatch).toHaveBeenCalled();
    });

    // 部分失败：警告色提示 + 失败原因列展示
    await waitFor(() => {
      expect(screen.getByText("批量注册完成：成功 1 / 失败 1")).toBeTruthy();
      expect(screen.getByText("校验失败")).toBeTruthy();
      expect(screen.getByText("指标编码已存在")).toBeTruthy();
    });
  });

  it("度量列为空时提示且不发请求", async () => {
    renderPage();
    const modal = await openBatchModal();

    // 只填空白行（合法域/源表但无度量列 → 前端拦截不发请求）
    await fillBatchForm(modal, "  \n  ");
    fireEvent.click(within(modal).getByText("提交批量注册"));

    await waitFor(() => {
      expect(screen.getByText("请至少选择一个度量列")).toBeTruthy();
    });
    expect(mockedBatch).not.toHaveBeenCalled();
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <AntApp>
        <MemoryRouter initialEntries={["/lineage", "/create"]}>
          <Routes>
            <Route path="/lineage" element={<div>lineage-page</div>} />
            <Route path="/create" element={<MetricCreate />} />
          </Routes>
        </MemoryRouter>
      </AntApp>,
    );
    await screen.findByText("注册指标（草稿）");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("lineage-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <AntApp>
        <MemoryRouter initialEntries={["/create"]}>
          <Routes>
            <Route path="/dashboard" element={<div>dashboard-page</div>} />
            <Route path="/create" element={<MetricCreate />} />
          </Routes>
        </MemoryRouter>
      </AntApp>,
    );
    await screen.findByText("注册指标（草稿）");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });
});

describe("MetricCreate 粘贴 SQL 智能推断", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedTree.mockResolvedValue(TREE);
    mockedDict.mockResolvedValue([]);
    mockedSuggest.mockResolvedValue(NO_SUGGESTION);
    mockedSuggestDomain.mockResolvedValue(NO_DOMAIN_SUGGESTION);
    mockedCatalogs.mockResolvedValue({
      items: [makeCatalog("dwd.sales_detail")],
      total: 1,
      page: 1,
      page_size: 20,
    });
  });

  it("SQL 推断成功：回填源表/度量列 + 展示推断摘要（含来源徽标）", async () => {
    mockedSuggest.mockResolvedValue({
      metric_code_suggestion: "sales_order_gmv_day",
      segments: { domain: "sales", biz_object: "order", measure: "gmv", period: "day" },
      fields: {
        source_table: { value: "dwd.sales_detail", source: "sql_parse", confidence: 0.9, reason: "SQL 解析源表" },
        measure_column: { value: "gmv", source: "sql_parse", confidence: 0.9, reason: "SQL 解析度量列" },
        name: { value: "订单销售额", source: "sql_parse", confidence: 0.8 },
        type: { value: "atomic", source: "sql_parse", confidence: 0.85 },
        granularity: { value: "day", source: "sql_parse", confidence: 0.9 },
        unit: { value: "CNY", source: "rule", confidence: 0.68 },
        aggregation: { value: "SUM", source: "sql_parse", confidence: 0.95 },
        time_semantics: { value: "PERIOD", source: "sql_parse", confidence: 0.6 },
        freshness: { value: "T1", source: "rule", confidence: 0.5 },
        dw_layer: { value: "DWD", source: "sql_parse", confidence: 0.8 },
        additivity: { value: "ADDITIVE", source: "rule", confidence: 0.6 },
        serving_mode: { value: "BATCH_ONLY", source: "sql_parse", confidence: 0.7 },
        metric_tier: { value: "T3", source: "fallback", confidence: 0.4 },
        definition_json: {
          value: { expression: "SUM(gmv)", source_fields: [{ table: "dwd.sales_detail", column: "gmv" }] },
          source: "sql_parse",
          confidence: 0.9,
        },
        definition_mode: { value: "expression", source: "sql_parse", confidence: 0.9 },
      },
      definition_json: { expression: "SUM(gmv)", source_fields: [{ table: "dwd.sales_detail", column: "gmv" }] },
      definition_mode: "expression",
      related_tables: ["dwd.sales_order", "dwd.shop_dim"],
    } as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openSqlInfer();

    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: "SELECT SUM(gmv) AS gmv FROM dwd.sales_detail GROUP BY dt, shop_id" },
    });
    fireEvent.click(screen.getByText("智能推断并回填字段"));

    // 推断摘要 Modal 弹出，展示识别字段
    await screen.findByText("SQL 智能推断结果");
    expect(screen.getAllByText("dwd.sales_detail").length).toBeGreaterThan(0);
    expect(screen.getByText("订单销售额")).toBeTruthy();
    // 关联表（血缘推断）展示
    await waitFor(() => expect(screen.getAllByText("dwd.sales_order").length).toBeGreaterThan(0));
    // 关闭摘要
    fireEvent.click(screen.getByText("知道了"));

    // 源表/度量列已回填到 Step1（指标定义 → 原子来源）——导航过去断言 Select 显示选中值
    await goToStep(1);
    await waitFor(() => {
      const srcInput = document.querySelector('input[id="source_table"]');
      const container = srcInput?.closest(".ant-select") as HTMLElement | null;
      expect(container?.textContent).toContain("dwd.sales_detail");
    });
    // 度量列下拉因回填 source_table 联动加载了列
    expect(mockedCatalogs).toHaveBeenCalledWith(
      expect.objectContaining({ entity_type: "TABLE", keyword: "dwd.sales_detail" })
    );
  });

  it("SQL 推断：血缘关联表按方向拆分为 依赖表（上游）+ 使用表（下游）回填", async () => {
    mockedSuggest.mockResolvedValue({
      metric_code_suggestion: "sales_order_gmv_day",
      segments: { domain: "sales", biz_object: "order", measure: "gmv", period: "day" },
      fields: {
        source_table: { value: "dwd.sales_detail", source: "sql_parse", confidence: 0.9, reason: "" },
        measure_column: { value: "gmv", source: "sql_parse", confidence: 0.9, reason: "" },
        name: { value: "订单销售额", source: "sql_parse", confidence: 0.8 },
      },
      definition_json: { expression: "SUM(gmv)", source_fields: [{ table: "dwd.sales_detail", column: "gmv" }] },
      definition_mode: "expression",
      // 方向拆分：上游依赖表 + 下游使用表（不再混向——此前 related_tables 一把抓）
      source_tables: ["ods.sales_order"],
      downstream_tables: ["ads.gmv_report"],
    } as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openSqlInfer();
    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: "SELECT SUM(gmv) AS gmv FROM dwd.sales_detail GROUP BY dt, shop_id" },
    });
    fireEvent.click(screen.getByText("智能推断并回填字段"));

    // 摘要 Modal：按方向分类展示（依赖表在上、使用表在下）
    await screen.findByText("SQL 智能推断结果");
    expect(screen.getByText("依赖表（上游）：")).toBeTruthy();
    expect(screen.getByText("使用表（下游）：")).toBeTruthy();
    expect(screen.getAllByText("ods.sales_order").length).toBeGreaterThan(0);
    expect(screen.getAllByText("ads.gmv_report").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText("知道了"));

    // 回填到 Step③ 口径定义：两个多选 Select 各自承接对应方向的表
    await goToStep(2);
    await waitFor(() => {
      const depLabel = Array.from(document.querySelectorAll(".ant-form-item-label")).find(
        (el) => el.textContent?.includes("依赖表")
      );
      const depItem = depLabel?.closest(".ant-form-item") as HTMLElement | null;
      expect(depItem?.textContent).toContain("ods.sales_order");
      const useLabel = Array.from(document.querySelectorAll(".ant-form-item-label")).find(
        (el) => el.textContent?.includes("使用表")
      );
      const useItem = useLabel?.closest(".ant-form-item") as HTMLElement | null;
      expect(useItem?.textContent).toContain("ads.gmv_report");
    });
  });

  it("SQL 推断后「一键采纳」将系统建议编码填入输入框（惰性设计）", async () => {
    mockedSuggest.mockResolvedValue({
      fields: {
        source_table: { source: "sql_parse", value: "dwd.sales_detail", confidence: 1, reason: "" },
        measure_column: { source: "sql_parse", value: "gmv", confidence: 1, reason: "" },
        name: { source: "rule", value: "订单销售额", confidence: 0.9, reason: "" },
      },
      metric_code_suggestion: "sales_order_gmv_day",
      suggested: [],
    } as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openSqlInfer();
    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: "SELECT SUM(amount) AS gmv FROM dwd.sales_detail GROUP BY dt" },
    });
    fireEvent.click(screen.getByText("智能推断并回填字段"));
    // 编码字段在向导 Step2（治理确认）——推断完成后导航过去
    await goToStep(2);
    await waitFor(() => {
      expect(screen.getByText(/系统建议: sales_order_gmv_day/)).toBeTruthy();
    });
    // 点「一键采纳」→ 编码输入框填入建议编码
    fireEvent.click(screen.getByText("一键采纳"));
    const codeInput = document.querySelector('input[id="metric_code"]') as HTMLInputElement;
    expect(codeInput?.value).toBe("sales_order_gmv_day");
  });

  it("SQL 推断进行中：页面中心展示大旋转图标（Spin 遮罩）", async () => {
    // 手动控制 promise，让推断停留在"进行中"状态以便断言遮罩
    let resolveSuggest!: (v: unknown) => void;
    mockedSuggest.mockReturnValue(
      new Promise((res) => {
        resolveSuggest = res;
      }) as never
    );
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openSqlInfer();

    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: "SELECT SUM(gmv) AS gmv FROM dwd.sales_detail GROUP BY dt, shop_id" },
    });
    fireEvent.click(screen.getByText("智能推断并回填字段"));

    // 推断中：中心遮罩 + 大号旋转图标 + 提示文案可见
    await waitFor(() => {
      const spinner = document.querySelector(".ant-spin.ant-spin-spinning");
      expect(spinner).toBeTruthy();
    });
    expect(screen.getByText("正在智能推断指标定义，请稍候…")).toBeTruthy();

    // 推断完成：遮罩消失
    resolveSuggest({
      fields: {
        source_table: { value: "dwd.sales_detail", source: "sql_parse" },
        measure_column: { value: "gmv", source: "sql_parse" },
      },
      definition_json: {},
    } as never);
    await waitFor(() => {
      expect(document.querySelector(".ant-spin.ant-spin-spinning")).toBeNull();
    });
  });

  it("SQL 推断：未粘贴 SQL 时「智能推断」按钮禁用；未选域也可打开抽屉（域建议在抽屉内）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    // 未选域时标题区「SQL 智能推断」按钮 enabled——域建议在抽屉内完成（FR-010 域建议增强）
    const titleBtn = screen.getByRole("button", { name: /SQL 智能推断/ }) as HTMLButtonElement;
    expect(titleBtn.disabled).toBe(false);
    expect(mockedSuggest).not.toHaveBeenCalled();
    // 打开抽屉（未选域也可打开）：SQL 为空时抽屉内「智能推断并回填字段」按钮 disabled
    fireEvent.click(titleBtn);
    await waitFor(() => expect(document.querySelector(".ant-drawer")).toBeTruthy());
    let inferBtn = screen.getByText("智能推断并回填字段").closest("button") as HTMLButtonElement;
    expect(inferBtn.disabled).toBe(true);
    expect(mockedSuggest).not.toHaveBeenCalled();
    // 粘贴 SQL 后（仍未选域）按钮 enabled——可触发 域建议 + SQL 推断
    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: "SELECT SUM(gmv) AS gmv FROM dwd.sales_detail GROUP BY dt" },
    });
    inferBtn = screen.getByText("智能推断并回填字段").closest("button") as HTMLButtonElement;
    expect(inferBtn.disabled).toBe(false);
    expect(mockedSuggest).not.toHaveBeenCalled();
  });

  it("SQL 推断失败：展示明确错误原因", async () => {
    mockedSuggest.mockRejectedValue(new Error("invalid SQL syntax"));
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openSqlInfer();
    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: "SELECT FROM WHERE" },
    });
    fireEvent.click(screen.getByText("智能推断并回填字段"));
    await waitFor(() => expect(screen.getByText(/SQL 推断失败/)).toBeTruthy());
  });

  it("冲突预检：冲突类型显示中文标签（same_name_diff_def→同名不同义，非原始英文）", async () => {
    mockedCheckConflict.mockResolvedValue({
      detections: [
        {
          conflict_type: "same_name_diff_def",
          existing_code: "sales_gmv_day",
          score: 0.9,
          severity: "high",
          block_publish: true,
          reason: "口径定义不一致",
        },
      ],
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();

    // 编码与口径定义在 Step2（治理+口径），预检按钮在 Step3（提交）
    await goToStep(2);
    const codeInput = screen.getByLabelText("指标编码") as HTMLInputElement;
    fireEvent.change(codeInput, { target: { value: "sales_test" } });
    const defInput = screen.getByLabelText("口径定义 (JSON)") as HTMLTextAreaElement;
    fireEvent.change(defInput, { target: { value: '{"expr": "sum(amount)"}' } });

    await goToStep(3);
    fireEvent.click(screen.getByRole("button", { name: /冲突预检/ }));
    // 正确映射：后端 ConflictType 值为 same_name_diff_def → 中文「同名不同义」
    await screen.findByText(/同名不同义/);
    expect(screen.queryByText(/same_name_diff_def/)).toBeNull();
  });
});

describe("MetricCreate SQL 推断业务域建议（FR-010 域建议增强）", () => {
  const INFER_RESULT = {
    metric_code_suggestion: null,
    segments: { domain: "", biz_object: null, measure: null, period: null },
    fields: {
      source_table: { value: "dwd.sales_detail", source: "sql_parse", confidence: 0.9, reason: "" },
      measure_column: { value: "gmv", source: "sql_parse", confidence: 0.9, reason: "" },
      name: { value: "订单销售额", source: "sql_parse", confidence: 0.8 },
    },
    definition_json: { expression: "SUM(gmv)", source_fields: [{ table: "dwd.sales_detail", column: "gmv" }] },
    definition_mode: "expression",
  };

  const SQL = "SELECT SUM(gmv) AS gmv FROM dwd.sales_detail GROUP BY dt";

  beforeEach(() => {
    vi.clearAllMocks();
    mockedTree.mockResolvedValue(TREE);
    mockedDict.mockResolvedValue([]);
    mockedSuggest.mockResolvedValue(INFER_RESULT as never);
    mockedSuggestDomain.mockResolvedValue(NO_DOMAIN_SUGGESTION);
    mockedCatalogs.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
  });

  async function openInferWithSql() {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openSqlInfer();
    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: SQL },
    });
    fireEvent.click(screen.getByText("智能推断并回填字段"));
  }

  it("未选域 SQL 推断：目录唯一命中业务域 → 自动应用并预填 Step0 域，推断按建议域重跑", async () => {
    mockedSuggestDomain.mockResolvedValue({
      status: "unique",
      domain: { code: "sales", name: "销售", confidence: 0.9, source: "catalog", reason: "采集目录命中" },
      candidates: [],
      matched_tables: ["dwd.sales_detail"],
    } as never);
    await openInferWithSql();
    // 抽屉内域建议成功提示（已自动应用）
    await waitFor(() => expect(screen.getByText(/已按建议选择业务域：销售/)).toBeTruthy());
    // autoSuggest 以建议域重跑
    await waitFor(() =>
      expect(mockedSuggest).toHaveBeenCalledWith(
        expect.objectContaining({ domain_code: "sales", sql: expect.stringContaining("SELECT") })
      )
    );
  });

  it("未选域 SQL 推断：表归属多个域 → 弹窗候选，选择后应用并重跑推断", async () => {
    mockedSuggestDomain.mockResolvedValue({
      status: "multiple",
      domain: null,
      candidates: [
        { code: "sales", name: "销售", confidence: 0.9, source: "catalog", reason: "a" },
        { code: "finance", name: "财务", confidence: 0.85, source: "mount", reason: "b" },
      ],
      matched_tables: ["dwd.sales_detail"],
    } as never);
    await openInferWithSql();
    // 多候选弹窗出现
    await screen.findByText("选择业务域（SQL 涉及表归属多个域）");
    // 选「财务」并确认 → 应用域建议 + 用该域重跑推断
    fireEvent.click(screen.getByText("财务（finance）"));
    fireEvent.click(screen.getByText("应用并推断"));
    await waitFor(() =>
      expect(mockedSuggest).toHaveBeenCalledWith(
        expect.objectContaining({ domain_code: "finance", sql: expect.stringContaining("SELECT") })
      )
    );
  });

  it("未选域 SQL 推断：AI 兜底推断业务域（表未被采集）→ 自动应用并标记 AI 来源", async () => {
    mockedSuggestDomain.mockResolvedValue({
      status: "llm",
      domain: { code: "medical_fee", name: "医疗收费", confidence: 0.7, source: "llm", reason: "AI 推断" },
      candidates: [],
      matched_tables: [],
    } as never);
    await openInferWithSql();
    await waitFor(() => expect(screen.getByText(/已按建议选择业务域：医疗收费/)).toBeTruthy());
    // 来源徽标为 AI
    expect(screen.getByText(/来源：AI/)).toBeTruthy();
  });

  it("已选域 SQL 推断：建议域与当前所选不同 → 抽屉展示冲突提示与切换入口", async () => {
    mockedSuggestDomain.mockResolvedValue({
      status: "unique",
      domain: { code: "finance", name: "财务", confidence: 0.9, source: "catalog", reason: "采集目录命中" },
      candidates: [],
      matched_tables: ["dwd.finance_bill"],
    } as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain(); // 选 sales
    await openSqlInfer();
    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: SQL },
    });
    fireEvent.click(screen.getByText("智能推断并回填字段"));
    await screen.findByText("SQL 智能推断结果");
    // 冲突提示 + 切换按钮
    expect(screen.getByText(/与当前所选不同/)).toBeTruthy();
    expect(screen.getByText("切换为 财务")).toBeTruthy();
  });

  it("未选域 SQL 推断：无法建议业务域 → 提示手动选择，推断照常（空域）", async () => {
    await openInferWithSql();
    await screen.findByText("SQL 智能推断结果");
    expect(screen.getByText(/未能自动推断业务域/)).toBeTruthy();
    await waitFor(() =>
      expect(mockedSuggest).toHaveBeenCalledWith(
        expect.objectContaining({ domain_code: "", sql: expect.stringContaining("SELECT") })
      )
    );
  });
});

describe("MetricCreate 源表选择惰性化", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedTree.mockResolvedValue(TREE);
    mockedDict.mockResolvedValue([]);
    mockedSuggest.mockResolvedValue(NO_SUGGESTION);
    mockedCatalogs.mockResolvedValue({
      items: [
        makeCatalog("dwd.sales_detail"),
        makeCatalog("dwd.sales_order"),
        makeCatalog("dwd.shop_dim"),
      ],
      total: 3,
      page: 1,
      page_size: 20,
    });
  });

  it("源表下拉展开时自动加载平台已采集的表（无需先输入关键词）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    // 源表在向导 Step1（指标定义）——导航过去
    await goToStep(1);
    // 点击源表 Select 展开下拉（id 在内部 input 上）
    const srcInput = document.querySelector('input[id="source_table"]') as HTMLInputElement;
    fireEvent.mouseDown(srcInput);
    // 展开即触发加载（onOpenChange → 空关键词加载默认表列表）
    await waitFor(() => expect(mockedCatalogs).toHaveBeenCalledWith(
      expect.objectContaining({ entity_type: "TABLE", page_size: 20 })
    ));
    await waitFor(() => expect(screen.getAllByText("dwd.sales_order").length).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getAllByText("dwd.shop_dim").length).toBeGreaterThan(0));
  });

  it("关联数据表（口径定义区）下拉展开时同样自动加载平台已采集的表", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    // 口径定义在向导 Step2（治理+口径）——导航过去
    await goToStep(2);
    // 展开「口径定义 → 关联数据表」多选下拉
    const relatedSelect = screen.getAllByText(/展开浏览已接入表/)[0];
    fireEvent.mouseDown(relatedSelect);
    // 展开即触发加载（onOpenChange → 空关键词加载默认表列表，与源表名一致）
    await waitFor(() => expect(mockedCatalogs).toHaveBeenCalledWith(
      expect.objectContaining({ entity_type: "TABLE", source_status: "active", page_size: 20 })
    ));
    await waitFor(() => expect(screen.getAllByText("dwd.sales_order").length).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getAllByText("dwd.shop_dim").length).toBeGreaterThan(0));
  });

  it("关联数据表（口径定义区）支持关键词搜索加载", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    // 口径定义在向导 Step2（治理+口径）——导航过去
    await goToStep(2);
    const relatedSelect = screen.getAllByText(/展开浏览已接入表/)[0];
    fireEvent.mouseDown(relatedSelect);
    // 关联数据表是多选 Select（.ant-select-multiple），其搜索输入框在容器内；源表/度量列等单选不受影响
    const relatedSearchInput = document.querySelector(
      ".ant-select-multiple .ant-select-selection-search-input"
    ) as HTMLInputElement;
    fireEvent.change(relatedSearchInput, { target: { value: "dwd.sales" } });
    await waitFor(() => expect(mockedCatalogs).toHaveBeenCalledWith(
      expect.objectContaining({ keyword: "dwd.sales", source_status: "active" })
    ));
  });
});

describe("MetricCreate 未采集表/字段手动输入", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedTree.mockResolvedValue(TREE);
    mockedDict.mockResolvedValue([]);
    mockedSuggest.mockResolvedValue(NO_SUGGESTION);
    // 模拟平台未采集：搜索任何关键词都返回空 → 触发「未采集，手动输入」选项注入
    mockedCatalogs.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
  });

  it("依赖表（上游）：搜索未采集表注入「未采集」选项，点选即以完整表名录入", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await goToStep(2);
    // 展开依赖表（上游）多选下拉（第 1 个「展开浏览已接入表」）
    const relatedSelect = screen.getAllByText(/展开浏览已接入表/)[0];
    const depSelectEl = relatedSelect.closest(".ant-select");
    fireEvent.mouseDown(relatedSelect);
    // 输入未采集表名 → 下拉注入选项（optionRender 标注「未采集，手动输入」）
    const searchInput = depSelectEl?.querySelector(
      ".ant-select-selection-search-input"
    ) as HTMLInputElement;
    fireEvent.change(searchInput, { target: { value: "ods.unknown_detail" } });
    const injected = await waitFor(() => {
      const el = document.querySelector(
        '.ant-select-item-option[title="ods.unknown_detail"]'
      ) as HTMLElement | null;
      expect(el).toBeTruthy();
      return el;
    });
    // 下拉里明确标识「未采集，手动输入」（通俗提示）
    expect(injected!.textContent).toContain("未采集，手动输入");
    fireEvent.click(injected!);
    // 选中后 tag 为干净表名（不带「未采集」后缀——提交的是 value 本身）
    await waitFor(() => {
      const tag = depSelectEl?.querySelector(".ant-select-selection-item");
      expect(tag?.textContent).toContain("ods.unknown_detail");
    });
    expect(screen.queryByText("ods.unknown_detail（未采集，手动输入）")).toBeNull();
  });

  it("使用表（下游）：同样支持未采集表手动录入（第 2 个多选下拉）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await goToStep(2);
    // 第 2 个「展开浏览已接入表」= 使用表（下游）
    const relatedSelect = screen.getAllByText(/展开浏览已接入表/)[1];
    const downSelectEl = relatedSelect.closest(".ant-select");
    fireEvent.mouseDown(relatedSelect);
    const searchInput = downSelectEl?.querySelector(
      ".ant-select-selection-search-input"
    ) as HTMLInputElement;
    fireEvent.change(searchInput, { target: { value: "ads.unknown_report" } });
    const injected = await waitFor(() => {
      const el = document.querySelector(
        '.ant-select-item-option[title="ads.unknown_report"]'
      ) as HTMLElement | null;
      expect(el).toBeTruthy();
      return el;
    });
    expect(injected!.textContent).toContain("未采集，手动输入");
    fireEvent.click(injected!);
    await waitFor(() => {
      const tag = downSelectEl?.querySelector(".ant-select-selection-item");
      expect(tag?.textContent).toContain("ads.unknown_report");
    });
  });

  it("度量列：未采集源表场景下可直接输入自定义列名（注入「未采集」列选项）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await goToStep(1);
    // 原子指标 Step1 度量列 Select：直接输入未采集列名
    const colInput = document.querySelector('input[id="measure_column"]') as HTMLInputElement;
    fireEvent.mouseDown(colInput);
    fireEvent.change(colInput, { target: { value: "pay_amt" } });
    const injected = await waitFor(() => {
      const el = document.querySelector(
        '.ant-select-item-option[title="pay_amt"]'
      ) as HTMLElement | null;
      expect(el).toBeTruthy();
      return el;
    });
    expect(injected!.textContent).toContain("未采集，手动输入");
    fireEvent.click(injected!);
    // 选中后选择框显示干净列名
    await waitFor(() => {
      const sel = document.querySelector(".ant-select-selection-item");
      expect(sel?.textContent).toContain("pay_amt");
    });
  });

  it("批量注册弹窗：源宽表未采集时可手动输入完整表名", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    const modal = await openBatchModal();
    // 源表 Select 搜索输入未采集宽表名 → 注入「未采集」选项
    const srcInput = modal.querySelector('input[id="source_table"]') as HTMLInputElement;
    fireEvent.mouseDown(srcInput);
    fireEvent.change(srcInput, { target: { value: "ods.unknown_wide" } });
    const injected = await waitFor(() => {
      const el = document.querySelector(
        '.ant-select-item-option[title="ods.unknown_wide"]'
      ) as HTMLElement | null;
      expect(el).toBeTruthy();
      return el;
    });
    expect(injected!.textContent).toContain("未采集，手动输入");
    fireEvent.click(injected!);
    // 选中后弹窗内显示完整表名
    await waitFor(() => {
      const sel = modal.querySelector(".ant-select-selection-item");
      expect(sel?.textContent).toContain("ods.unknown_wide");
    });
  });
});

describe("MetricCreate 指标类型级联（三类指标配置差异化，PRD 4.5）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedTree.mockResolvedValue(TREE);
    mockedDict.mockResolvedValue([{ code: "CNY", label: "元" }] as never);
    mockedSuggest.mockResolvedValue(NO_SUGGESTION);
    mockedCatalogs.mockResolvedValue({
      items: [makeCatalog("dwd.sales_detail", [{ name: "gmv" }, { name: "order_cnt" }])],
      total: 1,
      page: 1,
      page_size: 20,
    });
    mockedMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
  });

  it("默认原子指标：展示逻辑度量与源表/度量列/周期，隐藏依赖指标与计算表达式", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await goToStep(1);
    // 原子来源配置区（逻辑度量 + 兼容旧式源表/度量列/周期）展示
    expect(screen.getByText("② 原子来源（逻辑度量 + 聚合方式）")).toBeTruthy();
    expect(screen.getByText("逻辑度量（度量目录，OneData 原子层）")).toBeTruthy();
    expect(screen.getByText("源表名（兼容旧式来源，可选）")).toBeTruthy();
    expect(screen.getByText("度量列（兼容旧式来源，可选）")).toBeTruthy();
    expect(screen.getByText("统计周期（兼容旧式推断，可选）")).toBeTruthy();
    // 依赖指标 / 计算表达式为派生/复合专属，原子下不出现
    expect(screen.queryByText("② 依赖指标")).toBeNull();
    expect(screen.queryByText("计算表达式")).toBeNull();
    // OneData（界限文档 §2.3）：原子不挂物理表——治理 Step2 粒度/单位/币种/时间语义/新鲜度/数仓层隐藏，
    // 聚合方式保留（原子核心算法属性）
    await goToStep(2);
    expect(screen.queryByText("粒度")).toBeNull();
    expect(screen.queryByText("单位")).toBeNull();
    expect(screen.queryByText("币种（选填）")).toBeNull();
    expect(screen.queryByText("时间语义")).toBeNull();
    expect(screen.queryByText("新鲜度")).toBeNull();
    expect(screen.queryByText("数仓层")).toBeNull();
    expect(screen.getByText("聚合")).toBeTruthy();
    // 展开「高级治理设置」：分级/可加性/服务模式可见
    fireEvent.click(screen.getByText(/高级治理设置/));
    await waitFor(() => expect(screen.getByText("分级")).toBeTruthy());
    expect(screen.getByText("可加性")).toBeTruthy();
    expect(screen.getByText("服务模式")).toBeTruthy();
  });

  it("切换到派生指标：展示依赖指标（必填）与计算表达式，隐藏原子来源", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await goToStep(1);
    fireEvent.click(screen.getByText("派生指标"));
    // 依赖指标配置区（Step1）出现，原子专属配置隐藏
    expect(screen.getByText("② 依赖指标")).toBeTruthy();
    expect(screen.queryByText("② 原子来源（逻辑度量 + 聚合方式）")).toBeNull();
    expect(screen.queryByText("源表名（兼容旧式来源，可选）")).toBeNull();
    expect(screen.queryByText("度量列（兼容旧式来源，可选）")).toBeNull();
    // 计算表达式输入在 Step2（口径定义）——受控组件（Form.Item 无 name），label 无 htmlFor，须按文本查询
    await goToStep(2);
    await waitFor(() => expect(screen.getByText("计算表达式")).toBeTruthy());
  });

  it("派生指标未选依赖指标提交 → 前端拦截并提示依赖必填", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "sales_gmv_day" } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    fireEvent.click(screen.getByText("派生指标"));
    // 名称在 Step2（治理确认）必填——先填名称再导航到 Step3 提交（依赖指标/计算表达式留空）
    await goToStep(2);
    fireEvent.change(screen.getByPlaceholderText(/指标显示名称/), { target: { value: "客单价" } });
    await goToStep(3);
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    await waitFor(() =>
      expect(screen.getByText("派生/复合指标必须选择至少 1 个依赖指标")).toBeTruthy()
    );
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it("派生指标已选依赖但缺计算表达式提交 → 前端拦截并提示表达式必填", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "sales_gmv_day" } as any);
    // 依赖指标搜索返回已发布上游指标
    mockedMetrics.mockResolvedValue({
      items: [{ metric_code: "sales_gmv_amount_daily", name: "每日 GMV", type: "atomic", status: "PUBLISHED" }],
    } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    fireEvent.click(screen.getByText("派生指标"));
    // 在依赖指标多选输入已发布指标编码，回车后从下拉选中
    const depInput = document.querySelector(
      ".ant-select-multiple .ant-select-selection-search-input"
    ) as HTMLInputElement;
    fireEvent.change(depInput, { target: { value: "sales_gmv_amount_daily" } });
    await waitFor(() => expect(mockedMetrics).toHaveBeenCalled());
    await clickSelectOption("每日 GMV (sales_gmv_amount_daily)");
    // 名称在 Step2（治理确认）必填——先填名称再导航到 Step3 提交（计算表达式留空）
    await goToStep(2);
    fireEvent.change(screen.getByPlaceholderText(/指标显示名称/), { target: { value: "客单价" } });
    await goToStep(3);
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    await waitFor(() =>
      expect(screen.getByText("请填写计算表达式（如 gmv / order_cnt）")).toBeTruthy()
    );
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it("原子指标未选逻辑度量且未填口径提交 → 前端拦截并提示来源必填", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "sales_gmv_day" } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    // 名称在 Step2（治理确认）必填——先填名称再导航到 Step3 提交（默认 atomic：未选逻辑度量/源表度量列、口径为空）
    await goToStep(2);
    fireEvent.change(screen.getByPlaceholderText(/指标显示名称/), { target: { value: "GMV" } });
    await goToStep(3);
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    await waitFor(() =>
      expect(screen.getByText("原子指标请选择逻辑度量（推荐）或源表与度量列，或填写口径定义")).toBeTruthy()
    );
    expect(mockedCreate).not.toHaveBeenCalled();
  });
});
