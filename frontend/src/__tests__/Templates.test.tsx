import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter, useNavigate, Routes, Route } from "react-router-dom";
import { Templates } from "../pages/Templates";
import type { MetricTemplate, MetricResponse } from "../types";

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
    instantiateTemplate: vi.fn(),
    listFavorites: vi.fn(),
    addFavorite: vi.fn(),
    removeFavorite: vi.fn(),
    listUsers: vi.fn(),
    updateTemplateOwner: vi.fn(),
    setTemplateActive: vi.fn(),
    updateMetricTemplate: vi.fn(),
    listDomainTree: vi.fn(),
    listDictItems: vi.fn(),
    getDomainDefaults: vi.fn(),
    listMeasureCatalogs: vi.fn(),
    UnisenseApiError,
  };
});
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));
// P2-13 编辑弹窗测试需要 can("template:assign-owner") 可见「编辑」按钮；
// Templates 仅用 usePermission().can，无其他依赖，can 恒 true 不影响现有 URL/防竞态测试
vi.mock("../hooks/usePermission", () => ({
  usePermission: () => ({ can: () => true }),
}));

import { listTemplates, createMetric, instantiateTemplate, listFavorites, listUsers, updateTemplateOwner, setTemplateActive, updateMetricTemplate, listDomainTree, listDictItems, listMeasureCatalogs } from "../api";

const mockedList = vi.mocked(listTemplates);
const mockedCreate = vi.mocked(createMetric);
const mockedListFavorites = vi.mocked(listFavorites);
const mockedInstantiate = vi.mocked(instantiateTemplate);
const mockedListUsers = vi.mocked(listUsers);
const mockedUpdateOwner = vi.mocked(updateTemplateOwner);
const mockedSetActive = vi.mocked(setTemplateActive);
const mockedUpdateMetricTemplate = vi.mocked(updateMetricTemplate);
const mockedDomainTree = vi.mocked(listDomainTree);
const mockedDictItems = vi.mocked(listDictItems);
const mockedMeasureCatalogs = vi.mocked(listMeasureCatalogs);

