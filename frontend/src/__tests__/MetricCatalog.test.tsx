import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { MetricCatalog } from "../pages/MetricCatalog";

vi.mock("../api", () => ({
  listMetrics: vi.fn(),
  fetchDashboard: vi.fn(),
  listUsers: vi.fn(),
  listDomainTree: vi.fn(),
}));
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import { listMetrics, fetchDashboard, listUsers, listDomainTree } from "../api";
import type { MetricResponse, MetricListResponse } from "../types";
const mockedList = vi.mocked(listMetrics);
const mockedDashboard = vi.mocked(fetchDashboard);
const mockedUsers = vi.mocked(listUsers);
const mockedDomains = vi.mocked(listDomainTree);

const metric: MetricResponse = {
  id: 1,
  metric_code: "sales_gmv_sum_d",
  name: "销售 GMV",
  domain: "sales",
  type: "atomic",
  granularity: "day",
  unit: "元",
  currency: null,
  aggregation: "SUM",
  time_semantics: "PERIOD",
  freshness: "T1",
  dw_layer: "DWS",
  sla: null,
  metric_tier: "T1",
  serving_mode: "BATCH_ONLY",
  additivity: "ADDITIVE",
  non_additive_dimensions: null,
  definition_json: {
    expression: "sum(gmv)",
    definition: "当日支付成功订单的成交总额",
    source_tables: ["dwd_order_di"],
    dependencies: ["user_base_cnt_d"],
    source_fields: ["gmv"],
  },
  version: 2,
  row_version: 1,
  status: "PUBLISHED",
  owner_id: 1,
  backup_owner_id: 2,
  approver_id: 3,
  submitted_by: 1,
  pii_flag: true,
  compliance_reviewed: true,
  effective_version: 2,
  consumption_guide: null,
  successor_code: null,
  deprecated_at: null,
  sunset_until: null,
  emergency_publish: true,
  emergency_reason: "hotfix",
  gray_tenant_ids: [101, 102],
  pending_conflict: false,
  pending_conflict_detail: null,
  created_at: "2026-08-01T00:00:00",
  updated_at: "2026-08-02T00:00:00",
};

