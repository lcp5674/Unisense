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
    publishTerm: vi.fn(),
    approveTerm: vi.fn(),
    rejectTerm: vi.fn(),
    deprecateTerm: vi.fn(),
    reactivateTerm: vi.fn(),
    deleteTerm: vi.fn(),
    restoreTerm: vi.fn(),
    batchSubmitTerms: vi.fn(),
    batchPublishTerms: vi.fn(),
    batchApproveTerms: vi.fn(),
    batchRejectTerms: vi.fn(),
    batchDeprecateTerms: vi.fn(),
    batchReactivateTerms: vi.fn(),
    batchDeleteTerms: vi.fn(),
    inferTermSuggestion: vi.fn(),
    listDomainTree: vi.fn(),
    listUsers: vi.fn(),
    listTermConflicts: vi.fn(),
    resolveTermConflict: vi.fn(),
    listFavorites: vi.fn(),
    addFavorite: vi.fn(),
    removeFavorite: vi.fn(),
    fetchCurrentUser: vi.fn(),
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
  listUsers,
  fetchCurrentUser,
  approveTerm,
  rejectTerm,
  submitTerm,
  reactivateTerm,
  deleteTerm,
  restoreTerm,
  batchReactivateTerms,
  batchDeleteTerms,
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
const mockedFetchCurrentUser = vi.mocked(fetchCurrentUser);
const mockedUsers = vi.mocked(listUsers);
const mockedApprove = vi.mocked(approveTerm);
const mockedReject = vi.mocked(rejectTerm);
const mockedSubmit = vi.mocked(submitTerm);
const mockedReactivate = vi.mocked(reactivateTerm);
const mockedDelete = vi.mocked(deleteTerm);
const mockedRestore = vi.mocked(restoreTerm);
const mockedBatchReactivate = vi.mocked(batchReactivateTerms);
const mockedBatchDelete = vi.mocked(batchDeleteTerms);

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
  mockedUsers.mockResolvedValue([]);
  mockedDomainTree.mockResolvedValue([]);
  mockedListRelations.mockResolvedValue({ items: [], total: 0 });
  mockedFetchCurrentUser.mockResolvedValue({
    id: 1, username: "admin", display_name: "管理员", role: "platform_admin", domain: "finance", org_id: 1,
  });
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

  it("批量操作：勾选行后「批量提交审核」可用，确认弹窗提交调用 batchSubmitTerms", async () => {
    mockedBatchSubmit.mockResolvedValue({
      results: [{ code: "GMV", ok: true, message: "" }],
      ok_count: 1,
      fail_count: 0,
    });
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findAllByText("共 2 条");
    const batchBtn = screen.getByRole("button", { name: /批量操作/ }) as HTMLButtonElement;
    expect(batchBtn.disabled).toBe(true);
    // 勾选第一行（[0] 是表头全选，[1] 是第一行）
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    expect(batchBtn.disabled).toBe(false);
    fireEvent.click(batchBtn);
    // Dropdown 菜单 → 批量提交审核（草稿）
    fireEvent.click(await screen.findByText("批量提交审核（草稿）"));
    // 确认弹窗（提交说明初始值「批量提交术语审核」满足 min 4，直接确认）
    await screen.findByText(/确定批量提交审核选中的/);
    fireEvent.click(screen.getByRole("button", { name: "提交审核" }));
    await waitFor(() => {
      expect(mockedBatchSubmit).toHaveBeenCalledWith([
        { code: "GMV", change_reason: "批量提交术语审核", reviewer_id: null, reviewer_type: null, reviewer_domain: null },
      ]);
    });
  });

  it("批量废弃：勾选行 → 批量操作下拉 → 确认弹窗 → 调用 batchDeprecateTerms", async () => {
    mockedBatchDeprecate.mockResolvedValue({
      results: [{ code: "GMV", ok: true, message: "" }],
      ok_count: 1,
      fail_count: 0,
    });
    // 批量废弃只对已发布（PUBLISHED）生效——提供一条已发布术语
    mockedList.mockResolvedValue({
      items: [{ ...TERMS[0], status: "PUBLISHED" }],
      total: 1,
      page: 1,
      page_size: 20,
    });
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findAllByText("共 1 条");
    const batchBtn = screen.getByRole("button", { name: /批量操作/ }) as HTMLButtonElement;
    expect(batchBtn.disabled).toBe(true);
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    expect(batchBtn.disabled).toBe(false);
    fireEvent.click(batchBtn);
    fireEvent.click(await screen.findByText("批量废弃（已发布）"));
    await screen.findByText(/确定批量废弃选中的/);
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
    mockedListRelations.mockResolvedValue({
      items: [
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
      ],
      total: 2,
    });
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(screen.getAllByText("关系")[0]);

    // 弹窗标题 + 中心术语 + 统计条（上游/下游计数）+ 关系卡片短标签
    await screen.findByText(/术语关系图谱/);
    expect(screen.getAllByText("GMV").length).toBeGreaterThan(0);
    expect(screen.getByText((_c, el) => el?.textContent?.trim() === "上游 1")).toBeTruthy();
    expect(screen.getByText((_c, el) => el?.textContent?.trim() === "下游 1")).toBeTruthy();
    expect(screen.getByText("总成交额")).toBeTruthy();
    expect(screen.getByText("成交额(中国)")).toBeTruthy();
    // 关系类型短标签（图谱元数据）
    expect(screen.getByText("同义")).toBeTruthy();
    expect(screen.getByText("上位")).toBeTruthy();
    // 调用接口
    expect(mockedListRelations).toHaveBeenCalledWith("GMV");
  });

  it("关系图谱：无关系时展示空态，且可从图谱弹窗进入「建立关系」", async () => {
    mockedListRelations.mockResolvedValue({ items: [], total: 0 });
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(screen.getAllByText("关系")[0]);

    await screen.findByText(/术语关系图谱/);
    expect(screen.getByText(/暂无关联术语/)).toBeTruthy();
    // 从图谱弹窗内点「建立关系」→ 打开建立关系弹窗（限定在弹窗容器内）
    const graphDialog = await screen.findByRole("dialog");
    fireEvent.click(within(graphDialog).getByRole("button", { name: /建\s*立\s*关\s*系/ }));
    await screen.findByText("建立关系：GMV");
  });
});

