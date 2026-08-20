import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { message } from "antd";
import { MetricCatalog } from "../pages/MetricCatalog";

vi.mock("../api", () => ({
  listMetrics: vi.fn(),
  fetchDashboard: vi.fn(),
  listUsers: vi.fn(),
  listMeasureCatalogs: vi.fn(),
  listDomainTree: vi.fn(),
  fetchCurrentUser: vi.fn(),
  listFavorites: vi.fn(),
  addFavorite: vi.fn(),
  removeFavorite: vi.fn(),
  batchSubmitMetrics: vi.fn(),
  deleteMetric: vi.fn(),
  restoreMetric: vi.fn(),
  fetchMyPermissions: vi.fn(),
  compareMetricsMatrix: vi.fn(),
}));
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import {
  listMetrics,
  fetchDashboard,
  listUsers,
  listMeasureCatalogs,
  listDomainTree,
  fetchCurrentUser,
  listFavorites,
  addFavorite,
  removeFavorite,
  batchSubmitMetrics,
  deleteMetric,
  restoreMetric,
  fetchMyPermissions,
  compareMetricsMatrix,
} from "../api";
import type { MetricResponse, MetricListResponse } from "../types";
import { PermissionProvider } from "../hooks/usePermission";
const mockedList = vi.mocked(listMetrics);
const mockedDashboard = vi.mocked(fetchDashboard);
const mockedUsers = vi.mocked(listUsers);
const mockedMeasures = vi.mocked(listMeasureCatalogs);
const mockedDomains = vi.mocked(listDomainTree);
const mockedCurrentUser = vi.mocked(fetchCurrentUser);
const mockedFavorites = vi.mocked(listFavorites);
const mockedAddFavorite = vi.mocked(addFavorite);
const mockedRemoveFavorite = vi.mocked(removeFavorite);
const mockedBatchSubmit = vi.mocked(batchSubmitMetrics);
const mockedDeleteMetric = vi.mocked(deleteMetric);
const mockedPermissions = vi.mocked(fetchMyPermissions);
const mockedMatrix = vi.mocked(compareMetricsMatrix);

const metric: MetricResponse = {
  id: 1,
  metric_code: "sales_gmv_sum_d",
  name: "销售 GMV",
  domain: "sales",
  type: "atomic",
  // OneData 原子层：关联逻辑度量（度量目录）
  measure_id: 1,
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
    sql: "SELECT SUM(order_amount) AS gmv, dt FROM dwd_order_di GROUP BY dt",
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
  term_id: null,
  effective_version: 2,
  consumption_guide: null,
  successor_code: null,
  deprecated_at: null,
  sunset_until: null,
  emergency_publish: true,
  emergency_reason: "hotfix",
  emergency_reviewed_at: null,
  gray_tenant_ids: [101, 102],
  pending_conflict: false,
  pending_conflict_detail: null,
  pending_version: false,
  created_at: "2026-08-01T00:00:00",
  updated_at: "2026-08-02T00:00:00",
};