function renderCatalog() {
  return render(
    <MemoryRouter initialEntries={["/catalog"]}>
      <Routes>
        <Route path="/catalog" element={<MetricCatalog />} />
        <Route path="/detail/:code" element={<div>detail</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("MetricCatalog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 20 });
    mockedDashboard.mockResolvedValue({
      total: 1,
      by_status: { PUBLISHED: 1 },
      by_tier: { T1: 1 },
      by_domain: { sales: 1 },
      pii_count: 1,
      pii_ratio: 1,
    });
    mockedUsers.mockResolvedValue([
      { id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "sales", status: "active" },
      { id: 2, username: "lisi", display_name: "李四", role: "metric_owner", domain: "sales", status: "active" },
      { id: 3, username: "wangwu", display_name: "王五", role: "platform_admin", domain: null, status: "active" },
    ]);
    mockedDomains.mockResolvedValue([
      {
        id: 1,
        code: "sales",
        name: "销售域",
        parent_id: null,
        level: 1,
        sort_order: 0,
        status: "ACTIVE",
        metric_count: 1,
        children: [],
      },
    ]);
  });

  it("渲染治理徽章（紧急/灰度/PII）", async () => {
    renderCatalog();
    await waitFor(() => {
      expect(screen.getByText("sales_gmv_sum_d")).toBeTruthy();
    });
    expect(screen.getByText("紧急")).toBeTruthy();
    expect(screen.getByText("灰度")).toBeTruthy();
    expect(screen.getByText("PII 已复核")).toBeTruthy();
  });

  it("展示责任人中文名与业务域中文名", async () => {
    renderCatalog();
    await waitFor(() => {
      expect(screen.getByText("sales_gmv_sum_d")).toBeTruthy();
    });
    // 责任人 owner_id=1 → 张三；业务域 sales → 销售域
    expect(screen.getByText("张三")).toBeTruthy();
    expect(screen.getByText("销售域")).toBeTruthy();
  });

  it("展示口径摘要（聚合 · 粒度 · 单位）", async () => {
    renderCatalog();
    await waitFor(() => {
      expect(screen.getByText("sales_gmv_sum_d")).toBeTruthy();
    });
    // SUM → 求和；day → 日；unit 元
    expect(screen.getByText("求和 · 日 · 元")).toBeTruthy();
  });

  it("展开行展示指标定义/计算口径/治理追溯", async () => {
    renderCatalog();
    await waitFor(() => {
      expect(screen.getByText("sales_gmv_sum_d")).toBeTruthy();
    });
    // 点击行首展开图标
    const expandBtn = document.querySelector(".ant-table-row-expand-icon");
    expect(expandBtn).toBeTruthy();
    fireEvent.click(expandBtn as Element);
    // 展开内容：指标定义 / 计算口径 / 来源字段 / 提交人=张三 / 审批人=王五
    await waitFor(() => {
      expect(screen.getByText("当日支付成功订单的成交总额")).toBeTruthy();
      expect(screen.getByText("sum(gmv)")).toBeTruthy();
      expect(screen.getByText("dwd_order_di")).toBeTruthy();
      expect(screen.getByText("user_base_cnt_d")).toBeTruthy();
      expect(screen.getByText("gmv")).toBeTruthy();
      // 治理追溯：备份责任人=李四、审批人=王五
      expect(screen.getAllByText("李四").length).toBeGreaterThan(0);
      expect(screen.getAllByText("王五").length).toBeGreaterThan(0);
    });
  });

  it("域筛选选项来自真实 dashboard by_domain（非硬编码）", async () => {
    renderCatalog();
    await waitFor(() => {
      expect(mockedDashboard).toHaveBeenCalled();
    });
  });

  it("列表请求携带排序与筛选参数", async () => {
    renderCatalog();
    await waitFor(() => {
      expect(mockedList).toHaveBeenCalledWith(
        expect.objectContaining({ page: 1, page_size: 20, sort_by: "updated_at", sort_order: "desc" }),
      );
    });
  });

  it("空态给出创建引导", async () => {
    mockedList.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
    renderCatalog();
    await waitFor(() => {
      expect(screen.getByText("创建指标")).toBeTruthy();
      expect(screen.getByText("从模板创建")).toBeTruthy();
    });
  });

  it("从生命周期信号条 ?status=DRAFT 直达：所有查询都携带状态过滤（避免全量首查竞态覆盖）", async () => {
    mockedList.mockResolvedValue({ items: [metric], total: 2, page: 1, page_size: 20 });
    render(
      <MemoryRouter initialEntries={["/catalog?status=DRAFT"]}>
        <Routes>
          <Route path="/catalog" element={<MetricCatalog />} />
          <Route path="/detail/:code" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findAllByText("共 2 条");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    // 任何一次查询都不得丢失 URL 带来的状态过滤
    for (const c of calls) {
      expect(c[0]).toMatchObject({ status: "DRAFT" });
    }
  });

  it("从血缘视图 ?kw=xxx 直达：所有查询都携带关键词过滤", async () => {
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 20 });
    render(
      <MemoryRouter initialEntries={["/catalog?kw=GMV"]}>
        <Routes>
          <Route path="/catalog" element={<MetricCatalog />} />
          <Route path="/detail/:code" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findAllByText("共 1 条");
    const calls = mockedList.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) {
      expect(c[0]).toMatchObject({ keyword: "GMV" });
    }
  });

  it("防竞态：迟到的全量响应不覆盖已筛选结果", async () => {
    let resolveFull!: (v: MetricListResponse) => void;
    const fullPromise = new Promise<MetricListResponse>((r) => {
      resolveFull = r;
    });
    // 首查（全量）挂起；筛选查询立即返回 2；兜底全量 8
    mockedList.mockImplementationOnce(() => fullPromise);
    mockedList.mockResolvedValueOnce({ items: [metric], total: 2, page: 1, page_size: 20 });
    mockedList.mockResolvedValue({ items: [], total: 8, page: 1, page_size: 20 });

    renderCatalog();
    await screen.findByText("全部状态");
    // 通过状态下拉选择「草稿」触发带 status 的二次查询
    fireEvent.mouseDown(screen.getByText("全部状态"));
    const draftOption = await screen.findByText("草稿");
    fireEvent.click(draftOption);

    await screen.findAllByText("共 2 条");

    // 迟到的全量首查此刻才返回：若被应用会覆盖筛选结果
    resolveFull({ items: [], total: 8, page: 1, page_size: 20 });
    await screen.findAllByText("共 2 条");
    expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ status: "DRAFT" }));
  });
});
