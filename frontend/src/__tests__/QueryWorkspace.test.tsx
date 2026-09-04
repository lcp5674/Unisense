import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryWorkspace } from "../pages/QueryWorkspace";

// Mock API：QueryWorkspace 依赖的模块全量提供（含消费令牌存取 + 错误类）
vi.mock("../api", () => {
  class UnisenseApiError extends Error {
    code: string;
    traceId: string;
    status: number;
    detail?: Record<string, unknown> | null;
    constructor(message: string, code: string, status: number, traceId: string, detail?: Record<string, unknown> | null) {
      super(message);
      this.name = "UnisenseApiError";
      this.code = code;
      this.status = status;
      this.traceId = traceId;
      this.detail = detail;
    }
    get codeZh(): string {
      return this.code;
    }
  }
  return {
    UnisenseApiError,
    consumeDryRun: vi.fn(),
    consumeQuery: vi.fn(),
    consumeSemantic: vi.fn(),
    listMetrics: vi.fn(),
    listSnapshots: vi.fn(),
    listApiClients: vi.fn(),
    listDataSources: vi.fn(),
    queryDataSourceSql: vi.fn(),
    mintClientToken: vi.fn(),
    getConsumeToken: vi.fn(() => null),
    getConsumeTokenExpiry: vi.fn(() => null),
    getConsumeTokenClientId: vi.fn(() => null),
    setConsumeToken: vi.fn(),
    clearConsumeToken: vi.fn(),
    CONSUME_TOKEN_CHANGED_EVENT: "unisense:consume-token-changed",
  };
});

// Mock useTracking hook（返回稳定引用，避免 effect 依赖反复触发）
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import {
  UnisenseApiError,
  consumeDryRun,
  consumeQuery,
  consumeSemantic,
  listMetrics,
  listApiClients,
  listDataSources,
  queryDataSourceSql,
  getConsumeToken,
  getConsumeTokenExpiry,
  getConsumeTokenClientId,
  CONSUME_TOKEN_CHANGED_EVENT,
} from "../api";
const mockedConsumeDryRun = vi.mocked(consumeDryRun);
const mockedConsumeQuery = vi.mocked(consumeQuery);
const mockedConsumeSemantic = vi.mocked(consumeSemantic);
const mockedListMetrics = vi.mocked(listMetrics);
const mockedListApiClients = vi.mocked(listApiClients);
const mockedListDataSources = vi.mocked(listDataSources);
const mockedQueryDataSourceSql = vi.mocked(queryDataSourceSql);
const mockedGetConsumeToken = vi.mocked(getConsumeToken);
const mockedGetConsumeTokenExpiry = vi.mocked(getConsumeTokenExpiry);
const mockedGetConsumeTokenClientId = vi.mocked(getConsumeTokenClientId);

const mockSemanticData = {
  metric_code: "gmv_net",
  status: "ok",
  checks: [
    { check: "granularity", ok: true, detail: "指标粒度 day" },
  ],
  execution_plan: {
    metric_code: "gmv_net",
    expression_ast: { raw: "sum(amount)" },
    dialect_sql: "SELECT * FROM dws_metric_gmv_net LIMIT 1000",
  },
  meta: { grain: "day", unit: "元", pii: false, domain: "sales", status: "PUBLISHED" },
};

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/query"]}>
      <QueryWorkspace />
    </MemoryRouter>,
  );
}

// 构造最小合法 MetricResponse（下拉仅消费 metric_code/name/domain/pii_flag）
function mkMetric(code: string, name: string, domain: string, pii = false): import("../types").MetricResponse {
  return {
    id: 1,
    metric_code: code,
    name,
    domain,
    type: "atomic",
    granularity: "day",
    unit: "元",
    currency: null,
    aggregation: "SUM",
    time_semantics: "PERIOD",
    freshness: "T1",
    sla: null,
    dw_layer: "DWS",
    metric_tier: "T1",
    serving_mode: "BATCH_ONLY",
    additivity: "ADDITIVE",
    non_additive_dimensions: null,
    definition_json: {},
    version: 1,
    row_version: 1,
    status: "PUBLISHED",
    owner_id: 1,
    backup_owner_id: null,
    approver_id: null,
    submitted_by: null,
    pii_flag: pii,
    compliance_reviewed: true,
    term_id: null,
    effective_version: 1,
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
    created_at: "2026-08-01T00:00:00",
    updated_at: "2026-08-01T00:00:00",
  };
}

