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
    parseSqlBatch: vi.fn(),
    parseSqlTables: vi.fn(),
    batchRegisterFromSql: vi.fn(),
    getDomainDefaults: vi.fn(),
    checkConflict: vi.fn(),
    createMetric: vi.fn(),
    refineMetricDefinition: vi.fn(),
    getMetric: vi.fn(),
    updateMetric: vi.fn(),
    listTerms: vi.fn(),
    // 默认 platform_admin（不受域门禁限制）；跨域预检测试再覆盖为 domain_admin
    fetchCurrentUser: vi.fn().mockResolvedValue({
      id: 1,
      username: "tester",
      display_name: "测试员",
      role: "platform_admin",
      domain: null,
    }),
  };
});

import { listDomainTree, listDictItems, listCatalogs, batchRegisterMetrics, batchSubmitMetrics, listUsers, autoSuggestMetric, suggestDomain, parseSqlBatch, parseSqlTables, batchRegisterFromSql, checkConflict, createMetric, listMetrics, listMeasureCatalogs, listDimensions, listTerms, fetchCurrentUser, refineMetricDefinition, getMetric, updateMetric, getDomainDefaults } from "../api";
import type { DBCatalog, SubjectDomainTreeNode, AutoSuggestResponse, DomainSuggestionResponse, SqlBatchParseResult, MetricResponse } from "../types";

const mockedTree = vi.mocked(listDomainTree);
const mockedDict = vi.mocked(listDictItems);
const mockedCatalogs = vi.mocked(listCatalogs);
const mockedBatch = vi.mocked(batchRegisterMetrics);
const mockedBatchSubmit = vi.mocked(batchSubmitMetrics);
const mockedUsers = vi.mocked(listUsers);
const mockedSuggest = vi.mocked(autoSuggestMetric);
const mockedSuggestDomain = vi.mocked(suggestDomain);
const mockedParseSqlBatch = vi.mocked(parseSqlBatch);
const mockedParseSqlTables = vi.mocked(parseSqlTables);
const mockedBatchFromSql = vi.mocked(batchRegisterFromSql);
const mockedCheckConflict = vi.mocked(checkConflict);
const mockedCreate = vi.mocked(createMetric);
const mockedMetrics = vi.mocked(listMetrics);
const mockedRefine = vi.mocked(refineMetricDefinition);
const mockedDomainDefaults = vi.mocked(getDomainDefaults);
const mockedGetMetric = vi.mocked(getMetric);
const mockedUpdateMetric = vi.mocked(updateMetric);
const mockedListTerms = vi.mocked(listTerms);

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

/** 读取当前向导激活步骤（依据"下一步"按钮文案——Step0/1 各有唯一文案，Step2 无下一步）。 */
function currentStepIndex(): number {
  if (screen.queryByRole("button", { name: "下一步：指标基本信息" })) return 0;
  if (screen.queryByRole("button", { name: "下一步：具体实现" })) return 1;
  return 2;
}

