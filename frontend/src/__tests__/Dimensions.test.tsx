import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
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
    publishDimension: vi.fn(),
    deprecateDimension: vi.fn(),
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

import { listDimensions } from "../api";

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
});
