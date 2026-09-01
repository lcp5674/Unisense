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
    mintClientToken: vi.fn(),
    getConsumeToken: vi.fn(() => null),
    setConsumeToken: vi.fn(),
    clearConsumeToken: vi.fn(),
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
} from "../api";
const mockedConsumeDryRun = vi.mocked(consumeDryRun);
const mockedConsumeQuery = vi.mocked(consumeQuery);
const mockedConsumeSemantic = vi.mocked(consumeSemantic);
const mockedListMetrics = vi.mocked(listMetrics);

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

// 定位「指标」Form.Item 内的 Select 搜索输入框（antd 不把 placeholder 放到 input 上）
function metricSelectInput(): HTMLInputElement {
  const labelEl = screen.getByText("指标", { selector: "label" });
  const itemEl = labelEl.closest(".ant-form-item") as HTMLElement;
  return within(itemEl).getByRole("combobox") as HTMLInputElement;
}

describe("QueryWorkspace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
      expect(mockedConsumeSemantic).toHaveBeenCalledWith("gmv_net");
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

  it("未签发消费令牌时给出签发引导而非技术错误码", async () => {
    const user = userEvent.setup();
    mockedConsumeQuery.mockRejectedValue(
      new UnisenseApiError("X-Api-Key 格式应为 client_id:secret", "AUTH_APIKEY_INVALID", 401, "trace-test"),
    );
    renderPage();
    await waitFor(() => expect(screen.getByText("查询工作台")).toBeInTheDocument());

    await user.click(metricSelectInput());
    await user.click(await screen.findByText(/gmv_net · 净GMV/));

    await user.click(screen.getByRole("button", { name: /查\s*询/ }));

    await waitFor(() => {
      expect(screen.getByText(/需要消费令牌：请点击上方『从客户端签发令牌』后重试/)).toBeInTheDocument();
    });
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
      );
    });
  });
});