describe("Glossary 审核流（提交审核/通过/驳回，复用主数据审核组件）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedList.mockResolvedValue({ items: TERMS, total: 2, page: 1, page_size: 20 });
    mockedConflicts.mockResolvedValue({ items: [], total: 0 });
    mockedListFavorites.mockResolvedValue([]);
    mockedDomainTree.mockResolvedValue([]);
    mockedListRelations.mockResolvedValue({ items: [], total: 0 });
    mockedUsers.mockResolvedValue([
      {
        id: 5,
        username: "pharmacist",
        display_name: "李药师",
        role: "domain_admin",
        domain: "pharmacy",
        status: "active",
      },
    ]);
    mockedFetchCurrentUser.mockResolvedValue({
      id: 1, username: "admin", display_name: "管理员", role: "platform_admin", domain: "finance", org_id: 1,
    } as never);
  });

  it("DRAFT 术语显示「提交审核」，填写说明后调用 submitTerm（进 REVIEW）", async () => {
    mockedSubmit.mockResolvedValue({ ...TERMS[0], status: "REVIEW" } as never);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(screen.getAllByRole("button", { name: /提交审核/ })[0]);
    const modal = await screen.findByRole("dialog");
    fireEvent.change(within(modal).getByLabelText("提交说明"), {
      target: { value: "术语定义已与业务对齐，申请发布" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedSubmit).toHaveBeenCalledWith("GMV", {
        change_reason: "术语定义已与业务对齐，申请发布",
        reviewer_type: null,
        reviewer_id: null,
        reviewer_domain: null,
      }),
    );
    expect(await screen.findByText(/已提交审核/)).toBeInTheDocument();
  });

  it("REVIEW 术语（platform_admin 可审）审核通过并发布", async () => {
    const reviewRow = { ...TERMS[0], status: "REVIEW", submitted_by: 2 };
    mockedList.mockResolvedValue({ items: [reviewRow], total: 1, page: 1, page_size: 20 });
    mockedApprove.mockResolvedValue({ ...TERMS[0], status: "PUBLISHED" } as never);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(await screen.findByRole("button", { name: "审核通过并发布" }));
    await waitFor(() => expect(mockedApprove).toHaveBeenCalledWith("GMV", { comment: null }));
    expect(await screen.findByText(/审核通过，已发布/)).toBeInTheDocument();
  });

  it("REVIEW 术语驳回：填写原因后调用 rejectTerm，状态回 DRAFT", async () => {
    const reviewRow = { ...TERMS[0], status: "REVIEW", submitted_by: 2 };
    mockedList.mockResolvedValue({ items: [reviewRow], total: 1, page: 1, page_size: 20 });
    mockedReject.mockResolvedValue({ ...TERMS[0], status: "DRAFT" } as never);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(await screen.findByRole("button", { name: "驳回该主数据" }));
    const modal = await screen.findByRole("dialog");
    fireEvent.change(within(modal).getByLabelText("驳回原因"), {
      target: { value: "定义与业务实际不符，请补充边界说明" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedReject).toHaveBeenCalledWith("GMV", {
        reason: "定义与业务实际不符，请补充边界说明",
      }),
    );
    expect(await screen.findByText(/已驳回，可修改后重新提交/)).toBeInTheDocument();
  });

  it("提交审核指定「指定用户」时评审用户为选项框，选择用户后提交 reviewer_id", async () => {
    mockedSubmit.mockResolvedValue({ ...TERMS[0], status: "REVIEW" } as never);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(screen.getAllByRole("button", { name: /提交审核/ })[0]);
    const modal = await screen.findByRole("dialog");

    // 评审指派选「指定用户」
    fireEvent.mouseDown(within(modal).getByRole("combobox"));
    fireEvent.click(await screen.findByTitle("指定用户"));

    // 评审用户渲染为选项框（含用户下拉），而非手动输入框
    await waitFor(() => expect(within(modal).getAllByRole("combobox")).toHaveLength(2));
    expect(within(modal).queryByPlaceholderText("如 5")).toBeNull();
    fireEvent.mouseDown(within(modal).getAllByRole("combobox")[1]);
    fireEvent.click(await screen.findByTitle("李药师（#5）"));

    fireEvent.change(within(modal).getByLabelText("提交说明"), {
      target: { value: "术语定义已与业务对齐，申请发布" },
    });
    fireEvent.click(within(modal).getByRole("button", { name: /确 定|确定|OK/ }));

    await waitFor(() =>
      expect(mockedSubmit).toHaveBeenCalledWith("GMV", {
        change_reason: "术语定义已与业务对齐，申请发布",
        reviewer_type: "user",
        reviewer_id: 5,
        reviewer_domain: null,
      }),
    );
    expect(await screen.findByText(/已提交审核/)).toBeInTheDocument();
  });
});

