import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter, useNavigate, Routes, Route } from "react-router-dom";
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
    getTerm: vi.fn(),
    updateTerm: vi.fn(),
    createTermRelation: vi.fn(),
    listTermRelations: vi.fn(),
    submitTerm: vi.fn(),
    deprecateTerm: vi.fn(),
    batchSubmitTerms: vi.fn(),
    batchDeprecateTerms: vi.fn(),
    inferTermSuggestion: vi.fn(),
    listDomainTree: vi.fn(),
    listTermConflicts: vi.fn(),
    resolveTermConflict: vi.fn(),
    listFavorites: vi.fn(),
    addFavorite: vi.fn(),
    removeFavorite: vi.fn(),
    UnisenseApiError,
  };
});

import {
  listTerms,
  listTermConflicts,
  getTerm,
  updateTerm,
  createTermRelation,
  listTermRelations,
  listFavorites,
  batchSubmitTerms,
  batchDeprecateTerms,
  inferTermSuggestion,
  listDomainTree,
} from "../api";

const mockedList = vi.mocked(listTerms);
const mockedConflicts = vi.mocked(listTermConflicts);
const mockedGet = vi.mocked(getTerm);
const mockedListFavorites = vi.mocked(listFavorites);
const mockedUpdate = vi.mocked(updateTerm);
const mockedRelation = vi.mocked(createTermRelation);
const mockedListRelations = vi.mocked(listTermRelations);
const mockedBatchSubmit = vi.mocked(batchSubmitTerms);
const mockedBatchDeprecate = vi.mocked(batchDeprecateTerms);
const mockedInfer = vi.mocked(inferTermSuggestion);
const mockedDomainTree = vi.mocked(listDomainTree);

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
  mockedListFavorites.mockResolvedValue([]);
  mockedDomainTree.mockResolvedValue([]);
  mockedListRelations.mockResolvedValue([]);
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

  it("从总览仪表 ?status=xxx 直达：所有查询都携带状态过滤（资产卡片下钻）", async () => {
    render(
      <MemoryRouter initialEntries={["/glossary?status=PUBLISHED"]}>
        <Glossary />
      </MemoryRouter>,
    );

    await screen.findAllByText("共 2 条");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ status: "PUBLISHED" });
    }
  });

  it("从总览仪表 Owner 责任分布 ?owner_id= 直达：所有查询都携带责任人过滤", async () => {
    render(
      <MemoryRouter initialEntries={["/glossary?owner_id=1"]}>
        <Glossary />
      </MemoryRouter>,
    );

    await screen.findAllByText("共 2 条");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ owner_id: 1 });
    }
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

  it("详情：点击详情按钮拉取最新术语完整信息并展示", async () => {
    mockedGet.mockResolvedValue({ ...TERMS[0], version: 3, created_at: "2026-08-01T00:00:00" });
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );

    await screen.findByText("成交总额");
    // 表格行内每个术语都有详情按钮，取第一行（GMV）
    fireEvent.click(screen.getAllByText("详情")[0]);

    await screen.findByText("术语详情：GMV");
    // 详情弹窗展示完整字段（含列外字段 owner/版本）
    expect(screen.getByText("Owner ID")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
    expect(mockedGet).toHaveBeenCalledWith("GMV");
  });

  it("编辑：打开编辑弹窗回填当前值，提交时调用 updateTerm", async () => {
    mockedUpdate.mockResolvedValue(TERMS[0]);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );

    await screen.findByText("成交总额");
    fireEvent.click(screen.getAllByText("编辑")[0]);

    await screen.findByText("编辑术语：GMV");
    const nameInput = screen.getByDisplayValue("成交总额");
    fireEvent.change(nameInput, { target: { value: "成交总额(修订)" } });
    fireEvent.click(screen.getByText(/保\s*存/));

    await waitFor(() => {
      expect(mockedUpdate).toHaveBeenCalledWith(
        "GMV",
        expect.objectContaining({ name: "成交总额(修订)" }),
      );
    });
  });

  it("关系管理：提交时调用 createTermRelation 建立术语关系", async () => {
    mockedRelation.mockResolvedValue({
      id: 1,
      source_term_id: 1,
      target_term_id: 2,
      relation_type: "RELATED_TO",
      declared_by: null,
      source_type: "MANUAL",
      confirmed_at: null,
    });
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );

    await screen.findByText("成交总额");
    fireEvent.click(screen.getAllByRole("button", { name: /建\s*立\s*关\s*系/ })[0]);

    await screen.findByText("建立关系：GMV");
    // 关联目标术语（Select 搜索：下拉选项来自 listTerms；按编码/名称搜索，无需手输 ID）+ 关系类型
    fireEvent.mouseDown(screen.getByLabelText("关联目标术语"));
    await screen.findByText("AOV - 客单价");
    fireEvent.click(screen.getByText("AOV - 客单价"));
    fireEvent.mouseDown(screen.getByText("相关（RELATED_TO）"));
    // ok 按钮 antd 双字加空格「建 立」，精确匹配建立关系弹窗的提交按钮（不撞「建立关系」行内按钮）
    fireEvent.click(screen.getByRole("button", { name: "建 立" }));

    await waitFor(() => {
      expect(mockedRelation).toHaveBeenCalledWith(
        "GMV",
        expect.objectContaining({ target_term_id: 2, relation_type: "RELATED_TO" }),
      );
    });
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findAllByText("共 2 条");
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/lineage", "/glossary"]}>
        <Routes>
          <Route path="/lineage" element={<div>lineage-page</div>} />
          <Route path="/glossary" element={<Glossary />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findAllByText("共 2 条");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("lineage-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/glossary" element={<Glossary />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findAllByText("共 2 条");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });

  it("编辑弹窗支持编辑术语编码（updateTerm 携带 term_code）", async () => {
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(screen.getAllByText("编辑")[0]);
    await screen.findByText("编辑术语：GMV");
    // 编码字段已回填且可编辑
    const codeInput = screen.getByLabelText("术语编码") as HTMLInputElement;
    expect(codeInput.value).toBe("GMV");
    fireEvent.change(codeInput, { target: { value: "GMV_V2" } });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockedUpdate).toHaveBeenCalledWith(
        "GMV",
        expect.objectContaining({ term_code: "GMV_V2" }),
      );
    });
  });

  it("新建弹窗：AI 推断根据名称生成定义/同义词/边界并回填", async () => {
    mockedInfer.mockResolvedValue({
      definition: "成交总额是某周期内订单金额合计",
      synonyms: ["GMV", "gross merchandise volume"],
      boundary: "不含退款订单",
      confidence: 0.9,
    });
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findAllByText("共 2 条");
    fireEvent.click(screen.getAllByText("新建术语")[0]);
    await screen.findByPlaceholderText("如 成交总额");
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "成交总额" } });
    fireEvent.click(screen.getByText(/根据名称生成定义/));
    await waitFor(() => {
      expect(mockedInfer).toHaveBeenCalledWith("成交总额");
    });
    // 回填定义/同义词/边界
    await screen.findByDisplayValue("成交总额是某周期内订单金额合计");
    expect((screen.getByLabelText("同义词（逗号分隔）") as HTMLInputElement).value).toBe(
      "GMV, gross merchandise volume",
    );
  });

  it("新建弹窗：业务域为下拉选择（来自主题域树，不手造）", async () => {
    mockedDomainTree.mockResolvedValue([
      {
        id: 1, code: "finance", name: "财务域", parent_id: null, level: 1,
        sort_order: 0, status: "active", metric_count: 0, children: [],
      },
    ]);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findAllByText("共 2 条");
    fireEvent.click(screen.getAllByText("新建术语")[0]);
    await screen.findByPlaceholderText("如 成交总额");
    fireEvent.mouseDown(screen.getByLabelText("业务域"));
    await screen.findByText("财务域（finance）");
    fireEvent.click(screen.getByText("财务域（finance）"));
    // 选中后选项文本作为选中值保留（dropdown 关闭后仍可见）
    await waitFor(() => {
      expect(screen.getAllByText("财务域（finance）").length).toBeGreaterThan(0);
    });
  });

  it("批量操作：勾选行后批量发布按钮可用，提交调用 batchSubmitTerms", async () => {
    mockedBatchSubmit.mockResolvedValue([{ term_code: "GMV", ok: true, status: "PUBLISHED" }]);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findAllByText("共 2 条");
    const submitBtn = screen.getByRole("button", { name: /批量发布/ }) as HTMLButtonElement;
    expect(submitBtn.disabled).toBe(true);
    // 勾选第一行（[0] 是表头全选，[1] 是第一行）
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    expect(submitBtn.disabled).toBe(false);
    fireEvent.click(submitBtn);
    // 可控确认弹窗（文本被 <b> 数字拆分，用前缀匹配；ok 按钮 antd 双字加空格）
    await screen.findByText(/确定发布选中的/);
    fireEvent.click(screen.getByRole("button", { name: "发 布" }));
    await waitFor(() => {
      expect(mockedBatchSubmit).toHaveBeenCalledWith(["GMV"]);
    });
  });

  it("批量废弃：勾选行 → 确认弹窗 → 调用 batchDeprecateTerms", async () => {
    mockedBatchDeprecate.mockResolvedValue([
      { term_code: "GMV", ok: true, status: "DEPRECATED" },
    ]);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findAllByText("共 2 条");
    const depBtn = screen.getByRole("button", { name: /批量废弃/ }) as HTMLButtonElement;
    expect(depBtn.disabled).toBe(true);
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    expect(depBtn.disabled).toBe(false);
    fireEvent.click(depBtn);
    await screen.findByText(/确定废弃选中的/);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "废 弃" }));
    await waitFor(() => {
      expect(mockedBatchDeprecate).toHaveBeenCalledWith(["GMV"]);
    });
  });

  it("已废弃术语显示「再次发布」按钮（状态流程：废弃后可重新发布）", async () => {
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("客单价");
    // TERMS[1] 是 DEPRECATED → 应显示「再次发布」
    const buttons = screen.getAllByRole("button", { name: /再次发布/ });
    expect(buttons.length).toBeGreaterThan(0);
  });

  it("关系图谱：点击「关系」查看该术语的上游/下游关系", async () => {
    mockedListRelations.mockResolvedValue([
      {
        relation_type: "SYNONYM_OF",
        direction: "outgoing",
        peer: { id: 3, term_code: "GMV_TOTAL", name: "总成交额", domain: "finance", status: "PUBLISHED" },
      },
      {
        relation_type: "BROADER_THAN",
        direction: "incoming",
        peer: { id: 4, term_code: "GMV_CN", name: "成交额(中国)", domain: "finance", status: "PUBLISHED" },
      },
    ]);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(screen.getAllByText("关系")[0]);

    // 弹窗标题 + 中心术语 + 下游/上游分组
    await screen.findByText("术语关系图谱：GMV");
    expect(screen.getByText("▲ 上游（引用本术语）")).toBeTruthy();
    expect(screen.getByText("▼ 下游（本术语引用）")).toBeTruthy();
    expect(screen.getByText("总成交额")).toBeTruthy();
    expect(screen.getByText("成交额(中国)")).toBeTruthy();
    // 关系类型 Tag
    expect(screen.getByText(/同义（SYNONYM_OF）/)).toBeTruthy();
    expect(screen.getByText(/上位（BROADER_THAN）/)).toBeTruthy();
    // 调用接口
    expect(mockedListRelations).toHaveBeenCalledWith("GMV");
  });

  it("关系图谱：无关系时展示空态，且可从图谱弹窗进入「建立关系」", async () => {
    mockedListRelations.mockResolvedValue([]);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(screen.getAllByText("关系")[0]);

    await screen.findByText("术语关系图谱：GMV");
    expect(screen.getByText(/暂无关联术语/)).toBeTruthy();
    // 从图谱弹窗内点「建立关系」→ 打开建立关系弹窗（限定在弹窗容器内）
    const graphDialog = await screen.findByRole("dialog");
    fireEvent.click(within(graphDialog).getByRole("button", { name: /建\s*立\s*关\s*系/ }));
    await screen.findByText("建立关系：GMV");
  });
});
