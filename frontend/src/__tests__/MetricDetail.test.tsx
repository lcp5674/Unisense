import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { MetricDetail } from "../pages/MetricDetail";
import { PermissionProvider } from "../hooks/usePermission";
import type { MetricHealth, MetricResponse } from "../types";

vi.mock("../api", () => ({
  getMetric: vi.fn(),
  fetchArchivedMetric: vi.fn(),
  listVersions: vi.fn(),
  fetchCurrentUser: vi.fn(),
  fetchMyPermissions: vi.fn(),
  listFavorites: vi.fn(),
  listMetrics: vi.fn(),
  listCatalogs: vi.fn().mockResolvedValue({ items: [] }),
  getMetricHealth: vi.fn(),
  listDictItems: vi.fn(),
  listDimensions: vi.fn(),
  listDomainTree: vi.fn(),
  listUsers: vi.fn(),
  listSubscriptions: vi.fn(),
  fetchRelatedMetrics: vi.fn(),
  addFavorite: vi.fn(),
  removeFavorite: vi.fn(),
  approveMetric: vi.fn(),
  deprecateMetric: vi.fn(),
  recoverSourceDropped: vi.fn(),
  confirmDeprecateDropped: vi.fn(),
  emergencyPublishMetric: vi.fn(),
  piiReview: vi.fn(),
  promoteMetric: vi.fn(),
  rollbackMetric: vi.fn(),
  submitReview: vi.fn(),
  updateMetric: vi.fn(),
  updateMetricDescription: vi.fn(),
  suggestRenameName: vi.fn(),
  inferMetricDescription: vi.fn(),
  upsertSubscription: vi.fn(),
  // 详情页子组件依赖
  listQualityEvents: vi.fn().mockResolvedValue({ items: [] }),
  listQualityRules: vi.fn().mockResolvedValue({ items: [] }),
  listSnapshots: vi.fn().mockResolvedValue([]),
  qualityEventAck: vi.fn(),
  qualityEventResolve: vi.fn(),
  qualityEventClose: vi.fn(),
  confirmMetricVersion: vi.fn(),
  extendMetricVersion: vi.fn(),
  rejectMetricVersion: vi.fn(),
  lineageImpact: vi.fn().mockResolvedValue([]),
  listAudit: vi.fn().mockResolvedValue({ items: [] }),
  listMetricDimensions: vi.fn().mockResolvedValue([]),
  UnisenseApiError: class extends Error {},
}));
const trackMock = vi.fn();
vi.mock("../hooks/useTracking", () => ({
  useTracking: () => ({ track: trackMock }),
}));

import {
  getMetric,
  fetchArchivedMetric,
  listVersions,
  fetchCurrentUser,
  fetchMyPermissions,
  listDomainTree,
  listDictItems,
  listDimensions,
  listFavorites,
  listMetrics,
  listCatalogs,
  getMetricHealth,
  listUsers,
  listSubscriptions,
  fetchRelatedMetrics,
  updateMetric,
  updateMetricDescription,
  suggestRenameName,
  inferMetricDescription,
  submitReview,
  emergencyPublishMetric,
  recoverSourceDropped,
  confirmDeprecateDropped,
  UnisenseApiError,
} from "../api";
const mockedUpdateMetric = vi.mocked(updateMetric);
const mockedUpdateDesc = vi.mocked(updateMetricDescription);
const mockedSuggestRename = vi.mocked(suggestRenameName);
const mockedInferDesc = vi.mocked(inferMetricDescription);
const mockedGetMetric = vi.mocked(getMetric);
const mockedFetchArchived = vi.mocked(fetchArchivedMetric);
const mockedListVersions = vi.mocked(listVersions);
const mockedMyPerms = vi.mocked(fetchMyPermissions);
const mockedCurrentUser = vi.mocked(fetchCurrentUser);
const mockedDomainTree = vi.mocked(listDomainTree);
const mockedDictItems = vi.mocked(listDictItems);
const mockedDimensions = vi.mocked(listDimensions);
const mockedCatalogs = vi.mocked(listCatalogs);
const mockedListMetrics = vi.mocked(listMetrics);
const mockedFavorites = vi.mocked(listFavorites);
const mockedHealth = vi.mocked(getMetricHealth);
const mockedUsers = vi.mocked(listUsers);
const mockedSubs = vi.mocked(listSubscriptions);
const mockedRelated = vi.mocked(fetchRelatedMetrics);
const mockedSubmitReview = vi.mocked(submitReview);

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
  effective_version: 2,
  consumption_guide: null,
  successor_code: null,
  deprecated_at: null,
  sunset_until: null,
  emergency_publish: false,
  emergency_reason: null,
  gray_tenant_ids: null,
  pending_conflict: false,
  pending_conflict_detail: null,
  pending_version: false,
  created_at: "2026-08-01T00:00:00",
  updated_at: "2026-08-02T00:00:00",
};