function renderCatalog() {
  return render(
    <MemoryRouter initialEntries={["/catalog"]}>
      <Routes>
        <Route
          path="/catalog"
          element={
            // P6 批量按钮依赖权限 loading 门控：无 Provider 时 usePermission 默认
            // loading=true → 按钮永久禁用。包 Provider + beforeEach mock 权限快照。
            <PermissionProvider
              user={
                {
                  id: 1,
                  username: "admin",
                  display_name: "管理员",
                  role: "platform_admin",
                  domain: null,
                  org_id: 1,
                } as never
              }
            >
              <MetricCatalog />
            </PermissionProvider>
          }
        />
        <Route path="/detail/:code" element={<div>detail</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("MetricCatalog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // P6：renderCatalog 包 PermissionProvider，默认 admin 全权限（批量操作按钮可用）
    mockedPermissions.mockResolvedValue({
      user_id: 1,
      role: "platform_admin",
      home_domain: null,
      allowed_actions: ["read", "write", "approve", "deprecate"],
      ui_actions: ["metric:create", "metric:approve", "metric:deprecate"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
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
    // OneData 原子层：逻辑度量目录（明细抽屉"逻辑度量"行渲染用）
    mockedMeasures.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
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
    // 按用户群体差异化：默认平台管理员视角（admin=全部列、无角色默认筛选），
    // 避免 producer 群体默认列（无业务域/收藏/治理徽章）隐藏被断言列；同时清理
    // localStorage 列偏好残留，保证各用例从角色默认列起步。
    localStorage.clear();
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "admin",
      display_name: "管理员",
      role: "platform_admin",
      domain: null,
      org_id: 1,
    });
    mockedFavorites.mockResolvedValue([]);
  });

  it("渲染治理徽章（紧急/灰度/PII）", async () => {
    renderCatalog();
    await waitFor(() => {
      expect(screen.getByText("sales_gmv_sum_d")).toBeTruthy();
    });
    expect(screen.getByText("紧急")).toBeTruthy();
    expect(screen.getByText(/^灰度 \d+ 租户$/)).toBeTruthy();
    expect(screen.getByText("PII 已复核")).toBeTruthy();
  });

  it("展示责任人中文名与业务域中文名", async () => {
    renderCatalog();
    await waitFor(() => {
      expect(screen.getByText("sales_gmv_sum_d")).toBeTruthy();
    });
    // 责任人 owner_id=1 → 张三（提交人列同为此人时可能重复，用 getAllByText 容忍多命中）
    expect(screen.getAllByText("张三").length).toBeGreaterThan(0);
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
    // 展开内容：指标定义 / 计算口径 / 口径 SQL / 来源字段 / 提交人=张三 / 审批人=王五
    await waitFor(() => {
      expect(screen.getByText("当日支付成功订单的成交总额")).toBeTruthy();
      expect(screen.getByText("sum(gmv)")).toBeTruthy();
      // 口径 SQL：带标签 + SQL 文本
      expect(screen.getByText("口径 SQL：")).toBeTruthy();
      expect(screen.getByText("SELECT SUM(order_amount) AS gmv, dt FROM dwd_order_di GROUP BY dt")).toBeTruthy();
      expect(screen.getByText("dwd_order_di")).toBeTruthy();
      expect(screen.getByText("user_base_cnt_d")).toBeTruthy();
      expect(screen.getByText("gmv")).toBeTruthy();
      // 治理追溯：备份责任人=李四、审批人=王五
      expect(screen.getAllByText("李四").length).toBeGreaterThan(0);
      expect(screen.getAllByText("王五").length).toBeGreaterThan(0);
    });
  });

  it("业务消费者（analyst）展开行隐藏治理追溯，保留口径与责任方", async () => {
    // 角色差异化：consumer 群体明细抽屉聚焦"口径+责任方"，隐藏备份/提交/审批/时间/分层等治理追溯
    mockedCurrentUser.mockResolvedValue({
      id: 5,
      username: "analyst",
      display_name: "分析师",
      role: "analyst",
      domain: "sales",
      org_id: 1,
    });
    renderCatalog();
    await waitFor(() => {
      expect(screen.getByText("sales_gmv_sum_d")).toBeTruthy();
    });
    const expandBtn = document.querySelector(".ant-table-row-expand-icon");
    expect(expandBtn).toBeTruthy();
    fireEvent.click(expandBtn as Element);
    await waitFor(() => {
      // 口径定义与责任方保留（消费者关注"怎么算的 + 找谁问"）
      expect(screen.getByText("sum(gmv)")).toBeTruthy();
      expect(screen.getByText("产品需求方")).toBeTruthy();
      // 治理/运营追溯隐藏（备份责任人/提交人/审批人/创建时间/数据分层）
      expect(screen.queryByText("备份责任人")).toBeNull();
      expect(screen.queryByText("提交人")).toBeNull();
      expect(screen.queryByText("审批人")).toBeNull();
      expect(screen.queryByText("创建时间")).toBeNull();
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
      // page-head 与空态引导各有一组创建/模板按钮，断言均存在
      expect(screen.getAllByText("创建指标").length).toBeGreaterThan(0);
      expect(screen.getAllByText("从模板创建").length).toBeGreaterThan(0);
    });
  });

  it("有筛选时的空态给出「清除筛选」而非创建引导", async () => {
    mockedList.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
    // 用 ?status=DRAFT 直达制造"有筛选但无结果"状态
    render(
      <MemoryRouter initialEntries={["/catalog?status=DRAFT"]}>
        <Routes>
          <Route path="/catalog" element={<MetricCatalog />} />
          <Route path="/detail/:code" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("清除筛选")).toBeTruthy();
    });
    // 清除筛选后恢复创建引导（断言按钮切换回创建/模板）
    fireEvent.click(screen.getByText("清除筛选"));
    await waitFor(() => {
      expect(screen.getAllByText("创建指标").length).toBeGreaterThan(0);
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
    // （跳过批量下线替代指标探针：listMetrics({page_size:100, status:"PUBLISHED"}) 仅 2 键）
    for (const c of calls) {
      const p = c[0] ?? {};
      if (Object.keys(p).length === 2 && p.page_size === 100 && p.status === "PUBLISHED") continue;
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
      const p = c[0] ?? {};
      if (Object.keys(p).length === 2 && p.page_size === 100 && p.status === "PUBLISHED") continue;
      expect(c[0]).toMatchObject({ keyword: "GMV" });
    }
  });

  it("从总览 Owner 责任分布 ?owner_id=xx 直达：所有查询都携带责任人过滤", async () => {
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 20 });
    render(
      <MemoryRouter initialEntries={["/catalog?owner_id=2"]}>
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
      const p = c[0] ?? {};
      if (Object.keys(p).length === 2 && p.page_size === 100 && p.status === "PUBLISHED") continue;
      expect(c[0]).toMatchObject({ owner_id: 2 });
    }
  });

  it("防竞态：迟到的全量响应不覆盖已筛选结果", async () => {
    let resolveFull!: (v: MetricListResponse) => void;
    const fullPromise = new Promise<MetricListResponse>((r) => {
      resolveFull = r;
    });
    // 首查（全量）挂起；currentUserId 落地/筛选各触发一次返回 2；兜底全量 8
    mockedList.mockImplementationOnce(() => fullPromise);
    mockedList.mockResolvedValueOnce({ items: [metric], total: 2, page: 1, page_size: 20 });
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

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderCatalog();
    await waitFor(() => {
      expect(screen.getByText("sales_gmv_sum_d")).toBeTruthy();
    });
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/lineage", "/catalog?status=DRAFT"]}>
        <Routes>
          <Route path="/lineage" element={<div>lineage-page</div>} />
          <Route path="/catalog" element={<MetricCatalog />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("sales_gmv_sum_d");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("lineage-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/catalog"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/catalog" element={<MetricCatalog />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("sales_gmv_sum_d");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });

  it("点击返回：来源标记为总览仪表时优先回仪表盘（不依赖 history.length）", async () => {
    // 模拟从总览仪表「为你推荐 → 去目录」进入：location.state.from = "dashboard"
    render(
      <MemoryRouter initialEntries={[{ pathname: "/catalog", state: { from: "dashboard" } }]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/catalog" element={<MetricCatalog />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("sales_gmv_sum_d");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
  });

  it("健康度列：有评分显示分级标签，无评分显示未评分", async () => {
    const healthy = { ...metric, health_level: "GOOD", health_score: 78 };
    const unscored = { ...metric, metric_code: "sales_gmv_sum_w", health_level: null, health_score: null };
    mockedList.mockResolvedValue({ items: [healthy, unscored], total: 2, page: 1, page_size: 20 });
    renderCatalog();
    await screen.findByText("sales_gmv_sum_d");
    expect(screen.getByText("良好")).toBeTruthy();
    expect(screen.getByText("未评分")).toBeTruthy();
  });

  it("收藏：点击心形调用 addFavorite 并显示已收藏", async () => {
    mockedAddFavorite.mockResolvedValue({} as never);
    renderCatalog();
    await screen.findByText("sales_gmv_sum_d");
    const favBtn = screen.getByRole("button", { name: "收藏" });
    fireEvent.click(favBtn);
    await waitFor(() => {
      expect(mockedAddFavorite).toHaveBeenCalledWith("METRIC", "sales_gmv_sum_d");
    });
    expect(screen.getByRole("button", { name: "取消收藏" })).toBeTruthy();
  });

  it("收藏：再次点击已收藏的心形调用 removeFavorite", async () => {
    mockedFavorites.mockResolvedValue([
      { asset_type: "METRIC" as const, asset_id: "sales_gmv_sum_d" },
    ]);
    mockedRemoveFavorite.mockResolvedValue({} as never);
    renderCatalog();
    await screen.findByText("sales_gmv_sum_d");
    const unfavBtn = screen.getByRole("button", { name: "取消收藏" });
    fireEvent.click(unfavBtn);
    await waitFor(() => {
      expect(mockedRemoveFavorite).toHaveBeenCalledWith("METRIC", "sales_gmv_sum_d");
    });
    expect(screen.getByRole("button", { name: "收藏" })).toBeTruthy();
  });

  it("勾选超过 6 个：勾选不限量（可做批量操作），点「对比所选」时才提示并拦截", async () => {
    const messageSpy = vi
      .spyOn(message, "warning")
      .mockImplementation(() => undefined as never);
    const many = Array.from({ length: 8 }, (_, i) => ({
      ...metric,
      id: i + 1,
      metric_code: `metric_${i + 1}`,
    }));
    mockedList.mockResolvedValue({ items: many, total: 8, page: 1, page_size: 20 });
    renderCatalog();
    await screen.findByText("metric_1");
    // 全选 8 个 → 勾选不再被拦截（保留全部 8 个，供批量操作使用）
    const selectAll = document.querySelector(
      ".ant-table-selection-column input[type=checkbox]",
    ) as Element;
    fireEvent.click(selectAll);
    await waitFor(() => {
      // 对比按钮显示完整勾选数 8，且可用（不再因超 6 禁用）
      const btn = screen.getByRole("button", { name: /对比所选 \(8\)/ }) as HTMLButtonElement;
      expect(btn.disabled).toBe(false);
    });
    // 点「对比所选」→ 超 6 提示，弹窗不打开、勾选保持 8
    fireEvent.click(screen.getByRole("button", { name: /对比所选 \(8\)/ }));
    await waitFor(() => {
      expect(messageSpy).toHaveBeenCalledWith(expect.stringContaining("最多支持 6 个"));
    });
    expect(mockedMatrix).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /对比所选 \(8\)/ })).toBeTruthy();
    messageSpy.mockRestore();
  });

  it("勾选 2 个指标点「对比所选」：在当前页弹窗展示矩阵对比（不再跳转 /compare）", async () => {
    const two = [
      { ...metric, id: 1, metric_code: "sales_gmv_sum_d" },
      { ...metric, id: 2, metric_code: "sales_gmv_cnt_d" },
    ];
    mockedList.mockResolvedValue({ items: two, total: 2, page: 1, page_size: 20 });
    mockedMatrix.mockResolvedValue({
      metrics: ["sales_gmv_sum_d", "sales_gmv_cnt_d"],
      fields: {
        granularity: {
          values: { sales_gmv_sum_d: "day", sales_gmv_cnt_d: "day" },
          difference_level: "all_identical",
        },
      },
    } as never);
    renderCatalog();
    await screen.findByText("sales_gmv_sum_d");
    // 全选 2 个 → 对比按钮可用
    const selectAll = document.querySelector(
      ".ant-table-selection-column input[type=checkbox]",
    ) as Element;
    fireEvent.click(selectAll);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /对比所选 \(2\)/ })).toBeTruthy();
    });
    // 点「对比所选」→ 弹窗打开并请求矩阵，不跳转
    fireEvent.click(screen.getByRole("button", { name: /对比所选 \(2\)/ }));
    await waitFor(() => {
      expect(mockedMatrix).toHaveBeenCalledWith(["sales_gmv_sum_d", "sales_gmv_cnt_d"]);
    });
    expect(screen.getByText(/指标对比 \(2\)/)).toBeTruthy();
    // 矩阵表渲染（汇总条「共 1 项字段 · 2 个指标」）
    expect(await screen.findByText(/共 1 项字段 · 2 个指标/)).toBeTruthy();
    // 仍停留在指标目录（未跳转 /compare）
    expect(screen.getByRole("button", { name: /对比所选 \(2\)/ })).toBeTruthy();
  });

  it("批量操作：勾选草稿指标提交审核（DRAFT → REVIEW，走 /batch-submit）", async () => {
    const draft = { ...metric, status: "DRAFT" as const };
    mockedList.mockResolvedValue({ items: [draft], total: 1, page: 1, page_size: 20 });
    mockedBatchSubmit.mockResolvedValue({
      ok_count: 1,
      fail_count: 0,
      results: [{ metric_code: "sales_gmv_sum_d", ok: true, message: "" }],
    } as never);
    renderCatalog();
    await screen.findByText("sales_gmv_sum_d");
    // 勾选表头全选
    const selectAll = document.querySelector(".ant-table-selection-column input[type=checkbox]") as Element;
    fireEvent.click(selectAll);
    // 打开批量操作下拉 → 批量提交审核
    fireEvent.click(screen.getByRole("button", { name: /批量操作/ }));
    fireEvent.click(screen.getByText("批量提交审核（草稿）"));
    // 确认弹窗 → 提交（Ant 双字按钮渲染为"提 交"）
    await screen.findByText(/将勾选的/);
    fireEvent.click(screen.getByRole("button", { name: /提\s*交/ }));
    await waitFor(() => {
      expect(mockedBatchSubmit).toHaveBeenCalledWith([
        expect.objectContaining({
          metric_code: "sales_gmv_sum_d",
          change_reason: "批量提交审核",
          reviewer_id: null,
          reviewer_type: null,
          reviewer_domain: "sales",
        }),
      ]);
    });
  });

  it("批量操作：勾选草稿指标批量删除", async () => {
    const draft = { ...metric, status: "DRAFT" as const };
    mockedList.mockResolvedValue({ items: [draft], total: 1, page: 1, page_size: 20 });
    mockedDeleteMetric.mockResolvedValue({} as never);
    // 批量删除仅平台管理员可用（对齐后端 DELETE require_roles(platform_admin)）
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "admin",
      display_name: "管理员",
      role: "platform_admin",
      domain: null,
      org_id: 1,
    });
    renderCatalog();
    await screen.findByText("sales_gmv_sum_d");
    const selectAll = document.querySelector(".ant-table-selection-column input[type=checkbox]") as Element;
    fireEvent.click(selectAll);
    fireEvent.click(screen.getByRole("button", { name: /批量操作/ }));
    fireEvent.click(screen.getByText("批量删除（草稿）"));
    await screen.findByText(/将删除勾选的/);
    fireEvent.click(screen.getByRole("button", { name: "删 除" }));
    await waitFor(() => {
      expect(mockedDeleteMetric).toHaveBeenCalledWith("sales_gmv_sum_d");
    });
  });
});

