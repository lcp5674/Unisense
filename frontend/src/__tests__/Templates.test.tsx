import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { Templates } from "../pages/Templates";
import type { MetricTemplate } from "../types";

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
  }
  return {
    listTemplates: vi.fn(),
    createMetric: vi.fn(),
    UnisenseApiError,
  };
});
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import { listTemplates } from "../api";

const mockedList = vi.mocked(listTemplates);

const TPLS: MetricTemplate[] = [
  {
    id: 1,
    code: "tpl_gmv_daily",
    name: "GMV 日汇总模板",
    domain: "finance",
    description: "按日汇总 GMV",
    defaults_json: { aggregation: "SUM" },
    required_fields: ["metric_code"],
    type: "atomic",
    granularity: "daily",
    unit: "元",
    aggregation: "SUM",
    time_semantics: "PERIOD",
    freshness: "T1",
    dw_layer: "DWS",
    serving_mode: "BATCH_ONLY",
    additivity: "ADDITIVE",
    metric_tier: "T1",
    is_active: true,
    created_by: 1,
  },
  {
    id: 2,
    code: "tpl_aov_weekly",
    name: "客单价周模板",
    domain: "finance",
    description: "按周汇总客单价",
    defaults_json: { aggregation: "AVG" },
    required_fields: ["metric_code"],
    type: "atomic",
    granularity: "weekly",
    unit: "元",
    aggregation: "AVG",
    time_semantics: "PERIOD",
    freshness: "T1",
    dw_layer: "DWS",
    serving_mode: "BATCH_ONLY",
    additivity: "ADDITIVE",
    metric_tier: "T1",
    is_active: true,
    created_by: 1,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue(TPLS);
});

describe("Templates 页面", () => {
  it("从全局搜索 ?kw=xxx 直达：所有查询都携带关键词过滤（避免全量首查竞态覆盖）", async () => {
    render(
      <MemoryRouter initialEntries={["/templates?kw=GMV"]}>
        <Templates />
      </MemoryRouter>,
    );

    await screen.findByText("tpl_gmv_daily");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ is_active: true, keyword: "GMV" });
    }
  });

  it("URL 直达时搜索框预填关键词（?kw=）", async () => {
    render(
      <MemoryRouter initialEntries={["/templates?kw=GMV"]}>
        <Templates />
      </MemoryRouter>,
    );

    const input = await screen.findByPlaceholderText("搜索模板编码 / 名称 / 描述");
    expect((input as HTMLInputElement).value).toBe("GMV");
  });

  it("防竞态：迟到的首查响应不覆盖最新筛选结果", async () => {
    let resolveFull!: (v: MetricTemplate[]) => void;
    const fullPromise = new Promise<MetricTemplate[]>((r) => {
      resolveFull = r;
    });
    // 首查（挂起）；随后输入关键词触发二次查询立即返回 1 条；兜底返回全量 2 条
    mockedList.mockImplementationOnce(() => fullPromise);
    mockedList.mockResolvedValueOnce([TPLS[0]]);
    mockedList.mockResolvedValue(TPLS);

    render(
      <MemoryRouter>
        <Templates />
      </MemoryRouter>,
    );

    // 首查挂起，搜索框可用后输入关键词触发二次查询
    const searchInput = await screen.findByPlaceholderText("搜索模板编码 / 名称 / 描述");
    fireEvent.change(searchInput, { target: { value: "GMV" } });
    await screen.findByText("tpl_gmv_daily");

    // 迟到的首查此刻才返回：若被应用会覆盖筛选结果（tpl_aov_weekly 也会出现）
    resolveFull(TPLS);
    // 先给 React 处理迟到响应的时间，再断言未被覆盖（避免 waitFor 在更新前假绿）
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("tpl_aov_weekly")).toBeNull();
    expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ keyword: "GMV" }));
  });

  it("SPA 内 URL 关键词变化时重新按新关键词查询", async () => {
    function JumpBtn() {
      const navigate = useNavigate();
      return <button onClick={() => navigate("/templates?kw=AOV")}>跳到AOV</button>;
    }
    render(
      <MemoryRouter initialEntries={["/templates?kw=GMV"]}>
        <JumpBtn />
        <Templates />
      </MemoryRouter>,
    );

    await screen.findByText("tpl_gmv_daily");
    fireEvent.click(screen.getByText("跳到AOV"));
    await waitFor(() => {
      expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ keyword: "AOV" }));
    });
  });
});
