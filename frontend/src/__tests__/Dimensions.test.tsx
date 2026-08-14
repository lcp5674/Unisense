import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { Dimensions } from "../pages/Dimensions";
import type { Dimension } from "../types";

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
    listDimensions: vi.fn(),
    createDimension: vi.fn(),
    getDimension: vi.fn(),
    updateDimension: vi.fn(),
    publishDimension: vi.fn(),
    deprecateDimension: vi.fn(),
    bindMetricDimension: vi.fn(),
    listMetricDimensions: vi.fn(),
    listDimensionMappings: vi.fn(),
    createDimensionMapping: vi.fn(),
    listReconciliations: vi.fn(),
    submitReconciliation: vi.fn(),
    reviewReconciliation: vi.fn(),
    listDimensionMembers: vi.fn(),
    createDimensionMember: vi.fn(),
    listMetrics: vi.fn(),
    UnisenseApiError,
  };
});

import { listDimensions, listMetrics, getDimension, updateDimension, bindMetricDimension } from "../api";

const mockedList = vi.mocked(listDimensions);

const DIMS: Dimension[] = [
  {
    id: 1,
    dim_code: "dim_channel",
    name: "渠道",
    domain: "finance",
    type: "SCD1",
    description: "渠道维度",
    owner_id: 1,
    status: "PUBLISHED",
    created_at: "2026-08-01T00:00:00",
    updated_at: "2026-08-01T00:00:00",
  },
  {
    id: 2,
    dim_code: "dim_region",
    name: "区域",
    domain: "finance",
    type: "SCD1",
    description: "区域维度",
    owner_id: 1,
    status: "DRAFT",
    created_at: "2026-08-01T00:00:00",
    updated_at: "2026-08-01T00:00:00",
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue({ items: DIMS, total: 2 });
  // 维度列表 Tab 挂载即拉取指标候选（绑定指标下拉），默认返回空列表
  vi.mocked(listMetrics).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 });
});

describe("Dimensions 页面", () => {
  it("从全局搜索 ?kw=xxx 直达：所有查询都携带关键词过滤（避免全量首查竞态覆盖）", async () => {
    render(
      <MemoryRouter initialEntries={["/dimensions?kw=渠道"]}>
        <Dimensions />
      </MemoryRouter>,
    );

    await screen.findByText("dim_channel");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ keyword: "渠道" });
    }
  });

  it("URL 直达时搜索框预填关键词（?kw=）", async () => {
    render(
      <MemoryRouter initialEntries={["/dimensions?kw=渠道"]}>
        <Dimensions />
      </MemoryRouter>,
    );

    const input = await screen.findByPlaceholderText("搜索维度编码 / 名称 / 描述");
    expect((input as HTMLInputElement).value).toBe("渠道");
  });

  it("防竞态：迟到的首查响应不覆盖最新筛选结果", async () => {
    type DimListResponse = { items: Dimension[]; total: number };
    let resolveFull!: (v: DimListResponse) => void;
    const fullPromise = new Promise<DimListResponse>((r) => {
      resolveFull = r;
    });
    // 首查（挂起）；随后输入关键词触发二次查询立即返回 1 条；兜底返回全量 2 条
    mockedList.mockImplementationOnce(() => fullPromise);
    mockedList.mockResolvedValueOnce({ items: [DIMS[0]], total: 1 });
    mockedList.mockResolvedValue({ items: DIMS, total: 2 });

    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    // 首查挂起，搜索框可用后输入关键词触发二次查询
    const searchInput = await screen.findByPlaceholderText("搜索维度编码 / 名称 / 描述");
    fireEvent.change(searchInput, { target: { value: "渠道" } });
    await screen.findByText("dim_channel");

    // 迟到的首查此刻才返回：若被应用会覆盖筛选结果（dim_region 也会出现）
    resolveFull({ items: DIMS, total: 2 });
    // 先给 React 处理迟到响应的时间，再断言未被覆盖（避免 waitFor 在更新前假绿）
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("dim_region")).toBeNull();
    expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ keyword: "渠道" }));
  });

  it("SPA 内 URL 关键词变化时重新按新关键词查询", async () => {
    function JumpBtn() {
      const navigate = useNavigate();
      return <button onClick={() => navigate("/dimensions?kw=区域")}>跳到区域</button>;
    }
    render(
      <MemoryRouter initialEntries={["/dimensions?kw=渠道"]}>
        <JumpBtn />
        <Dimensions />
      </MemoryRouter>,
    );

    await screen.findByText("dim_channel");
    fireEvent.click(screen.getByText("跳到区域"));
    await waitFor(() => {
      expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ keyword: "区域" }));
    });
  });

  it("编辑维度：点击编辑打开预填表单，保存调用 updateDimension 并刷新列表", async () => {
    const user = userEvent.setup();
    vi.mocked(getDimension).mockResolvedValue(DIMS[1]);
    vi.mocked(updateDimension).mockResolvedValue({ ...DIMS[1], name: "区域（新）" });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    await screen.findByText("dim_region");
    const row = screen.getByText("dim_region").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /编\s*辑/ }));

    // Modal 打开且从详情端点拉取最新值预填
    await waitFor(() => {
      expect(screen.getByText(/编辑维度：dim_region/)).toBeInTheDocument();
      expect(getDimension).toHaveBeenCalledWith("dim_region");
    });
    const nameInput = screen.getByLabelText("名称") as HTMLInputElement;
    expect(nameInput.value).toBe("区域");

    await user.clear(nameInput);
    await user.type(nameInput, "区域（新）");
    await user.click(screen.getByRole("button", { name: /保\s*存/ }));

    await waitFor(() => {
      expect(updateDimension).toHaveBeenCalledWith(
        "dim_region",
        expect.objectContaining({ name: "区域（新）", domain: "finance", type: "SCD1" }),
      );
    });
    // 保存成功后重新拉取列表
    await waitFor(() => {
      expect(mockedList.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it("绑定指标：选择指标后调用 bindMetricDimension", async () => {
    const user = userEvent.setup();
    vi.mocked(listMetrics).mockResolvedValue({
      items: [{ id: 10, metric_code: "sales_gmv_day", name: "GMV" } as any],
      total: 1,
      page: 1,
      page_size: 200,
    });
    vi.mocked(bindMetricDimension).mockResolvedValue({
      id: 1,
      metric_id: 10,
      dim_code: "dim_channel",
      role: "filter",
      default_member: null,
    });
    render(
      <MemoryRouter>
        <Dimensions />
      </MemoryRouter>,
    );

    await screen.findByText("dim_channel");
    const row = screen.getByText("dim_channel").closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "绑定指标" }));

    await waitFor(() => {
      expect(screen.getByText(/绑定指标 → dim_channel/)).toBeInTheDocument();
    });
    const dialog = screen.getByRole("dialog");
    const metricItem = within(dialog).getByText("指标").closest(".ant-form-item") as HTMLElement;
    fireEvent.mouseDown(within(metricItem).getByRole("combobox"));
    await user.click(await screen.findByText("sales_gmv_day · GMV"));
    await user.click(within(dialog).getByRole("button", { name: /绑\s*定/ }));

    await waitFor(() => {
      expect(bindMetricDimension).toHaveBeenCalledWith(
        expect.objectContaining({ metric_id: 10, dim_code: "dim_channel", role: "filter" }),
      );
    });
  });
});