describe("MetricCatalog - 按钮级权限点过滤", () => {
  function renderWithPerm(uiActions: string[]) {
    mockedPermissions.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "sales",
      allowed_actions: ["read", "write"],
      ui_actions: uiActions,
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    const user = {
      id: 1,
      username: "zhangsan",
      display_name: "张三",
      role: "metric_owner",
      domain: "sales",
      org_id: 1,
    };
    return render(
      <MemoryRouter initialEntries={["/catalog"]}>
        <Routes>
          <Route
            path="/catalog"
            element={
              <PermissionProvider user={user}>
                <MetricCatalog />
              </PermissionProvider>
            }
          />
          <Route path="/detail/:code" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("无按钮级权限点时批量操作按钮禁用", async () => {
    const draft = { ...metric, status: "DRAFT" as const };
    mockedList.mockResolvedValue({ items: [draft], total: 1, page: 1, page_size: 20 });
    renderWithPerm([]);
    await screen.findByText("sales_gmv_sum_d");
    const selectAll = document.querySelector(".ant-table-selection-column input[type=checkbox]") as Element;
    fireEvent.click(selectAll);
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: /批量操作/ }) as HTMLButtonElement;
      expect(btn.disabled).toBe(true);
    });
  });

  it("具备 metric:approve 权限点时批量操作按钮可用", async () => {
    const draft = { ...metric, status: "DRAFT" as const };
    mockedList.mockResolvedValue({ items: [draft], total: 1, page: 1, page_size: 20 });
    renderWithPerm(["metric:approve"]);
    await screen.findByText("sales_gmv_sum_d");
    const selectAll = document.querySelector(".ant-table-selection-column input[type=checkbox]") as Element;
    fireEvent.click(selectAll);
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: /批量操作/ }) as HTMLButtonElement;
      expect(btn.disabled).toBe(false);
    });
  });

  it("metric_owner 组合（create+deprecate，无 approve）时批量操作按钮可用", async () => {
    // 指标负责人可批量提交审核（create）与删除草稿（deprecate），无需 approve——
    // 验证 canBatchManage 的 OR 逻辑对 owner 角色组合正确放行。
    const draft = { ...metric, status: "DRAFT" as const };
    mockedList.mockResolvedValue({ items: [draft], total: 1, page: 1, page_size: 20 });
    renderWithPerm(["metric:create", "metric:deprecate", "metric:edit"]);
    await screen.findByText("sales_gmv_sum_d");
    const selectAll = document.querySelector(".ant-table-selection-column input[type=checkbox]") as Element;
    fireEvent.click(selectAll);
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: /批量操作/ }) as HTMLButtonElement;
      expect(btn.disabled).toBe(false);
    });
  });

  it("domain_admin 组合（create+approve+deprecate 全量）时批量操作按钮可用", async () => {
    const draft = { ...metric, status: "DRAFT" as const };
    mockedList.mockResolvedValue({ items: [draft], total: 1, page: 1, page_size: 20 });
    renderWithPerm(["metric:create", "metric:approve", "metric:deprecate", "metric:edit"]);
    await screen.findByText("sales_gmv_sum_d");
    const selectAll = document.querySelector(".ant-table-selection-column input[type=checkbox]") as Element;
    fireEvent.click(selectAll);
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: /批量操作/ }) as HTMLButtonElement;
      expect(btn.disabled).toBe(false);
    });
  });

  it("无 metric:export 权限时导出按钮禁用（CSV 客户端生成，权限点仅前端生效）", async () => {
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 20 });
    renderWithPerm(["metric:create"]);
    await screen.findByText("sales_gmv_sum_d");
    const btn = screen.getByRole("button", { name: /导出/ }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("具备 metric:export 权限时导出按钮可用", async () => {
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 20 });
    renderWithPerm(["metric:create", "metric:export"]);
    await screen.findByText("sales_gmv_sum_d");
    const btn = screen.getByRole("button", { name: /导出/ }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  it("收藏列表加载失败时「我的收藏」按钮禁用（避免静默空集误导为无收藏）", async () => {
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 20 });
    mockedFavorites.mockRejectedValue(new Error("network"));
    renderWithPerm(["metric:create", "metric:export"]);
    await screen.findByText("sales_gmv_sum_d");
    const btn = screen.getByRole("button", { name: /我的收藏/ }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });
});