function renderDetail(initialEntry: { pathname: string; state?: { from?: string } }) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/detail/:code"
          element={
            <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "metric_owner", domain: "sales", org_id: 1 }}>
              <MetricDetail />
            </PermissionProvider>
          }
        />
        <Route path="/catalog" element={<div>catalog-page</div>} />
        <Route path="/dashboard" element={<div>dashboard-page</div>} />
        <Route path="/todo" element={<div>todo-page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("MetricDetail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedGetMetric.mockResolvedValue(metric);
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([
      { id: 1, code: "sales", name: "销售域", parent_id: null, level: 1, sort_order: 0, status: "active", metric_count: 0, children: [] },
    ]);
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "zhangsan",
      display_name: "张三",
      role: "metric_owner",
      domain: "sales",
      org_id: 1,
    });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    // renderDetail 也用 PermissionProvider 包裹：默认给 metric_owner 基础权限，让按钮按权限正常渲染
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "sales",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create", "metric:edit", "metric:deprecate", "catalog:view", "metric:infer-description"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
  });

  it("提供统一的返回按钮，从指标目录进入（历史栈有上一页）时回退到指标目录", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/catalog", "/detail/sales_gmv_sum_d"]}>
        <Routes>
          <Route path="/detail/:code" element={<MetricDetail />} />
          <Route path="/catalog" element={<div>catalog-page</div>} />
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
        </Routes>
      </MemoryRouter>,
    );
    const btn = await screen.findByRole("button", { name: /返\s*回/ });
    fireEvent.click(btn);
    await screen.findByText("catalog-page");
    lengthSpy.mockRestore();
  });

  it("URL 直达无上一页时点击返回兜底跳转总览仪表", async () => {
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    const btn = await screen.findByRole("button", { name: /返\s*回/ });
    fireEvent.click(btn);
    await screen.findByText("dashboard-page");
  });

  it("从总览仪表「为你推荐」进入时，返回按钮为统一文案并精确返回仪表盘", async () => {
    renderDetail({ pathname: "/detail/sales_gmv_sum_d", state: { from: "dashboard" } });
    // 来源感知影响跳转目标（仪表盘），文案统一为"返回"（与其他页面一致）
    const btn = await screen.findByRole("button", { name: /返\s*回/ });
    expect(btn.textContent).not.toMatch(/←/);
    fireEvent.click(btn);
    await screen.findByText("dashboard-page");
  });

  it("从待办中心进入时，返回按钮为统一文案并精确返回 /todo", async () => {
    renderDetail({ pathname: "/detail/sales_gmv_sum_d", state: { from: "todo" } });
    // 来源感知影响跳转目标（待办中心），文案统一为"返回"
    const btn = await screen.findByRole("button", { name: /返\s*回/ });
    expect(btn.textContent).not.toMatch(/←/);
    fireEvent.click(btn);
    await screen.findByText("todo-page");
  });

  it("仲裁为权威口径时展示绿色「权威口径」Tag（TD §12.4）", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      arbitration_mark: {
        status: "canonical",
        conflict_id: "CF-ABC",
        decision: "merge",
        ruled_at: "2026-08-15T04:00:00Z",
        opposite_code: "sales_gmv_d",
      },
    });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    const tag = await screen.findByText("权威口径");
    expect(tag).toBeInTheDocument();
    // 悬停 Tooltip 展示裁决明细（决策中文 + 落败方）
    fireEvent.mouseEnter(tag);
    expect(await screen.findByText(/合并口径/)).toBeInTheDocument();
    expect(screen.getByText(/落败方 sales_gmv_d/)).toBeInTheDocument();
  });

  it("仲裁保留差异时展示蓝色「已裁定共存」Tag", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      arbitration_mark: {
        status: "coexist",
        conflict_id: "CF-DEF",
        decision: "keep_diff",
        ruled_at: "2026-08-15T04:00:00Z",
        opposite_code: "sales_gmv_d",
      },
    });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    const tag = await screen.findByText("已裁定共存");
    expect(tag).toBeInTheDocument();
    fireEvent.mouseEnter(tag);
    expect(await screen.findByText(/保留差异/)).toBeInTheDocument();
  });

  it("仲裁「保留差异+指定改名」时展示橙色待改名 Tag，Owner 可去改名并清除标记", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      name: "旧名称",
      arbitration_mark: {
        status: "coexist",
        conflict_id: "CF-RENAME",
        decision: "keep_diff",
        ruled_at: "2026-08-15T04:00:00Z",
        opposite_code: "sales_gmv_d",
        rename_required: true,
        rename_opposite_code: "sales_gmv_d",
      },
    });
    mockedUpdateMetric.mockResolvedValue(metric);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });

    // 待改名 Tag 展示
    const tag = await screen.findByText("仲裁要求改名");
    expect(tag).toBeInTheDocument();

    // Owner 可点击「去改名」打开弹窗
    const renameBtn = screen.getByRole("button", { name: /去\s*改\s*名/ });
    fireEvent.click(renameBtn);
    await screen.findByText("指标改名（响应仲裁要求）");

    // 输入新名称 + 原因后提交 → 调 updateMetric（name + change_reason）
    const nameInput = screen.getByPlaceholderText("新的指标名称") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "新名称" } });
    const reasonInput = screen.getByPlaceholderText(/变更原因/) as HTMLTextAreaElement;
    fireEvent.change(reasonInput, { target: { value: "响应仲裁改名要求" } });
    fireEvent.click(screen.getByText("确认改名"));
    await waitFor(() =>
      expect(mockedUpdateMetric).toHaveBeenCalledWith("sales_gmv_sum_d", {
        name: "新名称",
        change_reason: "响应仲裁改名要求",
        row_version: 1,
      }),
    );
  });

  it("仲裁改名弹窗支持 AI 生成名称建议——候选展示、点选填入、可编辑后提交", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      name: "销售金额",
      arbitration_mark: {
        status: "coexist",
        conflict_id: "CF-RENAME",
        decision: "keep_diff",
        ruled_at: "2026-08-15T04:00:00Z",
        opposite_code: "sales_gmv_d",
        rename_required: true,
        rename_opposite_code: "sales_gmv_d",
      },
    });
    mockedSuggestRename.mockResolvedValue({
      current_name: "销售金额",
      suggestions: [
        { name: "销售金额（日口径）", reason: "追加统计周期以区分", source: "llm" },
        { name: "日销售总额", reason: "语义更聚焦日粒度", source: "llm" },
      ],
    });
    mockedUpdateMetric.mockResolvedValue(metric);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });

    // 打开改名弹窗
    const renameBtn = await screen.findByRole("button", { name: /去\s*改\s*名/ });
    fireEvent.click(renameBtn);
    await screen.findByText("指标改名（响应仲裁要求）");

    // 点击「AI 生成名称建议」→ 展示候选（含 AI 来源与理由）
    fireEvent.click(screen.getByRole("button", { name: /AI 生成名称建议/ }));
    expect(await screen.findByText("销售金额（日口径）")).toBeInTheDocument();
    expect(screen.getByText(/AI 生成 · 追加统计周期以区分/)).toBeInTheDocument();
    expect(mockedSuggestRename).toHaveBeenCalledWith(
      "sales_gmv_sum_d",
      "sales_gmv_d", // rename_opposite_code 作为对方指标
    );

    // 点选第二个候选 → 名称输入框填入该候选
    fireEvent.click(screen.getByText("日销售总额"));
    const nameInput = screen.getByPlaceholderText("新的指标名称") as HTMLInputElement;
    expect(nameInput.value).toBe("日销售总额");

    // 用户可继续编辑候选 → 提交改名
    fireEvent.change(nameInput, { target: { value: "日销售总额(新)" } });
    const reasonInput = screen.getByPlaceholderText(/变更原因/) as HTMLTextAreaElement;
    fireEvent.change(reasonInput, { target: { value: "响应仲裁改名要求" } });
    fireEvent.click(screen.getByText("确认改名"));
    await waitFor(() =>
      expect(mockedUpdateMetric).toHaveBeenCalledWith("sales_gmv_sum_d", {
        name: "日销售总额(新)",
        change_reason: "响应仲裁改名要求",
        row_version: 1,
      }),
    );
  });

  it("仲裁作废指标（METRIC_ARCHIVED）直访时展示醒目引导 + 历史详情并可跳转权威指标", async () => {
    localStorage.removeItem("unisense:archived_banner_dismissed");
    const err = Object.assign(
      new UnisenseApiError("指标已因口径裁决作废: sales_e2e_conflictb_day", "METRIC_ARCHIVED", 404, "test-trace"),
      {
        code: "METRIC_ARCHIVED",
        codeZh: "该指标已因口径裁决作废，请查看权威指标",
        detail: {
          metric_code: "sales_e2e_conflictb_day",
          successor_code: "sales_e2e_conflicta_day",
          arbitration_mark: {
            status: "defeated",
            conflict_id: "CF-ABC",
            decision: "merge",
            ruled_at: "2026-08-15T04:00:00Z",
            opposite_code: "sales_e2e_conflicta_day",
          },
        },
      },
    );
    mockedGetMetric.mockRejectedValue(err);
    // 作废历史详情（供追溯面板）
    mockedFetchArchived.mockResolvedValue({
      metric: { ...metric, metric_code: "sales_e2e_conflictb_day", name: "E2E 冲突指标 B" },
      successor_code: "sales_e2e_conflicta_day",
      arbitration_mark: { decision: "merge" },
    });
    renderDetail({ pathname: "/detail/sales_e2e_conflictb_day" });

    // 醒目引导（warning）而非裸「指标不存在」
    expect(await screen.findByRole("button", { name: /sales_e2e_conflicta_day/ })).toBeInTheDocument();
    expect(screen.queryByText("指标不存在")).not.toBeInTheDocument();
    // 历史详情面板展示作废指标详情
    expect(await screen.findByText("作废指标历史详情（仅供追溯）")).toBeInTheDocument();
    expect(screen.getByText("E2E 冲突指标 B")).toBeInTheDocument();
    // 首次进入弹出醒目引导（标题「指标已作废」——page-head + Modal 两处）
    expect(screen.getAllByText("指标已作废").length).toBeGreaterThan(0);
    // 权威指标跳转按钮 → 点击后以新 code 重新拉取详情
    const jump = screen.getByRole("button", { name: /sales_e2e_conflicta_day/ });
    fireEvent.click(jump);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalledWith("sales_e2e_conflicta_day"));
  });

  it("废弃指标显示「重新提交评审」，提交后走重评审闭环（DEPRECATED→REVIEW，TD §13）", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DEPRECATED",
      successor_code: "sales_gmv_sum_w",
      deprecated_at: "2026-08-10T00:00:00",
      sunset_until: "2026-08-20T00:00:00",
    } as MetricResponse);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    // DEPRECATED 状态下显示「重新提交评审」按钮（而非「提交评审」）
    const btn = await screen.findByRole("button", { name: /重新提交评审/ });
    fireEvent.click(btn);
    // 提交评审弹窗打开
    expect(
      screen.getByText("提交后将进入评审状态（DRAFT → REVIEW），由指定评审人通过或打回。"),
    ).toBeInTheDocument();
    // 提交 → submitReview 以重评审语义调用（未指派 → reviewer_type null）
    fireEvent.click(screen.getByRole("button", { name: "提交评审" }));
    await waitFor(() =>
      expect(mockedSubmitReview).toHaveBeenCalledWith("sales_gmv_sum_d", "提交评审", {
        reviewer_id: null,
        reviewer_type: null,
        reviewer_domain: "sales",
      }),
    );
  });

  it("DRAFT 且存在 reject_reason 时展示驳回原因引导横幅（FR-005 可追溯）", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      reject_reason: "粒度 day 与口径定义不符，请改为 order 粒度",
      rejected_at: "2026-08-14T10:00:00",
    });
    render(
      <MemoryRouter initialEntries={[{ pathname: "/detail/sales_gmv_sum_d" }]}>
        <Routes>
          <Route
            path="/detail/:code"
            element={
              <PermissionProvider
                user={{ id: 1, username: "u", display_name: "U", role: "metric_owner", domain: "sales", org_id: 1 }}
              >
                <MetricDetail />
              </PermissionProvider>
            }
          />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("该指标上次评审被驳回")).toBeInTheDocument();
    });
    expect(screen.getByText(/粒度 day 与口径定义不符/)).toBeInTheDocument();
  });
});