/** OneData 向导：点击「下一步/上一步」前进/回退到目标步骤（检测当前激活步骤，避免重复调用过度推进）。 */
async function goToStep(target: number) {
  let guard = 0;
  while (currentStepIndex() !== target && guard < 6) {
    if (currentStepIndex() < target) {
      const btn = screen.queryByRole("button", { name: /下一步/ });
      if (!btn) break;
      fireEvent.click(btn);
    } else {
      const prevBtn = screen.queryByRole("button", { name: /上一步/ });
      if (!prevBtn) break;
      fireEvent.click(prevBtn);
    }
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

/**
 * Step ①「③ 口径责任方」：填写数仓开发责任方（数仓开发必填，PRD 4.5）。
 * 用外部人员名称输入（不依赖 mockedUsers 用户列表），提交前调用。
 */
async function fillDwDeveloper(name = "数仓张三") {
  const item = screen.getByText("数仓开发").closest(".ant-form-item") as HTMLElement;
  const selector = item.querySelector(".ant-select-selector") as HTMLElement;
  fireEvent.mouseDown(selector);
  const input = item.querySelector(
    "input.ant-select-selection-search-input",
  ) as HTMLInputElement;
  fireEvent.change(input, { target: { value: name } });
  await clickSelectOption(`外部人员：${name}`);
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

// 全局默认：域默认值返回空对象（无预填）。注意 vi.clearAllMocks() 只清调用记录、
// 不重置 mockImplementation——单个用例覆盖 getDomainDefaults 后会泄漏到后续所有用例
// （域默认 type 预填会改变指标类型联动），故在此统一归位默认实现。
beforeEach(() => {
  mockedDomainDefaults.mockResolvedValue({});
  mockedListTerms.mockResolvedValue({ items: [] } as any);
});

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
          { name: "dept_code" },
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

  it("L2：批量注册失败项出现「重试失败项」按钮，点击仅重跑失败列", async () => {
    mockedBatch
      .mockResolvedValueOnce({
        batch_id: "batch_retry1",
        candidates: [
          { metric_code: "sales_gmv_day", status: "DRAFT", validation_errors: null },
          { metric_code: "sales_order_cnt_day", status: "VALIDATION_ERROR", validation_errors: "命名校验失败" },
        ],
      })
      .mockResolvedValueOnce({
        batch_id: "batch_retry2",
        candidates: [
          { metric_code: "sales_order_cnt_day", status: "DRAFT", validation_errors: null },
        ],
      });
    renderPage();
    const modal = await openBatchModal();
    await fillBatchForm(modal, "gmv\norder_cnt");
    fireEvent.click(within(modal).getByText("提交批量注册"));
    await waitFor(() => {
      expect(screen.getByText("批量注册完成：成功 1 / 失败 1")).toBeTruthy();
    });
    // 修复前无单条重试：仅「继续注册」全量重跑会把已建 DRAFT 的列再判冲突。
    // 修复后「重试失败项」仅重跑失败列（order_cnt）。
    const retryBtn = screen.getByRole("button", { name: /重试失败项/ });
    fireEvent.click(retryBtn);
    await waitFor(() => {
      // 第二次调用以失败列重跑（仅 order_cnt，不再包含已成功的 gmv）
      expect(mockedBatch).toHaveBeenLastCalledWith(
        expect.objectContaining({ measure_columns: ["order_cnt"] }),
      );
    });
  });

  it("M3：批量注册重复度量列自动去重（提交唯一列）", async () => {
    mockedBatch.mockResolvedValue({
      batch_id: "batch_dedupe",
      candidates: [
        { metric_code: "sales_gmv_day", status: "DRAFT", validation_errors: null },
      ],
    });
    renderPage();
    const modal = await openBatchModal();
    // tags 模式可产生重复 tag：先点选 gmv，再手输同值 tag 构造重复。
    // handleBatchSubmit 去重后仅提交唯一列（修复前重复列生成相同 metric_code，
    // 第二条被后端判 VALIDATION_ERROR 误导）
    await fillBatchForm(modal, "gmv");
    const tagInput = modal.querySelector(".ant-select-multiple input") as HTMLInputElement;
    fireEvent.change(tagInput, { target: { value: "gmv" } });
    fireEvent.keyDown(tagInput, { key: "Enter", code: "Enter" });
    fireEvent.click(within(modal).getByText("提交批量注册"));
    await waitFor(() => {
      expect(mockedBatch).toHaveBeenCalledWith(
        expect.objectContaining({ measure_columns: ["gmv"] }),
      );
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

  it("维度映射选择源表列后按列名自动推断维度名预填", async () => {
    mockedBatch.mockResolvedValue({
      batch_id: "batch_map2",
      candidates: [{ metric_code: "sales_gmv_day", status: "DRAFT", validation_errors: null }],
    });
    renderPage();
    const modal = await openBatchModal();
    await fillBatchForm(modal, "gmv");

    // 添加维度映射行：不手输维度名，直接选列 dept_code → 维度名自动预填为 dept
    fireEvent.click(within(modal).getByText("添加维度映射"));
    const colSelect = within(modal).getByText("选择源表列");
    fireEvent.mouseDown(colSelect);
    await clickSelectOption("dept_code");
    const dimInput = modal.querySelector('[data-testid="dim-name-auto"] input') as HTMLInputElement;
    await waitFor(() => {
      expect((dimInput as HTMLInputElement).value).toBe("dept");
    });

    fireEvent.click(within(modal).getByText("提交批量注册"));
    await waitFor(() => {
      expect(mockedBatch).toHaveBeenCalledWith(
        expect.objectContaining({ dimension_mapping: { dept: "dept_code" } }),
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
        type: { value: "derived", source: "sql_parse", confidence: 0.85 },
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

    // 源表已回填到 Step2 挂载实体行（SQL 推断一律派生，物理来源走挂载行而非原子
    // 来源卡——此前回填到顶层 source_table，派生分支不收集该字段导致源表丢失）
    await goToStep(2);
    await waitFor(() => {
      const srcInput = document.querySelector('input[id="mounts_0_source_table"]');
      const container = srcInput?.closest(".ant-select") as HTMLElement | null;
      expect(container?.textContent).toContain("dwd.sales_detail");
    });
    // 度量列下拉因回填 source_table 联动加载了列
    expect(mockedCatalogs).toHaveBeenCalledWith(
      expect.objectContaining({ entity_type: "TABLE", keyword: "dwd.sales_detail" })
    );
  });

  it("SQL 推断：LLM 推断按钮 → 携带 use_llm=true 调用后端并回填 LLM 字段", async () => {
    mockedSuggest.mockResolvedValue({
      metric_code_suggestion: "sales_gmv_day",
      segments: { domain: "sales", biz_object: "gmv", measure: "gmv", period: "day" },
      fields: {
        source_table: { value: "dwd.sales_detail", source: "llm", confidence: 0.7, reason: "AI 依据 SQL 语义推断" },
        measure_column: { value: "gmv", source: "llm", confidence: 0.7, reason: "AI 依据 SQL 语义推断" },
        name: { value: "日订单销售额", source: "llm", confidence: 0.7, reason: "AI 依据 SQL 语义推断" },
        type: { value: "derived", source: "rule", confidence: 0.8 },
        granularity: { value: "day", source: "rule", confidence: 0.8 },
        unit: { value: "CNY", source: "llm", confidence: 0.7 },
        aggregation: { value: "SUM", source: "llm", confidence: 0.7 },
        time_semantics: { value: "PERIOD", source: "rule", confidence: 0.6 },
        freshness: { value: "T1", source: "rule", confidence: 0.5 },
        dw_layer: { value: "DWD", source: "rule", confidence: 0.8 },
        additivity: { value: "ADDITIVE", source: "rule", confidence: 0.7 },
        serving_mode: { value: "BATCH_ONLY", source: "rule", confidence: 0.6 },
        metric_tier: { value: "T3", source: "rule", confidence: 0.5 },
        definition_json: {
          value: { expression: "SUM(gmv)", source_fields: [{ table: "dwd.sales_detail", column: "gmv" }] },
          source: "llm",
          confidence: 0.7,
        },
        definition_mode: { value: "expression", source: "llm", confidence: 0.7 },
      },
      definition_json: { expression: "SUM(gmv)", source_fields: [{ table: "dwd.sales_detail", column: "gmv" }] },
      definition_mode: "expression",
    } as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openSqlInfer();

    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: "SELECT SUM(gmv) AS gmv FROM dwd.sales_detail GROUP BY dt, shop_id" },
    });
    fireEvent.click(screen.getByText("LLM 推断并回填字段"));

    // LLM 模式：请求携带 use_llm=true
    await waitFor(() =>
      expect(mockedSuggest).toHaveBeenCalledWith(
        expect.objectContaining({ sql: expect.stringContaining("SUM(gmv)"), use_llm: true })
      )
    );
    // LLM 推断名称回填到摘要弹窗
    await screen.findByText("SQL 智能推断结果");
    expect(screen.getByText("日订单销售额")).toBeTruthy();
  });

  it("SQL 推断：匹配已发布逻辑度量 → 摘要弹窗推荐 + 一键应用回填 measure_id", async () => {
    mockedSuggest.mockResolvedValue({
      metric_code_suggestion: "sales_doctor_active_cnt_day",
      segments: { domain: "sales", biz_object: "doctor", measure: "active_cnt", period: "day" },
      fields: {
        source_table: { value: "dwd.doctor_visit", source: "sql_parse", confidence: 0.9, reason: "SQL 解析源表" },
        measure_column: { value: "doctor_code", source: "sql_parse", confidence: 0.9, reason: "SQL 解析度量列" },
        name: { value: "医生活跃数", source: "sql_parse", confidence: 0.8 },
        // 逻辑度量（measure_id）是原子指标专属概念（原子 = 逻辑度量 + 基础统计粒度，
        // 派生继承自原子）；方案 C 后 SQL 推断一律派生，故本用例显式用 atomic
        // 以覆盖「匹配已发布逻辑度量 → 推荐 + 一键应用回填」的原子来源卡交互。
        type: { value: "atomic", source: "sql_parse", confidence: 0.85 },
        granularity: { value: "day", source: "sql_parse", confidence: 0.9 },
        unit: { value: "人", source: "rule", confidence: 0.68 },
        aggregation: { value: "COUNT_DISTINCT", source: "sql_parse", confidence: 0.95 },
        time_semantics: { value: "PERIOD", source: "sql_parse", confidence: 0.6 },
        freshness: { value: "T1", source: "rule", confidence: 0.5 },
        dw_layer: { value: "DWD", source: "sql_parse", confidence: 0.8 },
        additivity: { value: "ADDITIVE", source: "rule", confidence: 0.6 },
        serving_mode: { value: "BATCH_ONLY", source: "sql_parse", confidence: 0.7 },
        metric_tier: { value: "T3", source: "fallback", confidence: 0.4 },
        definition_json: {
          value: {
            expression: "COUNT(DISTINCT doctor_code)",
            source_fields: [{ table: "dwd.doctor_visit", column: "doctor_code" }],
          },
          source: "sql_parse",
          confidence: 0.9,
        },
        definition_mode: { value: "expression", source: "sql_parse", confidence: 0.9 },
      },
      definition_json: {
        expression: "COUNT(DISTINCT doctor_code)",
        source_fields: [{ table: "dwd.doctor_visit", column: "doctor_code" }],
      },
      definition_mode: "expression",
      measure_suggestions: [
        {
          id: 7,
          measure_code: "doctor_active_cnt",
          name: "医生活跃数",
          measure_format: "NUMERIC",
          default_unit: "人",
          confidence: 1,
          reason: "度量列「doctor_code」与逻辑度量编码/同义词匹配",
        },
      ],
    } as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openSqlInfer();
    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: "SELECT COUNT(DISTINCT doctor_code) AS cnt FROM dwd.doctor_visit GROUP BY dt" },
    });
    fireEvent.click(screen.getByText("智能推断并回填字段"));

    // 摘要弹窗展示推荐逻辑度量 Tag
    await screen.findByText("SQL 智能推断结果");
    await screen.findByText(/推荐逻辑度量/);
    fireEvent.click(screen.getByText(/医生活跃数 \(doctor_active_cnt\)/));
    await waitFor(() =>
      expect(screen.getByText(/已应用逻辑度量「医生活跃数/)).toBeTruthy()
    );

    // 关闭摘要 → Step2 原子来源：逻辑度量已选中（下拉补进候选并显示选中值）
    fireEvent.click(screen.getByText("知道了"));
    await goToStep(2);
    await waitFor(() => {
      const item = document.querySelector(
        '.ant-select-selection-item[title*="doctor_active_cnt"]'
      ) as HTMLElement | null;
      expect(item).toBeTruthy();
    });
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

    // 回填到 Step⑥ 关联数据表（Step2 具体实现）：两个多选 Select 各自承接对应方向的表
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

  it("Q2: SQL 推断 SQL 模式自动回填数仓详细口径（dwDefinition）", async () => {
    mockedSuggest.mockResolvedValue({
      metric_code_suggestion: "outpatient_doctor_active_month",
      segments: { domain: "outpatient", biz_object: "doctor", measure: "active", period: "month" },
      fields: {
        source_table: { value: "wedw_dw.doctor_visit_agent_info_da", source: "sql_parse", confidence: 0.9, reason: "" },
        measure_column: { value: "doctor_code", source: "sql_parse", confidence: 0.9, reason: "" },
        name: { value: "月活", source: "sql_parse", confidence: 0.8 },
        definition_json: {
          value: {
            sql: "SELECT COUNT(DISTINCT doctor_code) AS cnt FROM wedw_dw.doctor_visit_agent_info_da",
            source_tables: ["wedw_dw.doctor_visit_agent_info_da"],
          },
          source: "sql_parse",
          confidence: 0.9,
        },
        definition_mode: { value: "sql", source: "sql_parse", confidence: 0.9 },
      },
      definition_json: {
        sql: "SELECT COUNT(DISTINCT doctor_code) AS cnt FROM wedw_dw.doctor_visit_agent_info_da",
        source_tables: ["wedw_dw.doctor_visit_agent_info_da"],
      },
      definition_mode: "sql",
    } as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openSqlInfer();
    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: "SELECT COUNT(DISTINCT doctor_code) AS cnt FROM wedw_dw.doctor_visit_agent_info_da" },
    });
    fireEvent.click(screen.getByText("智能推断并回填字段"));
    await screen.findByText("SQL 智能推断结果");
    fireEvent.click(screen.getByText("知道了"));
    // Q2：数仓详细口径（dwDefinition）自动回填推断的完整 SQL——用户无需再手填（在 Step⑤ 口径定义）
    await goToStep(2);
    await waitFor(() => {
      const dw = screen.getByLabelText("数仓SQL口径") as HTMLTextAreaElement;
      expect(dw.value).toContain("SELECT COUNT(DISTINCT doctor_code)");
    });
  });

  it("SQL 推断：摘要弹窗展示解析出的度量列清单（列名 + 聚合方式 + 原始表达式）", async () => {
    mockedSuggest.mockResolvedValue({
      metric_code_suggestion: "doctor_active_doctor_count_month",
      segments: { domain: "outpatient", biz_object: "doctor", measure: "doctor_code", period: "month" },
      fields: {
        source_table: { value: "wedw_dw.doctor_visit_agent_info_da", source: "sql_parse", confidence: 0.9, reason: "" },
        measure_column: { value: "doctor_code", source: "sql_parse", confidence: 0.9, reason: "" },
        name: { value: "医生活跃次数", source: "sql_parse", confidence: 0.8 },
        type: { value: "derived", source: "sql_parse", confidence: 0.85 },
        granularity: { value: "month", source: "sql_parse", confidence: 0.9 },
        aggregation: { value: "COUNT_DISTINCT", source: "sql_parse", confidence: 0.95 },
      },
      definition_json: { expression: "COUNT(DISTINCT doctor_code)" },
      definition_mode: "expression",
      // SQL 解析出的多度量列（月活 + 留存）——用户可确认识别是否成功
      parsed_measures: [
        {
          column: "doctor_code",
          agg: "COUNT_DISTINCT",
          alias: "current_month_active_doctor_cnt",
          table: "wedw_dw.doctor_visit_agent_info_da",
          expression: "COUNT(DISTINCT t1.doctor_code)",
        },
        {
          column: "doctor_code",
          agg: "COUNT_DISTINCT",
          alias: "last_month_active_doctor_cnt",
          table: "wedw_dw.doctor_visit_agent_info_da",
          expression: "COALESCE(COUNT(DISTINCT CASE WHEN NOT t2.doctor_code IS NULL THEN t2.doctor_code END), 0)",
        },
      ],
    } as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openSqlInfer();
    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: "insert overwrite table x select month_id, count(distinct doctor_code) as current_month_active_doctor_cnt from t group by month_id" },
    });
    fireEvent.click(screen.getByText("智能推断并回填字段"));

    // 摘要 Modal：展示两个解析出的度量列 + 聚合方式 + 原始表达式
    await screen.findByText("SQL 智能推断结果");
    expect(screen.getByText("SQL 解析出的度量列（2 个）——请核对是否真正识别成功：")).toBeTruthy();
    expect(screen.getAllByText("current_month_active_doctor_cnt").length).toBeGreaterThan(0);
    expect(screen.getAllByText("last_month_active_doctor_cnt").length).toBeGreaterThan(0);
    expect(screen.getAllByText("COUNT_DISTINCT").length).toBeGreaterThan(1);
    expect(screen.getByText(/COUNT\(DISTINCT t1\.doctor_code\)/)).toBeTruthy();
    // 多度量提示：可转批量解析分别创建（方案 A：SQL 物理口径回填为派生指标）
    expect(screen.getByText(/识别到 2 个度量列：当前回填首个「current_month_active_doctor_cnt」为派生指标/)).toBeTruthy();
    fireEvent.click(screen.getByText("知道了"));
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
    // 编码字段在向导 Step1（指标基本信息）——推断完成后导航过去
    await goToStep(1);
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

    // 编码在 Step1（② 指标基本信息），口径定义 JSON 在 Step2（⑤ 口径定义），预检按钮在 Step2（提交）
    await goToStep(1);
    const codeInput = screen.getByLabelText("指标编码") as HTMLInputElement;
    fireEvent.change(codeInput, { target: { value: "sales_test" } });

    await goToStep(2);
    const defInput = screen.getByLabelText("口径定义 (JSON)") as HTMLTextAreaElement;
    fireEvent.change(defInput, { target: { value: '{"expr": "sum(amount)"}' } });
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

  it("未选域 SQL 推断：域建议失败（none）→ 弹强制选域 Modal，选域后重跑推断", async () => {
    // beforeEach 默认 mock 即 NO_DOMAIN_SUGGESTION（none）
    await openInferWithSql();
    // 强制选域 Modal 出现（而非静默继续推断）
    await screen.findByText("请先选择业务域");
    // 域未定：此时不应已触发 autoSuggest（推断被中断等待选域）
    expect(mockedSuggest).not.toHaveBeenCalled();
    // 展开 Select 选「销售」并确认 → 应用域 + 用该域重跑推断
    fireEvent.mouseDown(screen.getByText("搜索并选择业务域"));
    fireEvent.click(await screen.findByText("销售 (sales)"));
    fireEvent.click(screen.getByText("选域并推断"));
    await waitFor(() =>
      expect(mockedSuggest).toHaveBeenCalledWith(
        expect.objectContaining({ domain_code: "sales", sql: expect.stringContaining("SELECT") })
      )
    );
    // 域建议已应用（预填 Step0 域）——Alert 与 message 各渲染一处
    await waitFor(() =>
      expect(screen.getAllByText(/已按建议选择业务域：销售/).length).toBeGreaterThan(0)
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

  it("未选域 SQL 推断：域建议失败（none）→ 取消强制选域则中断推断（不触发 autoSuggest）", async () => {
    await openInferWithSql();
    // 强制选域 Modal 出现
    await screen.findByText("请先选择业务域");
    // 取消 → 中断推断：不触发 autoSuggest，无推断结果弹窗
    const modal = document.querySelector(".ant-modal") as HTMLElement;
    fireEvent.click(within(modal).getByText(/取\s*消/));
    await waitFor(() => expect(mockedSuggest).not.toHaveBeenCalled());
    expect(screen.getByText(/已取消推断：请先选择业务域后再试/)).toBeTruthy();
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
    // 源表在向导 Step2（具体实现 → 原子来源）——导航过去
    await goToStep(2);
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

  it("关联数据表（Step⑥ 关联数据表）下拉展开时同样自动加载平台已采集的表", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    // 关联数据表在向导 Step2（具体实现）——导航过去
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

  it("关联数据表（Step⑥ 关联数据表）支持关键词搜索加载", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    // 关联数据表在向导 Step2（具体实现）——导航过去
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

  it("数仓SQL口径失焦 → 自动解析 SQL 提取源表并回填依赖表（上游）", async () => {
    mockedParseSqlTables.mockResolvedValue({ source_tables: ["dwd.fee_bill_di", "dims.region"] });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await goToStep(2);
    const dw = screen.getByLabelText("数仓SQL口径") as HTMLTextAreaElement;
    // blur 前获取依赖表（上游）Select 引用——回填后 placeholder「展开浏览已接入表」会消失，
    // 届时 getAllByText 只能匹配到使用表（下游），必须提前取引用（与既有未采集表测试一致）
    const depSelectEl = screen.getAllByText(/展开浏览已接入表/)[0].closest(".ant-select");
    fireEvent.change(dw, {
      target: {
        value:
          "SELECT dt, SUM(real_amount) AS amt FROM dwd.fee_bill_di LEFT JOIN dims.region r ON r.id = a.region_id GROUP BY dt",
      },
    });
    fireEvent.blur(dw);
    // 失焦后调用后端解析端点（透传数仓SQL口径文本）
    await waitFor(() =>
      expect(mockedParseSqlTables).toHaveBeenCalledWith(expect.stringContaining("dwd.fee_bill_di"))
    );
    // 依赖表（上游）自动回填解析出的表（该 Select 出现 tag，无需用户手输）
    await waitFor(() => {
      const tags = depSelectEl?.querySelectorAll(".ant-select-selection-item");
      expect(
        Array.from(tags ?? []).some((t) => t.textContent?.includes("dwd.fee_bill_di"))
      ).toBeTruthy();
    });
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
    await goToStep(2);
    // 原子指标 Step2 度量列 Select：直接输入未采集列名
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
    // Step1 指标基本信息：粒度作为逻辑概念在顶部可见（原子只读「日 (day)」），
    // 其余治理字段（单位/币种/时间语义/新鲜度/数仓层）隐藏；
    // 聚合方式保留（原子核心算法属性）；计算表达式为派生/复合专属，原子下不出现
    await goToStep(1);
    expect(screen.getByText("粒度")).toBeTruthy();
    expect(screen.getByText("日 (day)")).toBeTruthy();
    expect(screen.queryByText("单位")).toBeNull();
    expect(screen.queryByText("币种（选填）")).toBeNull();
    expect(screen.queryByText("时间语义")).toBeNull();
    expect(screen.queryByText("新鲜度")).toBeNull();
    expect(screen.queryByText("数仓层")).toBeNull();
    expect(screen.getByText("聚合")).toBeTruthy();
    expect(screen.queryByText("计算表达式")).toBeNull();
    // 展开「高级治理设置」：分级/可加性/服务模式可见
    fireEvent.click(screen.getByText(/高级治理设置/));
    await waitFor(() => expect(screen.getByText("分级")).toBeTruthy());
    expect(screen.getByText("可加性")).toBeTruthy();
    expect(screen.getByText("服务模式")).toBeTruthy();
    // Step2 具体实现：原子来源配置区（逻辑度量 + 兼容旧式源表/度量列）展示；
    // 依赖指标为派生/复合专属，原子下不出现
    await goToStep(2);
    expect(screen.getByText("④ 原子来源（逻辑度量 + 基础统计粒度）")).toBeTruthy();
    expect(screen.getByText("逻辑度量（原子指标口径）")).toBeTruthy();
    expect(screen.getByText("源表名（兼容旧式来源，可选）")).toBeTruthy();
    expect(screen.getByText("度量列（兼容旧式来源，可选）")).toBeTruthy();
    expect(screen.queryByText("统计周期（兼容旧式推断，可选）")).toBeNull();
    expect(screen.queryByText("④ 依赖指标")).toBeNull();
  });

  it("切换到派生指标：展示依赖指标（选填）与计算表达式，隐藏原子来源", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await goToStep(1);
    fireEvent.click(screen.getByText("派生指标"));
    await goToStep(2);
    // 依赖指标配置区（Step2 具体实现）出现，原子专属配置隐藏
    expect(screen.getByText("④ 依赖指标（派生选填）")).toBeTruthy();
    expect(screen.queryByText("④ 原子来源（逻辑度量 + 基础统计粒度）")).toBeNull();
    expect(screen.queryByText("源表名（兼容旧式来源，可选）")).toBeNull();
    expect(screen.queryByText("度量列（兼容旧式来源，可选）")).toBeNull();
    // 计算表达式输入在 Step2（⑤ 口径定义）——受控组件（Form.Item 无 name），label 无 htmlFor，须按文本查询
    await waitFor(() => expect(screen.getByText("计算表达式")).toBeTruthy());
  });

  it("Step1 选指标类型后粒度区即时联动（类型前置修复：原子只读日粒度，派生/复合可编辑）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await goToStep(1);
    // 默认原子：粒度区为只读「日 (day)」（Form.Item 内无 Select）
    let granItem = screen.getByText("粒度").closest(".ant-form-item") as HTMLElement;
    expect(granItem.querySelector(".ant-select")).toBeNull();
    expect(screen.getByText("日 (day)")).toBeTruthy();
    // 切派生：粒度区变可编辑 Select（无需再跑到 Step2 才改类型）；label 语义升级为「主粒度（兜底）」
    fireEvent.click(screen.getByText("派生指标"));
    await waitFor(() => {
      granItem = screen.getByText("主粒度（兜底）").closest(".ant-form-item") as HTMLElement;
      expect(granItem.querySelector(".ant-select")).toBeTruthy();
    });
    // 方案 A 指引：extra 明确「粒度维度（业务实体，如 医院/科室）在下一步挂载实体行逐变体配置」——用户不再误以为粒度只能单选
    expect(screen.getByText(/粒度维度（业务实体，如 医院\/科室）在下一步「挂载实体」行逐变体配置/)).toBeTruthy();
    // 类型提示同步为派生语义
    expect(screen.getByText(/原子指标 \+ 业务限定 \+ 时间周期/)).toBeTruthy();
    // 切回原子：粒度恢复只读
    fireEvent.click(screen.getByText("原子指标"));
    await waitFor(() => {
      granItem = screen.getByText("粒度").closest(".ant-form-item") as HTMLElement;
      expect(granItem.querySelector(".ant-select")).toBeNull();
    });
    expect(screen.getByText("日 (day)")).toBeTruthy();
  });

  it("逻辑概念：关联维度在 Step1 ② 展示——expression 与 SQL 两种口径模式均可见", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await goToStep(1);
    // 关联维度（受控 Select）随逻辑概念展示在 ② 指标基本信息
    expect(screen.getByText("关联维度（可选）")).toBeTruthy();
    expect(screen.getByText("选择平台维度或输入维度编码（可搜索）")).toBeTruthy();
    // 切到 SQL 口径定义模式（Step2 ④ 卡）后，Step1 ② 的关联维度仍可见（不再受口径模式门控）
    await goToStep(2);
    fireEvent.click(screen.getByText("SQL 模式"));
    await goToStep(1);
    expect(screen.getByText("关联维度（可选）")).toBeTruthy();
    expect(screen.getByText("选择平台维度或输入维度编码（可搜索）")).toBeTruthy();
  });

  it("关联维度下拉仅加载已发布维度（status=PUBLISHED 防回归：此前误传 active 选项框恒空，后改为不带 status 展示全部；业务规则要求可关联维度必须已发布）", async () => {
    const mockedDims = vi.mocked(listDimensions);
    mockedDims.mockResolvedValue({
      items: [
        { id: 1, dim_code: "dept", name: "科室", description: "", domain: "sales", owner_id: 1, status: "PUBLISHED", row_version: 1 },
      ],
      total: 1,
      page: 1,
      page_size: 200,
    } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    // 修复核心：listDimensions 必须携带 status="PUBLISHED"（此前误传 "active" → 后端精确匹配恒空；不带 status 又会展示未发布维度）
    await waitFor(() => {
      expect(mockedDims).toHaveBeenCalled();
      const lastParams = mockedDims.mock.calls[mockedDims.mock.calls.length - 1]?.[0];
      expect(lastParams).toBeDefined();
      expect(lastParams!.status).toBe("PUBLISHED");
    });
    // 选项框展示 mock 返回的已发布维度（label = `${name} (${dim_code})`）——展开关联维度下拉后断言选项出现
    await goToStep(1);
    fireEvent.mouseDown(screen.getByText("选择平台维度或输入维度编码（可搜索）"));
    await waitFor(() => expect(screen.getByText("科室 (dept)")).toBeTruthy());
  });

  it("派生指标未选依赖（纯周期派生）提交 → 前端放行（依赖/表达式均可选）", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "sales_gmv_day" } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    fireEvent.click(screen.getByText("派生指标"));
    await goToStep(2);
    // 名称在 Step1（指标基本信息）必填——先填名称再回到 Step2 提交（依赖指标/计算表达式留空）
    await goToStep(1);
    fireEvent.change(screen.getByPlaceholderText(/指标显示名称/), { target: { value: "本月活跃医生数" } });
    // 数仓开发责任方必填（PRD 4.5）：回 Step 1 责任方卡填写后再提交
    await fillDwDeveloper();
    await goToStep(2);
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    // F1：派生 = 原子 + 业务限定 + 时间周期，依赖/公式均可选——未选依赖、未填表达式
    // 均不再前端拦截，直接提交（口径合法性由后端类型化校验兜底）
    await waitFor(() => expect(mockedCreate).toHaveBeenCalled());
  });

  it("未填数仓开发责任方提交 → 前端拦截并提示（PRD 4.5 必填，不调用 createMetric）", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "sales_gmv_day" } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    fireEvent.click(screen.getByText("派生指标"));
    await goToStep(2);
    // 名称必填 → 回 Step1 填名称（不填数仓开发）→ 回 Step2 提交
    await goToStep(1);
    fireEvent.change(screen.getByPlaceholderText(/指标显示名称/), { target: { value: "本月活跃医生数" } });
    await goToStep(2);
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    // 前端 handleSubmit 显式校验拦截（向导分步卸载后 antd 不校验未挂载字段）：
    // 提示 + 跳回 Step1，不调用 createMetric
    await screen.findByText(/请先填写数仓开发责任方/);
    expect(mockedCreate).not.toHaveBeenCalled();
    // 已跳回 Step1（「下一步：具体实现」按钮可见，责任方卡重新渲染）
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "下一步：具体实现" })).toBeTruthy(),
    );
  });

  it("派生指标已选依赖但缺计算表达式提交 → 前端放行（仅复合必填表达式）", async () => {
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
    await goToStep(2);
    // 在依赖指标多选输入已发布指标编码，回车后从下拉选中
    const depInput = document.querySelector(
      ".ant-select-multiple .ant-select-selection-search-input"
    ) as HTMLInputElement;
    fireEvent.change(depInput, { target: { value: "sales_gmv_amount_daily" } });
    await waitFor(() => expect(mockedMetrics).toHaveBeenCalled());
    await clickSelectOption("每日 GMV (sales_gmv_amount_daily)");
    // 名称在 Step1（指标基本信息）必填——先填名称再回到 Step2 提交（计算表达式留空）
    await goToStep(1);
    fireEvent.change(screen.getByPlaceholderText(/指标显示名称/), { target: { value: "客单价" } });
    // 数仓开发责任方必填（PRD 4.5）：回 Step 1 责任方卡填写后再提交
    await fillDwDeveloper();
    await goToStep(2);
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    // F1：仅复合必填表达式——派生带依赖但缺表达式同样放行提交
    await waitFor(() => expect(mockedCreate).toHaveBeenCalled());
  });

  it("R5：派生类型下计算表达式无必填红标（仅复合必填表达式）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await goToStep(1);
    fireEvent.click(screen.getByText("派生指标"));
    await goToStep(2);
    await waitFor(() => expect(screen.getByText("计算表达式")).toBeTruthy());
    // R5：派生 = 原子 + 业务限定 + 时间周期，计算表达式非必填——Form.Item 无 required 红标
    const item = screen.getByText("计算表达式").closest(".ant-form-item") as HTMLElement;
    expect(item.className).not.toContain("ant-form-item-required");
  });

  it("派生指标显示基础原子选择器并提交 base_atomic（OneData 基础原子绑定）", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "sales_gmv_day" } as any);
    // 基础原子搜索只返回已发布原子指标
    mockedMetrics.mockResolvedValue({
      items: [
        { metric_code: "active_doctor_daily", name: "日活跃医生数", type: "atomic", status: "PUBLISHED" },
      ],
    } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    fireEvent.click(screen.getByText("派生指标"));
    await goToStep(2);
    // OneData：派生 = 基础原子 + 业务限定 + 时间周期——基础原子选择器出现（选填）
    expect(screen.getByText("基础原子指标")).toBeTruthy();
    // 在基础原子单选 Select 内输入搜索 → 从下拉选「日活跃医生数」
    const baseItem = screen.getByText("基础原子指标").closest(".ant-form-item") as HTMLElement;
    const baseInput = baseItem.querySelector(
      ".ant-select-selection-search-input",
    ) as HTMLInputElement;
    fireEvent.mouseDown(baseItem.querySelector(".ant-select-selector") as HTMLElement);
    fireEvent.change(baseInput, { target: { value: "active" } });
    await waitFor(() => expect(mockedMetrics).toHaveBeenCalled());
    await clickSelectOption("日活跃医生数 (active_doctor_daily)");
    // 名称在 Step1（指标基本信息）必填 → 回到 Step2 提交
    await goToStep(1);
    fireEvent.change(screen.getByPlaceholderText(/指标显示名称/), {
      target: { value: "本月医院活跃医生数" },
    });
    // 数仓开发责任方必填（PRD 4.5）：责任方卡填写后再提交
    await fillDwDeveloper();
    await goToStep(2);
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    await waitFor(() => expect(mockedCreate).toHaveBeenCalled());
    const body = mockedCreate.mock.calls[0][0];
    // base_atomic 合入 definition_json（血缘注册据此生成「原子→派生」BASED_ON 基础边）
    expect(body.definition_json.base_atomic).toBe("active_doctor_daily");
  });

  it("基础原子指标下拉框默认预置已发布原子指标（无需输入即可点选，修复空值）", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "sales_gmv_day" } as any);
    // 挂载时预加载应取回已发布原子指标
    mockedMetrics.mockResolvedValue({
      items: [
        { metric_code: "active_doctor_daily", name: "日活跃医生数", type: "atomic", status: "PUBLISHED" },
      ],
    } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    fireEvent.click(screen.getByText("派生指标"));
    await goToStep(2);
    expect(screen.getByText("基础原子指标")).toBeTruthy();
    // 打开下拉框但不输入关键词——应直接看到挂载时预置的原子指标（修复此前空值必须手打搜索）
    const baseItem = screen.getByText("基础原子指标").closest(".ant-form-item") as HTMLElement;
    fireEvent.mouseDown(baseItem.querySelector(".ant-select-selector") as HTMLElement);
    await waitFor(() =>
      expect(screen.getByText("日活跃医生数 (active_doctor_daily)")).toBeTruthy(),
    );
    // 服务端类型过滤：基础原子预加载请求必带 metric_type=atomic（替代前端页内 filter，
    // 混合类型不再占满单页导致原子指标漏项）
    await waitFor(() =>
      expect(mockedMetrics).toHaveBeenCalledWith(
        expect.objectContaining({ status: "PUBLISHED", metric_type: "atomic" }),
      ),
    );
  });

  it("挂载实体：选源表后度量列下拉自动带出该表列（修复选表后列框为空/残留）", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "sales_gmv_day" } as any);
    mockedCatalogs.mockResolvedValue({
      items: [
        makeCatalog("dwd.sales_detail", [
          { name: "gmv", type: "decimal" },
          { name: "order_cnt", type: "bigint" },
        ]),
      ],
      total: 1,
      page: 1,
      page_size: 20,
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    fireEvent.click(screen.getByText("派生指标"));
    await goToStep(2);
    // 挂载实体区（Step④ 派生分支）：源表 Select 占位符文本唯一定位
    const mountTableSel = screen
      .getByText("源表（如 dwd.sales_detail）")
      .closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(mountTableSel.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("dwd.sales_detail");
    // 选表后挂载度量列下拉应展示该表列（此前 mount_source_table 无 onChange，列框为空/残留别的表列）
    const mountColSel = screen
      .getByText("度量列（可直接输入列名）")
      .closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(mountColSel.querySelector(".ant-select-selector") as HTMLElement);
    await waitFor(() => expect(screen.getByText("gmv (decimal)")).toBeTruthy());
    expect(screen.getByText("order_cnt (bigint)")).toBeTruthy();
  });

  it("多变体挂载：添加变体行可录入多套粒度/限定/周期并随创建提交 mounts 数组", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "sales_gmv_day" } as any);
    // 主粒度下拉需要时间粒度字典（beforeEach 默认空数组）——提供日/月 + 业务实体医生
    mockedDict.mockImplementation((async (dictType: string) => {
      if (dictType === "granularity") {
        return [
          { code: "day", label: "日" },
          { code: "month", label: "月" },
          { code: "doctor", label: "医生" },
          { code: "hospital", label: "医院" },
        ] as never;
      }
      return [] as never;
    }) as never);
    // 变体级责任方（方案 B）：第一行产品需求方选平台用户
    mockedUsers.mockResolvedValue([
      { id: 101, username: "zhangsan", display_name: "张三", role: "user", domain: "sales", status: "active" },
    ] as never);
    mockedCatalogs.mockResolvedValue({
      items: [
        makeCatalog("dwd.sales_detail", [
          { name: "gmv", type: "decimal" },
          { name: "order_cnt", type: "bigint" },
        ]),
        makeCatalog("dwd.hospital_fee", [{ name: "fee", type: "decimal" }]),
      ],
      total: 2,
      page: 1,
      page_size: 20,
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    fireEvent.click(screen.getByText("派生指标"));
    await goToStep(2);
    // 第一行变体：选源表 + 度量列 + 粒度（Select 手输注入） + 业务限定 + 产品需求方
    const mountTableSel = screen
      .getByText("源表（如 dwd.sales_detail）")
      .closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(mountTableSel.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("dwd.sales_detail");
    const mountColSel = screen
      .getByText("度量列（可直接输入列名）")
      .closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(mountColSel.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("gmv (decimal)");
    // 主粒度 Select：时间粒度点选「日 (day)」
    const grainSel1 = screen
      .getByText("主粒度（如 月）")
      .closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(grainSel1.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("日 (day)");
    // 粒度维度（方案 B）：业务实体「医生」点选——进粒度维度而非主粒度
    const dimSel1 = screen
      .getByText("粒度维度（如 医院，可多选）")
      .closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(dimSel1.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("医生 (doctor)");
    fireEvent.change(screen.getByPlaceholderText("业务限定（如 病种=门特）"), {
      target: { value: "场景=门诊" },
    });
    // 第一行产品需求方：选平台用户张三（空=继承指标级；此处显式指定变体级责任方）
    const productOwnerSel = screen
      .getByText("产品需求方（空=继承指标级）")
      .closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(productOwnerSel.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("张三（101）");
    // 添加第二行变体：不同表/粒度
    fireEvent.click(screen.getByRole("button", { name: /添加变体/ }));
    // 第一行已选主粒度「日 (day)」（placeholder 消失），第二行主粒度仍显示 placeholder
    expect(
      document.querySelector('.ant-select-selection-item[title="日 (day)"]'),
    ).toBeTruthy();
    const grainSel2 = screen
      .getByText("主粒度（如 月）")
      .closest(".ant-select") as HTMLElement;
    const tableSels2 = screen.getAllByText("源表（如 dwd.sales_detail）");
    fireEvent.mouseDown(
      (tableSels2[0].closest(".ant-select") as HTMLElement).querySelector(".ant-select-selector") as HTMLElement,
    );
    await clickSelectOption("dwd.hospital_fee");
    const colSels2 = screen.getAllByText("度量列（可直接输入列名）");
    fireEvent.mouseDown(
      (colSels2[0].closest(".ant-select") as HTMLElement).querySelector(".ant-select-selector") as HTMLElement,
    );
    await clickSelectOption("fee (decimal)");
    fireEvent.mouseDown(grainSel2.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("日 (day)");
    // 第二行粒度维度：点选「医院 (hospital)」
    const dimSels2 = screen.getAllByText("粒度维度（如 医院，可多选）");
    const dimSel2 = (dimSels2[0].closest(".ant-select") as HTMLElement);
    fireEvent.mouseDown(dimSel2.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("医院 (hospital)");
    // 名称必填 → 回 Step1 填名称 → 提交
    await goToStep(1);
    fireEvent.change(screen.getByPlaceholderText(/指标显示名称/), {
      target: { value: "费用金额多变体" },
    });
    // 数仓开发责任方必填（PRD 4.5）：Step1 责任方卡填写后再回 Step2 提交
    await fillDwDeveloper();
    await goToStep(2);
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    await waitFor(() => expect(mockedCreate).toHaveBeenCalled());
    const body = mockedCreate.mock.calls[0][0] as {
      mounts?: Array<Record<string, unknown>>;
    };
    expect(body.mounts).toHaveLength(2);
    expect(body.mounts![0]).toMatchObject({
      source_table: "dwd.sales_detail",
      granularity: "day",
      // 组合粒度（方案 B）：业务实体「医生」进粒度维度而非主粒度（值=字典 code）
      granularity_dims: ["doctor"],
      business_filter: "场景=门诊",
      // 变体级责任方：第一行产品需求方张三（方案 B）
      product_owner_id: 101,
      product_owner_name: null,
    });
    expect(body.mounts![1]).toMatchObject({
      source_table: "dwd.hospital_fee",
      granularity: "day",
      granularity_dims: ["hospital"],
      // 第二行未设责任方 → 空（缺省继承指标级）
      product_owner_id: null,
    });
  });

  it("挂载粒度 Select 化：主粒度展示时间粒度、粒度维度展示业务实体（方案 B 拆分）", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "sales_gmv_day" } as any);
    // 按字典类型返回：granularity 返回内置粒度（日/月/医生），其余沿用默认币种
    mockedDict.mockImplementation((async (dictType: string) => {
      if (dictType === "granularity") {
        return [
          { code: "day", label: "日" },
          { code: "month", label: "月" },
          { code: "doctor", label: "医生" },
        ] as never;
      }
      return [{ code: "CNY", label: "元" }] as never;
    }) as never);
    mockedCatalogs.mockResolvedValue({
      items: [makeCatalog("dwd.sales_detail", [{ name: "gmv", type: "decimal" }])],
      total: 1,
      page: 1,
      page_size: 20,
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    fireEvent.click(screen.getByText("派生指标"));
    await goToStep(2);
    // 主粒度下拉打开：时间粒度（日/月）直接可见可点选（业务实体医生不在主粒度）
    const grainSel = screen
      .getByText("主粒度（如 月）")
      .closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(grainSel.querySelector(".ant-select-selector") as HTMLElement);
    await waitFor(() => expect(screen.getByText("日 (day)")).toBeTruthy());
    expect(screen.getByText("月 (month)")).toBeTruthy();
    expect(screen.queryByText("医生 (doctor)")).toBeNull();
    await clickSelectOption("日 (day)");
    // 选中后行内展示所选主粒度（selector 选中值）
    expect(
      document.querySelector('.ant-select-selection-item[title="日 (day)"]'),
    ).toBeTruthy();
    // 粒度维度下拉打开：业务实体粒度（医生）直接可见可点选
    const dimSel = screen
      .getByText("粒度维度（如 医院，可多选）")
      .closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(dimSel.querySelector(".ant-select-selector") as HTMLElement);
    await waitFor(() => expect(screen.getByText("医生 (doctor)")).toBeTruthy());
    await clickSelectOption("医生 (doctor)");
    expect(
      document.querySelector('.ant-select-selection-item[title="医生 (doctor)"]'),
    ).toBeTruthy();
  });

  it("原子指标未选逻辑度量且未填口径提交 → 前端拦截并提示来源必填", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "sales_gmv_day" } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    // 名称在 Step1（指标基本信息）必填——先填名称再回到 Step2 提交（默认 atomic：未选逻辑度量/源表度量列、口径为空）
    await goToStep(1);
    fireEvent.change(screen.getByPlaceholderText(/指标显示名称/), { target: { value: "GMV" } });
    await goToStep(2);
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    await waitFor(() =>
      expect(screen.getByText("原子指标请选择逻辑度量（推荐）或源表与度量列，或填写口径定义")).toBeTruthy()
    );
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it("推断徽标：选域自动回填后字段显示来源徽标（程序回填不误清）", async () => {
    mockedSuggest.mockResolvedValue({
      metric_code_suggestion: "sales_gmv_day",
      segments: { domain: "sales", biz_object: "order", measure: "gmv", period: "day" },
      fields: {
        name: { value: "订单销售额", source: "sql_parse", confidence: 0.8, reason: "SQL 解析名称" },
        additivity: { value: "SEMI_ADDITIVE", source: "rule", confidence: 0.6, reason: "按域默认规则" },
      },
      definition_json: null,
      definition_mode: null,
    } as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    // 名称徽标出现（来源=SQL解析，置信度 80%）
    await waitFor(() => expect(screen.getByText(/SQL解析 · 80%/)).toBeTruthy());
    // 展开高级治理设置：可加性徽标（规则 · 60%）也出现
    fireEvent.click(screen.getByText(/高级治理设置/));
    await waitFor(() => expect(screen.getByText(/规则 · 60%/)).toBeTruthy());
    // 未做任何手动修改：两个徽标均保留（程序回填不清徽标——防回归）
    expect(screen.getByText(/SQL解析 · 80%/)).toBeTruthy();
    expect(screen.getByText(/规则 · 60%/)).toBeTruthy();
  });

  it("推断徽标：手动修改被推断字段的值 → 仅该字段徽标清除（值被覆盖，徽标不再准确）", async () => {
    mockedSuggest.mockResolvedValue({
      metric_code_suggestion: "sales_gmv_day",
      segments: { domain: "sales", biz_object: "order", measure: "gmv", period: "day" },
      fields: {
        name: { value: "订单销售额", source: "sql_parse", confidence: 0.8, reason: "SQL 解析名称" },
        additivity: { value: "SEMI_ADDITIVE", source: "rule", confidence: 0.6, reason: "按域默认规则" },
      },
      definition_json: null,
      definition_mode: null,
    } as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    await waitFor(() => expect(screen.getByText(/SQL解析 · 80%/)).toBeTruthy());
    fireEvent.click(screen.getByText(/高级治理设置/));
    await waitFor(() => expect(screen.getByText(/规则 · 60%/)).toBeTruthy());
    // 手动修改名称（改成与推断值不同的值）
    fireEvent.change(screen.getByPlaceholderText(/指标显示名称/), { target: { value: "自定义销售名称" } });
    // 名称徽标消失（值已被用户覆盖）
    await waitFor(() => expect(screen.queryByText(/SQL解析 · 80%/)).toBeNull());
    // 未被修改的可加性徽标保留
    expect(screen.getByText(/规则 · 60%/)).toBeTruthy();
  });

  it("域默认预填指标类型=派生 → 类型与粒度区同步联动（setFieldsValue 不触发 onValuesChange 的状态同步缺陷）", async () => {
    // 域默认值配置 type=derived（后端 /domains/{code}/defaults 返回自由 JSON 的 defaults_json）
    mockedDomainDefaults.mockResolvedValue({ type: "derived" } as never);
    mockedDict.mockImplementation((async (dictType: string) => {
      if (dictType === "granularity") {
        return [
          { code: "day", label: "日" },
          { code: "month", label: "月" },
        ] as never;
      }
      return [] as never;
    }) as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    // 域默认已把表单 type 预填为 derived —— Segmented 应显示派生指标选中态
    await waitFor(() =>
      expect(document.querySelector(".ant-segmented-item-selected")?.textContent).toContain("派生指标")
    );
    // 关键：类型联动粒度区 —— 派生指标应渲染「主粒度（兜底）」可编辑 Select，
    // 而非原子分支的只读文本「日 (day)」。此前 setFieldsValue 不触发 onValuesChange，
    // metricType state 停留在 atomic，导致「显示派生指标 + 只读日粒度」的状态撕裂。
    await waitFor(() => {
      const granItem = screen
        .getByText("主粒度（兜底）")
        .closest(".ant-form-item") as HTMLElement;
      expect(granItem.querySelector(".ant-select")).toBeTruthy();
    });
    // 原子分支的只读粒度（label「粒度」+ 无 Select）已消失——不再与「派生指标」选中态撕裂
    expect(screen.queryByText("粒度", { selector: ".ant-form-item-label label" })).toBeNull();
  });

  it("向导步骤不能随意点击跳转——必须按顺序逐步完成（未到达的步骤点击被拦截）", async () => {
    renderPage();
    await screen.findByText("① 选择业务域");
    const stepItems = () => document.querySelectorAll(".ant-steps-item");
    // 点击第 N 步：antd 将点击绑定在 .ant-steps-item-container（role=button）上
    const clickStep = (idx: number) => {
      const el = stepItems()[idx]?.querySelector(".ant-steps-item-container");
      if (el) fireEvent.click(el);
    };
    // 初始在 Step 0（业务域），直接点击第 3 步「具体实现」→ 被拦截，仍停留 Step 0
    clickStep(2);
    await waitFor(() => {
      expect(screen.getByText("请按顺序完成当前步骤，再进入下一步")).toBeTruthy();
    });
    expect(screen.getByText("① 选择业务域")).toBeTruthy();
    expect(screen.queryByText("⑥ 关联数据表")).toBeNull();
    // 点「下一步」进入 Step 1（指标基本信息）
    fireEvent.click(screen.getByRole("button", { name: "下一步：指标基本信息" }));
    await waitFor(() => {
      expect(screen.getByText("② 指标基本信息")).toBeTruthy();
    });
    // 再直接点第 3 步 → 仍被拦截（未到达过，不能向前跳）
    clickStep(2);
    await waitFor(() => {
      expect(screen.queryByText("⑥ 关联数据表")).toBeNull();
    });
    expect(screen.getByText("② 指标基本信息")).toBeTruthy();
    // 点「下一步」进入 Step 2（具体实现）——按顺序到达
    fireEvent.click(screen.getByRole("button", { name: "下一步：具体实现" }));
    await waitFor(() => {
      expect(screen.getByText("⑥ 关联数据表")).toBeTruthy();
    });
    // 已到达过 Step 2，可点击 Steps 回看 Step 0（业务域）——回看已到达步骤不受限
    clickStep(0);
    await waitFor(() => {
      expect(screen.getByText("① 选择业务域")).toBeTruthy();
    });
  });
});

describe("MetricCreate 三层口径与分角色双字段（业务/伪代码/数仓SQL口径）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedTree.mockResolvedValue(TREE);
    mockedDict.mockResolvedValue([{ code: "CNY", label: "元" }] as never);
    mockedSuggest.mockResolvedValue(NO_SUGGESTION);
    mockedCatalogs.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
    mockedMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
    // 逻辑度量目录返回一个已发布度量（原子指标选它继承格式/单位）
    (listMeasureCatalogs as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [
        {
          id: 1,
          measure_code: "medical_fee_amt",
          name: "门诊收费金额",
          measure_format: "AMOUNT",
          default_unit: "CNY",
          default_decimal_places: 2,
          source_system: ["HIS"],
          domain: "medical_fee",
          status: "PUBLISHED",
        },
      ],
      total: 1,
      page: 1,
      page_size: 200,
    });
  });

  it("Step④ 展示「业务口径」与「伪代码口径（系统开发）」「数仓SQL口径」输入区", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await goToStep(2);

    const biz = screen.getByRole("textbox", { name: "业务口径" }) as HTMLTextAreaElement;
    const pseudo = screen.getByRole("textbox", { name: "伪代码口径" }) as HTMLTextAreaElement;
    const dw = screen.getByRole("textbox", { name: "数仓SQL口径" }) as HTMLTextAreaElement;
    fireEvent.change(biz, { target: { value: "按就诊号去重统计的就诊次数" } });
    fireEvent.change(pseudo, { target: { value: "SUM(收费金额) 按结算日期去重" } });
    fireEvent.change(dw, { target: { value: "SELECT visit_date, SUM(real_amount) FROM dwd.fee_bill_di" } });
    expect(biz.value).toBe("按就诊号去重统计的就诊次数");
    expect(pseudo.value).toBe("SUM(收费金额) 按结算日期去重");
    expect(dw.value).toBe("SELECT visit_date, SUM(real_amount) FROM dwd.fee_bill_di");
    // 分角色引导文案存在（通俗提示）
    expect(screen.getByText(/口径分角色填写/)).toBeInTheDocument();
  });

  it("提交时 definition/pseudo_definition/dw_definition 三层口径合入 definition_json", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "medical_fee_amt_daily" } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(2);
    // 选逻辑度量（原子指标 OneData 继承源）+ 填三层口径（Step2 ⑤ 口径定义）
    fireEvent.mouseDown(screen.getByText("选择或搜索逻辑度量（如 支付金额 pay_amt）"));
    await clickSelectOption("门诊收费金额 (medical_fee_amt)");
    fireEvent.change(screen.getByRole("textbox", { name: "业务口径" }), {
      target: { value: "按就诊号去重统计的门诊收费金额" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "伪代码口径" }), {
      target: { value: "SUM(收费金额) 按结算日期去重" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "数仓SQL口径" }), {
      target: { value: "SELECT visit_date, SUM(real_amount) AS amt FROM dwd.fee_bill_di" },
    });
    // 名称在 Step1（② 指标基本信息）必填——填名称后回 Step2 提交
    await goToStep(1);
    fireEvent.change(screen.getByPlaceholderText(/指标显示名称/), { target: { value: "门诊收费金额" } });
    // 数仓开发责任方必填（PRD 4.5）：Step1 责任方卡填写后再回 Step2 提交
    await fillDwDeveloper();
    await goToStep(2);
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));

    await waitFor(() => {
      expect(mockedCreate).toHaveBeenCalled();
      const body = mockedCreate.mock.calls[0][0] as {
        definition_json: { definition?: string; pseudo_definition?: string; dw_definition?: string };
      };
      expect(body.definition_json.definition).toBe("按就诊号去重统计的门诊收费金额");
      expect(body.definition_json.pseudo_definition).toBe("SUM(收费金额) 按结算日期去重");
      expect(body.definition_json.dw_definition).toBe(
        "SELECT visit_date, SUM(real_amount) AS amt FROM dwd.fee_bill_di",
      );
    });
  });

  it("消费指南区块：填写后创建草稿透传 consumption_guide（guide_source=manual 语义）", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "medical_fee_amt_daily" } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(2);
    fireEvent.mouseDown(screen.getByText("选择或搜索逻辑度量（如 支付金额 pay_amt）"));
    await clickSelectOption("门诊收费金额 (medical_fee_amt)");
    await goToStep(1);
    // 展开「消费指南（选填）」Collapse
    fireEvent.click(screen.getByText(/消费指南（选填）/));
    // 推荐用法组添加一项并填写
    fireEvent.click(screen.getAllByRole("button", { name: /添加一项/ })[0]);
    const usageInput = screen.getByPlaceholderText(/适用 sales 域 daily 粒度分析/);
    fireEvent.change(usageInput, { target: { value: "适用门诊域按日分析" } });
    // 注意事项组添加一项并填写
    fireEvent.click(screen.getAllByRole("button", { name: /添加一项/ })[1]);
    const cautionInput = screen.getByPlaceholderText(/该指标包含 PII 数据/);
    fireEvent.change(cautionInput, { target: { value: "含敏感就诊信息" } });
    // 数仓开发责任方必填（PRD 4.5）：Step1 责任方卡填写后再回 Step2 提交
    await fillDwDeveloper();
    await goToStep(2);
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));

    await waitFor(() => {
      expect(mockedCreate).toHaveBeenCalled();
      const body = mockedCreate.mock.calls[0][0] as {
        consumption_guide?: { recommended_usage: string[]; cautions: string[]; related_metrics: string[] };
      };
      expect(body.consumption_guide?.recommended_usage).toEqual(["适用门诊域按日分析"]);
      expect(body.consumption_guide?.cautions).toEqual(["含敏感就诊信息"]);
      expect(body.consumption_guide?.related_metrics).toEqual([]);
    });
  });

  it("业务描述与关联术语：填写后创建草稿透传 description/term_id", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "medical_fee_amt_daily" } as any);
    mockedListTerms.mockResolvedValue({
      items: [{ id: 5, name: "门诊收费", term_code: "term_outpatient" }],
    } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(2);
    fireEvent.mouseDown(screen.getByText("选择或搜索逻辑度量（如 支付金额 pay_amt）"));
    await clickSelectOption("门诊收费金额 (medical_fee_amt)");
    await goToStep(1);
    // 展开「业务描述与关联术语（选填）」Collapse
    fireEvent.click(screen.getByText(/业务描述与关联术语（选填）/));
    const desc = screen.getByPlaceholderText(/指标的业务含义、口径背景、适用场景/);
    fireEvent.change(desc, { target: { value: "门诊收费总金额指标（含退费扣除）" } });
    // 搜索并选择关联术语（防抖 onSearch → listTerms；Select 用 testid 定位内部 input）
    const termInput = (screen.getByTestId("termSelect") as HTMLElement).querySelector(
      "input",
    ) as HTMLElement;
    fireEvent.mouseDown(termInput);
    fireEvent.change(termInput, { target: { value: "门诊" } });
    await screen.findByText("门诊收费（term_outpatient）");
    fireEvent.click(screen.getByText("门诊收费（term_outpatient）"));
    // 数仓开发责任方必填（PRD 4.5）：Step1 责任方卡填写后再回 Step2 提交
    await fillDwDeveloper();
    await goToStep(2);
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));

    await waitFor(() => {
      expect(mockedCreate).toHaveBeenCalled();
      const body = mockedCreate.mock.calls[0][0] as { description?: string; term_id?: number };
      expect(body.description).toBe("门诊收费总金额指标（含退费扣除）");
      expect(body.term_id).toBe(5);
    });
  });

  it("关联术语：首次打开下拉即加载已发布术语（无需先输入搜索）", async () => {
    mockedCreate.mockResolvedValue({ metric_code: "medical_fee_amt_daily" } as any);
    mockedListTerms.mockResolvedValue({
      items: [
        { id: 5, name: "门诊收费", term_code: "term_outpatient", domain: "outpatient" },
        { id: 6, name: "住院收费", term_code: "term_inpatient", domain: "outpatient" },
      ],
    } as any);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await goToStep(1);
    fireEvent.click(screen.getByText(/业务描述与关联术语（选填）/));
    const termInput = (screen.getByTestId("termSelect") as HTMLElement).querySelector(
      "input",
    ) as HTMLElement;
    // 打开下拉 → onDropdownVisibleChange 触发空搜索加载（带域过滤），无需输入即有选项
    fireEvent.mouseDown(termInput);
    await waitFor(() => expect(mockedListTerms).toHaveBeenCalled());
    await screen.findByText("门诊收费（term_outpatient）");
    await screen.findByText("住院收费（term_inpatient）");
  });

  it("Step④ 三层口径 AI 生成/丰富增强：业务口径有值点「AI 丰富增强」回填", async () => {
    mockedRefine.mockResolvedValue({
      content: "按就诊号去重统计的门诊就诊总人次（含跨院区）",
      source: "llm",
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await goToStep(2);
    const biz = screen.getByRole("textbox", { name: "业务口径" }) as HTMLTextAreaElement;
    fireEvent.change(biz, { target: { value: "门诊就诊次数" } });
    // 业务口径有值 → 按钮显示「AI 丰富增强」（限定在业务口径 Form.Item 内，避免命中同名按钮）
    const bizItem = biz.closest(".ant-form-item") as HTMLElement;
    const enrichBtn = within(bizItem).getByRole("button", { name: /AI 丰富增强/ });
    expect(enrichBtn).toBeTruthy();
    fireEvent.click(enrichBtn);
    await waitFor(() => {
      const bizArea = screen.getByRole("textbox", { name: "业务口径" }) as HTMLTextAreaElement;
      expect(bizArea.value).toBe("按就诊号去重统计的门诊就诊总人次（含跨院区）");
    });
    // 请求载荷：field=business + action=enrich + 当前值
    expect(mockedRefine).toHaveBeenCalledWith(
      expect.objectContaining({
        field: "business",
        action: "enrich",
        current: "门诊就诊次数",
      }),
    );
  });
});