describe("MetricCatalog URL 筛选直达（分享/刷新保持）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedDashboard.mockResolvedValue({ total: 0, by_domain: {}, counts: {} } as any);
    mockedDomains.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, role: "platform_admin" } as any);
    mockedFavorites.mockResolvedValue([]);
    mockedUsers.mockResolvedValue([]);
    (fetchMyPermissions as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({});
  });

  it("带 ?domain=sales&lifecycle=created_7d 直达时，列表按域过滤且 created_after 生效", async () => {
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 20 });
    render(
      <PermissionProvider user={{ id: 1, role: "platform_admin" } as any}>
      <MemoryRouter initialEntries={["/catalog?domain=sales&lifecycle=created_7d"]}>
        <Routes>
          <Route path="/catalog" element={<MetricCatalog />} />
          <Route path="/detail/:code" element={<div>detail</div>} />
          </Routes>
        </MemoryRouter>
      </PermissionProvider>,
    );
    // 首次 load 后 lifecycleDate effect 会触发二次 load（依赖含 lifecycleDate）；
    // 断言存在同时带 domain=sales 且 created_after 已计算的调用（URL 直达的生命周期快筛真正生效）
    await waitFor(() => {
      const call = mockedList.mock.calls.find(
        (c) => c[0]?.domain === "sales" && typeof c[0]?.created_after === "string",
      );
      expect(call).toBeTruthy();
    });
  });

  it("带 ?status=PUBLISHED&tier=T1 直达时，状态与分级过滤生效", async () => {
    mockedList.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
    render(
      <PermissionProvider user={{ id: 1, role: "platform_admin" } as any}>
      <MemoryRouter initialEntries={["/catalog?status=PUBLISHED&tier=T1"]}>
        <Routes>
          <Route path="/catalog" element={<MetricCatalog />} />
          <Route path="/detail/:code" element={<div>detail</div>} />
          </Routes>
        </MemoryRouter>
      </PermissionProvider>,
    );
    await waitFor(() => {
      const call = mockedList.mock.calls.find((c) => c[0]?.status === "PUBLISHED");
      expect(call).toBeTruthy();
      expect(call?.[0]?.metric_tier).toBe("T1");
    });
  });
});