describe("Glossary 生命周期（重新启用/删除/回收站恢复）", () => {
  const deprecatedTerm: GlossaryTerm = { ...TERMS[1], term_code: "AOV_OLD", name: "旧术语", status: "DEPRECATED" };

  beforeEach(() => {
    vi.clearAllMocks();
    mockedConflicts.mockResolvedValue({ items: [], total: 0 });
    mockedListFavorites.mockResolvedValue([]);
    mockedDomainTree.mockResolvedValue([]);
    mockedListRelations.mockResolvedValue({ items: [], total: 0 });
    mockedUsers.mockResolvedValue([]);
    mockedFetchCurrentUser.mockResolvedValue({
      id: 1, username: "admin", display_name: "管理员", role: "platform_admin", domain: null, org_id: 1,
    } as never);
  });

  it("DEPRECATED 术语显示「重新启用」与「删除」，点重新启用调用 reactivateTerm", async () => {
    mockedList.mockResolvedValue({ items: [deprecatedTerm], total: 1 });
    mockedReactivate.mockResolvedValue({ ...deprecatedTerm, status: "DRAFT" } as never);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("旧术语");
    fireEvent.click(screen.getByRole("button", { name: /重新启用/ }));
    fireEvent.click(await screen.findByRole("button", { name: /确 定|确定|OK/ }));
    await waitFor(() => expect(mockedReactivate).toHaveBeenCalledWith("AOV_OLD"));
    expect(await screen.findByText(/已重新启用/)).toBeInTheDocument();
  });

  it("DRAFT 术语点删除调用 deleteTerm", async () => {
    mockedList.mockResolvedValue({ items: [TERMS[0]], total: 1 });
    mockedDelete.mockResolvedValue(TERMS[0] as never);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(screen.getByRole("button", { name: /删除/ }));
    fireEvent.click(await screen.findByRole("button", { name: /确 定|确定|OK/ }));
    await waitFor(() => expect(mockedDelete).toHaveBeenCalledWith("GMV"));
    expect(await screen.findByText(/已删除/)).toBeInTheDocument();
  });

  it("回收站视图显示「恢复」按钮，点击调用 restoreTerm", async () => {
    mockedList.mockResolvedValue({ items: [TERMS[0]], total: 1 });
    mockedRestore.mockResolvedValue(TERMS[0] as never);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    // 切换到回收站视图（antd Select placeholder 为文本节点，用 getByText 展开）
    fireEvent.mouseDown(screen.getByText("回收站"));
    fireEvent.click(await screen.findByTitle("回收站"));
    await waitFor(() =>
      expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ deleted: true })),
    );
    fireEvent.click(await screen.findByRole("button", { name: /恢 复|恢复/ }));
    fireEvent.click(await screen.findByRole("button", { name: /确 定|确定|OK/ }));
    await waitFor(() => expect(mockedRestore).toHaveBeenCalledWith("GMV"));
    expect(await screen.findByText(/已恢复/)).toBeInTheDocument();
  });
});

