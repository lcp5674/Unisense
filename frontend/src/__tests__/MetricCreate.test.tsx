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
    batchRegisterMetrics: vi.fn(),
    autoSuggestMetric: vi.fn(),
    checkConflict: vi.fn(),
  };
});

import { listDomainTree, listDictItems, listCatalogs, batchRegisterMetrics, autoSuggestMetric, checkConflict } from "../api";
import type { DBCatalog, SubjectDomainTreeNode } from "../types";

const mockedTree = vi.mocked(listDomainTree);
const mockedDict = vi.mocked(listDictItems);
const mockedCatalogs = vi.mocked(listCatalogs);
const mockedBatch = vi.mocked(batchRegisterMetrics);
const mockedSuggest = vi.mocked(autoSuggestMetric);
const mockedCheckConflict = vi.mocked(checkConflict);

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
    mockedCatalogs.mockResolvedValue({
      items: [makeCatalog("dwd.sales_detail")],
      total: 1,
      page: 1,
      page_size: 20,
    });
  });

  /** 选择业务域（Cascader 弹出面板点第一层「销售 (sales)」）。 */
  async function pickDomain() {
    const cascaderInput = document.querySelector(".ant-cascader input") as HTMLInputElement;
    fireEvent.mouseDown(cascaderInput);
    await waitFor(() => {
      const item = document.querySelector(".ant-cascader-menu-item[title='销售 (sales)']");
      expect(item).toBeTruthy();
      if (item) fireEvent.click(item);
    });
  }

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

    // 源表/度量列已回填到②自动推断区（Select 显示选中值）
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
    fireEvent.change(screen.getByPlaceholderText(/SELECT SUM\(amount\) AS gmv/), {
      target: { value: "SELECT SUM(amount) AS gmv FROM dwd.sales_detail GROUP BY dt" },
    });
    fireEvent.click(screen.getByText("智能推断并回填字段"));
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

  it("SQL 推断：未选域或未粘贴 SQL 时「智能推断」按钮禁用（惰性引导）", async () => {
    renderPage();
    await screen.findByText("注册指标（草稿）");
    // 未选域时按钮 disabled，点击不触发请求
    const btn = screen.getByText("智能推断并回填字段").closest("button") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(mockedSuggest).not.toHaveBeenCalled();
  });

  it("SQL 推断失败：展示明确错误原因", async () => {
    mockedSuggest.mockRejectedValue(new Error("invalid SQL syntax"));
    renderPage();
    await screen.findByText("注册指标（草稿）");
    await pickDomain();
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

    // 填指标编码与口径定义（expression 模式默认），再点冲突预检
    const codeInput = screen.getByLabelText("指标编码") as HTMLInputElement;
    fireEvent.change(codeInput, { target: { value: "sales_test" } });
    const defInput = screen.getByLabelText("口径定义 (JSON)") as HTMLTextAreaElement;
    fireEvent.change(defInput, { target: { value: '{"expr": "sum(amount)"}' } });

    fireEvent.click(screen.getByRole("button", { name: /冲突预检/ }));
    // 正确映射：后端 ConflictType 值为 same_name_diff_def → 中文「同名不同义」
    await screen.findByText(/同名不同义/);
    expect(screen.queryByText(/same_name_diff_def/)).toBeNull();
  });
});

describe("MetricCreate 源表选择惰性化", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedTree.mockResolvedValue(TREE);
    mockedDict.mockResolvedValue([]);
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
    // 展开「口径定义 → 关联数据表」多选下拉
    const relatedSelect = screen.getByText(/展开浏览已接入表/);
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
    const relatedSelect = screen.getByText(/展开浏览已接入表/);
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