describe("MetricCatalog 已应用筛选回显（非空态可感知子集）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedDashboard.mockResolvedValue({ total: 0, by_domain: {}, counts: {} } as any);
    mockedDomains.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, role: "platform_admin" } as any);
    mockedFavorites.mockResolvedValue([]);
    mockedUsers.mockResolvedValue([]);
    (fetchMyPermissions as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({});
  });

  it("带 ?status=PUBLISHED&tier=T1 直达且有数据时，表上方回显已应用筛选 Tag，可一键关闭", async () => {
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 20 });
    render(
      <PermissionProvider user={{ id: 1, role: "platform_admin" } as any}>
        <MemoryRouter initialEntries={["/catalog?status=PUBLISHED&tier=T1"]}>
          <Routes>
            <Route path="/catalog" element={<MetricCatalog />} />
            <Route path="/detail/:code" element={<div>detail</div>} />
          </Routes>
        </MemoryRouter>
      </PermissionProvider>,
    );
    await screen.findByText("sales_gmv_sum_d");
    // 非空态下显示"已应用筛选"回显条（区别于仅空态才出现的清除按钮）
    expect(screen.getByText("已应用筛选：")).toBeTruthy();
    expect(screen.getByText(/状态：已发布/)).toBeTruthy();
    expect(screen.getByText(/分级：T1/)).toBeTruthy();
    // 关闭分级 Tag（antd closable 的 × 关闭图标）后，后续请求不再携带 tier
    fireEvent.click(screen.getByText(/分级：T1/).closest(".ant-tag")?.querySelector(".ant-tag-close-icon") as Element);
    await waitFor(() => {
      const call = mockedList.mock.calls.find((c) => c[0]?.metric_tier === undefined || c[0]?.metric_tier === "");
      expect(call).toBeTruthy();
    });
  });
});

