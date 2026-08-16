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
  getMetricHealth: vi.fn(),
  listUsers: vi.fn(),
  listSubscriptions: vi.fn(),
  fetchRelatedMetrics: vi.fn(),
  addFavorite: vi.fn(),
  removeFavorite: vi.fn(),
  approveMetric: vi.fn(),
  deprecateMetric: vi.fn(),
  emergencyPublishMetric: vi.fn(),
  piiReview: vi.fn(),
  promoteMetric: vi.fn(),
  rollbackMetric: vi.fn(),
  submitReview: vi.fn(),
  updateMetric: vi.fn(),
  suggestRenameName: vi.fn(),
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
  listFavorites,
  getMetricHealth,
  listUsers,
  listSubscriptions,
  fetchRelatedMetrics,
  updateMetric,
  suggestRenameName,
  submitReview,
  emergencyPublishMetric,
  UnisenseApiError,
} from "../api";
const mockedUpdateMetric = vi.mocked(updateMetric);
const mockedSuggestRename = vi.mocked(suggestRenameName);
const mockedGetMetric = vi.mocked(getMetric);
const mockedFetchArchived = vi.mocked(fetchArchivedMetric);
const mockedListVersions = vi.mocked(listVersions);
const mockedMyPerms = vi.mocked(fetchMyPermissions);
const mockedCurrentUser = vi.mocked(fetchCurrentUser);
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
      ui_actions: ["metric:create", "metric:edit", "metric:deprecate", "catalog:view"],
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
});

describe("MetricDetail 按钮级权限过滤", () => {
  function renderWithPerms(ui_actions: string[]) {
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "custom",
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
});