describe("MetricDetail 按钮级权限过滤", () => {
  function renderWithPerms(ui_actions: string[], role = "custom") {
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role,
      home_domain: "sales",
      allowed_actions: ["read", "write"],
      ui_actions,
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    return render(
      <MemoryRouter initialEntries={[{ pathname: "/detail/sales_gmv_sum_d" }]}>
        <Routes>
          <Route
            path="/detail/:code"
            element={
              <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role, domain: "sales", org_id: 1 }}>
                <MetricDetail />
              </PermissionProvider>
            }
          />
          <Route path="/catalog" element={<div>catalog-page</div>} />
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("有 metric:approve 权限点时 REVIEW 状态显示审批/灰度发布按钮", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "REVIEW", pii_flag: false });
    renderWithPerms(["metric:approve", "metric:emergency-publish"]);
    expect(await screen.findByText("审批通过")).toBeInTheDocument();
    expect(screen.getByText("灰度发布")).toBeInTheDocument();
    expect(screen.getByText("紧急发布")).toBeInTheDocument();
  });

  it("无 metric:approve 权限点时 REVIEW 状态不显示发布按钮", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "REVIEW", pii_flag: false });
    renderWithPerms(["metric:view"]);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    // 等待权限快照加载后断言按钮隐藏
    await waitFor(() => expect(screen.queryByText("审批通过")).not.toBeInTheDocument());
    expect(screen.queryByText("灰度发布")).not.toBeInTheDocument();
    expect(screen.queryByText("紧急发布")).not.toBeInTheDocument();
  });

  it("无 metric:deprecate 权限点时 PUBLISHED 状态不显示废弃按钮", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "PUBLISHED", pii_flag: false });
    renderWithPerms(["metric:view"]);
    await waitFor(() => expect(screen.queryByText("废弃")).not.toBeInTheDocument());
  });

  it("具备 pii:review 权限点且 PII 未复核时显示 PII 合规复核按钮", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "REVIEW", pii_flag: true, compliance_reviewed: false });
    renderWithPerms(["pii:review"]);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("PII 合规复核")).toBeInTheDocument());
  });

  it("无 pii:review 权限点时即使 PII 未复核也不显示 PII 复核按钮", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "REVIEW", pii_flag: true, compliance_reviewed: false });
    renderWithPerms(["metric:view"]);
    await waitFor(() => expect(screen.queryByText("PII 合规复核")).not.toBeInTheDocument());
  });

  it("紧急发布原因不足 10 字时前端拦截，不调用 API（避免 422 甩给后端）", async () => {
    const emergencySpy = vi.fn();
    vi.mocked(emergencyPublishMetric).mockImplementation(emergencySpy);
    mockedGetMetric.mockResolvedValue({ ...metric, status: "REVIEW", pii_flag: false });
    renderWithPerms(["metric:emergency-publish", "metric:approve"]);
    await screen.findByText("紧急发布");
    fireEvent.click(screen.getByText("紧急发布"));
    // 打开弹窗，输入不足 10 字的原因
    await screen.findByText("确认紧急发布");
    fireEvent.change(screen.getByPlaceholderText(/紧急发布原因/), { target: { value: "太短" } });
    fireEvent.click(screen.getByText("确认紧急发布"));
    await waitFor(() => expect(screen.getByText(/至少 10 字/)).toBeInTheDocument());
    expect(emergencySpy).not.toHaveBeenCalled();
  });

  it("REVIEW 状态不显示「废弃」按钮（产品语义：仅已发布/灰度可废弃）", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "REVIEW", pii_flag: false });
    renderWithPerms(["metric:deprecate", "metric:approve"]);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText("废弃")).not.toBeInTheDocument());
  });

  it("EXPERIMENTAL（灰度）状态不显示「废弃」与「提交评审」——后端状态机仅支持 promote/rollback，防止 409 拒绝", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "EXPERIMENTAL", pii_flag: false });
    renderWithPerms(["metric:deprecate", "metric:create", "metric:edit", "metric:rollback"]);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.queryByText("废弃")).not.toBeInTheDocument();
      expect(screen.queryByText("提交评审")).not.toBeInTheDocument();
    });
    // 灰度状态应有「全量发布 / 回滚」退出路径
    await waitFor(() => {
      expect(screen.getByText("全量发布")).toBeTruthy();
      expect(screen.getByText("回滚")).toBeTruthy();
    });
  });

  it("权限快照加载完成前不显示「审批通过」按钮（fail-open 消除）", async () => {
    // 快照挂起（模拟慢加载），Provider 一直处于 loading
    mockedMyPerms.mockReturnValue(new Promise(() => {}));
    mockedGetMetric.mockResolvedValue({ ...metric, status: "REVIEW", pii_flag: false });
    render(
      <MemoryRouter initialEntries={[{ pathname: "/detail/sales_gmv_sum_d" }]}>
        <Routes>
          <Route
            path="/detail/:code"
            element={
              <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "custom", domain: "sales", org_id: 1 }}>
                <MetricDetail />
              </PermissionProvider>
            }
          />
          <Route path="/catalog" element={<div>catalog-page</div>} />
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    // 快照未加载完（loading=true），审批按钮不应渲染（后端会拒绝，UI 也不该短暂可见）
    expect(screen.queryByText("审批通过")).not.toBeInTheDocument();
    expect(screen.queryByText("废弃")).not.toBeInTheDocument();
  });

  it("REVIEW 状态标签显示「审核中」而非「审核」", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "REVIEW", pii_flag: false });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    expect(await screen.findByText("审核中")).toBeInTheDocument();
  });

  it("有 metric:infer-description 权限点时仲裁改名弹窗显示「AI 生成名称建议」", async () => {
    const renameMetric: MetricResponse = {
      ...metric,
      status: "PUBLISHED",
      pii_flag: false,
      arbitration_mark: {
        status: "coexist",
        conflict_id: "C-1",
        decision: "keep_diff",
        ruled_at: "2026-08-15T04:00:00Z",
        opposite_code: "sales_gmv_d",
        rename_required: true,
      },
    };
    mockedGetMetric.mockResolvedValue(renameMetric);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "sales",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:infer-description", "metric:edit"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    render(
      <MemoryRouter initialEntries={[{ pathname: "/detail/sales_gmv_sum_d" }]}>
        <Routes>
          <Route
            path="/detail/:code"
            element={
              <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "metric_owner", domain: "sales", org_id: 1 }}>
                <MetricDetail />
              </PermissionProvider>
            }
          />
          <Route path="/catalog" element={<div>catalog-page</div>} />
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    fireEvent.click(await screen.findByText("去改名"));
    expect(await screen.findByText("AI 生成名称建议")).toBeInTheDocument();
  });

  it("无 metric:infer-description 权限点时仲裁改名弹窗不显示「AI 生成名称建议」", async () => {
    const renameMetric: MetricResponse = {
      ...metric,
      status: "PUBLISHED",
      pii_flag: false,
      arbitration_mark: {
        status: "coexist",
        conflict_id: "C-1",
        decision: "keep_diff",
        ruled_at: "2026-08-15T04:00:00Z",
        opposite_code: "sales_gmv_d",
        rename_required: true,
      },
    };
    mockedGetMetric.mockResolvedValue(renameMetric);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "sales",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:edit"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    render(
      <MemoryRouter initialEntries={[{ pathname: "/detail/sales_gmv_sum_d" }]}>
        <Routes>
          <Route
            path="/detail/:code"
            element={
              <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "metric_owner", domain: "sales", org_id: 1 }}>
                <MetricDetail />
              </PermissionProvider>
            }
          />
          <Route path="/catalog" element={<div>catalog-page</div>} />
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    fireEvent.click(await screen.findByText("去改名"));
    await waitFor(() => expect(screen.queryByText("AI 生成名称建议")).not.toBeInTheDocument());
  });

  it("DATA_SOURCE_DROPPED 状态显示「源已恢复」和「确认退役」按钮", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DATA_SOURCE_DROPPED", pii_flag: false });
    renderWithPerms(["metric:deprecate", "metric:edit"]);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    expect(await screen.findByText("源已恢复")).toBeInTheDocument();
    expect(screen.getByText("确认退役")).toBeInTheDocument();
  });

  it("点「源已恢复」调用 recoverSourceDropped 并刷新", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DATA_SOURCE_DROPPED", pii_flag: false });
    renderWithPerms(["metric:deprecate", "metric:edit"]);
    fireEvent.click(await screen.findByText("源已恢复"));
    await waitFor(() =>
      expect(vi.mocked(recoverSourceDropped)).toHaveBeenCalledWith("sales_gmv_sum_d"),
    );
  });

  it("DSD 状态「确认退役」弹窗提交调 confirmDeprecateDropped", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DATA_SOURCE_DROPPED", pii_flag: false });
    renderWithPerms(["metric:deprecate", "metric:edit"]);
    fireEvent.click(await screen.findByText("确认退役"));
    await screen.findByText("确认退役（数据源下线）");
    // 弹窗内确认按钮 okText 为「确认废弃」（退役本质是废弃）：用 role 定位避免与页面「确认退役」按钮歧义
    fireEvent.click(screen.getByRole("button", { name: /确\s*认\s*废\s*弃/ }));
    await waitFor(() =>
      expect(vi.mocked(confirmDeprecateDropped)).toHaveBeenCalledWith("sales_gmv_sum_d", ""),
    );
  });

  it("有 infer-description 权限时显示「AI 生成描述」，点击调用接口并展示生成描述", async () => {
    // PII 指标 + 敏感角色（合规官）：piiMasked=false，描述可见、AI 生成功能完整验证。
    // 非敏感角色触发 AI 生成描述属越权场景（canInferDesc=false），不在此用例。
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "admin",
      display_name: "管理员",
      role: "compliance_officer",
      domain: "sales",
      org_id: 1,
    });
    mockedGetMetric.mockResolvedValue(metric);
    mockedInferDesc.mockResolvedValue({ ...metric, description: "由 AI 生成的业务描述" });
    renderWithPerms(["metric:infer-description"], "compliance_officer");
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    fireEvent.click(await screen.findByText("AI 生成描述"));
    await waitFor(() => expect(mockedInferDesc).toHaveBeenCalledWith("sales_gmv_sum_d", undefined));
    expect(await screen.findByText("由 AI 生成的业务描述")).toBeInTheDocument();
  });

  it("Owner 可「编辑描述」：弹窗修改后保存调用 updateMetricDescription 并刷新", async () => {
    // 按钮级权限过滤 describe 不继承外层 beforeEach 的 currentUser mock，
    // 编辑按钮依赖 currentUser.role（isOwnerOrAdmin）——显式设为 metric_owner。
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "zhangsan",
      display_name: "张三",
      role: "metric_owner",
      domain: "sales",
      org_id: 1,
    });
    mockedGetMetric.mockResolvedValue({ ...metric, pii_flag: false, description: "原描述" });
    mockedUpdateDesc.mockResolvedValue({ ...metric, description: "修改后的描述" });
    renderWithPerms(["metric:create"]);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    // 打开编辑弹窗
    fireEvent.click(await screen.findByText("编辑描述"));
    const textarea = document.querySelector(".ant-modal textarea") as HTMLTextAreaElement;
    expect(textarea).toBeTruthy();
    fireEvent.change(textarea, { target: { value: "修改后的描述" } });
    fireEvent.click(screen.getByRole("button", { name: /保存描述/ }));
    await waitFor(() =>
      expect(mockedUpdateDesc).toHaveBeenCalledWith("sales_gmv_sum_d", "修改后的描述", 1),
    );
    // 保存后描述区展示新值（Modal 未销毁时 textarea 仍保留旧值，故用 getAllByText 断言描述区存在）
    expect(screen.getAllByText("修改后的描述").length).toBeGreaterThanOrEqual(1);
  });

  it("加载失败显示『指标加载失败』与重试按钮（而非误导的『指标不存在』），点击重试恢复", async () => {
    // 首次 getMetric 失败（模拟网络/服务异常），重试后成功
    mockedGetMetric
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValue(metric);
    render(
      <MemoryRouter initialEntries={["/detail/sales_gmv_sum_d"]}>
        <Routes>
          <Route
            path="/detail/:code"
            element={
              <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "metric_owner", domain: "sales", org_id: 1 }}>
                <MetricDetail />
              </PermissionProvider>
            }
          />
        </Routes>
      </MemoryRouter>,
    );
    // 失败态：显示加载失败 + 重试按钮，不显示"指标不存在"
    await waitFor(() => {
      expect(screen.getByText(/指标加载失败/)).toBeTruthy();
    });
    expect(screen.queryByText("指标不存在")).toBeNull();
    // 点击重试 → 重新拉取并渲染指标（按钮文字被 antd 拆分为"重 试"，从含该文本的按钮定位）
    const retryBtn = screen.getAllByText(/重\s*试/).map((el) => el.closest("button")).find(Boolean);
    expect(retryBtn).toBeTruthy();
    fireEvent.click(retryBtn as Element);
    // 核心行为断言：重试后详情渲染成功（不依赖精确调用计数，避免完整测试套件的环境计数污染）
    await waitFor(() => {
      // 详情渲染成功（编码出现在标题/编码/责任链多处，用 getAllByText）
      expect(screen.getAllByText("sales_gmv_sum_d").length).toBeGreaterThan(0);
    });
  });
  it("PII 指标对非敏感角色隐藏「编辑描述」与「AI 生成描述」按钮（消除编辑空描述清空原文风险）", async () => {
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "zhangsan",
      display_name: "张三",
      role: "metric_owner",
      domain: "sales",
      org_id: 1,
    });
    // pii_flag=true 保持默认：metric_owner 属非敏感角色 → piiMasked=true
    mockedGetMetric.mockResolvedValue({ ...metric, description: "PII 指标描述" });
    renderWithPerms(["metric:create", "metric:infer-description"]);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    await screen.findByText(/业务描述已隐藏/);
    expect(screen.queryByText("编辑描述")).toBeNull();
    expect(screen.queryByText("AI 生成描述")).toBeNull();
  });

  it("DRAFT 草稿显示编辑按钮，保存后调用 updateMetric（含乐观锁 row_version）", async () => {
    // 第二个 describe 无 beforeEach，显式补全 Promise.all 依赖（防继承脆弱）
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DRAFT" });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedDomainTree.mockResolvedValue([
      { id: 1, code: "sales", name: "销售域", parent_id: null, level: 1, sort_order: 0, status: "active", metric_count: 0, children: [] },
    ]);
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "zhangsan",
      display_name: "张三",
      role: "metric_owner",
      domain: "sales",
      org_id: 1,
    });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    // metric_owner + metric:create → canCreate && isOwnerOrAdmin 均满足，显示编辑按钮
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "sales",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    // 编辑按钮在 DRAFT 状态显示
    fireEvent.click(await screen.findByRole("button", { name: /编辑/ }));
    // 弹窗回填当前名称
    await waitFor(() => {
      expect(document.querySelector(".ant-modal")).toBeTruthy();
    });
    const nameInput = document.querySelector('.ant-modal input[id="name"]') as HTMLInputElement;
    expect(nameInput?.value).toBe("销售 GMV");
    // 填变更原因并保存 → 调用 updateMetric（含 row_version）
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "修正口径描述" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      expect(mockedUpdateMetric).toHaveBeenCalledWith(
        "sales_gmv_sum_d",
        expect.objectContaining({ name: "销售 GMV", change_reason: "修正口径描述", row_version: metric.row_version }),
      );
    });
  });

  it("编辑弹窗关联维度回填并合入口径 definition_json.dimensions", async () => {
    // 第二个 describe 无 beforeEach，显式补全 Promise.all 依赖
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      definition_json: { expression: "sum(gmv)", dimensions: ["dim_channel"] },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({
      items: [{ id: 1, dim_code: "dim_channel", name: "渠道", domain: "sales", type: "SCD1", description: "渠道维度", owner_id: 1, status: "PUBLISHED", created_at: "", updated_at: "" }],
      total: 1,
    });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "sales", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "sales",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    fireEvent.click(await screen.findByRole("button", { name: /编辑/ }));
    await waitFor(() => {
      expect(document.querySelector(".ant-modal")).toBeTruthy();
    });
    // 填变更原因并保存 → definition_json.dimensions 保留回填的维度
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "修正口径" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      expect(mockedUpdateMetric).toHaveBeenCalledWith(
        "sales_gmv_sum_d",
        expect.objectContaining({ definition_json: expect.objectContaining({ dimensions: ["dim_channel"] }) }),
      );
    });
  });

  it("非原子指标编辑弹窗依赖指标回填并合入口径 dependencies", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      type: "derived",
      definition_json: { expression: "sum(gmv)", dependencies: ["sales_gmv_day"] },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({
      items: [{ metric_code: "sales_gmv_day", name: "销售 GMV" } as unknown as MetricResponse],
      total: 1,
      page: 1,
      page_size: 100,
    });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "sales", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "sales",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    fireEvent.click(await screen.findByRole("button", { name: /编辑/ }));
    await waitFor(() => {
      expect(document.querySelector(".ant-modal")).toBeTruthy();
    });
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "修正口径" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      expect(mockedUpdateMetric).toHaveBeenCalledWith(
        "sales_gmv_sum_d",
        expect.objectContaining({ definition_json: expect.objectContaining({ dependencies: ["sales_gmv_day"] }) }),
      );
    });
  });

  it("编辑弹窗落地表（source_table）回填并保存时保留在 definition_json", async () => {
    // 指标 definition_json 含落地表 → openEdit 回填；保存时 source_table 保留合入口径
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      definition_json: { expression: "sum(gmv)", source_table: "dwd.legacy_sales" },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedCatalogs.mockResolvedValue({
      items: [{ entity_name: "dwd.sales_detail", source_name: "数仓", entity_type: "TABLE" }] as unknown as Array<import("../types").DBCatalog>,
      total: 1,
      page: 1,
      page_size: 20,
    });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "sales", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "sales",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    fireEvent.click(await screen.findByRole("button", { name: /编辑/ }));
    await waitFor(() => {
      expect(document.querySelector(".ant-modal")).toBeTruthy();
    });
    // AutoComplete 用 data-testid 精确定位（antd AutoComplete 渲染为 .ant-select-auto-complete）
    const acInput = document.querySelector(
      '[data-testid="editSourceTable"] input',
    ) as HTMLInputElement | null;
    expect(acInput).toBeTruthy();
    // 修改为新的落地表（模拟选择）
    // 落地表已回填当前定义值（dwd.legacy_sales）；此测试验证「打开编辑弹窗 → 保存」
    // 时 source_table 保留在 definition_json（openEdit 回填 + handleSubmitEdit 合入路径）；
    // 「修改落地表」的 onSearch/onSelect 更新逻辑由页面实现（手输/选择均同步 state+dirty）
    expect((acInput as HTMLInputElement).value).toBe("dwd.legacy_sales");
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "修正口径描述" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      expect(mockedUpdateMetric).toHaveBeenCalledWith(
        "sales_gmv_sum_d",
        expect.objectContaining({ definition_json: expect.objectContaining({ source_table: "dwd.legacy_sales" }) }),
      );
    });
  });

  it("编辑弹窗遗留粒度/单位值兜底（字典未收录时仍显示并保留，防静默清空）", async () => {
    // 存量指标粒度 "daily" 不在字典（字典为空），openEdit 应将其作为兜底选项加入，
    // 保存时 granularity/unit 不被静默清空（数据丢失防护）。
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      granularity: "daily",
      unit: "USD",
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]); // 字典为空 → 遗留值必须兜底
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "sales", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "sales",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    fireEvent.click(await screen.findByRole("button", { name: /编辑/ }));
    await waitFor(() => {
      expect(document.querySelector(".ant-modal")).toBeTruthy();
    });
    // 遗留粒度 "daily" 显示为选中项（兜底选项已加入，未被静默清空）
    await waitFor(() => {
      const selected = document.querySelectorAll(".ant-modal .ant-select-selection-item");
      const texts = Array.from(selected).map((el) => el.textContent);
      expect(texts.some((t) => t && t.includes("daily"))).toBeTruthy();
    });
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "修正口径" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      expect(mockedUpdateMetric).toHaveBeenCalledWith(
        "sales_gmv_sum_d",
        expect.objectContaining({ granularity: "daily", unit: "USD" }),
      );
    });
  });

  it("编辑弹窗清空关联维度即从口径移除（dirty 语义：清空≠未改保留）", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      definition_json: { expression: "sum(gmv)", dimensions: ["dim_channel"] },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({
      items: [{ id: 1, dim_code: "dim_channel", name: "渠道", domain: "sales", type: "SCD1", description: "渠道维度", owner_id: 1, status: "PUBLISHED", created_at: "", updated_at: "" }],
      total: 1,
    });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "sales", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "sales",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    fireEvent.click(await screen.findByRole("button", { name: /编辑/ }));
    await waitFor(() => {
      expect(document.querySelector(".ant-modal")).toBeTruthy();
    });
    // 清空"关联维度"多选（点击已选 Tag 的清除图标 → dirty=true 且为空）
    const clearIcons = document.querySelectorAll(
      '.ant-modal .ant-select-multiple .ant-select-selection-item-remove',
    );
    if (clearIcons.length) {
      fireEvent.click(clearIcons[0] as HTMLElement);
    }
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "移除关联维度" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      expect(mockedUpdateMetric).toHaveBeenCalledWith(
        "sales_gmv_sum_d",
        expect.objectContaining({
          definition_json: expect.not.objectContaining({ dimensions: expect.anything() }),
        }),
      );
    });
  });

});