describe("MetricCatalog 加载失败降级", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedDashboard.mockResolvedValue({ total: 0, by_domain: {}, counts: {} } as any);
    mockedDomains.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, role: "platform_admin" } as any);
    mockedFavorites.mockResolvedValue([]);
    mockedUsers.mockResolvedValue([]);
    (fetchMyPermissions as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({});
  });

  it("列表加载失败时显示失败提示与重试按钮，重试后恢复列表", async () => {
    // 初始渲染 + fetchCurrentUser 就绪会触发前两次 load（都会失败）；点重试后（第 3 次）成功
    let callNo = 0;
    mockedList.mockImplementation(() => {
      callNo += 1;
      if (callNo <= 2) return Promise.reject(new Error("network down"));
      return Promise.resolve({ items: [metric], total: 1, page: 1, page_size: 20 });
    });
    render(
      <PermissionProvider user={{ id: 1, role: "platform_admin" } as any}>
      <MemoryRouter initialEntries={["/catalog"]}>
        <Routes>
          <Route path="/catalog" element={<MetricCatalog />} />
          <Route path="/detail/:code" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>
      </PermissionProvider>,
    );
    // 失败后显示重试空态（区别于"空结果"引导）
    const retry = await screen.findByRole("button", { name: /重\s*试/ });
    expect(screen.getByText(/加载指标列表失败/)).toBeTruthy();
    fireEvent.click(retry);
    // 重试成功后恢复列表（不再显示失败提示）
    await waitFor(() => {
      expect(screen.queryByText(/加载指标列表失败/)).toBeNull();
    });
    expect(screen.getByText(metric.name)).toBeTruthy();
  });

  it("搜索输入不立即触发请求——回车/点搜索确认后才过滤（防抖惰性搜索）", async () => {
    render(
      <PermissionProvider user={{ id: 1, role: "platform_admin" } as any}>
        <MemoryRouter initialEntries={["/catalog"]}>
          <Routes>
            <Route path="/catalog" element={<MetricCatalog />} />
            <Route path="/detail/:code" element={<div>detail</div>} />
          </Routes>
        </MemoryRouter>
      </PermissionProvider>,
    );
    await screen.findByText(metric.name);
    mockedList.mockClear();
    // 输入关键词：仅更新输入框显示，不发请求
    const searchInput = screen.getByPlaceholderText("搜索指标名 / 编码 / 描述");
    fireEvent.change(searchInput, { target: { value: "gmv" } });
    await waitFor(() => expect((searchInput as HTMLInputElement).value).toBe("gmv"));
    expect(mockedList).not.toHaveBeenCalled();
    // 回车确认：触发一次过滤请求（携带 keyword）
    fireEvent.keyDown(searchInput, { key: "Enter", code: "Enter" });
    await waitFor(() => {
      expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ keyword: "gmv" }));
    });
  });
});

describe("MetricCatalog 空态权限感知", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedDashboard.mockResolvedValue({ total: 0, by_domain: {}, counts: {} } as any);
    mockedDomains.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, role: "viewer" } as any);
    mockedFavorites.mockResolvedValue([]);
    mockedUsers.mockResolvedValue([]);
    mockedList.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 });
    // 无任何创建权限（viewer/analyst）——用与真实 API 一致的结构（ui_actions 空数组）
    (fetchMyPermissions as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      user_id: 1, role: "viewer", home_domain: "", allowed_actions: [], ui_actions: [],
      granted_domains: [], metric_whitelist: [], row_level_restricted: false,
      grants: [], expiring_soon: [],
    });
  });

  it("无创建权限时空态不显示创建按钮，改为引导联系管理员", async () => {
    const user = { id: 1, username: "zhangsan", display_name: "张三", role: "viewer", domain: "sales", org_id: 1 };
    render(
      <MemoryRouter initialEntries={["/catalog"]}>
        <Routes>
          <Route
            path="/catalog"
            element={
              <PermissionProvider user={user}>
                <MetricCatalog />
              </PermissionProvider>
            }
          />
          <Route path="/detail/:code" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>,
    );
    // 先等空态出现（列表为空），再等权限加载完成（can() 在 snapshot 为 null 时默认放行，
    // 需等 fetchMyPermissions 生效后权限引导才出现）
    await waitFor(() => {
      expect(screen.getByText("目录还是空的，创建第一个指标或从模板开始")).toBeTruthy();
    });
    await waitFor(() => {
      expect(screen.getByText("如需创建指标，请联系域管理员或平台管理员")).toBeTruthy();
    });
    // 权限引导出现（canCreate=false）后，不应再出现「创建指标 / 从模板创建」按钮
    expect(screen.queryAllByText("创建指标").length).toBe(0);
    expect(screen.queryAllByText("从模板创建").length).toBe(0);
  });
});