describe("Glossary 审核候选按角色过滤", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedList.mockResolvedValue({ items: TERMS, total: 2, page: 1, page_size: 20 });
    mockedConflicts.mockResolvedValue({ items: [], total: 0 });
    mockedListFavorites.mockResolvedValue([]);
    mockedDomainTree.mockResolvedValue([
      { id: 1, code: "finance", name: "财务域", parent_id: null, level: 1, sort_order: 0, status: "ACTIVE", metric_count: 0, children: [] },
      { id: 2, code: "pharmacy", name: "药房域", parent_id: null, level: 1, sort_order: 0, status: "ACTIVE", metric_count: 0, children: [] },
    ]);
    mockedListRelations.mockResolvedValue({ items: [], total: 0 });
    mockedUsers.mockResolvedValue([
      { id: 5, username: "pharmacist", display_name: "李药师", role: "domain_admin", domain: "pharmacy", status: "active" },
      { id: 6, username: "viewer1", display_name: "普通用户", role: "viewer", domain: "finance", status: "active" },
    ]);
    mockedFetchCurrentUser.mockResolvedValue({
      id: 1, username: "admin", display_name: "管理员", role: "platform_admin", domain: null, org_id: 1,
    } as never);
  });

  it("评审用户下拉只列出有审核权的用户（domain_admin/reviewer），普通用户被过滤", async () => {
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(screen.getAllByRole("button", { name: /提交审核/ })[0]);
    const modal = await screen.findByRole("dialog");
    fireEvent.mouseDown(within(modal).getByRole("combobox"));
    fireEvent.click(await screen.findByTitle("指定用户"));

    // 用户下拉含 domain_admin（李药师），不含普通用户（viewer）
    fireEvent.mouseDown(within(modal).getAllByRole("combobox")[1]);
    expect(await screen.findByTitle("李药师（#5）")).toBeInTheDocument();
    expect(screen.queryByTitle("普通用户（#6）")).toBeNull();
  });

  it("平台管理员可见全部评审域；域管理员仅可见自己域", async () => {
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(screen.getAllByRole("button", { name: /提交审核/ })[0]);
    const modal = await screen.findByRole("dialog");
    fireEvent.mouseDown(within(modal).getByRole("combobox"));
    fireEvent.click(await screen.findByTitle("指定域评审组"));

    // 平台管理员（admin）：全部域可见
    fireEvent.mouseDown(within(modal).getAllByRole("combobox")[1]);
    expect(await screen.findByTitle("财务域（finance）")).toBeInTheDocument();
    expect(screen.getByTitle("药房域（pharmacy）")).toBeInTheDocument();
  });

  it("域管理员提交时评审域下拉仅显示自己域", async () => {
    mockedFetchCurrentUser.mockResolvedValue({
      id: 9, username: "pharm_admin", display_name: "药房管理员", role: "domain_admin", domain: "pharmacy", org_id: 1,
    } as never);
    render(
      <MemoryRouter initialEntries={["/glossary"]}>
        <Glossary />
      </MemoryRouter>,
    );
    await screen.findByText("成交总额");
    fireEvent.click(screen.getAllByRole("button", { name: /提交审核/ })[0]);
    const modal = await screen.findByRole("dialog");
    fireEvent.mouseDown(within(modal).getByRole("combobox"));
    fireEvent.click(await screen.findByTitle("指定域评审组"));

    // 域管理员仅能看到自己域（pharmacy），财务域被过滤
    fireEvent.mouseDown(within(modal).getAllByRole("combobox")[1]);
    expect(await screen.findByTitle("药房域（pharmacy）")).toBeInTheDocument();
    expect(screen.queryByTitle("财务域（finance）")).toBeNull();
  });
});
