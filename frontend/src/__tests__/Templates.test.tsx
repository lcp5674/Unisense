import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter, useNavigate, Routes, Route } from "react-router-dom";
import { Templates } from "../pages/Templates";
import type { MetricTemplate, MetricResponse, MeasureCatalog } from "../types";

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
  resolveUserNames: vi.fn().mockResolvedValue([]),
    updateTemplateOwner: vi.fn(),
    setTemplateActive: vi.fn(),
    updateMetricTemplate: vi.fn(),
    listDomainTree: vi.fn(),
    listDictItems: vi.fn(),
    getDomainDefaults: vi.fn(),
    listMeasureCatalogs: vi.fn(),
    listCatalogs: vi.fn(),
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

import { listTemplates, createMetric, instantiateTemplate, listFavorites, listUsers, updateTemplateOwner, setTemplateActive, updateMetricTemplate, listDomainTree, listDictItems, listMeasureCatalogs, listCatalogs } from "../api";

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
const mockedListCatalogs = vi.mocked(listCatalogs);

/** 已发布逻辑度量（供原子模板预设/实例化的 measure 下拉与 stat_caliber 预览） */
const MEASURES: MeasureCatalog[] = [
  {
    id: 1,
    measure_code: "pay_amt",
    name: "支付金额",
    description: null,
    measure_format: "AMOUNT",
    default_unit: "元",
    default_decimal_places: 2,
    source_system: null,
    synonyms: null,
    row_version: 1,
    category: "FEE",
    stat_caliber: "收费明细按结算日期去重后求和",
    domain: "finance",
    owner_id: 1,
    status: "PUBLISHED",
    created_at: "2026-01-01T00:00:00",
    updated_at: "2026-01-01T00:00:00",
  },
];

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
  // resetAllMocks：清调用记录 + mockImplementation（用例内设置的实现不跨用例泄漏）
  vi.resetAllMocks();
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
  // 挂载实体选项框：默认无已采集表（用例内按需覆盖）
  mockedListCatalogs.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
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
    // 模板 required_fields 含 metric_code：填指标编码（模板编码非 4 段不预填）
    fireEvent.change(screen.getAllByPlaceholderText("留空自动生成")[0], {
      target: { value: "fin_gmv_inst_daily" },
    });

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
    // 原子类型 → 显示逻辑度量下拉（主词「逻辑度量」+ 业务别名「原子指标口径」），且预填模板 measure_id（TPLS[0].measure_id=1）
    await waitFor(() => {
      expect(screen.getByText("逻辑度量（原子指标口径）")).toBeTruthy();
    });
    // 模板 required_fields 含 metric_code：填指标编码
    fireEvent.change(screen.getAllByPlaceholderText("留空自动生成")[0], {
      target: { value: "fin_gmv_inst_daily" },
    });
    fireEvent.click(screen.getByText("实例化创建"));
    await waitFor(() => {
      expect(mockedInstantiate).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ measure_id: 1 }),
      );
    });
  });

  it("S7：原子模板实例化——不渲染粒度编辑框且提交强制 day 粒度", async () => {
    mockedInstantiate.mockResolvedValue(CREATED);
    render(
      <MemoryRouter>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    fireEvent.click(screen.getAllByText("实例化指标")[0]);
    await screen.findByText("从模板实例化：GMV 日汇总模板");
    // S7：原子类型不渲染「粒度」编辑框（原子 = 逻辑度量 + 基础统计粒度（日），
    // 粒度/周期归派生与挂载实体层）——即使模板预设了非日粒度（TPLS[0].granularity=daily）
    const modal = document.querySelector(".ant-modal") as HTMLElement;
    expect(within(modal).queryByText("粒度")).toBeNull();
    // 模板 required_fields 含 metric_code：填指标编码
    fireEvent.change(screen.getAllByPlaceholderText("留空自动生成")[0], {
      target: { value: "fin_gmv_inst_daily" },
    });
    fireEvent.click(screen.getByText("实例化创建"));
    await waitFor(() => {
      // 提交强制 day：忽略模板/表单预设的非日粒度，防「原子 + 非日粒度」
      expect(mockedInstantiate).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ granularity: "day" }),
      );
    });
  });

  it("模板必填 currency/pii_flag：币种标注不适用自动跳过，PII 开关渲染且提交携带默认值", async () => {
    const tpl = { ...TPLS[0], required_fields: ["metric_code", "currency", "pii_flag"] };
    mockedList.mockResolvedValue({ items: [tpl], total: 1 } as never);
    mockedDictItems.mockResolvedValue([
      { id: 1, dict_type: "currency", code: "CNY", label: "人民币", status: "active", sort_order: 1 },
      { id: 2, dict_type: "currency", code: "USD", label: "美元", status: "active", sort_order: 2 },
    ] as any);
    mockedInstantiate.mockResolvedValue(CREATED);
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    fireEvent.click(screen.getAllByText("实例化指标")[0]);
    await screen.findByText("从模板实例化：GMV 日汇总模板");
    // 原子类型：currency 不适用 → 必填提示标注「不适用，自动跳过」
    expect(screen.getByText("（不适用，自动跳过）")).toBeTruthy();
    // 币种与 PII 开关仍渲染（用户可自愿填写/开关）——币种在提示条（删除线）与表单标签各一处
    expect(screen.getAllByText("币种").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("含 PII")).toBeTruthy();
    // 填指标编码（metric_code 可留空自动生成，此处自愿填写）
    fireEvent.change(screen.getAllByPlaceholderText("留空自动生成")[0], {
      target: { value: "fin_gmv_inst_day" },
    });
    fireEvent.click(screen.getByText("实例化创建"));
    await waitFor(() => {
      // currency 豁免不阻塞；pii_flag 默认 false 随请求提交
      expect(mockedInstantiate).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ pii_flag: false }),
      );
    });
  });

  it("模板必填 metric_code 但留空：不强制填写，提交由系统自动生成（对齐后端豁免）", async () => {
    const tpl = { ...TPLS[0], required_fields: ["metric_code"] };
    mockedList.mockResolvedValue({ items: [tpl], total: 1 } as never);
    mockedInstantiate.mockResolvedValue(CREATED);
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    fireEvent.click(screen.getAllByText("实例化指标")[0]);
    await screen.findByText("从模板实例化：GMV 日汇总模板");
    // 提示语说明「留空由系统自动生成」（不再强制必须填写）
    expect(screen.getByText(/留空由系统自动生成/)).toBeTruthy();
    // 直接提交（metric_code 留空）——不被 antd required 规则拦截
    fireEvent.click(screen.getByText("实例化创建"));
    await waitFor(() => {
      expect(mockedInstantiate).toHaveBeenCalledWith(1, expect.objectContaining({}));
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
    // 打开编辑弹窗：先展开「更多」下拉，再点「编辑」菜单项（操作列已收敛为 主操作+更多）
    fireEvent.click(screen.getAllByText("更多")[0]);
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
    // 必填字段以中文业务名展示（metric_code → 指标编码）
    expect(screen.getAllByText("指标编码").length).toBeGreaterThan(0);
    expect(screen.getAllByText("finance").length).toBeGreaterThan(0);
    // 关闭弹窗
    fireEvent.click(screen.getByRole("button", { name: /关\s*闭/ }));
    await waitFor(() => {
      expect(screen.queryByText("模板详情：GMV 日汇总模板")).toBeNull();
    });
  });

  it("详情弹窗责任方：平台用户 id 解析为姓名（不再是『用户 #N』）", async () => {
    // 模板预设 product_owner_id=2（users 列表第 2 个 = Bob），name 为空
    mockedList.mockResolvedValue({
      items: [{ ...TPLS[0], product_owner_id: 2, product_owner_name: null, tech_owner_id: 1, tech_owner_name: null }],
      total: 1,
    });
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    fireEvent.click(screen.getAllByText("详情")[0]);
    await screen.findByText("模板详情：GMV 日汇总模板");
    // 产品需求方 = Bob（id=2 解析），技术方 = Alice（id=1 解析）
    expect(screen.getByText("Bob")).toBeTruthy();
    expect(screen.getByText("Alice")).toBeTruthy();
    expect(screen.queryByText("用户 #2")).toBeNull();
    expect(screen.queryByText("用户 #1")).toBeNull();
  });

  it("编辑弹窗必填字段：下拉展示可选字段清单（指标编码/统计粒度等）", async () => {
    render(
      <MemoryRouter>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    // 打开编辑弹窗：先展开「更多」下拉，再点「编辑」菜单项（操作列已收敛为 主操作+更多）
    fireEvent.click(screen.getAllByText("更多")[0]);
    fireEvent.click(screen.getAllByText("编辑")[0]);
    await screen.findByText("编辑模板：tpl_gmv_daily");
    // 定位必填字段 tags Select（通过 Form.Item label 定位，placeholder 有值时不显示）
    const reqItem = screen.getByText("必填字段（实例化时强制填写）").closest(".ant-form-item");
    const reqSelect = reqItem!.querySelector(".ant-select");
    expect(reqSelect).toBeTruthy();
    fireEvent.mouseDown(reqSelect!.querySelector(".ant-select-selector")!);
    // 下拉出现可选字段（中文业务名；虚拟滚动下可见前几项）
    expect((await screen.findAllByText("指标编码")).length).toBeGreaterThan(0);
    expect(screen.getByText("指标名称")).toBeTruthy();
    expect(screen.getByText("统计粒度")).toBeTruthy();
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

  it("模板作用引导：展开后展示作用说明与原子/派生/复合三类参考样例", async () => {
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    // 引导默认收起：未展开时不展示样例标题
    expect(screen.queryByText("门诊支付金额（日）")).toBeNull();
    const header = screen.getByText(/指标模板是什么/).closest(".ant-collapse-header") as HTMLElement;
    fireEvent.click(header);
    // 展开后：作用说明 + 三类样例
    expect(await screen.findByText(/模板 = 指标的「样板」/)).toBeTruthy();
    expect(screen.getByText("门诊支付金额（日）")).toBeTruthy();
    expect(screen.getByText("科室维度支付金额（月）")).toBeTruthy();
    expect(screen.getByText("门诊支付金额占比")).toBeTruthy();
  });

  it("详情弹窗默认口径：SQL 模式展示模式标签与 SQL 原文（不再暴露完整 JSON）", async () => {
    mockedList.mockResolvedValue({
      items: [
        {
          ...TPLS[0],
          defaults_json: {
            definition_json: { sql: "select sum(amount) from dwd_order_di", source_tables: ["dwd_order_di"] },
          },
        },
      ],
      total: 1,
    });
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    fireEvent.click(screen.getAllByText("详情")[0]);
    await screen.findByText("模板详情：GMV 日汇总模板");
    // 模式标签 + SQL 原文 + 源表 Tag
    expect(screen.getByText("SQL 模式")).toBeTruthy();
    expect(screen.getByText(/select sum\(amount\) from dwd_order_di/)).toBeTruthy();
    expect(screen.getByText("dwd_order_di")).toBeTruthy();
    // 不再把完整 JSON（含 "sql": 键名）暴露给用户
    expect(screen.queryByText(/"sql":/)).toBeNull();
  });

  it("编辑弹窗默认口径：模板口径含 sql 时回填 SQL 模式并预填内容", async () => {
    mockedList.mockResolvedValue({
      items: [{ ...TPLS[0], defaults_json: { definition_json: { sql: "select 1 from t" } } }],
      total: 1,
    });
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    // 打开编辑弹窗：先展开「更多」下拉，再点「编辑」菜单项（操作列已收敛为 主操作+更多）
    fireEvent.click(screen.getAllByText("更多")[0]);
    fireEvent.click(screen.getAllByText("编辑")[0]);
    await screen.findByText("编辑模板：tpl_gmv_daily");
    // 原子模板（TPLS[0].type=atomic）口径由逻辑度量继承 → 编辑器折叠为高级项，先展开
    fireEvent.click(screen.getByText("高级：补充物理口径定义（一般留空）"));
    const item = screen.getByText("默认口径（实例化时自动合并）").closest(".ant-form-item") as HTMLElement;
    // Segmented 选中项为「SQL 模式」
    const selected = item.querySelector(".ant-segmented-item-selected");
    expect(selected?.textContent).toContain("SQL 模式");
    // textarea 预填 SQL（表达式模式渲染的是 input，非 textarea）
    expect((item.querySelector("textarea") as HTMLTextAreaElement).value).toBe("select 1 from t");
  });

  it("编辑弹窗默认口径：切换 SQL 模式填写后保存，写入 defaults_json.definition_json={sql}", async () => {
    mockedUpdateMetricTemplate.mockResolvedValue(TPLS[0]);
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    // 打开编辑弹窗：先展开「更多」下拉，再点「编辑」菜单项（操作列已收敛为 主操作+更多）
    fireEvent.click(screen.getAllByText("更多")[0]);
    fireEvent.click(screen.getAllByText("编辑")[0]);
    await screen.findByText("编辑模板：tpl_gmv_daily");
    // 原子模板：编辑器折叠为高级项，先展开再切模式
    fireEvent.click(screen.getByText("高级：补充物理口径定义（一般留空）"));
    fireEvent.click(await screen.findByText("SQL 模式"));
    const item = screen.getByText("默认口径（实例化时自动合并）").closest(".ant-form-item") as HTMLElement;
    fireEvent.change(item.querySelector("textarea")!, {
      target: { value: "select sum(amount) from dwd_order_di" },
    });
    fireEvent.click(screen.getByRole("button", { name: /保存修改/ }));
    await waitFor(() => expect(mockedUpdateMetricTemplate).toHaveBeenCalled());
    const payload = mockedUpdateMetricTemplate.mock.calls[0][1] as {
      defaults_json?: { definition_json?: unknown };
    };
    expect(payload.defaults_json?.definition_json).toEqual({
      sql: "select sum(amount) from dwd_order_di",
    });
  });

  it("编辑弹窗默认口径：高级 JSON 模式输入非法 JSON 时阻止保存", async () => {
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    // 打开编辑弹窗：先展开「更多」下拉，再点「编辑」菜单项（操作列已收敛为 主操作+更多）
    fireEvent.click(screen.getAllByText("更多")[0]);
    fireEvent.click(screen.getAllByText("编辑")[0]);
    await screen.findByText("编辑模板：tpl_gmv_daily");
    // 原子模板：编辑器折叠为高级项，先展开再切模式
    fireEvent.click(screen.getByText("高级：补充物理口径定义（一般留空）"));
    fireEvent.click(await screen.findByText("高级 JSON"));
    const item = screen.getByText("默认口径（实例化时自动合并）").closest(".ant-form-item") as HTMLElement;
    fireEvent.change(item.querySelector("textarea")!, { target: { value: "{bad json" } });
    fireEvent.click(screen.getByRole("button", { name: /保存修改/ }));
    await waitFor(() => expect(screen.getByText(/JSON 格式错误/)).toBeTruthy());
    expect(mockedUpdateMetricTemplate).not.toHaveBeenCalled();
  });

  it("实例化弹窗口径：切 SQL 模式填写后提交给 instantiateTemplate", async () => {
    mockedInstantiate.mockResolvedValue(CREATED);
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    const row = screen.getByText("tpl_gmv_daily").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /实例化指标/ }));
    await screen.findByText(/从模板实例化：/);
    // 原子类型（TPLS[0].type=atomic）口径由逻辑度量继承 → 编辑器折叠为高级项，先展开再切换模式
    fireEvent.click(screen.getByText("高级：补充物理口径定义（一般留空）"));
    await screen.findByText("SQL 模式");
    fireEvent.click(screen.getByText("SQL 模式"));
    const item = screen.getByText("口径定义（可留空用模板默认）").closest(".ant-form-item") as HTMLElement;
    fireEvent.change(item.querySelector("textarea")!, {
      target: { value: "select sum(amount) from dwd_order_di" },
    });
    // 模板 required_fields 含 metric_code：填指标编码
    fireEvent.change(screen.getAllByPlaceholderText("留空自动生成")[0], {
      target: { value: "fin_gmv_inst_daily" },
    });
    fireEvent.click(screen.getByRole("button", { name: /实例化创建/ }));
    await waitFor(() => expect(mockedInstantiate).toHaveBeenCalled());
    const [, payload] = mockedInstantiate.mock.calls[0];
    expect(payload.definition_json).toEqual({ sql: "select sum(amount) from dwd_order_di" });
  });

  it("原子类型实例化：口径由逻辑度量继承——口径定义折叠为高级项并只读预览 stat_caliber", async () => {
    mockedMeasureCatalogs.mockResolvedValue({ items: MEASURES, total: 1, page: 1, page_size: 200 });
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    const row = screen.getByText("tpl_gmv_daily").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /实例化指标/ }));
    await screen.findByText(/从模板实例化：/);
    // 模板预设 measure_id=1（支付金额）→ 只读预览该逻辑度量的统计口径，无需再写物理口径
    expect(
      await screen.findByText("已选逻辑度量的统计口径（实例化后自动继承，无需重复填写）"),
    ).toBeTruthy();
    expect(screen.getByText("收费明细按结算日期去重后求和")).toBeTruthy();
    // 物理口径编辑器折叠为「高级」项，默认收起
    expect(screen.getByText("高级：补充物理口径定义（一般留空）")).toBeTruthy();
  });

  it("派生类型实例化：口径定义直接展开（不折叠为高级项）", async () => {
    mockedMeasureCatalogs.mockResolvedValue({ items: MEASURES, total: 1, page: 1, page_size: 200 });
    render(
      <MemoryRouter initialEntries={["/templates"]}>
        <Templates />
      </MemoryRouter>,
    );
    await screen.findByText("tpl_gmv_daily");
    const row = screen.getByText("tpl_gmv_daily").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /实例化指标/ }));
    await screen.findByText(/从模板实例化：/);
    // 切类型为派生（派生依赖 expression/sql，口径必须可编辑）
    const modal = document.querySelector(".ant-modal") as HTMLElement;
    const typeItem = within(modal).getByText("类型").closest(".ant-form-item") as HTMLElement;
    fireEvent.mouseDown(typeItem.querySelector(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("派生指标"));
    // 派生不折叠：无「高级项」入口，模式切换器直接可见
    expect(screen.queryByText("高级：补充物理口径定义（一般留空）")).toBeNull();
    expect(screen.getByText("SQL 模式")).toBeTruthy();
  });

  describe("挂载实体选项框（派生类型：源表/列/粒度 Select 化）", () => {
    // 打开实例化弹窗并切换类型为派生（复用既有交互：类型 Select → 点「派生指标」）
    async function openDerivedInstantiate() {
      render(
        <MemoryRouter initialEntries={["/templates"]}>
          <Templates />
        </MemoryRouter>,
      );
      await screen.findByText("tpl_gmv_daily");
      fireEvent.click(screen.getAllByText("实例化指标")[0]);
      await screen.findByText(/从模板实例化：/);
      const modal = document.querySelector(".ant-modal") as HTMLElement;
      const typeItem = within(modal).getByText("类型").closest(".ant-form-item") as HTMLElement;
      fireEvent.mouseDown(typeItem.querySelector(".ant-select-selector")!);
      fireEvent.click(await screen.findByText("派生指标"));
      return modal;
    }

    it("派生类型：源表/度量列/粒度均为选项框（基于采集目录与粒度字典）", async () => {
      mockedDictItems.mockResolvedValue([
        { id: 1, dict_type: "granularity", code: "day", label: "日", status: "active", sort_order: 1 },
        { id: 2, dict_type: "granularity", code: "month", label: "月", status: "active", sort_order: 2 },
      ] as any);
      const modal = await openDerivedInstantiate();
      // 挂载实体区域：3 个 Select（源表/列/粒度），不再是自由 Input
      const mountItem = within(modal).getByText("挂载实体（指标的家，OneData 挂载层）").closest(".ant-form-item") as HTMLElement;
      expect(mountItem.querySelectorAll(".ant-select").length).toBeGreaterThanOrEqual(3);
      expect(mountItem.querySelector("input.ant-input")).toBeNull();
      // 粒度下拉展开后展示粒度管理字典项（日/月，label=中文 (code)）
      const granularitySel = mountItem.querySelectorAll(".ant-select")[2] as HTMLElement;
      fireEvent.mouseDown(granularitySel.querySelector(".ant-select-selector")!);
      expect(await screen.findByText("月 (month)")).toBeTruthy();
    });

    it("选源表后：度量列下拉自动带出该表列（name + type + comment）", async () => {
      mockedListCatalogs.mockImplementation((async (params: any) => {
        if (params?.keyword === "dwd_sales_detail") {
          return {
            items: [
              {
                entity_name: "dwd_sales_detail",
                source_name: "hive",
                schema_def: {
                  columns: [
                    { name: "pay_amt", type: "decimal", comment: "支付金额" },
                    { name: "order_cnt", type: "bigint", comment: "订单数" },
                  ],
                },
              },
            ],
            total: 1,
            page: 1,
            page_size: 5,
          };
        }
        return {
          items: [
            { entity_name: "dwd_sales_detail", source_name: "hive", schema_def: { columns: [] } },
            { entity_name: "dwd_order_di", source_name: "hive", schema_def: { columns: [] } },
          ],
          total: 2,
          page: 1,
          page_size: 20,
        };
      }) as any);
      const modal = await openDerivedInstantiate();
      const mountItem = within(modal).getByText("挂载实体（指标的家，OneData 挂载层）").closest(".ant-form-item") as HTMLElement;
      const selects = mountItem.querySelectorAll(".ant-select");
      // 展开源表下拉（第 1 个 Select）→ 已采集表列表出现（含 source_name）
      fireEvent.mouseDown(selects[0].querySelector(".ant-select-selector")!);
      fireEvent.click(await screen.findByText("dwd_sales_detail（hive）"));
      // 展开列下拉（第 2 个 Select）→ 该表列带出
      fireEvent.mouseDown(selects[1].querySelector(".ant-select-selector")!);
      expect(await screen.findByText("pay_amt (decimal) — 支付金额")).toBeTruthy();
      expect(screen.getByText("order_cnt (bigint) — 订单数")).toBeTruthy();
    });

    it("未采集源表：输入后选中（未采集兜底），提交携带该表名", async () => {
      // 点击包含目标文本的 option（多个下拉可能并存未采集提示，须精确到选项元素）
      async function clickUncollectedOption(text: string) {
        const opts = await screen.findAllByText(new RegExp(text));
        const item = opts.find((el) => el.closest(".ant-select-item-option"));
        if (!item) throw new Error(`未找到 ${text} 的未采集选项`);
        fireEvent.click(item);
      }
      const modal = await openDerivedInstantiate();
      const mountItem = within(modal).getByText("挂载实体（指标的家，OneData 挂载层）").closest(".ant-form-item") as HTMLElement;
      const srcSel = mountItem.querySelectorAll(".ant-select")[0] as HTMLElement;
      // 源表输入未采集表名 → 下拉出现「（未采集，手动输入）」项
      fireEvent.mouseDown(srcSel.querySelector(".ant-select-selector")!);
      const srcInput = srcSel.querySelector(".ant-select-selection-search-input") as HTMLInputElement;
      fireEvent.change(srcInput, { target: { value: "wedw_uncollected_tbl" } });
      await clickUncollectedOption("wedw_uncollected_tbl");
      expect(screen.getAllByText(/wedw_uncollected_tbl/).length).toBeGreaterThan(0);
      // 未采集列：输入后选中（选源表后列下拉为空，未采集兜底可输入）
      const colSel = mountItem.querySelectorAll(".ant-select")[1] as HTMLElement;
      fireEvent.mouseDown(colSel.querySelector(".ant-select-selector")!);
      const colInput = colSel.querySelector(".ant-select-selection-search-input") as HTMLInputElement;
      fireEvent.change(colInput, { target: { value: "uncollected_col" } });
      await clickUncollectedOption("uncollected_col");
      // 粒度：字典未加载时输入 day 选中（未采集兜底）
      const granSel = mountItem.querySelectorAll(".ant-select")[2] as HTMLElement;
      fireEvent.mouseDown(granSel.querySelector(".ant-select-selector")!);
      const granInput = granSel.querySelector(".ant-select-selection-search-input") as HTMLInputElement;
      fireEvent.change(granInput, { target: { value: "day" } });
      await clickUncollectedOption("day");
      // 模板 required_fields 含 metric_code：填指标编码
      fireEvent.change(screen.getAllByPlaceholderText("留空自动生成")[0], {
        target: { value: "fin_gmv_inst_weekly" },
      });
      // 提交：payload.mount 携带未采集表/列/粒度（不破坏既有自由输入能力）
      fireEvent.click(screen.getByText("实例化创建"));
      await waitFor(() => {
        const payload = mockedInstantiate.mock.calls[0][1] as { mount?: { source_table?: string; source_column?: string; granularity?: string } };
        expect(payload.mount?.source_table).toBe("wedw_uncollected_tbl");
        expect(payload.mount?.source_column).toBe("uncollected_col");
        expect(payload.mount?.granularity).toBe("day");
      });
    });
  });
});