describe("MetricCatalog 回收站（已删除草稿恢复）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedDashboard.mockResolvedValue({ total: 0, by_domain: {}, counts: {} } as any);
    mockedDomains.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, role: "platform_admin" } as any);
    mockedFavorites.mockResolvedValue([]);
    mockedUsers.mockResolvedValue([]);
    (fetchMyPermissions as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      user_id: 1, role: "platform_admin", home_domain: "", allowed_actions: [], ui_actions: ["metric:create", "metric:deprecate", "metric:edit", "metric:export"],
      granted_domains: [], metric_whitelist: [], row_level_restricted: false, grants: [], expiring_soon: [],
    });
  });

  it("回收站视图下拉取已删除草稿（deleted=true）并展示恢复按钮", async () => {
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 20 });
    const user = { id: 1, username: "admin", display_name: "管理员", role: "platform_admin", domain: null, org_id: 1 };
    render(
      <MemoryRouter initialEntries={["/catalog"]}>
        <Routes>
          <Route path="/catalog" element={<PermissionProvider user={user}><MetricCatalog /></PermissionProvider>} />
          <Route path="/detail/:code" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /回收站/ }));
    await waitFor(() => {
      expect(mockedList.mock.calls.some((c) => c[0]?.deleted === true)).toBe(true);
    });
    expect(screen.getByRole("button", { name: /恢复/ })).toBeTruthy();
  });

  it("点击恢复调用 restoreMetric", async () => {
    const restoreMock = vi.mocked(restoreMetric).mockResolvedValue(metric as any);
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 20 });
    const user = { id: 1, username: "admin", display_name: "管理员", role: "platform_admin", domain: null, org_id: 1 };
    render(
      <MemoryRouter initialEntries={["/catalog"]}>
        <Routes>
          <Route path="/catalog" element={<PermissionProvider user={user}><MetricCatalog /></PermissionProvider>} />
          <Route path="/detail/:code" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /回收站/ }));
    fireEvent.click(await screen.findByRole("button", { name: /恢复/ }));
    await waitFor(() => expect(restoreMock).toHaveBeenCalledWith(metric.metric_code));
  });

  it("回收站视图导出按钮禁用（含已软删指标，避免误用为正式数据）", async () => {
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 20 });
    const user = { id: 1, username: "admin", display_name: "管理员", role: "platform_admin", domain: null, org_id: 1 };
    render(
      <MemoryRouter initialEntries={["/catalog"]}>
        <Routes>
          <Route path="/catalog" element={<PermissionProvider user={user}><MetricCatalog /></PermissionProvider>} />
          <Route path="/detail/:code" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /回收站/ }));
    await waitFor(() => {
      expect(mockedList.mock.calls.some((c) => c[0]?.deleted === true)).toBe(true);
    });
    const btn = screen.getByRole("button", { name: /导出/ }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("DRAFT 且存在 reject_reason 时状态列显示「被驳回」标识（FR-005 可追溯）", async () => {
    mockedList.mockResolvedValue({
      items: [{ ...metric, status: "DRAFT", reject_reason: "粒度与口径不符，请修正" }],
      total: 1,
      page: 1,
      page_size: 20,
    });
    renderCatalog();
    await screen.findByText("sales_gmv_sum_d");
    expect(screen.getByText("被驳回")).toBeInTheDocument();
  });

});

