import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { Glossary } from "../pages/Glossary";
import type { GlossaryTerm } from "../types";

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
    listTerms: vi.fn(),
    createTerm: vi.fn(),
    submitTerm: vi.fn(),
    deprecateTerm: vi.fn(),
    listTermConflicts: vi.fn(),
    resolveTermConflict: vi.fn(),
    UnisenseApiError,
  };
});

import { listTerms, listTermConflicts } from "../api";

const mockedList = vi.mocked(listTerms);
const mockedConflicts = vi.mocked(listTermConflicts);

const TERMS: GlossaryTerm[] = [
  {
    id: 1,
    term_code: "GMV",
    name: "成交总额",
    definition: "一定周期内成交订单金额总和",
    domain: "finance",
    synonyms: ["gross merchandise volume"],
    boundary: null,
    status: "DRAFT",
    owner_id: 1,
    version: 1,
    created_at: "2026-08-13T00:00:00",
    updated_at: "2026-08-13T00:00:00",
  },
  {
    id: 2,
    term_code: "AOV",
    name: "客单价",
    definition: "成交总额除以订单数",
    domain: "finance",
    synonyms: [],
    boundary: null,
    status: "DEPRECATED",
    owner_id: 1,
    version: 2,
    created_at: "2026-08-13T00:00:00",
    updated_at: "2026-08-13T00:00:00",
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue({ items: TERMS, total: 2, page: 1, page_size: 20 });
  mockedConflicts.mockResolvedValue({ items: [], total: 0 });
});

describe("Glossary 页面", () => {
  it("从全局搜索 ?kw=xxx 直达：所有查询都携带关键词过滤（避免全量首查竞态覆盖）", async () => {
    render(
      <MemoryRouter initialEntries={["/glossary?kw=GMV"]}>
        <Glossary />
      </MemoryRouter>,
    );

    await screen.findAllByText("共 2 条");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ search: "GMV" });
    }
  });

  it("URL 直达时搜索框预填关键词（?kw=）", async () => {
    render(
      <MemoryRouter initialEntries={["/glossary?kw=GMV"]}>
        <Glossary />
      </MemoryRouter>,
    );

    const input = await screen.findByPlaceholderText("搜索术语名/定义/编码");
    expect((input as HTMLInputElement).value).toBe("GMV");
  });

  it("防竞态：迟到的首查响应不覆盖最新筛选结果", async () => {
    type TermListResponse = { items: GlossaryTerm[]; total: number; page: number; page_size: number };
    let resolveFull!: (v: TermListResponse) => void;
    const fullPromise = new Promise<TermListResponse>((r) => {
      resolveFull = r;
    });
    // 首查（挂起）；随后切换状态触发二次查询立即返回 2；兜底返回 8
    mockedList.mockImplementationOnce(() => fullPromise);
    mockedList.mockResolvedValueOnce({ items: TERMS, total: 2, page: 1, page_size: 20 });
    mockedList.mockResolvedValue({ items: [], total: 8, page: 1, page_size: 20 });

    render(
      <MemoryRouter initialEntries={["/glossary?kw=GMV"]}>
        <Glossary />
      </MemoryRouter>,
    );

    await screen.findByText("全部状态");
    fireEvent.mouseDown(screen.getByText("全部状态"));
    const published = await screen.findByText("已发布");
    fireEvent.click(published);

    await screen.findAllByText("共 2 条");

    // 迟到的首查此刻才返回：若被应用会覆盖筛选结果（total 变 8）
    resolveFull({ items: [], total: 8, page: 1, page_size: 20 });
    await screen.findAllByText("共 2 条");
    expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ status: "PUBLISHED" }));
  });

  it("SPA 内 URL 关键词变化时重新按新关键词查询", async () => {
    function JumpBtn() {
      const navigate = useNavigate();
      return <button onClick={() => navigate("/glossary?kw=AOV")}>跳到AOV</button>;
    }
    render(
      <MemoryRouter initialEntries={["/glossary?kw=GMV"]}>
        <JumpBtn />
        <Glossary />
      </MemoryRouter>,
    );

    await screen.findAllByText("共 2 条");
    fireEvent.click(screen.getByText("跳到AOV"));
    await waitFor(() => {
      expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ search: "AOV" }));
    });
  });
});