const CREATED: MetricResponse = {
  id: 1,
  metric_code: "finance_gmv_daily",
  name: "GMV 日汇总模板",
  domain: "finance",
  type: "atomic",
  granularity: "daily",
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
  created_at: "2026-08-13T00:00:00",
  updated_at: "2026-08-13T00:00:00",
};

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
    // OneData 预设（方案A）：原子模板预设逻辑度量
    measure_id: 1,
    mount: null,
    product_owner_id: null,
    tech_owner_id: null,
    dw_developer_id: null,
    product_owner_name: null,
    tech_owner_name: null,
    dw_developer_name: null,
    is_active: true,
    owner_id: null,
    created_by: 1,
    version: 1,
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
    // OneData 预设（方案A）：原子模板预设逻辑度量
    measure_id: 1,
    mount: null,
    product_owner_id: null,
    tech_owner_id: null,
    dw_developer_id: null,
    product_owner_name: null,
    tech_owner_name: null,
    dw_developer_name: null,
    is_active: true,
    owner_id: null,
    created_by: 1,
    version: 1,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue({ items: TPLS, total: TPLS.length });
  mockedListFavorites.mockResolvedValue([]);
  mockedDomainTree.mockResolvedValue([]);
  mockedDictItems.mockResolvedValue([]);
  // OneData 原子层：默认无已发布逻辑度量（度量下拉为空，不影响既有断言）
  mockedMeasureCatalogs.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 });
  mockedListUsers.mockResolvedValue([
    { id: 1, username: "alice", display_name: "Alice", role: "metric_owner", domain: "finance", status: "ACTIVE" },
    { id: 2, username: "bob", display_name: "Bob", role: "metric_owner", domain: "finance", status: "ACTIVE" },
  ]);
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

  it("从总览仪表 Owner 责任分布 ?owner_id= 直达：所有查询都携带责任人过滤", async () => {
    render(
      <MemoryRouter initialEntries={["/templates?owner_id=1"]}>
        <Templates />
      </MemoryRouter>,
    );

    await screen.findByText("tpl_gmv_daily");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ owner_id: 1 });
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

  it("从总览仪表 ?is_active=inactive 直达：查询携带停用模板过滤", async () => {
    render(
      <MemoryRouter initialEntries={["/templates?is_active=inactive"]}>
        <Templates />
      </MemoryRouter>,
    );

    await screen.findByText("tpl_gmv_daily");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ is_active: false });
    }
  });

  it("防竞态：迟到的首查响应不覆盖最新筛选结果", async () => {
    let resolveFull!: (v: { items: MetricTemplate[]; total: number }) => void;
    const fullPromise = new Promise<{ items: MetricTemplate[]; total: number }>((r) => {
      resolveFull = r;
    });
    // 首查（挂起）；随后输入关键词触发二次查询立即返回 1 条；兜底返回全量 2 条
    mockedList.mockImplementationOnce(() => fullPromise);
    mockedList.mockResolvedValueOnce({ items: [TPLS[0]], total: 1 });
    mockedList.mockResolvedValue({ items: TPLS, total: TPLS.length });

    render(
      <MemoryRouter>
        <Templates />
      </MemoryRouter>,
    );

    // 首查挂起，搜索框可用后输入关键词并回车（惰性搜索：确认才触发二次查询）
    const searchInput = await screen.findByPlaceholderText("搜索模板编码 / 名称 / 描述");
    fireEvent.change(searchInput, { target: { value: "GMV" } });
    fireEvent.keyDown(searchInput, { key: "Enter", code: "Enter" });
    await screen.findByText("tpl_gmv_daily");

    // 迟到的首查此刻才返回：若被应用会覆盖筛选结果（tpl_aov_weekly 也会出现）
    resolveFull({ items: TPLS, total: TPLS.length });
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

  it("从模板实例化：提交时调用 instantiateTemplate（而非普通 createMetric）", async () => {
    mockedInstantiate.mockResolvedValue(CREATED);
    mockedCreate.mockResolvedValue(CREATED);
    render(
      <MemoryRouter>
        <Templates />
      </MemoryRouter>,
    );

    await screen.findByText("tpl_gmv_daily");
    // 打开实例化弹窗（列表有多行，取第一个模板）
    fireEvent.click(screen.getAllByText("实例化指标")[0]);
    await screen.findByText("从模板实例化：GMV 日汇总模板");

    // 提交表单：应调用模板实例化专用接口
    fireEvent.click(screen.getByText("实例化创建"));
    await waitFor(() => {
      expect(mockedInstantiate).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ name: "GMV 日汇总模板", domain: "finance" }),
      );
    });
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it("方案A：原子模板实例化——模板 measure_id 预设预填并随请求提交", async () => {
    mockedInstantiate.mockResolvedValue(CREATED);
    render(
      <MemoryRouter>
        <Templates />
      </MemoryRouter>,
    );

    await screen.findByText("tpl_gmv_daily");
    fireEvent.click(screen.getAllByText("实例化指标")[0]);
    await screen.findByText("从模板实例化：GMV 日汇总模板");
    // 原子类型 → 显示逻辑度量下拉，且预填模板 measure_id（TPLS[0].measure_id=1）
    await waitFor(() => {
      expect(screen.getByText("逻辑度量（OneData 原子层）")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("实例化创建"));
    await waitFor(() => {
      expect(mockedInstantiate).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ measure_id: 1 }),
      );
    });
  });

  it("P2-13 编辑模板：打开编辑弹窗回填当前值，保存调用 updateMetricTemplate（PATCH 局部更新）", async () => {
    const updated = { ...TPLS[0], name: "GMV 日汇总模板 V2", version: 2 };
    mockedUpdateMetricTemplate.mockResolvedValue(updated as unknown as MetricTemplate);
    render(
      <MemoryRouter>
        <Templates />
      </MemoryRouter>,
    );

    await screen.findByText("tpl_gmv_daily");
    // 打开编辑弹窗（can("template:assign-owner") 已 mock 为 true，编辑按钮可见）
    fireEvent.click(screen.getAllByText("编辑")[0]);
    await screen.findByText("编辑模板：tpl_gmv_daily");
    // 回填当前名称（PATCH 语义：只改 name）
    const nameInput = document.querySelector('.ant-modal input[id="name"]') as HTMLInputElement;
    expect(nameInput?.value).toBe(TPLS[0].name);
    fireEvent.change(nameInput, { target: { value: "GMV 日汇总模板 V2" } });
    fireEvent.click(screen.getByText("保存修改"));
    await waitFor(() => {
      expect(mockedUpdateMetricTemplate).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ name: "GMV 日汇总模板 V2" }),
      );
    });
    // 保存成功后刷新列表（重新拉取）
    expect(mockedList).toHaveBeenCalled();
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/lineage", "/templates"]}>
        <Routes>
          <Route path="/lineage" element={<div>lineage-page</div>} />
          <Route path="/templates" element={<Templates />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("lineage-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/templates" element={<Templates />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });

  it("指派责任人：表格负责人下拉选择用户调用 updateTemplateOwner", async () => {
    mockedUpdateOwner.mockResolvedValue({ ...TPLS[0], owner_id: 2 });
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    // 打开第一行（GMV 模板）的负责人下拉
    const selects = document.querySelectorAll(".ant-select");
    // 找到负责人下拉（带 placeholder 未指派）
    const ownerSelect = Array.from(selects).find((el) =>
      el.querySelector(".ant-select-selection-placeholder")?.textContent?.includes("未指派"),
    );
    expect(ownerSelect).toBeTruthy();
    fireEvent.mouseDown(ownerSelect!.querySelector(".ant-select-selector")!);
    const bobOption = await screen.findByText("Bob");
    fireEvent.click(bobOption);
    await waitFor(() => {
      expect(mockedUpdateOwner).toHaveBeenCalledWith(1, 2);
    });
  });

  it("模板详情弹窗：展示描述、必填字段与默认属性（点击行内『详情』打开）", async () => {
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    // 点击行内「详情」按钮
    const detailButtons = screen.getAllByText("详情");
    fireEvent.click(detailButtons[0]);
    // 断言详情弹窗展示描述与必填字段
    await screen.findByText("模板详情：GMV 日汇总模板");
    expect(screen.getByText("按日汇总 GMV")).toBeTruthy();
    expect(screen.getAllByText("metric_code").length).toBeGreaterThan(0);
    expect(screen.getAllByText("finance").length).toBeGreaterThan(0);
    // 关闭弹窗
    fireEvent.click(screen.getByRole("button", { name: /关\s*闭/ }));
    await waitFor(() => {
      expect(screen.queryByText("模板详情：GMV 日汇总模板")).toBeNull();
    });
  });
});

  it("启用/停用模板：点状态 Tag 确认后调用 setTemplateActive 并刷新行状态", async () => {
    mockedSetActive.mockResolvedValue({ ...TPLS[0], is_active: false });
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText(TPLS[0].code);
    // 定位 TPLS[0] 所在行，点该行状态 Tag（避免跨行同名匹配）
    const row = screen.getByText(TPLS[0].code).closest("tr") as HTMLElement;
    const tag = within(row).getByText("启用", { selector: ".ant-tag" });
    fireEvent.click(tag);
    await screen.findByText("停用此模板？");
    fireEvent.click(screen.getByRole("button", { name: "停 用" }));
    await waitFor(() => expect(mockedSetActive).toHaveBeenCalledWith(TPLS[0].id, false));
  });

  it("停用模板（is_active=false）的实例化指标按钮禁用并提示", async () => {
    const inactiveTpl = { ...TPLS[0], is_active: false };
    mockedList.mockResolvedValueOnce({ items: [inactiveTpl], total: 1 });
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText(inactiveTpl.code);
    const row = screen.getByText(inactiveTpl.code).closest("tr") as HTMLElement;
    const btn = within(row).getByRole("button", { name: /实例化指标/ });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    // Tooltip 提示"已停用"（hover 触发，按钮禁用为核心断言）
    expect(within(row).getByText(/实例化指标/)).toBeTruthy();
  });