describe("列宽拖拽（resizable columns）", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockedPermissions.mockResolvedValue({
      user_id: 1,
      role: "platform_admin",
      home_domain: null,
      allowed_actions: ["read", "write", "approve", "deprecate"],
      ui_actions: ["metric:create", "metric:approve", "metric:deprecate"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
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
    // OneData 原子层：逻辑度量目录（明细抽屉"逻辑度量"行渲染用）
    mockedMeasures.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
    mockedDomains.mockResolvedValue([
      {
        id: 1, code: "sales", name: "销售域", parent_id: null, level: 1,
        sort_order: 0, status: "ACTIVE", metric_count: 1, children: [],
      },
    ]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "sales", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    // jsdom 默认不触发 requestAnimationFrame（需 pretendToBeVisual），stub 成同步执行，
    // 让 startResize 内的列宽更新即时生效，便于断言 localStorage 持久化。
    vi.stubGlobal("requestAnimationFrame", (cb: (t: number) => void) => {
      cb(0);
      return 0;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {});
  });

  it("拖拽手柄：mousedown+mousemove 持久化列宽到 localStorage，且重置按钮可清除偏好", async () => {
    renderCatalog();
    const handles = await screen.findAllByLabelText("拖拽调整列宽");
    expect(handles.length).toBeGreaterThan(0);
    const handle = handles[0];
    fireEvent.mouseDown(handle, { clientX: 100 });
    fireEvent.mouseMove(handle, { clientX: 240 }); // 右拖 140px（事件冒泡到 document 上的 mousemove 监听）
    fireEvent.mouseUp(handle);
    await waitFor(() => {
      const stored = JSON.parse(localStorage.getItem("unisense.metric-catalog.colWidths") || "{}");
      const keys = Object.keys(stored);
      expect(keys.length).toBe(1);
      expect(stored[keys[0]]).toBeGreaterThan(190); // 比默认 190 更宽
    });
    // 重置列宽按钮存在且可用（存在自定义列宽时）
    const resetBtn = screen.getByRole("button", { name: /重置列宽/ }) as HTMLButtonElement;
    expect(resetBtn.disabled).toBe(false);
    fireEvent.click(resetBtn);
    await waitFor(() => {
      expect(localStorage.getItem("unisense.metric-catalog.colWidths")).toBe("{}");
    });
    expect((screen.getByRole("button", { name: /重置列宽/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("初始渲染从 localStorage 恢复记忆的列宽（重置按钮可用）", async () => {
    localStorage.setItem("unisense.metric-catalog.colWidths", JSON.stringify({ metric_code: 320 }));
    renderCatalog();
    await waitFor(() => {
      // 记忆的宽度被读入 state，重置按钮可点（说明存在自定义列宽偏好）
      expect((screen.getByRole("button", { name: /重置列宽/ }) as HTMLButtonElement).disabled).toBe(false);
    });
  });
});

describe("MetricCatalog 按用户群体差异化（OneData 治理）", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockedPermissions.mockResolvedValue({
      user_id: 1,
      role: "platform_admin",
      home_domain: null,
      allowed_actions: ["read", "write", "approve", "deprecate"],
      ui_actions: ["metric:create", "metric:approve", "metric:deprecate"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
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
    mockedMeasures.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 });
    mockedDomains.mockResolvedValue([
      { id: 1, code: "sales", name: "销售域", parent_id: null, level: 1, sort_order: 0, status: "ACTIVE", metric_count: 1, children: [] },
    ]);
    mockedFavorites.mockResolvedValue([]);
  });

  /** 覆盖 fetchCurrentUser 角色（列显隐/默认筛选以它为准），PermissionProvider 权限随之一致。 */
  function renderWithRole(role: string, domain: string | null = null) {
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "u",
      display_name: "用户",
      role,
      domain,
      org_id: 1,
    } as never);
    return render(
      <MemoryRouter initialEntries={["/catalog"]}>
        <Routes>
          <Route
            path="/catalog"
            element={
              <PermissionProvider
                user={{ id: 1, username: "u", display_name: "用户", role, domain, org_id: 1 } as never}
              >
                <MetricCatalog />
              </PermissionProvider>
            }
          />
          <Route path="/detail/:code" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("reviewer 默认筛选 REVIEW 且应用治理审核列（含提交人）", async () => {
    renderWithRole("reviewer");
    await waitFor(() => {
      expect(
        mockedList.mock.calls.some(([p]) => (p as { status?: string }).status === "REVIEW"),
      ).toBe(true);
    });
    await screen.findByText("sales_gmv_sum_d");
    // 治理审核默认列：表头含提交人，不含健康/业务域（生产者/消费者专属列）
    const thead = document.querySelector(".ant-table-thead") as HTMLElement;
    expect(within(thead).getByText("提交人")).toBeTruthy();
    expect(within(thead).queryByText("健康")).toBeNull();
    expect(within(thead).queryByText("业务域")).toBeNull();
  });

  it("compliance_officer 默认只看 PII 指标", async () => {
    renderWithRole("compliance_officer");
    await waitFor(() => {
      expect(
        mockedList.mock.calls.some(([p]) => (p as { pii_flag?: boolean }).pii_flag === true),
      ).toBe(true);
    });
    expect(screen.getByText("只看PII")).toBeTruthy();
  });

  it("metric_owner 默认只看我的指标并应用生产者列（含健康）", async () => {
    renderWithRole("metric_owner");
    await waitFor(() => {
      expect(
        mockedList.mock.calls.some(([p]) => (p as { owner_id?: number }).owner_id === 1),
      ).toBe(true);
    });
    await screen.findByText("sales_gmv_sum_d");
    // 生产者默认列：表头含健康，不含业务域（消费者/治理列）
    const thead = document.querySelector(".ant-table-thead") as HTMLElement;
    expect(within(thead).getByText("健康")).toBeTruthy();
    expect(within(thead).queryByText("业务域")).toBeNull();
  });

  it("domain_admin 默认按本人域过滤", async () => {
    renderWithRole("domain_admin", "sales");
    await waitFor(() => {
      expect(
        mockedList.mock.calls.some(([p]) => (p as { domain?: string }).domain === "sales"),
      ).toBe(true);
    });
  });

  it("平台管理员（admin）默认全量列", async () => {
    renderWithRole("platform_admin");
    await screen.findByText("sales_gmv_sum_d");
    const thead = document.querySelector(".ant-table-thead") as HTMLElement;
    expect(within(thead).getByText("业务域")).toBeTruthy();
    expect(within(thead).getByText("健康")).toBeTruthy();
    expect(within(thead).getByText("治理徽章")).toBeTruthy();
    expect(within(thead).getByText("提交人")).toBeTruthy();
  });

  it("点击恢复角色默认恢复群体默认列", async () => {
    renderWithRole("platform_admin");
    await screen.findByText("sales_gmv_sum_d");
    const thead = document.querySelector(".ant-table-thead") as HTMLElement;
    expect(within(thead).getByText("健康")).toBeTruthy();
    // 打开列设置，取消「健康」列 → 列隐藏（表头判断，避免 Dropdown 弹层选项文本干扰）
    fireEvent.click(screen.getByRole("button", { name: /列设置/ }));
    const popup = document.querySelector(".ant-dropdown:not(.ant-dropdown-hidden)") as HTMLElement;
    expect(popup).toBeTruthy();
    fireEvent.click(within(popup).getByText("健康"));
    await waitFor(() => {
      expect(within(thead).queryByText("健康")).toBeNull();
    });
    // 点击恢复角色默认 → 健康列回归（admin 默认全量列）
    fireEvent.click(within(popup).getByText("恢复角色默认"));
    await waitFor(() => {
      expect(within(thead).getByText("健康")).toBeTruthy();
    });
  });
});