describe("MetricCreate SQL 批量解析（FR-010 批量注册增强）", () => {
  /** 批量解析结果：单语句 2 原子 + 1 复合（合成复合开关开启时后端返回）。 */
  const SQL_BATCH_RESULT: SqlBatchParseResult = {
    statements: [
      {
        index: 0,
        sql: "SELECT dt, SUM(amount) AS gmv, COUNT(DISTINCT user_id) AS uv FROM dwd.sales_detail GROUP BY dt",
        source_tables: ["dwd.sales_detail"],
        measure_count: 2,
        group_by: ["dt"],
      },
    ],
    candidates: [
      {
        key: "0:amount",
        metric_code: "sales_order_amount_day",
        name: "日订单金额",
        // 方案 A：SQL 推断候选一律派生（原子只从逻辑度量目录创建）
        type: "derived",
        source_table: "dwd.sales_detail",
        measure_column: "amount",
        aggregation: "SUM",
        period: "day",
        unit: "CNY",
        granularity: "day",
        definition_json: { expression: "SUM(amount)" },
        definition_mode: "expression",
        statement_index: 0,
      },
      {
        key: "0:user_id",
        metric_code: "sales_order_userid_day",
        name: "日去重用户",
        type: "derived",
        source_table: "dwd.sales_detail",
        measure_column: "user_id",
        aggregation: "COUNT_DISTINCT",
        period: "day",
        unit: "PERSON",
        granularity: "day",
        definition_json: { expression: "COUNT(DISTINCT user_id)" },
        definition_mode: "expression",
        granularity_dims: ["hosp_code", "enter_source"],
        statement_index: 0,
      },
      {
        key: "0:composite",
        metric_code: "sales_order_amountuserid_day",
        name: "日订单金额、日去重用户复合",
        type: "composite",
        source_table: "dwd.sales_detail",
        measure_column: null,
        aggregation: null,
        period: "day",
        unit: null,
        granularity: "day",
        definition_json: {
          sql: "SELECT dt, SUM(amount), COUNT(DISTINCT user_id) FROM dwd.sales_detail GROUP BY dt",
          dependencies: ["sales_order_amount_day", "sales_order_userid_day"],
        },
        definition_mode: "sql",
        dependencies: ["sales_order_amount_day", "sales_order_userid_day"],
        statement_index: 0,
      },
    ],
    skipped: [],
    domain: { code: "sales", name: "销售", status: "user", confidence: null, candidates: [], matched_tables: [] },
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockedTree.mockResolvedValue(TREE);
    mockedDict.mockResolvedValue([]);
    mockedSuggest.mockResolvedValue(NO_SUGGESTION);
    mockedSuggestDomain.mockResolvedValue(NO_DOMAIN_SUGGESTION);
    mockedParseSqlBatch.mockResolvedValue(SQL_BATCH_RESULT);
    mockedBatchFromSql.mockResolvedValue({
      batch_id: "sqlbatch_test",
      candidates: [
        { metric_code: "sales_order_amount_day", status: "DRAFT", validation_errors: null },
        { metric_code: "sales_order_userid_day", status: "DRAFT", validation_errors: null },
        { metric_code: "sales_order_amountuserid_day", status: "DRAFT", validation_errors: null },
      ],
    });
    mockedCatalogs.mockResolvedValue({
      items: [makeCatalog("dwd.sales_detail")],
      total: 1,
      page: 1,
      page_size: 20,
    });
  });

  /** 打开抽屉并切换到「批量解析」模式。 */
  async function openBatchMode() {
    await openSqlInfer();
    fireEvent.click(screen.getByText("批量解析"));
    fireEvent.change(screen.getByPlaceholderText(/批量解析：粘贴含多个 SELECT/), {
      target: {
        value: "SELECT dt, SUM(amount) AS gmv, COUNT(DISTINCT user_id) AS uv FROM dwd.sales_detail GROUP BY dt",
      },
    });
    fireEvent.click(screen.getByText("解析候选"));
    await screen.findByText(/共 3 个候选/);
  }

  it("批量解析：抽屉可拖宽（ResizableDrawer 手柄）+ 候选卡片化字段标签展示", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openBatchMode();

    // 抽屉可拖宽：左缘拖拽手柄存在（对齐全站详情抽屉 ResizableDrawer 交互）
    expect(document.querySelector(".resizable-drawer .drawer-resize-handle")).toBeTruthy();
    // 候选卡片化：字段以「小标签置顶」形式分组展示（聚合/依赖指标/指标编码），
    // 不再全部挤在单行 flex——标签是卡片布局的标志性结构（方案 A：SQL 候选默认
    // 派生，字段区含物理属性聚合 + 依赖指标，关联逻辑度量为原子专属）
    expect(screen.getAllByText("聚合").length).toBeGreaterThan(0);
    expect(screen.getAllByText("依赖指标（派生可选）").length).toBeGreaterThan(0);
    expect(screen.getAllByText("指标编码").length).toBeGreaterThan(0);
  });

  it("批量解析：候选「粒度维度」预填 GROUP BY 非时间键（唯一性构成，可编辑）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openBatchMode();

    // 候选卡片字段区有「粒度维度」标签（组合粒度：GROUP BY 非时间键全部为粒度维度）
    expect(screen.getAllByText("粒度维度").length).toBeGreaterThan(0);
    // 预填的粒度维度（后端从 GROUP BY 非时间键回填）以多选 Tag 呈现
    await waitFor(() => {
      const select = document.querySelector(
        '[data-testid="sql-batch-granularity-dims-0:user_id"]',
      ) as HTMLElement;
      expect(select).toBeTruthy();
      expect(select.textContent || "").toContain("hosp_code");
      expect(select.textContent || "").toContain("enter_source");
    });
  });

  it("批量解析：粘贴大段 SQL → 解析候选 → 默认勾选原子 + 复合行带发布提示", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openBatchMode();

    // 请求参数：statement 模式 + 合成复合默认开（A/B/C 三轮增强：外层宽表 ETL 的
    // 算术派生列与含运算语句默认产出复合候选，不再静默缺失）
    expect(mockedParseSqlBatch).toHaveBeenCalledWith(
      expect.objectContaining({ split_mode: "statement", synthesize_composite: true })
    );
    // 默认勾选 2 个原子（复合未自动勾选）
    expect(screen.getByText(/已勾选 2 个/)).toBeTruthy();
    // 原子候选（Input 值）+ 复合候选（文本 + 发布提示 Tag）
    expect(screen.getByDisplayValue("日订单金额")).toBeTruthy();
    expect(screen.getByDisplayValue("日去重用户")).toBeTruthy();
    expect(screen.getByDisplayValue("日订单金额、日去重用户复合")).toBeTruthy();
    expect(screen.getByText("需先发布依赖原子")).toBeTruthy();
    // 语句分组标题
    expect(screen.getByText(/语句 1 · dwd.sales_detail · 3 个候选/)).toBeTruthy();
  });

  it("批量编辑向导：打开分步向导批量编辑全部候选（不再逐条跳单条）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openBatchMode();

    // 打开批量编辑向导（全部候选）
    const wizardBtn = screen.getByTestId("sql-batch-open-wizard");
    expect(wizardBtn).toBeTruthy();
    fireEvent.click(wizardBtn);
    // Modal 经 portal 渲染到 body（antd 打开态在 jsdom 可能带 aria-hidden，
    // testing-library 文本查询会排除——用 DOM 级断言更稳）
    await waitFor(() => {
      expect(document.body.querySelector(".ant-modal-title")?.textContent || "").toContain("批量编辑向导");
    });
    const modal = document.querySelector(".ant-modal") as HTMLElement;
    expect(modal).toBeTruthy();
    // 步骤切换后 Modal 内容会被 antd 重挂载，within 引用失效——每次重新定位
    const m = () => within(document.querySelector(".ant-modal") as HTMLElement);
    // Step 0 基本信息：表格含 3 个候选名称（Input）
    expect(m().getByDisplayValue("日订单金额")).toBeTruthy();
    expect(m().getByDisplayValue("日去重用户")).toBeTruthy();
    expect(m().getByDisplayValue("日订单金额、日去重用户复合")).toBeTruthy();
    // 行内批量编辑名称
    fireEvent.change(m().getByDisplayValue("日订单金额"), {
      target: { value: "订单金额（改）" },
    });
    expect(m().getByDisplayValue("订单金额（改）")).toBeTruthy();
    // 跳 Step 1 口径与责任（逻辑度量/依赖/责任方表格）——用「下一步」导航按钮
    fireEvent.click(m().getByTestId("sql-batch-wizard-next"));
    await waitFor(() => {
      expect(document.querySelector('[data-testid="sql-batch-wizard-t1"]')).toBeTruthy();
    });
    const t1 = within(document.querySelector('[data-testid="sql-batch-wizard-t1"]') as HTMLElement);
    expect(t1.getAllByText("逻辑度量").length).toBeGreaterThanOrEqual(1);
    expect(t1.getAllByText("产品负责").length).toBeGreaterThanOrEqual(1);
    // 口径三方责任完整：技术方/数仓开发列与产品负责并列（此前只有产品负责一列）
    expect(t1.getAllByText("技术方").length).toBeGreaterThanOrEqual(1);
    expect(t1.getAllByText("数仓开发").length).toBeGreaterThanOrEqual(1);
    // 跳 Step 2 确认提交：汇总 + 创建按钮
    fireEvent.click(m().getByTestId("sql-batch-wizard-next"));
    await waitFor(() => {
      expect(document.body.textContent).toContain("共 3 个候选");
    });
    expect(m().getByRole("button", { name: /批量创建选中指标/ })).toBeTruthy();
  });

  it("批量编辑向导：步骤③点击「批量创建选中指标」正常触发 batch-register-from-sql（回归：无反应）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();

    // 打开批量编辑向导
    fireEvent.click(screen.getByTestId("sql-batch-open-wizard"));
    await waitFor(() => {
      expect(document.body.querySelector(".ant-modal-title")?.textContent || "").toContain("批量编辑向导");
    });
    const m = () => within(document.querySelector(".ant-modal") as HTMLElement);
    // 跳到 Step 2（下一步 × 2）
    fireEvent.click(m().getByTestId("sql-batch-wizard-next"));
    await waitFor(() => {
      expect(document.querySelector('[data-testid="sql-batch-wizard-t1"]')).toBeTruthy();
    });
    fireEvent.click(m().getByTestId("sql-batch-wizard-next"));
    await waitFor(() => {
      expect(document.body.textContent).toContain("共 3 个候选");
    });
    // 点击批量创建按钮
    const btn = m().getByRole("button", { name: /批量创建选中指标/ });
    expect(btn).toBeTruthy();
    expect((btn as HTMLButtonElement).disabled).toBe(false);
    expect((btn as HTMLButtonElement).textContent).toContain("（2）");
    fireEvent.click(btn);
    await waitFor(
      () => {
        expect(mockedBatchFromSql).toHaveBeenCalled();
      },
      { timeout: 3000 },
    );
  });

  it("勾选联动：取消被复合依赖的原子 → 弹窗；「跳过复合」同时取消原子与复合", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openBatchMode();

    // 勾选复合候选（当前未勾选）
    fireEvent.click(screen.getByRole("checkbox", { name: "勾选 日订单金额、日去重用户复合" }));
    await screen.findByText(/已勾选 3 个/);
    // 取消被复合依赖的原子 → 弹窗（不立即取消）
    fireEvent.click(screen.getByRole("checkbox", { name: "勾选 日订单金额" }));
    await screen.findByText("复合指标依赖该原子");
    // 「跳过复合」：原子 + 复合都被取消，仅剩另一原子
    fireEvent.click(screen.getByRole("button", { name: /跳过复合/ }));
    await waitFor(() => {
      expect(screen.queryByText(/已勾选 1 个/)).toBeTruthy();
    });
    expect(screen.getByRole("checkbox", { name: "勾选 日去重用户" })).toBeTruthy();
  });

  it("勾选联动：弹窗「回滚勾选」保留原子（不取消）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openBatchMode();

    fireEvent.click(screen.getByRole("checkbox", { name: "勾选 日订单金额、日去重用户复合" }));
    await screen.findByText(/已勾选 3 个/);
    fireEvent.click(screen.getByRole("checkbox", { name: "勾选 日订单金额" }));
    await screen.findByText("复合指标依赖该原子");
    fireEvent.click(screen.getByRole("button", { name: /回滚勾选/ }));
    await waitFor(() => {
      expect(screen.queryByText(/已勾选 3 个/)).toBeTruthy();
    });
  });

  it("批量创建：勾选候选 → batch-register-from-sql → 结果分桶 + 复合发布提示", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();

    // 勾选复合后创建 3 个
    fireEvent.click(screen.getByRole("checkbox", { name: "勾选 日订单金额、日去重用户复合" }));
    await screen.findByText(/已勾选 3 个/);
    fireEvent.click(screen.getByText(/批量创建选中指标/));

    await waitFor(() => {
      expect(mockedBatchFromSql).toHaveBeenCalled();
      const body = mockedBatchFromSql.mock.calls[0][0];
      expect(body.domain).toBe("sales");
      expect(body.candidates.length).toBe(3);
      expect(body.candidates.some((c: { type: string }) => c.type === "composite")).toBe(true);
    });
    // 结果分桶展示 + 复合「需先发布依赖原子」提示
    await screen.findByText(/批量创建完成：成功 3 \/ 失败 0/);
    expect(screen.getByText(/含 1 个复合候选/)).toBeTruthy();
    expect(screen.getByText(/需先逐个发布原子/)).toBeTruthy();
  });

  /** 快速编辑抽屉用的最小指标详情（getMetric 返回）。 */
  function makeQuickMetric(code: string): MetricResponse {
    return {
      id: 1,
      metric_code: code,
      name: code === "sales_order_amount_day" ? "日订单金额" : "日去重用户",
      domain: "sales",
      type: "atomic",
      granularity: "day",
      unit: "元",
      currency: "CNY",
      aggregation: "SUM",
      time_semantics: "累计",
      freshness: "T+1",
      sla: null,
      dw_layer: "DWS",
      metric_tier: "T1",
      serving_mode: "api",
      additivity: "ADDITIVE",
      non_additive_dimensions: null,
      definition_json: { expression: "SUM(amount)", source_fields: ["amount"] },
      version: 1,
      row_version: 7,
      status: "DRAFT",
      owner_id: 1,
      backup_owner_id: null,
      approver_id: null,
      submitted_by: null,
      pii_flag: false,
      compliance_reviewed: false,
      term_id: null,
      effective_version: null,
      consumption_guide: null,
      successor_code: null,
      deprecated_at: null,
      sunset_until: null,
      emergency_publish: false,
      emergency_reason: null,
      emergency_reviewed_at: null,
      gray_tenant_ids: null,
      pending_conflict: false,
      pending_conflict_detail: null,
      pending_version: false,
      created_at: "2026-08-27T00:00:00",
      updated_at: "2026-08-27T00:00:00",
    };
  }

  it("批量创建结果：快速编辑抽屉——打开/前后切换/保存走 updateMetric 乐观锁", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();

    fireEvent.click(screen.getByRole("checkbox", { name: "勾选 日订单金额、日去重用户复合" }));
    await screen.findByText(/已勾选 3 个/);
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await screen.findByText(/批量创建完成：成功 3 \/ 失败 0/);

    // 打开第一个候选的快速编辑抽屉：getMetric 拉取当前值回填
    mockedGetMetric.mockResolvedValue(makeQuickMetric("sales_order_amount_day"));
    fireEvent.click(screen.getByTestId("sql-batch-quick-edit-sales_order_amount_day"));
    await waitFor(() => {
      expect((screen.getByTestId("sql-batch-quick-name") as HTMLInputElement).value).toBe(
        "日订单金额",
      );
    });
    expect(mockedGetMetric).toHaveBeenCalledWith("sales_order_amount_day");
    // 位置指示：第 1 / 3 条
    expect(screen.getByText("1 / 3")).toBeTruthy();

    // 下一条切换 → getMetric 拉取下一候选并回填（不影响当前窗口/页面）
    mockedGetMetric.mockResolvedValue(makeQuickMetric("sales_order_userid_day"));
    fireEvent.click(screen.getByTestId("sql-batch-quick-edit-next"));
    await waitFor(() => {
      expect((screen.getByTestId("sql-batch-quick-name") as HTMLInputElement).value).toBe(
        "日去重用户",
      );
    });
    expect(mockedGetMetric).toHaveBeenCalledWith("sales_order_userid_day");

    // 修改名称 + 变更原因（默认已预填），保存 → updateMetric 携带 row_version 乐观锁
    mockedUpdateMetric.mockResolvedValue({
      ...makeQuickMetric("sales_order_userid_day"),
      name: "日去重用户数",
      row_version: 8,
    });
    fireEvent.change(screen.getByTestId("sql-batch-quick-name"), {
      target: { value: "日去重用户数" },
    });
    fireEvent.change(screen.getByTestId("sql-batch-quick-reason"), {
      target: { value: "批量创建后快速编辑" },
    });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockedUpdateMetric).toHaveBeenCalledWith(
        "sales_order_userid_day",
        expect.objectContaining({
          name: "日去重用户数",
          change_reason: "批量创建后快速编辑",
          row_version: 7,
        }),
      );
    });
  });

  it("批量创建：候选改类型为原子后可关联逻辑度量 → 提交透传 measure_id + 原始 SQL", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();

    // 方案 A：SQL 候选默认派生，「关联逻辑度量」为原子专属——先把「0:amount」改类型为原子
    const typeSelect = screen.getByTestId("sql-batch-type-0:amount").closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(typeSelect.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("原子");

    // 打开「0:amount」候选的逻辑度量选择器，选「门诊收费金额」（id=1）
    const measureSelect = screen.getByTestId("sql-batch-measure-0:amount").closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(measureSelect.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("门诊收费金额 (medical_fee_amt)");

    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await waitFor(() => {
      expect(mockedBatchFromSql).toHaveBeenCalled();
      const body = mockedBatchFromSql.mock.calls[0][0];
      const amount = body.candidates.find((c: { key: string }) => c.key === "0:amount");
      // OneData 接线：选择器选中的逻辑度量透传（此前批量候选无 measure_id，全部游离）
      expect(amount?.measure_id).toBe(1);
      // 口径溯源：候选无 raw_sql 时从语句 meta 按 statement_index 提取整句原文
      expect(amount?.raw_sql).toContain("SUM(amount) AS gmv");
    });
  });

  it("批量创建：候选默认派生（方案 A）→ 只读口径表达式 + 可选依赖 + 挂载透传", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();

    // 方案 A：SQL 候选一律派生（原子只从逻辑度量目录创建）——候选默认即派生，
    // 无公式依赖（口径由解析出的聚合表达式承载，只读展示而非「计算表达式待填」）
    const typeSelect = screen.getByTestId("sql-batch-type-0:user_id").closest(".ant-select") as HTMLElement;
    expect(typeSelect.textContent || "").toContain("派生");

    // 派生候选：显示解析出的只读口径表达式 + 「派生（周期驱动，无公式依赖）」Tag
    //（两个基础候选均为派生，Tag 出现多次），不再显示计算表达式输入框（公式场景归复合）
    expect(await screen.findByText("COUNT(DISTINCT user_id)")).toBeTruthy();
    expect(screen.getAllByText("派生（周期驱动，无公式依赖）").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("sql-batch-expr-0:user_id")).toBeNull();

    // 依赖指标（派生可选）：从本批基础候选选择「日订单金额」
    const depsSelect = screen.getByTestId("sql-batch-deps-0:user_id").closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(depsSelect.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("日订单金额 (sales_order_amount_day)");

    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await waitFor(() => {
      expect(mockedBatchFromSql).toHaveBeenCalled();
      const body = mockedBatchFromSql.mock.calls[0][0];
      const derived = body.candidates.find((c: { key: string }) => c.key === "0:user_id");
      expect(derived?.type).toBe("derived");
      // 依赖指标合入 definition_json（血缘据此建上游边）；无 calc_expression → 保留解析口径
      expect(derived?.definition_json.dependencies).toEqual(["sales_order_amount_day"]);
      expect(derived?.definition_json.expression).toBe("COUNT(DISTINCT user_id)");
      // 派生透传挂载实体（OneData 挂载层：源表/列/主粒度/粒度维度/周期/域）
      expect(derived?.mount).toEqual({
        source_table: "dwd.sales_detail",
        source_column: "user_id",
        granularity: "day",
        // 组合粒度（方案 B）：GROUP BY 非时间键（hosp_code/enter_source）全部为粒度维度
        granularity_dims: ["hosp_code", "enter_source"],
        default_period: "day",
        domain: "sales",
      });
    });
  });

  it("批量创建：候选指标类型可在线改为复合 → 依赖指标 + 计算表达式（无挂载）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();

    // 把「0:user_id」原子候选改为复合（OneData：多指标运算/公式 = 复合）
    const typeSelect = screen.getByTestId("sql-batch-type-0:user_id").closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(typeSelect.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("复合");

    // 复合候选显示依赖指标多选 + 计算表达式输入
    const exprInput = screen.getByTestId("sql-batch-expr-0:user_id") as HTMLInputElement;
    expect(exprInput).toBeTruthy();
    fireEvent.change(exprInput, { target: { value: "sales_order_amount_day / sales_order_userid_day" } });

    // 依赖指标：从本批原子候选选择「日订单金额」
    const depsSelect = screen.getByTestId("sql-batch-deps-0:user_id").closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(depsSelect.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("日订单金额 (sales_order_amount_day)");

    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await waitFor(() => {
      expect(mockedBatchFromSql).toHaveBeenCalled();
      const body = mockedBatchFromSql.mock.calls[0][0];
      const composite = body.candidates.find((c: { key: string }) => c.key === "0:user_id");
      expect(composite?.type).toBe("composite");
      // 计算表达式 + 依赖指标合入 definition_json（血缘据此建上游边）
      expect(composite?.definition_json.expression).toBe("sales_order_amount_day / sales_order_userid_day");
      expect(composite?.definition_json.dependencies).toEqual(["sales_order_amount_day"]);
      // 复合不设挂载（OneData：挂载实体仅派生承载）
      expect(composite?.mount).toBeUndefined();
    });
  });

  it("批量创建：跨域权限预检——受限用户选非本域 → 前端拦截整批提交", async () => {
    // domain_admin 用户所属域为 sales，但批量选择 domain=finance → 前端拦截
    (fetchCurrentUser as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: 7,
      username: "admin",
      display_name: "管理员",
      role: "domain_admin",
      domain: "sales",
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    // 选非本域「财务 (finance)」
    const cascaderInput = document.querySelector(".ant-cascader input") as HTMLInputElement;
    fireEvent.mouseDown(cascaderInput);
    await waitFor(() => {
      const item = document.querySelector(".ant-cascader-menu-item[title='财务 (finance)']");
      expect(item).toBeTruthy();
      if (item) fireEvent.click(item);
    });
    await openBatchMode();
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    // 前端拦截：提示仅可批量注册本域，不发起请求
    expect(await screen.findByText(/您仅可批量注册本域指标/)).toBeTruthy();
    expect(mockedBatchFromSql).not.toHaveBeenCalled();
  });

  it("批量解析：无候选时按 skipped 原因分类提示（不再一律「请检查 SELECT」）", async () => {
    mockedParseSqlBatch.mockResolvedValueOnce({
      statements: [
        { index: 0, sql: "DROP TABLE IF EXISTS t", source_tables: [], measure_count: 0, group_by: [] },
        {
          index: 1,
          sql: "SELECT dt, SUM(x) AS v FROM t GROUP BY dt",
          source_tables: ["t"],
          measure_count: 0,
          group_by: ["dt"],
        },
      ],
      candidates: [],
      skipped: [
        { index: 0, sql: "DROP...", reason: "ddl_only" },
        { index: 1, sql: "SELECT...", reason: "parse_failed" },
      ],
      domain: {
        code: "sales",
        name: "销售",
        status: "user",
        confidence: null,
        candidates: [],
        matched_tables: [],
      },
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openSqlInfer();
    fireEvent.click(screen.getByText("批量解析"));
    fireEvent.change(screen.getByPlaceholderText(/批量解析：粘贴含多个 SELECT/), {
      target: { value: "DROP TABLE IF EXISTS t; SELECT dt, SUM(x) AS v FROM t GROUP BY dt" },
    });
    fireEvent.click(screen.getByText("解析候选"));

    // 分类文案（去重后拼接）而非旧泛化提示
    expect(await screen.findByText(/建表\/删表等非查询语句（无聚合度量）已跳过/)).toBeTruthy();
    expect(
      screen.getByText(/含聚合但语法\/方言无法识别，已尝试 AI 兜底仍未能提取/),
    ).toBeTruthy();
    // 无候选 → 不出现成功文案
    expect(screen.queryByText(/已解析 .* 个候选指标/)).toBeNull();
  });

  it("批量解析：切分模式可切换，custom 模式携带 delimiters/start_markers 规则（P2-8）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openSqlInfer();
    fireEvent.click(screen.getByText("批量解析"));
    // 默认 statement；切换到「自定义规则」
    const splitSelect = screen.getByTestId("sql-batch-split-mode").closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(splitSelect.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("自定义规则");
    // custom 模式出现规则输入框
    fireEvent.change(screen.getByPlaceholderText(/分隔符正则/), {
      target: { value: "GO;" },
    });
    fireEvent.change(screen.getByPlaceholderText(/起始标记正则/), {
      target: { value: "CREATE TABLE" },
    });
    fireEvent.change(screen.getByPlaceholderText(/批量解析：粘贴含多个 SELECT/), {
      target: { value: "SELECT 1" },
    });
    fireEvent.click(screen.getByText("解析候选"));
    await waitFor(() => {
      expect(mockedParseSqlBatch).toHaveBeenCalledWith(
        expect.objectContaining({
          split_mode: "custom",
          custom_rules: { delimiters: ["GO;"], start_markers: ["CREATE TABLE"] },
        }),
      );
    });
  });

  it("批量创建：候选编码为空时按最终域生成 4 段编码（P0-1）", async () => {
    mockedParseSqlBatch.mockResolvedValueOnce({
      ...SQL_BATCH_RESULT,
      candidates: [
        {
          ...SQL_BATCH_RESULT.candidates[0],
          metric_code: null as unknown as string,
          suggested_domain_code: null,
        },
      ],
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openSqlInfer();
    fireEvent.click(screen.getByText("批量解析"));
    fireEvent.change(screen.getByPlaceholderText(/批量解析：粘贴含多个 SELECT/), {
      target: {
        value: "SELECT dt, SUM(amount) AS gmv FROM dwd.sales_detail GROUP BY dt",
      },
    });
    fireEvent.click(screen.getByText("解析候选"));
    await screen.findByText(/共 1 个候选/);
    // 编码为空 → 输入框为空（已选域，placeholder 提示可修改）
    const codeInput = screen.getByTestId("sql-batch-code-0:amount") as HTMLInputElement;
    expect(codeInput.value).toBe("");
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await waitFor(() => {
      expect(mockedBatchFromSql).toHaveBeenCalled();
      const body = mockedBatchFromSql.mock.calls[0][0];
      // resolveCode：域 sales + 表 sales_detail → biz sales + 列 amount + 周期 day
      expect(body.candidates[0].metric_code).toBe("sales_sales_amount_day");
      // P1-5：granularity 随候选提交
      expect(body.candidates[0].granularity).toBe("day");
    });
  });

  it("批量创建：条件计数候选编码用 code_col 别名锚点（P：不再全落 metric 撞码）", async () => {
    mockedParseSqlBatch.mockResolvedValueOnce({
      ...SQL_BATCH_RESULT,
      statements: [
        {
          index: 0,
          sql: "SELECT expert_id, SUM(CASE WHEN create_date='2026-01-01' THEN 1 ELSE 0 END) AS expert_consultation_cnt_day FROM wedw_dw.wy_order_info GROUP BY expert_id",
          source_tables: ["wedw_dw.wy_order_info"],
          measure_count: 1,
          group_by: ["expert_id"],
        },
      ],
      candidates: [
        {
          ...SQL_BATCH_RESULT.candidates[0],
          key: "0:expert_consultation_cnt_day",
          metric_code: null as unknown as string,
          name: "当日问诊次数",
          source_table: "wedw_dw.wy_order_info",
          measure_column: "*",
          code_col: "expert_consultation_cnt_day",
          alias: "expert_consultation_cnt_day",
          definition_json: { expression: "SUM(CASE WHEN create_date='2026-01-01' THEN 1 ELSE 0 END)" },
          suggested_domain_code: null,
        },
      ],
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openSqlInfer();
    fireEvent.click(screen.getByText("批量解析"));
    fireEvent.change(screen.getByPlaceholderText(/批量解析：粘贴含多个 SELECT/), {
      target: {
        value: "SELECT expert_id, SUM(CASE WHEN create_date='2026-01-01' THEN 1 ELSE 0 END) AS expert_consultation_cnt_day FROM wedw_dw.wy_order_info GROUP BY expert_id",
      },
    });
    fireEvent.click(screen.getByText("解析候选"));
    await screen.findByText(/共 1 个候选/);
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await waitFor(() => {
      expect(mockedBatchFromSql).toHaveBeenCalled();
      const body = mockedBatchFromSql.mock.calls[0][0];
      // 条件计数（measure_column="*"）→ code_col 别名锚点：sales_wy_expertconsultationcntday_day
      expect(body.candidates[0].metric_code).toBe("sales_wy_expertconsultationcntday_day");
    });
  });

  it("批量创建：候选周期行内可编辑，提交携带修改后的 period（P2-9/R6）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    // 第一个原子候选的周期 Select 当前为「日 (day)」，改为「月 (month)」
    const periodSelect = (
      screen.getByTestId("sql-batch-period-0:amount").closest(".ant-select") as HTMLElement
    );
    fireEvent.mouseDown(periodSelect.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("月 (month)");
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await waitFor(() => {
      expect(mockedBatchFromSql).toHaveBeenCalled();
      const body = mockedBatchFromSql.mock.calls[0][0];
      const atom = body.candidates.find((c: { key: string }) => c.key === "0:amount");
      expect(atom?.period).toBe("month");
      // R6：原子候选选非日周期 → type 自动联动为派生（原子 = 逻辑度量 + 日粒度，
      // 非日周期归派生），不再出现「原子却带非日周期」
      expect(atom?.type).toBe("derived");
    });
  });

  it("批量创建：候选改类型为原子 → 非日周期回落为 day（S5 反向联动）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    // 先把周期改成 month（R6 自动升派生），再改类型为「原子」→ 周期/粒度应回落 day
    const periodSelect = (
      screen.getByTestId("sql-batch-period-0:amount").closest(".ant-select") as HTMLElement
    );
    fireEvent.mouseDown(periodSelect.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("月 (month)");
    const typeSelect = (
      screen.getByTestId("sql-batch-type-0:amount").closest(".ant-select") as HTMLElement
    );
    fireEvent.mouseDown(typeSelect.querySelector(".ant-select-selector") as HTMLElement);
    await clickSelectOption("原子");
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await waitFor(() => {
      expect(mockedBatchFromSql).toHaveBeenCalled();
      const body = mockedBatchFromSql.mock.calls[0][0];
      const atom = body.candidates.find((c: { key: string }) => c.key === "0:amount");
      // S5：改类型为原子 → 非日周期回落 day（原子 = 逻辑度量 + 基础统计粒度（日），
      // 不允许「原子 + 月粒度」落入 4 段编码）
      expect(atom?.type).toBe("atomic");
      expect(atom?.period).toBe("day");
      expect(atom?.granularity).toBe("day");
    });
  });

  it("批量创建：复合候选依赖指标与计算表达式显示必填星号（S8）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    // 复合候选行（0:composite）依赖指标与计算表达式均有必填红标星号（对齐创建向导）
    expect(screen.getByTestId("sql-batch-req-deps-0:composite")).toBeTruthy();
    expect(screen.getByTestId("sql-batch-req-expr-0:composite")).toBeTruthy();
  });

  it("批量创建：失败项可一键重试，仅重跑失败候选（P1-1）", async () => {
    mockedBatchFromSql
      .mockResolvedValueOnce({
        batch_id: "sqlbatch_fail1",
        candidates: [
          { metric_code: "sales_order_amount_day", status: "DRAFT", validation_errors: null },
          {
            metric_code: "sales_order_userid_day",
            status: "VALIDATION_ERROR",
            validation_errors: "候选参数校验失败",
          },
          {
            metric_code: "sales_order_amountuserid_day",
            status: "VALIDATION_ERROR",
            validation_errors: "依赖未创建",
          },
        ],
      })
      .mockResolvedValueOnce({
        batch_id: "sqlbatch_retry",
        candidates: [
          { metric_code: "sales_order_userid_day", status: "DRAFT", validation_errors: null },
          { metric_code: "sales_order_amountuserid_day", status: "DRAFT", validation_errors: null },
        ],
      });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await screen.findByText(/批量创建完成：成功 1 \/ 失败 2/);
    // 重试失败项（2）——仅重跑失败候选，成功候选不重复创建
    fireEvent.click(screen.getByText(/重试失败项（2）/));
    await waitFor(() => {
      expect(mockedBatchFromSql).toHaveBeenCalledTimes(2);
      const retryBody = mockedBatchFromSql.mock.calls[1][0];
      expect(retryBody.candidates.length).toBe(2);
      expect(
        retryBody.candidates.map((c: { metric_code: string }) => c.metric_code).sort(),
      ).toEqual(["sales_order_amountuserid_day", "sales_order_userid_day"]);
    });
  });

  it("批量创建：原子 DRAFT 可一键提交评审，复合候选排除（P1-1）", async () => {
    mockedBatchSubmit.mockResolvedValue({ results: [], ok_count: 2, fail_count: 0 });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await screen.findByText(/批量创建完成：成功 3 \/ 失败 0/);
    fireEvent.click(screen.getByRole("button", { name: /批量提交评审（免核对）/ }));
    await waitFor(() => {
      expect(mockedBatchSubmit).toHaveBeenCalled();
      const payload = mockedBatchSubmit.mock.calls[0][0] as Array<{ code: string }>;
      // 只送审 2 个原子 DRAFT，复合候选（依赖未发布）被排除
      expect(payload.map((x) => x.code).sort()).toEqual([
        "sales_order_amount_day",
        "sales_order_userid_day",
      ]);
    });
  });

  it("批量解析：LLM 兜底候选展示「AI 推断」复核标识（P2-2）", async () => {
    mockedParseSqlBatch.mockResolvedValueOnce({
      ...SQL_BATCH_RESULT,
      candidates: [{ ...SQL_BATCH_RESULT.candidates[0], source: "llm" }],
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openSqlInfer();
    fireEvent.click(screen.getByText("批量解析"));
    fireEvent.change(screen.getByPlaceholderText(/批量解析：粘贴含多个 SELECT/), {
      target: {
        value: "SELECT dt, SUM(x) AS v FROM t GROUP BY dt",
      },
    });
    fireEvent.click(screen.getByText("解析候选"));
    await screen.findByText(/共 1 个候选/);
    // AI 推断 Tag 出现（规则层候选不显示）
    expect(screen.getByText("AI 推断")).toBeTruthy();
  });

  it("批量解析：CASE/窗口口径候选展示「口径需核对」标识（A-1/2）", async () => {
    mockedParseSqlBatch.mockResolvedValueOnce({
      ...SQL_BATCH_RESULT,
      candidates: [{ ...SQL_BATCH_RESULT.candidates[0], needs_review: true }],
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openSqlInfer();
    fireEvent.click(screen.getByText("批量解析"));
    fireEvent.change(screen.getByPlaceholderText(/批量解析：粘贴含多个 SELECT/), {
      target: { value: "SELECT SUM(CASE WHEN status='paid' THEN amount END) AS v FROM t" },
    });
    fireEvent.click(screen.getByText("解析候选"));
    await screen.findByText(/共 1 个候选/);
    expect(screen.getByText("口径需核对")).toBeTruthy();
  });

  it("批量解析：草稿持久化——解析成功写 localStorage，重新进入恢复候选（生产就绪）", async () => {
    localStorage.removeItem("unisense.sql-batch.draft");
    mockedParseSqlBatch.mockResolvedValueOnce(SQL_BATCH_RESULT);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openSqlInfer();
    fireEvent.click(screen.getByText("批量解析"));
    fireEvent.change(screen.getByPlaceholderText(/批量解析：粘贴含多个 SELECT/), {
      target: { value: "SELECT dt, SUM(amount) AS gmv FROM t GROUP BY dt" },
    });
    fireEvent.click(screen.getByText("解析候选"));
    await screen.findByText(/共 3 个候选/);
    // 解析成功 → 草稿写入 localStorage（含 SQL/结果/切分配置）
    const draft = JSON.parse(localStorage.getItem("unisense.sql-batch.draft") ?? "null");
    expect(draft).toBeTruthy();
    expect(draft.result.candidates.length).toBeGreaterThan(0);
    expect(draft.sql).toContain("SUM(amount)");
    // 重新进入（卸载后重渲染）→ 挂载 useEffect 恢复候选（提示「已恢复上次草稿」）
    const { unmount } = renderPage();
    unmount();
    localStorage.setItem("unisense.sql-batch.draft", JSON.stringify(draft));
    renderPage();
    await screen.findByText(/已恢复上次的 SQL 批量解析草稿/);
    expect(screen.getByText(/共 3 个候选/)).toBeTruthy();
    localStorage.removeItem("unisense.sql-batch.draft");
  });

  it("批量解析：语句级建议域展示（跨域脚本提示，P2-10）", async () => {
    mockedParseSqlBatch.mockResolvedValueOnce({
      ...SQL_BATCH_RESULT,
      statements: [{ ...SQL_BATCH_RESULT.statements[0], suggested_domain: "health" }],
      candidates: SQL_BATCH_RESULT.candidates.map((c) => ({
        ...c,
        suggested_domain_code: "health",
      })),
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openBatchMode();
    // 当前未选域（selectedDomain 空）→ 展示「建议域 health」Tag（两个原子候选各一个）
    expect(screen.getAllByText("建议域 health").length).toBeGreaterThan(0);
  });

  it("批量解析：LLM 推断并回填字段按钮携带 use_llm=true（LLM 模式）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openSqlInfer();
    fireEvent.click(screen.getByText("批量解析"));
    fireEvent.change(screen.getByPlaceholderText(/批量解析：粘贴含多个 SELECT/), {
      target: {
        value: "SELECT dt, SUM(amount) AS gmv, COUNT(DISTINCT user_id) AS uv FROM dwd.sales_detail GROUP BY dt",
      },
    });
    fireEvent.click(screen.getByText("LLM 推断并回填字段"));
    // LLM 模式：请求携带 use_llm=true + 成功提示区分
    await waitFor(() => {
      expect(mockedParseSqlBatch).toHaveBeenCalledWith(
        expect.objectContaining({ use_llm: true, split_mode: "statement" }),
      );
    });
    await screen.findByText(/已用 LLM 全字段推断解析 3 个候选指标/);
    // 普通「解析候选」不带 use_llm
    mockedParseSqlBatch.mockClear();
    fireEvent.click(screen.getByText("解析候选"));
    await waitFor(() => {
      const calls = mockedParseSqlBatch.mock.calls;
      const lastCall = calls[calls.length - 1]?.[0] as { use_llm?: boolean };
      expect(lastCall?.use_llm).toBeFalsy();
    });
  });

  it("批量候选可在线编辑：改单位/粒度/编码后提交创建携带修改值", async () => {
    // unit 字典 mock（供「单位」行内 Select 选项）
    mockedDict.mockResolvedValue([{ code: "USD", label: "美元" }] as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();

    // 单位：CNY → 美元 (USD)
    fireEvent.mouseDown(screen.getByTestId("sql-batch-unit-0:amount").querySelector(".ant-select-selector")!);
    await clickSelectOption("美元 (USD)");
    // 粒度：day → 月 (month)
    fireEvent.mouseDown(screen.getByTestId("sql-batch-granularity-0:amount").querySelector(".ant-select-selector")!);
    await clickSelectOption("月 (month)");
    // 编码可编辑
    fireEvent.change(screen.getByTestId("sql-batch-code-0:amount"), {
      target: { value: "sales_order_amount_month" },
    });

    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await waitFor(() => expect(mockedBatchFromSql).toHaveBeenCalled());
    const payload = mockedBatchFromSql.mock.calls[0][0] as {
      candidates: Array<{ key: string; unit: string | null; granularity: string | null; metric_code: string }>;
    };
    const cand = payload.candidates.find((c) => c.key === "0:amount");
    expect(cand).toBeTruthy();
    expect(cand!.unit).toBe("USD");
    expect(cand!.granularity).toBe("month");
    expect(cand!.metric_code).toBe("sales_order_amount_month");
  });

  it("Q1: 批量候选「在向导中编辑」完整回填单条向导表单核对", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openBatchMode();
    // 候选行出现「在向导中编辑」按钮，点击完整回填单条向导
    fireEvent.click(screen.getByTestId("sql-batch-to-wizard-0:amount"));
    // 抽屉关闭 + 定位到单条向导 Step①（指标基本信息：类型/粒度前置在此）让用户核对
    await waitFor(() => expect(screen.getByText("指标类型")).toBeTruthy());
    // 编码/名称已回填到 Step② 指标基本信息（用户可改后回 Step2 手动提交创建）
    await waitFor(() => {
      expect((screen.getByLabelText("指标编码") as HTMLInputElement).value).toBe(
        "sales_order_amount_day",
      );
      expect((screen.getByLabelText("名称") as HTMLInputElement).value).toBe("日订单金额");
    });
    // 源表/度量列已回填（方案 A：派生候选物理来源进 Step2 挂载实体区；回 Step2 核对
    // 挂载卡首行源表 Select 选中值）
    await goToStep(2);
    await waitFor(() => {
      const items = Array.from(document.querySelectorAll(".ant-select-selection-item"));
      expect(items.some((el) => el.textContent?.includes("dwd.sales_detail"))).toBe(true);
    });
  });

  it("批量解析：候选「查看完整口径」弹出完整口径定义（expression/source_tables/partition_key/dw_definition 不截断）", async () => {
    mockedParseSqlBatch.mockResolvedValueOnce({
      ...SQL_BATCH_RESULT,
      candidates: SQL_BATCH_RESULT.candidates.map((c) =>
        c.key === "0:amount"
          ? {
              ...c,
              definition_json: {
                expression: "SUM(amount)",
                source_tables: ["dwd.sales_detail"],
                // source_fields 为对象数组 [{table, column}]——必须渲染为「表.列」而非 [object Object]
                source_fields: [{ table: "dwd.sales_detail", column: "gmv" }],
                partition_key: "dt",
                dw_definition:
                  "SELECT dt, SUM(amount) AS gmv FROM dwd.sales_detail GROUP BY dt",
              },
            }
          : c,
      ),
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await openBatchMode();
    // 点「查看完整口径」→ 弹出完整口径定义 Modal（此前口径只在行内截断展示）
    fireEvent.click(screen.getByTestId("sql-batch-def-0:amount"));
    await waitFor(() => {
      expect(document.body.querySelector(".ant-modal-title")?.textContent || "").toContain("口径定义详情");
    });
    const modal = within(document.querySelector(".ant-modal") as HTMLElement);
    expect(modal.getByText("口径表达式")).toBeTruthy();
    expect(modal.getByText("SUM(amount)")).toBeTruthy();
    expect(modal.getByText("源表")).toBeTruthy();
    expect(modal.getByText("dwd.sales_detail")).toBeTruthy();
    // source_fields 对象数组渲染为「表.列」而非 [object Object]（此前 String(对象) 变 [object Object]）
    expect(modal.getByText("上游字段（源表.列）")).toBeTruthy();
    expect(modal.getByText("dwd.sales_detail.gmv")).toBeTruthy();
    expect(modal.queryByText("[object Object]")).toBeNull();
    expect(modal.getByText("时间列 / 分区键")).toBeTruthy();
    expect(modal.getByText("dt")).toBeTruthy();
    expect(modal.getByText("数仓详细口径（完整 SQL）")).toBeTruthy();
    expect(
      modal.getByText(/SELECT dt, SUM\(amount\) AS gmv FROM dwd\.sales_detail GROUP BY dt/),
    ).toBeTruthy();
  });

  it("批量解析：候选行可设置技术方/数仓开发（对齐产品负责），提交携带三方责任", async () => {
    // 责任方用户选项（ownerUsers 加载 listUsers）
    mockedUsers.mockResolvedValue([
      { id: 101, username: "zhangsan", display_name: "张三", role: "user", domain: "sales", status: "active" },
      { id: 102, username: "lisi", display_name: "李四", role: "user", domain: "sales", status: "active" },
    ] as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    // 候选卡片底部行出现技术方/数仓开发（此前只有产品负责）
    expect(screen.getAllByText("技术方").length).toBeGreaterThan(0);
    expect(screen.getAllByText("数仓开发").length).toBeGreaterThan(0);
    // 技术方选 张三
    fireEvent.mouseDown(screen.getByTestId("sql-batch-tech-0:amount").querySelector(".ant-select-selector")!);
    await clickSelectOption("张三");
    // 数仓开发选 李四
    fireEvent.mouseDown(screen.getByTestId("sql-batch-dw-0:amount").querySelector(".ant-select-selector")!);
    await clickSelectOption("李四");
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await waitFor(() => expect(mockedBatchFromSql).toHaveBeenCalled());
    const payload = mockedBatchFromSql.mock.calls[0][0] as {
      candidates: Array<{ key: string; tech_owner_id: number | null; dw_developer_id: number | null }>;
    };
    const cand = payload.candidates.find((c) => c.key === "0:amount");
    expect(cand).toBeTruthy();
    expect(cand!.tech_owner_id).toBe(101);
    expect(cand!.dw_developer_id).toBe(102);
  });

  it("批量设置责任方：勾选多个候选一次设置技术方，提交携带三方责任", async () => {
    mockedUsers.mockResolvedValue([
      { id: 101, username: "zhangsan", display_name: "张三", role: "user", domain: "sales", status: "active" },
      { id: 102, username: "lisi", display_name: "李四", role: "user", domain: "sales", status: "active" },
    ] as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    // 打开批量设置责任方弹窗（收敛后入口：批量编辑向导 Step 1 工具条）
    fireEvent.click(screen.getByTestId("sql-batch-open-wizard"));
    await waitFor(() => {
      expect(document.body.querySelector(".ant-modal-title")?.textContent || "").toContain("批量编辑向导");
    });
    const wm = () => within(document.querySelector(".ant-modal") as HTMLElement);
    fireEvent.click(wm().getByTestId("sql-batch-wizard-next"));
    await waitFor(() => {
      expect(document.querySelector('[data-testid="sql-batch-wizard-t1"]')).toBeTruthy();
    });
    fireEvent.click(wm().getByTestId("sql-batch-wizard-open-owner"));
    const ownerModal = within(
      screen.getByTestId("sql-batch-owner-role").closest(".ant-modal") as HTMLElement,
    );
    // 选「技术方」角色
    fireEvent.click(ownerModal.getByText("技术方"));
    // 选负责人 张三（ownerUsers 下拉）
    fireEvent.mouseDown(screen.getByTestId("sql-batch-owner-user").querySelector(".ant-select-selector")!);
    await clickSelectOption("张三");
    fireEvent.click(screen.getByRole("button", { name: /应\s*用/ }));
    // 默认已勾选 2 个原子候选 → 提交 payload 两个候选均带 tech_owner_id=101
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await waitFor(() => expect(mockedBatchFromSql).toHaveBeenCalled());
    const payload = mockedBatchFromSql.mock.calls[0][0] as {
      candidates: Array<{ key: string; tech_owner_id: number | null }>;
    };
    const c1 = payload.candidates.find((c) => c.key === "0:amount");
    const c2 = payload.candidates.find((c) => c.key === "0:user_id");
    expect(c1?.tech_owner_id).toBe(101);
    expect(c2?.tech_owner_id).toBe(101);
  });

  it("批量设置责任方：应用范围切「全部候选」时未勾选复合候选也生效", async () => {
    mockedUsers.mockResolvedValue([
      { id: 101, username: "zhangsan", display_name: "张三", role: "user", domain: "sales", status: "active" },
    ] as never);
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    fireEvent.click(screen.getByTestId("sql-batch-open-wizard"));
    await waitFor(() => {
      expect(document.body.querySelector(".ant-modal-title")?.textContent || "").toContain("批量编辑向导");
    });
    const wm = () => within(document.querySelector(".ant-modal") as HTMLElement);
    fireEvent.click(wm().getByTestId("sql-batch-wizard-next"));
    await waitFor(() => {
      expect(document.querySelector('[data-testid="sql-batch-wizard-t1"]')).toBeTruthy();
    });
    fireEvent.click(wm().getByTestId("sql-batch-wizard-open-owner"));
    // 应用范围 → 全部候选（3 个）
    fireEvent.click(screen.getByText("全部候选（3 个）"));
    fireEvent.mouseDown(screen.getByTestId("sql-batch-owner-user").querySelector(".ant-select-selector")!);
    await clickSelectOption("张三");
    fireEvent.click(screen.getByRole("button", { name: /应\s*用/ }));
    // 复合候选未勾选也被批量设置：候选行「产品负责」Select 显示「张三」
    const compositeOwner = screen.getByTestId("sql-batch-owner-0:composite");
    await waitFor(() => {
      expect(within(compositeOwner).getByText("张三")).toBeTruthy();
    });
  });

  it("批量设置粒度：勾选候选一次设置主粒度+粒度维度，候选行同步（同一 SQL 候选粒度应一致）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    // 打开批量编辑向导 → Step 0 → 工具条「批量设置粒度」（顶部入口已收敛到向导内）
    fireEvent.click(screen.getByTestId("sql-batch-open-wizard"));
    await waitFor(() => {
      expect(document.body.querySelector(".ant-modal-title")?.textContent || "").toContain("批量编辑向导");
    });
    const wm = () => within(document.querySelector(".ant-modal") as HTMLElement);
    fireEvent.click(wm().getByTestId("sql-batch-wizard-open-grain"));
    // 主粒度选「月 (month)」
    fireEvent.mouseDown(
      screen.getByTestId("sql-batch-grain-main").querySelector(".ant-select-selector")!,
    );
    await clickSelectOption("月 (month)");
    // 粒度维度手输「医院」（tags；字典 mock 为空，antd 会把输入值作为可建选项展示）
    const dimsSel = screen.getByTestId("sql-batch-grain-dims");
    fireEvent.mouseDown(dimsSel.querySelector(".ant-select-selector")!);
    const dimsInput = dimsSel.querySelector(
      ".ant-select-selection-search-input",
    ) as HTMLInputElement;
    fireEvent.change(dimsInput, { target: { value: "医院" } });
    await clickSelectOption("医院");
    // 确认「医院」tag 已进入粒度 Modal，再点应用
    await waitFor(() => {
      expect(within(dimsSel).getAllByText("医院").length).toBeGreaterThan(0);
    });
    fireEvent.click(screen.getByRole("button", { name: /应\s*用/ }));
    // 应用后候选行「粒度」Select 显示 月、粒度维度 tags 显示 医院
    await waitFor(() => {
      const grainSel = screen.getByTestId("sql-batch-granularity-0:amount");
      expect(grainSel.querySelector(".ant-select-selection-item")?.textContent).toContain("月");
    });
    const candDimsSel = screen.getByTestId("sql-batch-granularity-dims-0:amount");
    expect(within(candDimsSel).getByText("医院")).toBeTruthy();
    // 提交 payload：已勾选候选均带 granularity=month + mount.granularity_dims=[医院]
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await waitFor(() => expect(mockedBatchFromSql).toHaveBeenCalled());
    const payload = mockedBatchFromSql.mock.calls[0][0] as {
      candidates: Array<{
        key: string;
        granularity: string | null;
        mount?: { granularity_dims?: string[] | null } | null;
      }>;
    };
    expect(payload.candidates.length).toBeGreaterThan(0);
    for (const c of payload.candidates) {
      expect(c.granularity).toBe("month");
      expect(c.mount?.granularity_dims).toEqual(["医院"]);
    }
  });

  it("批量编辑向导：Step 0 粒度维度列 tags 可编辑（不再只有时间粒度）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    fireEvent.click(screen.getByTestId("sql-batch-open-wizard"));
    await waitFor(() => {
      expect(document.body.querySelector(".ant-modal-title")?.textContent || "").toContain("批量编辑向导");
    });
    // Step 0 表格同时含「主粒度」「粒度维度」两列（与粒度管理/候选列表同步）
    const t0El = document.querySelector('[data-testid="sql-batch-wizard-t0"]') as HTMLElement;
    const t0 = within(t0El);
    expect(t0.getAllByText("主粒度").length).toBeGreaterThan(0);
    expect(t0.getAllByText("粒度维度").length).toBeGreaterThan(0);
    // 第一行粒度维度 tags 手输「医院」，表格出现该 tag
    const dimsInput = t0El.querySelector(
      ".ant-select-multiple .ant-select-selection-search-input",
    ) as HTMLInputElement;
    fireEvent.change(dimsInput, { target: { value: "医院" } });
    fireEvent.keyDown(dimsInput, { key: "Enter", code: "Enter" });
    await waitFor(() => {
      expect(t0.getAllByText("医院").length).toBeGreaterThan(0);
    });
  });

  it("批量创建：点击后显示阻塞进度 Modal，完成后自动关闭（体验优化）", async () => {
    let resolveCreate!: (v: unknown) => void;
    mockedBatchFromSql.mockReturnValueOnce(
      new Promise((res) => {
        resolveCreate = res;
      }) as never,
    );
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    // 确保勾选 2 个候选（对默认勾选类型免疫：HEAD 默认勾选原子，并行曾改派生——
    // 已勾选则不动，未勾选才点，避免「点击=取消」语义反转）
    const cb1 = screen.getByRole("checkbox", { name: "勾选 日订单金额" }) as HTMLInputElement;
    if (!cb1.checked) fireEvent.click(cb1);
    const cb2 = screen.getByRole("checkbox", { name: "勾选 日去重用户" }) as HTMLInputElement;
    if (!cb2.checked) fireEvent.click(cb2);
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    // 创建中：进度文案出现（抽屉小字 + Modal 大字）
    await waitFor(() => {
      expect(screen.getAllByText(/正在批量创建 2 个指标为草稿/).length).toBeGreaterThan(0);
    });
    // Modal 面板存在且展示阻塞说明（此前仅按钮 loading + 结果区小字，无明确反馈）
    const modal = document.querySelector(".ant-modal");
    expect(modal).toBeTruthy();
    expect(
      within(modal as HTMLElement).getByText("逐条校验并写入数据库，请勿关闭窗口"),
    ).toBeTruthy();
    // 创建完成后自动关闭（结果区 Alert 出现，进度让位）
    resolveCreate({ batch_id: "sqlbatch_progress", candidates: [] });
    await waitFor(() => {
      expect(screen.getByText(/批量创建完成：成功 0 \/ 失败 0/)).toBeTruthy();
    });
  });

  it("批量创建失败项：按失败原因显示具体操作入口——编码冲突→改编码重试、依赖缺失→补依赖重试", async () => {
    mockedBatchFromSql.mockResolvedValueOnce({
      batch_id: "sqlbatch_fail2",
      candidates: [
        { metric_code: "sales_order_amount_day", status: "DRAFT", validation_errors: null },
        {
          metric_code: "sales_order_userid_day",
          status: "VALIDATION_ERROR",
          validation_errors: "指标编码已存在",
        },
        {
          metric_code: "sales_order_amountuserid_day",
          status: "VALIDATION_ERROR",
          validation_errors: "依赖指标未创建或不存在",
        },
      ],
    });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    // 确保勾选 2 个原子候选（对默认勾选类型免疫：已勾选则不动，未勾选才点）
    const cb1 = screen.getByRole("checkbox", { name: "勾选 日订单金额" }) as HTMLInputElement;
    if (!cb1.checked) fireEvent.click(cb1);
    const cb2 = screen.getByRole("checkbox", { name: "勾选 日去重用户" }) as HTMLInputElement;
    if (!cb2.checked) fireEvent.click(cb2);
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await screen.findByText(/批量创建完成：成功 1 \/ 失败 2/);
    // 编码冲突行 → 「改编码重试」；依赖缺失行 → 「补依赖重试」；均带「重试」兜底
    expect(screen.getByRole("button", { name: "改编码重试" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "补依赖重试" })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "重试" }).length).toBeGreaterThanOrEqual(2);
    // 点「改编码重试」→ 完整回填单条向导（此前失败项操作列为空，用户只能干看原因）
    fireEvent.click(screen.getByRole("button", { name: "改编码重试" }));
    await waitFor(() => {
      expect((screen.getByLabelText("指标编码") as HTMLInputElement).value).toBe(
        "sales_order_userid_day",
      );
    });
  });

  it("批量创建失败项：点行内「重试」仅重跑该失败候选（单条重试）", async () => {
    mockedBatchFromSql
      .mockResolvedValueOnce({
        batch_id: "sqlbatch_fail3",
        candidates: [
          { metric_code: "sales_order_amount_day", status: "DRAFT", validation_errors: null },
          {
            metric_code: "sales_order_userid_day",
            status: "VALIDATION_ERROR",
            validation_errors: "候选参数校验失败",
          },
        ],
      })
      .mockResolvedValueOnce({
        batch_id: "sqlbatch_retry1",
        candidates: [
          { metric_code: "sales_order_userid_day", status: "DRAFT", validation_errors: null },
        ],
      });
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
    await openBatchMode();
    // 确保勾选 2 个原子候选（对默认勾选类型免疫：已勾选则不动，未勾选才点）
    const cb1 = screen.getByRole("checkbox", { name: "勾选 日订单金额" }) as HTMLInputElement;
    if (!cb1.checked) fireEvent.click(cb1);
    const cb2 = screen.getByRole("checkbox", { name: "勾选 日去重用户" }) as HTMLInputElement;
    if (!cb2.checked) fireEvent.click(cb2);
    fireEvent.click(screen.getByText(/批量创建选中指标/));
    await screen.findByText(/批量创建完成：成功 1 \/ 失败 1/);
    // 点失败行「重试」→ 第二次调用仅含该失败候选（不重跑已成功的）
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => {
      expect(mockedBatchFromSql).toHaveBeenCalledTimes(2);
      const body = mockedBatchFromSql.mock.calls[1][0] as {
        candidates: Array<{ metric_code: string }>;
      };
      expect(body.candidates.map((c) => c.metric_code)).toEqual(["sales_order_userid_day"]);
    });
  });
});