// 定位「指标」Form.Item 内的 Select 搜索输入框（antd 不把 placeholder 放到 input 上）
function metricSelectInput(): HTMLInputElement {
  const labelEl = screen.getByText("指标", { selector: "label" });
  const itemEl = labelEl.closest(".ant-form-item") as HTMLElement;
  return within(itemEl).getByRole("combobox") as HTMLInputElement;
}

describe("QueryWorkspace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 令牌 mock 默认「无令牌」；各测试按需覆盖，且不泄漏到后续用例（clearAllMocks 不清 implementation）
    mockedGetConsumeToken.mockReturnValue(null);
    mockedGetConsumeTokenExpiry.mockReturnValue(null);
    mockedGetConsumeTokenClientId.mockReturnValue(null);
    // 默认无 ACTIVE 客户端 → 授权范围无限制（展示全部 PUBLISHED），与既有用例语义一致
    mockedListApiClients.mockResolvedValue([]);
    // SQL 查询 Tab：默认一个数据源 + 空结果（各测试按需覆盖）
    mockedListDataSources.mockResolvedValue({
      items: [
        {
          source_id: "mysql_unisense",
          name: "主库",
          source_type: "mysql",
          domain: "sales",
          cluster_id: null,
          coverage: 0.8,
          health_status: "healthy",
          connection_config_present: true,
          databases: null,
          schedule_cron: null,
          schedule_enabled: false,
          collection_mode: "FULL",
          enabled: true,
          created_by: 1,
          created_at: "2026-08-01T00:00:00",
          updated_at: "2026-08-01T00:00:00",
        },
      ],
      total: 1,
      page: 1,
      page_size: 200,
    });
    mockedQueryDataSourceSql.mockResolvedValue({
      columns: ["id", "name"],
      rows: [{ id: 1, name: "测试" }],
      total: 1,
      truncated: false,
      elapsed_ms: 5,
    });
    mockedListMetrics.mockResolvedValue({
      items: [
        {
          id: 1,
          metric_code: "gmv_net",
          name: "净GMV",
          domain: "sales",
          type: "atomic",
          granularity: "day",
          unit: "元",
          currency: null,
          aggregation: "SUM",
          time_semantics: "PERIOD",
          freshness: "T1",
          sla: null,
          dw_layer: "DWS",
          metric_tier: "T1",
          serving_mode: "BATCH_ONLY",
          additivity: "ADDITIVE",
          non_additive_dimensions: null,
          definition_json: { expr: "sum(amount)" },
          version: 1,
          row_version: 1,
          status: "PUBLISHED",
          owner_id: 1,
          backup_owner_id: null,
          approver_id: null,
          submitted_by: null,
          pii_flag: false,
          compliance_reviewed: true,
          term_id: null,
          effective_version: 1,
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
          created_at: "2026-08-01T00:00:00",
          updated_at: "2026-08-01T00:00:00",
        },
      ],
      total: 1,
      page: 1,
      page_size: 100,
    });
    mockedConsumeSemantic.mockResolvedValue(mockSemanticData);
    mockedConsumeDryRun.mockResolvedValue({
      metric_code: "gmv_net",
      status: "ok",
      checks: [],
      execution_plan: { metric_code: "gmv_net" },
      meta: {},
    });
  });

  it("renders query workspace with action buttons", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("查询工作台")).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /语义校验/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /执行查询/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /指标语义/ })).toBeInTheDocument();
  });

  it("warns when 指标语义 clicked without selecting metric", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /指标语义/ }));

    await waitFor(() => {
      expect(screen.getByText("请选择指标")).toBeInTheDocument();
    });
    expect(mockedConsumeSemantic).not.toHaveBeenCalled();
  });

  it("loads semantic and opens drawer after selecting metric", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());

    // 选择指标（antd Select：点搜索输入框 → 点下拉选项）
    await user.click(metricSelectInput());
    await user.click(await screen.findByText(/gmv_net · 净GMV/));

    await user.click(screen.getByRole("button", { name: /指标语义/ }));

    await waitFor(() => {
      expect(mockedConsumeSemantic).toHaveBeenCalledWith("gmv_net", { forceUser: true });
    });
    // 抽屉标题 + 校验项 + 元信息渲染
    await waitFor(() => {
      expect(screen.getByText(/指标语义：gmv_net/)).toBeInTheDocument();
    });
    expect(screen.getByText("校验项")).toBeInTheDocument();
    expect(screen.getByText("元信息")).toBeInTheDocument();
    expect(trackMock).toHaveBeenCalledWith("consume_semantic", "gmv_net", "metric");
  });

  it("shows error message when semantic fetch fails", async () => {
    const user = userEvent.setup();
    mockedConsumeSemantic.mockRejectedValue(new Error("指标不存在"));
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());

    await user.click(metricSelectInput());
    await user.click(await screen.findByText(/gmv_net · 净GMV/));

    await user.click(screen.getByRole("button", { name: /指标语义/ }));

    await waitFor(() => {
      expect(screen.getByText("操作失败")).toBeInTheDocument();
    });
  });

  it("未签发消费令牌时给出签发引导而非技术错误码（消费接入调试模式）", async () => {
    const user = userEvent.setup();
    mockedConsumeQuery.mockRejectedValue(
      new UnisenseApiError("X-Api-Key 格式应为 client_id:secret", "AUTH_APIKEY_INVALID", 401, "trace-test"),
    );
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());

    // 切到「消费接入调试」模式（该模式展示令牌状态与签发引导）
    await user.click(screen.getByText("消费接入调试"));

    await user.click(metricSelectInput());
    await user.click(await screen.findByText(/gmv_net · 净GMV/));

    await user.click(screen.getByRole("button", { name: /查\s*询/ }));

    await waitFor(() => {
      expect(screen.getByText(/需要消费令牌：请点击上方『从客户端签发令牌』后重试/)).toBeInTheDocument();
    });
  });

  it("有未过期令牌时展示『已就绪』与剩余分钟（消费接入调试模式）", async () => {
    const user = userEvent.setup();
    mockedGetConsumeToken.mockReturnValue("fake-consume-jwt");
    mockedGetConsumeTokenExpiry.mockReturnValue(Date.now() + 3600000);
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());

    // 切到「消费接入调试」模式后令牌状态 Alert 才展示
    await user.click(screen.getByText("消费接入调试"));
    await waitFor(() =>
      expect(screen.getByText("消费令牌已就绪（角色 consume）")).toBeInTheDocument(),
    );
    expect(screen.getByText(/令牌剩余 60 分钟/)).toBeInTheDocument();
  });

  it("消费令牌被清除（request 401 触发）后 UI 实时回到『需要消费令牌』（消费接入调试模式）", async () => {
    const user = userEvent.setup();
    mockedGetConsumeToken.mockReturnValue("fake-consume-jwt");
    mockedGetConsumeTokenExpiry.mockReturnValue(Date.now() + 3600000);
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    await user.click(screen.getByText("消费接入调试"));
    await waitFor(() =>
      expect(screen.getByText("消费令牌已就绪（角色 consume）")).toBeInTheDocument(),
    );
    // 模拟 request() 在 401 时 clearConsumeToken：localStorage 空 + 派发变更事件
    mockedGetConsumeToken.mockReturnValue(null);
    mockedGetConsumeTokenExpiry.mockReturnValue(null);
    fireEvent(window, new Event(CONSUME_TOKEN_CHANGED_EVENT));
    await waitFor(() => expect(screen.getByText("需要消费令牌")).toBeInTheDocument());
  });

  it("指标下拉按接入方授权域收敛：仅展示 scope_domain 内 + 白名单内（PII 需显式白名单）的 PUBLISHED 指标", async () => {
    const user = userEvent.setup();
    // 令牌已绑定 e2e_app → 切到调试模式时自动选中该客户端并按其授权范围收敛
    mockedGetConsumeTokenClientId.mockReturnValue("e2e_app");
    mockedListApiClients.mockResolvedValue([
      {
        client_id: "e2e_app",
        scope_domain: "outpatient",
        metric_whitelist: ["outp_patient_cnt", "outp_pii_ok"],
        qps: 100,
        daily_quota: 1000,
        status: "ACTIVE",
      },
    ]);
    mockedListMetrics.mockResolvedValue({
      items: [
        mkMetric("outp_patient_cnt", "患者数", "outpatient"), // 域内 + 白名单 → 展示
        mkMetric("outp_fee_day", "门诊费用", "outpatient"), // 域内但不在白名单 → 隐藏
        mkMetric("gmv_net", "净GMV", "sales"), // 域外 → 隐藏
        mkMetric("outp_pii_raw", "原始PII", "outpatient", true), // PII 未显式白名单 → 隐藏
        mkMetric("outp_pii_ok", "授权PII", "outpatient", true), // PII + 白名单 → 展示
      ],
      total: 5,
      page: 1,
      page_size: 100,
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    // 切到「消费接入调试」：令牌绑定 e2e_app → 自动选中并按授权域收敛
    await user.click(screen.getByText("消费接入调试"));
    // 服务端按 scope_domain 过滤
    await waitFor(() =>
      expect(mockedListMetrics).toHaveBeenCalledWith(expect.objectContaining({ domain: "outpatient" })),
    );
    await user.click(metricSelectInput());
    expect(await screen.findByText(/outp_patient_cnt · 患者数/)).toBeInTheDocument();
    expect(await screen.findByText(/outp_pii_ok · 授权PII/)).toBeInTheDocument();
    expect(screen.queryByText(/outp_fee_day · 门诊费用/)).not.toBeInTheDocument();
    expect(screen.queryByText(/gmv_net · 净GMV/)).not.toBeInTheDocument();
    expect(screen.queryByText(/outp_pii_raw · 原始PII/)).not.toBeInTheDocument();
  });

  it("指标查询模式：初始展示全部指标、无令牌/客户端 UI，切换后恢复收敛", async () => {
    const user = userEvent.setup();
    // 存在首个 ACTIVE 客户端（sales 域 + 白名单）且令牌未绑定——query 模式不被绑架
    mockedListApiClients.mockResolvedValue([
      {
        client_id: "e2e_app",
        scope_domain: "sales",
        metric_whitelist: ["sales_e2e_gmv_day"],
        qps: 20,
        daily_quota: 100000,
        status: "ACTIVE",
      },
    ]);
    mockedListMetrics.mockResolvedValue({
      items: [
        mkMetric("outp_visit_day", "门诊就诊量", "outpatient"),
        mkMetric("gmv_net", "净GMV", "sales"),
      ],
      total: 2,
      page: 1,
      page_size: 100,
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    // 指标查询模式：全量（不传 domain）+ 展示内部视角说明，不渲染令牌/客户端 UI
    expect(mockedListMetrics).toHaveBeenCalledWith(expect.objectContaining({ domain: undefined }));
    expect(screen.getByText("指标查询（平台内部视角）")).toBeInTheDocument();
    expect(screen.queryByText("需要消费令牌")).not.toBeInTheDocument();
    expect(screen.queryByText(/消费客户端/)).not.toBeInTheDocument();
    // 下拉可见全部已发布指标
    await user.click(metricSelectInput());
    expect(await screen.findByText(/outp_visit_day · 门诊就诊量/)).toBeInTheDocument();
    expect(await screen.findByText(/gmv_net · 净GMV/)).toBeInTheDocument();
    // 切到调试模式：出现令牌/客户端 UI
    await user.click(screen.getByText("消费接入调试"));
    await waitFor(() => expect(screen.getByText("需要消费令牌")).toBeInTheDocument());
    expect(screen.getByText(/消费客户端/)).toBeInTheDocument();
  });

  it("未选择/未绑定客户端时展示全部 PUBLISHED 指标，不被首个 ACTIVE 客户端授权范围绑架", async () => {
    const user = userEvent.setup();
    // DB 存在首个 ACTIVE 客户端（e2e_app，sales 域 + 白名单），但令牌未绑定 → 不收敛
    mockedListApiClients.mockResolvedValue([
      {
        client_id: "e2e_app",
        scope_domain: "sales",
        metric_whitelist: ["sales_e2e_gmv_day", "sales_e2e_ordercnt_day"],
        qps: 20,
        daily_quota: 100000,
        status: "ACTIVE",
      },
    ]);
    mockedListMetrics.mockResolvedValue({
      items: [
        mkMetric("outp_visit_day", "门诊就诊量", "outpatient"),
        mkMetric("gmv_net", "净GMV", "sales"),
      ],
      total: 2,
      page: 1,
      page_size: 100,
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    // 初始不传 domain（不收敛）——全部 PUBLISHED 指标可见
    expect(mockedListMetrics).toHaveBeenCalledWith(expect.objectContaining({ domain: undefined }));
    await user.click(metricSelectInput());
    expect(await screen.findByText(/outp_visit_day · 门诊就诊量/)).toBeInTheDocument();
    expect(await screen.findByText(/gmv_net · 净GMV/)).toBeInTheDocument();
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/lineage", "/query"]}>
        <Routes>
          <Route path="/lineage" element={<div>lineage-page</div>} />
          <Route path="/query" element={<QueryWorkspace />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("lineage-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/query"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/query" element={<QueryWorkspace />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });

  it("URL ?metric_code= 带参直达时自动选中该指标（详情页「试算」入口）", async () => {
    render(
      <MemoryRouter initialEntries={["/query?metric_code=gmv_net"]}>
        <QueryWorkspace />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    // 指标 Select 选中值显示为「编码 · 名称」，无需手动点选
    await waitFor(() => {
      expect(screen.getByText("gmv_net · 净GMV")).toBeInTheDocument();
    });
  });

  it("带参直达后直接 dry-run：使用 URL 指标编码发起语义校验", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/query?metric_code=gmv_net"]}>
        <QueryWorkspace />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /语义校验/ }));
    await waitFor(() => {
      expect(mockedConsumeDryRun).toHaveBeenCalledWith(
        expect.objectContaining({ metric_code: "gmv_net" }),
        { forceUser: true },
      );
    });
  });

  it("指标查询模式强制用户通道（forceUser=true），调试模式才放开消费令牌", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/query?metric_code=gmv_net"]}>
        <QueryWorkspace />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    // 默认「指标查询」模式：明确「内部用户 · 免令牌」标识
    expect(screen.getByText("内部用户 · 免令牌")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /语义校验/ }));
    await waitFor(() => {
      expect(mockedConsumeDryRun).toHaveBeenCalledWith(
        expect.objectContaining({ metric_code: "gmv_net" }),
        { forceUser: true },
      );
    });
    // 切到「消费接入调试」：标识变化，请求不再强制用户通道
    await user.click(screen.getByText("消费接入调试"));
    await waitFor(() => expect(screen.getByText("模拟接入方")).toBeInTheDocument());
    mockedConsumeDryRun.mockClear();
    await user.click(screen.getByRole("button", { name: /语义校验/ }));
    await waitFor(() => {
      expect(mockedConsumeDryRun).toHaveBeenCalledWith(
        expect.objectContaining({ metric_code: "gmv_net" }),
        { forceUser: false },
      );
    });
  });

  it("自定义日期范围：选「自定义」后展示 RangePicker，提交 YYYY-MM-DD~YYYY-MM-DD", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    // 先选指标（否则 dry-run 早退）
    await user.click(metricSelectInput());
    await user.click(await screen.findByText(/gmv_net · 净GMV/));
    // 选中「自定义」选项
    const rangeLabel = screen.getByText("日期范围", { selector: "label" });
    const rangeItem = rangeLabel.closest(".ant-form-item") as HTMLElement;
    await user.click(within(rangeItem).getByRole("combobox"));
    await user.click(await screen.findByText("自定义"));
    // RangePicker 出现（双输入框占位符）
    const startInput = await screen.findByPlaceholderText("开始日期");
    const endInput = screen.getByPlaceholderText("结束日期");
    await user.click(startInput);
    await user.keyboard("2026-08-01{Enter}");
    await user.click(endInput);
    await user.keyboard("2026-08-15{Enter}");
    // 输入框按 YYYY-MM-DD 数字格式展示（中文日期环境，不出现 Sep/Oct 英文月份）
    expect(startInput).toHaveValue("2026-08-01");
    expect(endInput).toHaveValue("2026-08-15");
    // 执行语义校验，断言提交的自定义区间
    await user.click(screen.getByRole("button", { name: /语义校验/ }));
    await waitFor(() => {
      expect(mockedConsumeDryRun).toHaveBeenCalledWith(
        expect.objectContaining({ date_range: "2026-08-01~2026-08-15" }),
        { forceUser: true },
      );
    });
  });

  it("SQL 查询 Tab：填写行数上限后提交带 limit；留空（不限）提交不带 limit", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    await user.click(screen.getByText("SQL 查询"));
    expect(await screen.findByText("数据源只读 SQL 查询")).toBeInTheDocument();
    // 选择数据源
    const dsLabel = screen.getByText("数据源", { selector: "label" });
    const dsItem = dsLabel.closest(".ant-form-item") as HTMLElement;
    await user.click(within(dsItem).getByRole("combobox"));
    await user.click(await screen.findByText(/mysql_unisense · 主库/));
    // 填写行数上限 50
    const limitLabel = screen.getByText("返回行数上限", { selector: "label" });
    const limitItem = limitLabel.closest(".ant-form-item") as HTMLElement;
    await user.clear(within(limitItem).getByRole("spinbutton"));
    await user.type(within(limitItem).getByRole("spinbutton"), "50");
    // 输入 SQL 并执行
    const sqlTextarea = screen.getByPlaceholderText(/SELECT \* FROM db\.table/);
    await user.clear(sqlTextarea);
    await user.type(sqlTextarea, "SELECT id FROM t");
    await user.click(screen.getByRole("button", { name: /执行 SQL/ }));
    await waitFor(() => {
      expect(mockedQueryDataSourceSql).toHaveBeenCalledWith(
        "mysql_unisense", "SELECT id FROM t", 50,
      );
    });
  });

  it("SQL 查询 Tab：选数据源 + 写 SQL + 执行 → 展示结果表", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    // 切到 SQL 查询 Tab
    await user.click(screen.getByText("SQL 查询"));
    expect(await screen.findByText("数据源只读 SQL 查询")).toBeInTheDocument();
    // 选择数据源（点击选项内容 div —— antd 下拉选项的完整 label）
    const dsLabel = screen.getByText("数据源", { selector: "label" });
    const dsItem = dsLabel.closest(".ant-form-item") as HTMLElement;
    await user.click(within(dsItem).getByRole("combobox"));
    await user.click(await screen.findByText(/mysql_unisense · 主库/));
    // 输入 SQL
    const sqlTextarea = screen.getByPlaceholderText(/SELECT \* FROM db\.table/);
    await user.clear(sqlTextarea);
    await user.type(sqlTextarea, "SELECT id FROM t WHERE x = 1");
    // 执行
    await user.click(screen.getByRole("button", { name: /执行 SQL/ }));
    await waitFor(() => {
      expect(mockedQueryDataSourceSql).toHaveBeenCalledWith(
        "mysql_unisense",
        "SELECT id FROM t WHERE x = 1",
        undefined,
      );
    });
    // 结果表展示
    expect(await screen.findByText(/查询成功：1 行/)).toBeInTheDocument();
    expect(screen.getByText("测试")).toBeInTheDocument();
  });

  it("SQL 查询 Tab：切换「每页条数」立即生效——45 行结果默认 20 条/页，切 100 条后一屏铺满", async () => {
    const user = userEvent.setup();
    // 45 行 > 默认每页 20 → 触发前端分页
    const rows = Array.from({ length: 45 }, (_, i) => ({ id: i + 1, name: `行 ${i + 1}` }));
    mockedQueryDataSourceSql.mockResolvedValue({
      columns: ["id", "name"],
      rows,
      total: 45,
      truncated: false,
      elapsed_ms: 5,
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    await user.click(screen.getByText("SQL 查询"));
    expect(await screen.findByText("数据源只读 SQL 查询")).toBeInTheDocument();
    const dsLabel = screen.getByText("数据源", { selector: "label" });
    const dsItem = dsLabel.closest(".ant-form-item") as HTMLElement;
    await user.click(within(dsItem).getByRole("combobox"));
    await user.click(await screen.findByText(/mysql_unisense · 主库/));
    const sqlTextarea = screen.getByPlaceholderText(/SELECT \* FROM db\.table/);
    await user.clear(sqlTextarea);
    await user.type(sqlTextarea, "SELECT id FROM t");
    await user.click(screen.getByRole("button", { name: /执行 SQL/ }));
    expect(await screen.findByText(/查询成功：45 行/)).toBeInTheDocument();

    const bodyRows = () => document.querySelectorAll(".ant-table-tbody .ant-table-row").length;
    // 默认每页 20 条：分页生效，首屏只渲染 20 行（而非 45 全量）
    await waitFor(() => expect(bodyRows()).toBe(20));

    // 打开「每页条数」选择器 → 选 100 条/页
    const sizeSel = document.querySelector(".ant-pagination-options .ant-select-selector") as HTMLElement;
    expect(sizeSel).toBeTruthy();
    await user.click(sizeSel);
    await user.click(await screen.findByRole("option", { name: /100/ }));
    // 切换后一屏铺满 45 行 —— 修复前 pageSize 受控恒 20，切 100 无反应（行数仍 20）
    await waitFor(() => expect(bodyRows()).toBe(45));
  });

  it("SQL 查询 Tab：USE 切换后展示当前库 Tag，未限定表名查询自动补前缀", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    await user.click(screen.getByText("SQL 查询"));
    await waitFor(() => expect(screen.getByText("数据源只读 SQL 查询")).toBeInTheDocument());
    // 选择数据源
    const dsLabel = screen.getByText("数据源", { selector: "label" });
    const dsItem = dsLabel.closest(".ant-form-item") as HTMLElement;
    await user.click(within(dsItem).getByRole("combobox"));
    await user.click(await screen.findByText(/mysql_unisense · 主库/));
    const sqlTextarea = screen.getByPlaceholderText(/SELECT \* FROM db\.table/);
    // 执行 USE ssb → 后端写会话并返回 current_db
    mockedQueryDataSourceSql.mockResolvedValueOnce({
      columns: [], rows: [], total: 0, truncated: false, elapsed_ms: 1,
      current_db: "ssb", note: "已切换到库 ssb",
    });
    await user.clear(sqlTextarea);
    await user.type(sqlTextarea, "USE ssb");
    await user.click(screen.getByRole("button", { name: /执行 SQL/ }));
    // 「当前库」Tag 出现（strong.mono 直接文本是库名，唯一匹配）
    expect(await screen.findByText("ssb", { selector: "strong.mono" })).toBeInTheDocument();
    expect(screen.getByText(/^当前库：/)).toBeInTheDocument();
    // 后续未限定表名查询照常提交（后端自动补前缀）
    mockedQueryDataSourceSql.mockResolvedValueOnce({
      columns: ["id"], rows: [{ id: 1 }], total: 1, truncated: false, elapsed_ms: 2,
      current_db: "ssb",
    });
    await user.clear(sqlTextarea);
    await user.type(sqlTextarea, "SELECT id FROM customer");
    await user.click(screen.getByRole("button", { name: /执行 SQL/ }));
    await waitFor(() => {
      expect(mockedQueryDataSourceSql).toHaveBeenLastCalledWith(
        "mysql_unisense", "SELECT id FROM customer", undefined,
      );
    });
  });

  it("SQL 查询 Tab：SHOW DATABASES 结果点击库名 → 自动执行 USE 切换当前库", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());
    await user.click(screen.getByText("SQL 查询"));
    await waitFor(() => expect(screen.getByText("数据源只读 SQL 查询")).toBeInTheDocument());
    const dsLabel = screen.getByText("数据源", { selector: "label" });
    const dsItem = dsLabel.closest(".ant-form-item") as HTMLElement;
    await user.click(within(dsItem).getByRole("combobox"));
    await user.click(await screen.findByText(/mysql_unisense · 主库/));
    const sqlTextarea = screen.getByPlaceholderText(/SELECT \* FROM db\.table/);
    // SHOW DATABASES → 结果首列库名可点击
    mockedQueryDataSourceSql.mockResolvedValueOnce({
      columns: ["Database"], rows: [{ Database: "ssb" }, { Database: "sales" }],
      total: 2, truncated: false, elapsed_ms: 3,
    });
    await user.clear(sqlTextarea);
    await user.type(sqlTextarea, "SHOW DATABASES");
    await user.click(screen.getByRole("button", { name: /执行 SQL/ }));
    // antd Button 文本包在 <span> 内，getByText(selector) 匹配直接文本会落空 → 用 role
    const ssbLink = await screen.findByRole("button", { name: /ssb/ });
    expect(ssbLink).toBeInTheDocument();
    // 点击库名 → 自动 USE `ssb`
    mockedQueryDataSourceSql.mockResolvedValueOnce({
      columns: [], rows: [], total: 0, truncated: false, elapsed_ms: 1,
      current_db: "ssb", note: "已切换到库 ssb",
    });
    await user.click(ssbLink);
    await waitFor(() => {
      expect(mockedQueryDataSourceSql).toHaveBeenLastCalledWith(
        "mysql_unisense", "USE `ssb`", undefined,
      );
    });
    expect(await screen.findByText("ssb", { selector: "strong.mono" })).toBeInTheDocument();
  });
});
