import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useSearchParams } from "react-router-dom";
import { MetricDetail } from "../pages/MetricDetail";
import { PermissionProvider } from "../hooks/usePermission";
import type { MeasureCatalog, MetricHealth, MetricResponse, MetricVersionResponse, SystemDictItem } from "../types";

vi.mock("../api", () => ({
  getMetric: vi.fn(),
  fetchArchivedMetric: vi.fn(),
  listVersions: vi.fn(),
  fetchCurrentUser: vi.fn(),
  fetchMyPermissions: vi.fn(),
  listFavorites: vi.fn(),
  listMetrics: vi.fn(),
  listMeasureCatalogs: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 200 }),
  listMetricMounts: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  deleteMetricMount: vi.fn().mockResolvedValue(undefined),
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
  completeEmergencyReview: vi.fn(),
  piiReview: vi.fn(),
  promoteMetric: vi.fn(),
  rollbackMetric: vi.fn(),
  // P2-11 术语绑定写路径：listTerms 搜索 + bindMetricTerm 绑定/解绑
  listTerms: vi.fn(),
  bindMetricTerm: vi.fn(),
  submitReview: vi.fn(),
  updateMetric: vi.fn(),
  updateMetricDescription: vi.fn(),
  updateConsumptionGuide: vi.fn(),
  suggestRenameName: vi.fn(),
  inferMetricDescription: vi.fn(),
  refineMetricDefinition: vi.fn(),
  upsertSubscription: vi.fn(),
  verifyDictValues: vi.fn(),
  notifyUnknownDictValues: vi.fn(),
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
  listMeasureCatalogs,
  listMetricMounts,
  deleteMetricMount,
  listCatalogs,
  getMetricHealth,
  listUsers,
  listSubscriptions,
  fetchRelatedMetrics,
  updateMetric,
  updateMetricDescription,
  updateConsumptionGuide,
  suggestRenameName,
  inferMetricDescription,
  refineMetricDefinition,
  submitReview,
  emergencyPublishMetric,
  completeEmergencyReview,
  recoverSourceDropped,
  confirmDeprecateDropped,
  promoteMetric,
  rollbackMetric,
  listTerms,
  bindMetricTerm,
  verifyDictValues,
  notifyUnknownDictValues,
  UnisenseApiError,
} from "../api";
const mockedUpdateMetric = vi.mocked(updateMetric);
const mockedUpdateDesc = vi.mocked(updateMetricDescription);
const mockedUpdateGuide = vi.mocked(updateConsumptionGuide);
const mockedSuggestRename = vi.mocked(suggestRenameName);
const mockedInferDesc = vi.mocked(inferMetricDescription);
const mockedRefine = vi.mocked(refineMetricDefinition);
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
const mockedListMeasureCatalogs = vi.mocked(listMeasureCatalogs);
const mockedListMetricMounts = vi.mocked(listMetricMounts);
const mockedDeleteMetricMount = vi.mocked(deleteMetricMount);
const mockedFavorites = vi.mocked(listFavorites);
const mockedHealth = vi.mocked(getMetricHealth);
const mockedUsers = vi.mocked(listUsers);
const mockedSubs = vi.mocked(listSubscriptions);
const mockedRelated = vi.mocked(fetchRelatedMetrics);
const mockedVerifyDictValues = vi.mocked(verifyDictValues);
const mockedNotifyUnknownDictValues = vi.mocked(notifyUnknownDictValues);
const mockedSubmitReview = vi.mocked(submitReview);

const metric: MetricResponse = {
  id: 1,
  metric_code: "sales_gmv_sum_d",
  name: "销售 GMV",
  domain: "outpatient",
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
  emergency_publish: false,
  emergency_reason: null,
  emergency_reviewed_at: null,
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
            <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "metric_owner", domain: "outpatient", org_id: 1 }}>
              <MetricDetail />
            </PermissionProvider>
          }
        />
        <Route path="/catalog" element={<div>catalog-page</div>} />
        <Route path="/dashboard" element={<div>dashboard-page</div>} />
        <Route path="/dicts" element={<div>dicts-page</div>} />
        <Route path="/todo" element={<div>todo-page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

// 试算跳转探针：渲染目标路由并回显 query 参数，验证「试算」按钮带码跳转
function QueryProbe() {
  const [sp] = useSearchParams();
  return <div>{`query-page-${sp.get("metric_code") ?? "none"}`}</div>;
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
      domain: "outpatient",
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
      home_domain: "outpatient",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create", "metric:edit", "metric:deprecate", "catalog:view", "metric:infer-description"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    // 字典未收录值治理引导：默认后端复核「全部已收录」→ 不阻断保存；
    // 引导流程测试单独覆盖 verifyDictValues 返回实际未收录值。
    mockedVerifyDictValues.mockResolvedValue({ unknown: [] });
    mockedNotifyUnknownDictValues.mockResolvedValue({ notified: 0, unknown: 0 });
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

  it("存量原子指标未关联逻辑度量时展示 OneData 化引导", async () => {
    // fixture 为 atomic 且显式无 measure_id（旧式物理来源）→ 引导在「度量目录」建逻辑度量后关联
    mockedGetMetric.mockResolvedValue({ ...metric, measure_id: undefined });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await waitFor(() => {
      expect(screen.getByText("该原子指标未关联逻辑度量（存量旧式来源）")).toBeTruthy();
    });
  });

  it("原子指标已关联逻辑度量时不展示 OneData 化引导", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, measure_id: 7 });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await waitFor(() => {
      expect(screen.queryByText("该原子指标未关联逻辑度量（存量旧式来源）")).toBeNull();
    });
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
      new UnisenseApiError("指标已因口径裁决作废: outp_e2e_conflictb_day", "METRIC_ARCHIVED", 404, "test-trace"),
      {
        code: "METRIC_ARCHIVED",
        codeZh: "该指标已因口径裁决作废，请查看权威指标",
        detail: {
          metric_code: "outp_e2e_conflictb_day",
          successor_code: "outp_e2e_conflicta_day",
          arbitration_mark: {
            status: "defeated",
            conflict_id: "CF-ABC",
            decision: "merge",
            ruled_at: "2026-08-15T04:00:00Z",
            opposite_code: "outp_e2e_conflicta_day",
          },
        },
      },
    );
    mockedGetMetric.mockRejectedValue(err);
    // 作废历史详情（供追溯面板）
    mockedFetchArchived.mockResolvedValue({
      metric: { ...metric, metric_code: "outp_e2e_conflictb_day", name: "门诊冲突指标 B" },
      successor_code: "outp_e2e_conflicta_day",
      arbitration_mark: { decision: "merge" },
    });
    renderDetail({ pathname: "/detail/outp_e2e_conflictb_day" });

    // 醒目引导（warning）而非裸「指标不存在」
    expect(await screen.findByRole("button", { name: /outp_e2e_conflicta_day/ })).toBeInTheDocument();
    expect(screen.queryByText("指标不存在")).not.toBeInTheDocument();
    // 历史详情面板展示作废指标详情
    expect(await screen.findByText("作废指标历史详情（仅供追溯）")).toBeInTheDocument();
    expect(screen.getByText("门诊冲突指标 B")).toBeInTheDocument();
    // 首次进入弹出醒目引导（标题「指标已作废」——page-head + Modal 两处）
    expect(screen.getAllByText("指标已作废").length).toBeGreaterThan(0);
    // 权威指标跳转按钮 → 点击后以新 code 重新拉取详情
    const jump = screen.getByRole("button", { name: /outp_e2e_conflicta_day/ });
    fireEvent.click(jump);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalledWith("outp_e2e_conflicta_day"));
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
        reviewer_domain: "outpatient",
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
                user={{ id: 1, username: "u", display_name: "U", role: "metric_owner", domain: "outpatient", org_id: 1 }}
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
  it("删除：指标创建者（原 Owner）可在详情页删除自己的草稿", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DRAFT" }); // owner_id=1
    // 授予 metric:delete 权限点（默认 beforeEach 无），当前用户 metric_owner(id=1) === owner_id(1)
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create", "metric:edit", "metric:delete", "metric:deprecate"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    // currentUser(id=1) === metric.owner_id(1) → 创建者可删（对齐后端「管理员或原 Owner」）
    // 删除按钮带 DeleteOutlined icon，accessible name 为「delete 删 除」，用正则匹配
    // 注意：不点击弹窗——Modal.confirm 为静态方法渲染到独立 portal，跨测试残留会污染后续用例
    expect(await screen.findByRole("button", { name: /删\s*除/ })).toBeInTheDocument();
  });
  it("删除：非创建者且非管理员在详情页看不到删除按钮", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DRAFT", owner_id: 2 }); // 他人草稿
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create", "metric:edit", "metric:delete", "metric:deprecate"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await screen.findByText("销售 GMV");
    // 有 metric:delete 权限点，但非创建者且非管理员 → 不渲染删除按钮
    await waitFor(() => expect(screen.queryByRole("button", { name: /删\s*除/ })).not.toBeInTheDocument());
  });
  it("编辑弹窗聚合方式独立字段：回填 + 提交直接携带（非治理属性，走口径变更语义）", async () => {
    // 聚合方式（SUM/AVG）本质是口径变更，与粒度/单位同级——编辑弹窗应独立回填并提交，
    // 而非混入治理属性 dirty 机制（后端据此触发版本确认，修复 aggregation 判定矛盾）。
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DRAFT", aggregation: "SUM" });
    // 聚合字典 mock 含 AVG/SUM，使聚合方式下拉可展开选择 AVG（beforeEach 默认空字典无选项）
    mockedDictItems.mockResolvedValue([
      { code: "AVG", label: "平均 (AVG)", status: "active" } as SystemDictItem,
      { code: "SUM", label: "求和 (SUM)", status: "active" } as SystemDictItem,
    ]);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    fireEvent.click(await screen.findByText("编辑"));
    await screen.findByText("编辑指标");
    // 聚合方式独立回填 SUM（聚合 "SUM" 是聚合字段独有值——其他字段回填 day/元/PERIOD 等不含它）
    let agSelect: HTMLElement | null = null;
    await waitFor(() => {
      const items = Array.from(document.querySelectorAll(".ant-modal .ant-select-selection-item")) as HTMLElement[];
      const hit = items.find((e) => (e.textContent ?? "").includes("SUM"));
      expect(hit).toBeTruthy();
      agSelect = hit?.closest(".ant-select") as HTMLElement | null;
    });
    // 修改聚合方式为 AVG
    fireEvent.mouseDown((agSelect as unknown as HTMLElement).querySelector(".ant-select-selector") as HTMLElement);
    await waitFor(() => {
      const opt = document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option[title*="AVG"]') as HTMLElement;
      expect(opt).toBeTruthy();
      fireEvent.click(opt);
    });
    // 填变更原因并保存 → payload 直接携带 aggregation: "AVG"（非 govPayload 展开）
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "聚合方式从求和改为平均" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      const lastCall = mockedUpdateMetric.mock.calls[mockedUpdateMetric.mock.calls.length - 1]?.[1];
      expect(lastCall).toMatchObject({ aggregation: "AVG", change_reason: expect.any(String) });
    });
  });

  it("编辑弹窗单位遗留值兜底：字典未收录的历史值 label 追加「(不在字典中)」提示", async () => {
    // 方案 B：unit=cnt 不在字典返回里（历史脏数据）→ 兜底选项 value 保留 cnt（防静默清空）、
    // label 追加「(不在字典中)」提示，治理者一眼识别并可决策补录字典。
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DRAFT", unit: "cnt" });
    // 单位字典 mock 含 CNY/CNT 但不含 cnt → 触发兜底
    mockedDictItems.mockResolvedValue([
      { code: "CNY", label: "人民币元 (CNY)", status: "active" } as SystemDictItem,
      { code: "CNT", label: "计数 (CNT)", status: "active" } as SystemDictItem,
    ]);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    fireEvent.click(await screen.findByText("编辑"));
    await screen.findByText("编辑指标");
    // 单位选中项显示「cnt (不在字典中)」（value 仍为 cnt，保存不回传别名）
    await waitFor(() => {
      const items = Array.from(document.querySelectorAll(".ant-modal .ant-select-selection-item")) as HTMLElement[];
      const unitItem = items.find((e) => (e.textContent ?? "").includes("cnt (不在字典中)"));
      expect(unitItem).toBeTruthy();
    });
  });

  it("保存含字典未收录值时弹治理引导：无收录权限者「通知管理员收录/打回并保存」", async () => {
    // 无 dict:create 权限：保存 unit=cnt（字典未收录脏值）→ 前端检测 + 后端复核确认未收录 →
    // 引导弹窗出现；确认后通知全部管理员收录/打回，并按原值保存（受控词表不自动新增）。
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DRAFT", unit: "cnt" });
    mockedDictItems.mockResolvedValue([
      { code: "CNY", label: "人民币元 (CNY)", status: "active" } as SystemDictItem,
      { code: "CNT", label: "计数 (CNT)", status: "active" } as SystemDictItem,
    ]);
    // 后端复核：确认 unit=cnt 确实未收录（beforeEach 默认返回空 unknown 不触发引导）
    mockedVerifyDictValues.mockResolvedValue({ unknown: [{ dict_type: "unit", value: "cnt" }] });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    fireEvent.click(await screen.findByText("编辑"));
    await screen.findByText("编辑指标");
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "修正口径" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    // 引导弹窗出现（列出未收录值 cnt）
    await screen.findByText("发现字典未收录值");
    expect(screen.getByText("cnt", { exact: true })).toBeInTheDocument();
    // 确认 → 通知管理员 + 按原值保存
    fireEvent.click(screen.getByRole("button", { name: /通知管理员收录\/打回并保存/ }));
    await waitFor(() => {
      expect(mockedNotifyUnknownDictValues).toHaveBeenCalledWith(
        expect.objectContaining({
          metric_code: "sales_gmv_sum_d",
          values: [{ dict_type: "unit", value: "cnt" }],
        }),
      );
    });
    await waitFor(() => {
      const lastCall = mockedUpdateMetric.mock.calls[mockedUpdateMetric.mock.calls.length - 1]?.[1];
      expect(lastCall).toMatchObject({ unit: "cnt", change_reason: expect.any(String) });
    });
  });

  it("有收录权限者保存未收录值时引导前往参照数据管理收录（跳转 /dicts，不保存）", async () => {
    // 有 dict:create 权限：引导弹窗主操作变为「前往参照数据管理收录」——
    // 放弃本次保存跳转 /dicts 补词条（受控词表由治理者维护，不自动新增）。
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DRAFT", unit: "cnt" });
    mockedDictItems.mockResolvedValue([
      { code: "CNY", label: "人民币元 (CNY)", status: "active" } as SystemDictItem,
      { code: "CNT", label: "计数 (CNT)", status: "active" } as SystemDictItem,
    ]);
    mockedVerifyDictValues.mockResolvedValue({ unknown: [{ dict_type: "unit", value: "cnt" }] });
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create", "metric:edit", "dict:create"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    fireEvent.click(await screen.findByText("编辑"));
    await screen.findByText("编辑指标");
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "修正口径" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await screen.findByText("发现字典未收录值");
    fireEvent.click(screen.getByRole("button", { name: /前往参照数据管理收录/ }));
    await screen.findByText("dicts-page"); // 已跳转参照数据管理
    expect(mockedUpdateMetric).not.toHaveBeenCalled();
    expect(mockedNotifyUnknownDictValues).not.toHaveBeenCalled();
  });

  it("无收录权限者点「暂不保存」→ 不保存不通知，编辑弹窗保留", async () => {
    // 「暂不保存」放弃本次保存：既不写指标也不打扰管理员，编辑弹窗保留供修改取值后重提。
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DRAFT", unit: "cnt" });
    mockedDictItems.mockResolvedValue([
      { code: "CNY", label: "人民币元 (CNY)", status: "active" } as SystemDictItem,
      { code: "CNT", label: "计数 (CNT)", status: "active" } as SystemDictItem,
    ]);
    mockedVerifyDictValues.mockResolvedValue({ unknown: [{ dict_type: "unit", value: "cnt" }] });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    fireEvent.click(await screen.findByText("编辑"));
    await screen.findByText("编辑指标");
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "修正口径" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await screen.findByText("发现字典未收录值");
    fireEvent.click(screen.getByRole("button", { name: /暂不保存/ }));
    await waitFor(() => {
      expect(mockedUpdateMetric).not.toHaveBeenCalled();
      expect(mockedNotifyUnknownDictValues).not.toHaveBeenCalled();
    });
    expect(screen.getByText("编辑指标")).toBeInTheDocument(); // 编辑弹窗保留
  });

  it("治理审核（reviewer）默认聚焦「版本历史」Tab，而非质量快照", async () => {
    mockedCurrentUser.mockResolvedValue({
      id: 9,
      username: "reviewer",
      display_name: "评审人",
      role: "reviewer",
      domain: "outpatient",
      org_id: 1,
    } as any);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await waitFor(() => expect(document.querySelector(".ant-tabs")).toBeTruthy());
    await waitFor(() => {
      const activeTab = document.querySelector(".ant-tabs-tab-active");
      expect(activeTab?.textContent).toContain("版本历史");
    });
  });

  it("指标生产者（metric_owner）默认聚焦「血缘影响」Tab", async () => {
    // beforeEach 已设 metric_owner（producer 群体）
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await waitFor(() => expect(document.querySelector(".ant-tabs")).toBeTruthy());
    await waitFor(() => {
      const activeTab = document.querySelector(".ant-tabs-tab-active");
      expect(activeTab?.textContent).toContain("血缘影响");
    });
  });

  it("有健康数据时第一屏展示「最近校验」时间（让人信：最近一次校验时间）", async () => {
    mockedHealth.mockResolvedValue({
      metric_id: 1,
      score: 88,
      level: "EXCELLENT",
      completeness_score: 90,
      activity_score: 85,
      quality_score: 95,
      owner_response_score: 80,
      lineage_coverage_score: 90,
      missing_dimensions: null,
      calculated_at: "2026-08-18T03:20:00",
    });
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await waitFor(() => {
      expect(screen.getByText(/最近校验：/)).toBeTruthy();
    });
    // 校验时间走上海时区中文格式（后端 UTC → 上海 +08:00 偏移）
    expect(screen.getByText(/最近校验：2026年8月18日 11:20/)).toBeTruthy();
  });

  it("健康数据缺失时不展示「最近校验」（空健康静默降级，不误导）", async () => {
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await screen.findByText("销售 GMV");
    expect(screen.queryByText(/最近校验：/)).toBeNull();
  });

  it("头部「试算」按钮跳转查询工作台并携带本指标编码", async () => {
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
          <Route path="/query" element={<QueryProbe />} />
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
        </Routes>
      </MemoryRouter>,
    );
    const btn = await screen.findByRole("button", { name: /试\s*算/ });
    fireEvent.click(btn);
    await screen.findByText("query-page-sales_gmv_sum_d");
  });

  describe("MetricDetail 状态机细分引导（新增/变更/破坏性/重评审 + 前后对比）", () => {
  function makeVersion(partial: Partial<MetricVersionResponse> & { version: number }): MetricVersionResponse {
    return {
      id: partial.version,
      metric_id: 1,
      change_type: "CREATE",
      definition_json: {},
      diff_json: null,
      status: "DRAFT",
      change_reason: "",
      created_by: 1,
      published_at: null,
      created_at: "2026-08-01T00:00:00",
      ...partial,
    };
  }

  it("REVIEW 新增指标（首次提交）：展示新增 Tag 且无变更前后对比", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "REVIEW", version: 1, effective_version: null, approver_id: null, pii_flag: false });
    mockedListVersions.mockResolvedValue([makeVersion({ version: 1, change_type: "CREATE", status: "REVIEW" })]);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await screen.findByText("新增指标");
    expect(screen.queryByText("变更前后对比")).toBeNull();
  });

  it("REVIEW 变更指标：展示 v1→v2 与变更前后对比", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "REVIEW", version: 2, effective_version: 1, approver_id: null, pii_flag: false });
    mockedListVersions.mockResolvedValue([
      makeVersion({
        version: 2,
        change_type: "UPDATE",
        status: "REVIEW",
        diff_json: {
          expression: { before: "sum(gmv)", after: "sum(gmv_amount)", change_type: "UPDATE" },
        },
      }),
      makeVersion({ version: 1, change_type: "CREATE", status: "PUBLISHED", published_at: "2026-08-01T00:00:00" }),
    ]);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await screen.findByText("变更前后对比");
    expect(screen.getByText(/变更指标 v1→v2/)).toBeTruthy();
    // 变更后值（diff after）唯一出现；变更前值同时出现在口径定义中，用 getAllByText 兼容
    expect(screen.getByText("sum(gmv_amount)")).toBeTruthy();
    expect(screen.getAllByText("sum(gmv)").length).toBeGreaterThanOrEqual(1);
  });

  it("REVIEW 破坏性变更：展示破坏性 Tag 与对比", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "REVIEW", version: 2, effective_version: 1, approver_id: null, pii_flag: false });
    mockedListVersions.mockResolvedValue([
      makeVersion({
        version: 2,
        change_type: "BREAKING",
        status: "REVIEW",
        diff_json: {
          granularity: { before: "day", after: "month", change_type: "BREAKING" },
        },
      }),
      makeVersion({ version: 1, change_type: "CREATE", status: "PUBLISHED", published_at: "2026-08-01T00:00:00" }),
    ]);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await screen.findByText(/破坏性变更 v2/);
    expect(screen.getByText("变更前后对比")).toBeTruthy();
  });

  it("REVIEW 废弃恢复重评审：当前版本仍已发布时展示重评审 Tag", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "REVIEW", version: 1, effective_version: 1, approver_id: null, pii_flag: false });
    mockedListVersions.mockResolvedValue([
      makeVersion({ version: 1, change_type: "CREATE", status: "PUBLISHED", published_at: "2026-08-01T00:00:00" }),
    ]);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await screen.findByText(/废弃恢复重评审/);
  });

  it("EXPERIMENTAL：展示灰度说明而非草稿提示", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "EXPERIMENTAL", version: 2, effective_version: 1, pii_flag: false });
    mockedListVersions.mockResolvedValue([]);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await screen.findByText(/灰度实验（EXPERIMENTAL）/);
    expect(screen.queryByText(/尚未提交评审/)).toBeNull();
  });

  it("DRAFT：展示草稿提示", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DRAFT", version: 1, effective_version: null, approver_id: null, pii_flag: false });
    mockedListVersions.mockResolvedValue([]);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await screen.findByText(/草稿（DRAFT）/);
  });

  it("PUBLISHED 且经历过变更：轻量提示当前为变更后口径", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "PUBLISHED", version: 2, effective_version: 1, pii_flag: false });
    mockedListVersions.mockResolvedValue([
      makeVersion({ version: 2, change_type: "UPDATE", status: "PUBLISHED", published_at: "2026-08-02T00:00:00" }),
      makeVersion({ version: 1, change_type: "CREATE", status: "PUBLISHED", published_at: "2026-08-01T00:00:00" }),
    ]);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await screen.findByText(/当前口径为「变更指标」/);
  });

  it("PUBLISHED 首次创建：不展示变更提示", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, status: "PUBLISHED", version: 1, effective_version: 1, pii_flag: false });
    mockedListVersions.mockResolvedValue([
      makeVersion({ version: 1, change_type: "CREATE", status: "PUBLISHED", published_at: "2026-08-01T00:00:00" }),
    ]);
    renderDetail({ pathname: "/detail/sales_gmv_sum_d" });
    await screen.findByText("销售 GMV");
    expect(screen.queryByText(/当前口径为「变更指标」/)).toBeNull();
  });
  });
});


describe("MetricDetail 按钮级权限过滤", () => {
  // 该 describe 无顶层 beforeEach（历史沿革依赖前序 describe 的 mock 泄漏）。
  // 治理引导涉及的新 mock 必须在此显式重置——否则上个 describe 末尾测试设置的
  // verifyDictValues（返回 unknown）会泄漏进来，把本 describe 的保存测试全阻断。
  beforeEach(() => {
    mockedVerifyDictValues.mockResolvedValue({ unknown: [] });
    mockedNotifyUnknownDictValues.mockResolvedValue({ notified: 0, unknown: 0 });
  });

  function renderWithPerms(ui_actions: string[], role = "custom") {
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role,
      home_domain: "outpatient",
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
              <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role, domain: "outpatient", org_id: 1 }}>
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
    // M1 修复：未指派评审人时非管理角色按钮显示为「审批通过（未被指派评审）」且禁用
    // （与后端 _assert_reviewer_authorized 未指派仅 domain_admin 兜底一致）——按钮仍展示，
    // 用正则匹配变体文案
    expect(await screen.findByText(/审批通过/)).toBeInTheDocument();
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

  it("P3-15: 紧急发布未补审时显示补审按钮，确认后调用 completeEmergencyReview", async () => {
    // 「按钮级权限过滤」为独立 describe，隔离运行时需自足设置 load() 依赖（防 mock 泄漏缺失）
    mockedListVersions.mockResolvedValue([]);
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedDomainTree.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "u",
      display_name: "U",
      role: "metric_owner",
      domain: "outpatient",
      org_id: 1,
    });
    const reviewSpy = vi.fn().mockResolvedValue(undefined);
    vi.mocked(completeEmergencyReview).mockImplementation(reviewSpy);
    mockedGetMetric.mockResolvedValue({ ...metric, emergency_publish: true, emergency_reviewed_at: null });
    renderWithPerms(["metric:emergency-publish", "metric:approve"]);
    await screen.findByText("紧急补审");
    fireEvent.click(screen.getByText("紧急补审"));
    await screen.findByText("确认补审");
    fireEvent.click(screen.getByText("确认补审"));
    await waitFor(() => expect(reviewSpy).toHaveBeenCalledWith("sales_gmv_sum_d"));
  });

  it("P3-15: 紧急发布已补审（emergency_reviewed_at 有值）时不显示补审按钮", async () => {
    mockedListVersions.mockResolvedValue([]);
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedDomainTree.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "u",
      display_name: "U",
      role: "metric_owner",
      domain: "outpatient",
      org_id: 1,
    });
    mockedGetMetric.mockResolvedValue({
      ...metric,
      emergency_publish: true,
      emergency_reviewed_at: "2026-08-10T00:00:00Z",
    });
    renderWithPerms(["metric:emergency-publish", "metric:approve"]);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText("紧急补审")).not.toBeInTheDocument());
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

  it("P2-10: 灰度全量发布前弹确认框——确认后调用 promoteMetric", async () => {
    // 「按钮级权限过滤」为独立 describe，不继承外层 beforeEach 的 mock 泄漏（隔离运行时
    // 需自足）：显式补齐 load() 所需全部依赖，避免「指标加载失败」假失败。
    mockedListVersions.mockResolvedValue([]);
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "zhangsan",
      display_name: "张三",
      role: "metric_owner",
      domain: "outpatient",
      org_id: 1,
    });
    mockedGetMetric.mockResolvedValue({ ...metric, status: "EXPERIMENTAL", pii_flag: false });
    renderWithPerms(["metric:edit", "metric:rollback"]);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    // 点击「全量发布」→ 出现确认弹窗
    await waitFor(() => expect(screen.getByText("全量发布")).toBeTruthy());
    fireEvent.click(screen.getByText("全量发布"));
    await waitFor(() => expect(screen.getAllByText(/确认全量发布/).length).toBeGreaterThan(0));
    fireEvent.click(screen.getByText("确认发布"));
    await waitFor(() => expect(vi.mocked(promoteMetric)).toHaveBeenCalledWith("sales_gmv_sum_d"));
  });

  it("P2-10: 灰度回滚前弹高风险确认框——确认后调用 rollbackMetric", async () => {
    mockedListVersions.mockResolvedValue([]);
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "zhangsan",
      display_name: "张三",
      role: "metric_owner",
      domain: "outpatient",
      org_id: 1,
    });
    mockedGetMetric.mockResolvedValue({ ...metric, status: "EXPERIMENTAL", pii_flag: false });
    renderWithPerms(["metric:edit", "metric:rollback"]);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("回滚")).toBeTruthy());
    fireEvent.click(screen.getByText("回滚"));
    await waitFor(() => expect(screen.getAllByText(/确认回滚灰度版本/).length).toBeGreaterThan(0));
    fireEvent.click(screen.getByText("确认回滚"));
    await waitFor(() => expect(vi.mocked(rollbackMetric)).toHaveBeenCalledWith("sales_gmv_sum_d"));
  });

  it("P2-11: 关联术语可搜索并绑定——选中术语调用 bindMetricTerm", async () => {
    mockedListVersions.mockResolvedValue([]);
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({
      id: 1,
      username: "zhangsan",
      display_name: "张三",
      role: "metric_owner",
      domain: "outpatient",
      org_id: 1,
    });
    vi.mocked(listTerms).mockResolvedValue({
      items: [
        {
          id: 7,
          term_code: "CJ_AMT",
          name: "成交金额",
          definition: "订单成交金额",
          domain: "outpatient",
          synonyms: [],
          boundary: null,
          status: "PUBLISHED",
          owner_id: 1,
          created_at: null,
          updated_at: null,
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    });
    mockedGetMetric.mockResolvedValue({ ...metric, status: "PUBLISHED", pii_flag: false, term_id: null });
    renderWithPerms(["metric:edit"]);
    await waitFor(() => expect(mockedGetMetric).toHaveBeenCalled());
    // 打开术语下拉并搜索（antd Select placeholder 是 span 文本，非 input 属性）
    const termSelect = await screen.findByText("搜索并绑定业务术语");
    // 限定在术语绑定 Select 内查询 combobox——血缘影响 Tab 内嵌 LineageImpact 有跳数 Select，全局 getByRole 会多匹配
    const termBox = termSelect.closest(".ant-select") as HTMLElement;
    fireEvent.mouseDown(termBox);
    fireEvent.change(within(termBox).getByRole("combobox"), { target: { value: "成交" } });
    await waitFor(() => expect(listTerms).toHaveBeenCalled());
    // 选中「成交金额」→ 触发绑定（选项标签为「名称（term_code）」）
    const option = await screen.findByText(/成交金额（CJ_AMT）/);
    fireEvent.click(option);
    await waitFor(() => expect(vi.mocked(bindMetricTerm)).toHaveBeenCalledWith("sales_gmv_sum_d", 7));
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
              <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "custom", domain: "outpatient", org_id: 1 }}>
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
      home_domain: "outpatient",
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
              <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "metric_owner", domain: "outpatient", org_id: 1 }}>
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
      home_domain: "outpatient",
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
              <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "metric_owner", domain: "outpatient", org_id: 1 }}>
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
      domain: "outpatient",
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
      domain: "outpatient",
      org_id: 1,
    });
    mockedGetMetric.mockResolvedValue({ ...metric, pii_flag: false, description: "原描述" });
    mockedUpdateDesc.mockResolvedValue({ ...metric, description: "修改后的描述" });
    // 编辑描述按钮受 canEdit 门禁（metric:edit 权限点）——显式带上，匹配安全加固后的契约
    renderWithPerms(["metric:create", "metric:edit"]);
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
              <PermissionProvider user={{ id: 1, username: "u", display_name: "U", role: "metric_owner", domain: "outpatient", org_id: 1 }}>
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
      domain: "outpatient",
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
      domain: "outpatient",
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
      home_domain: "outpatient",
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

  it("编辑弹窗修改消费指南：先调 updateConsumptionGuide 成功后再调 updateMetric", async () => {
    mockedUpdateGuide.mockClear();
    mockedUpdateMetric.mockClear();
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      consumption_guide: { recommended_usage: ["旧推荐用法"], cautions: [], related_metrics: [] },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedDomainTree.mockResolvedValue([
      { id: 1, code: "sales", name: "销售域", parent_id: null, level: 1, sort_order: 0, status: "active", metric_count: 0, children: [] },
    ]);
    mockedCurrentUser.mockResolvedValue({
      id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1,
    });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1, role: "metric_owner", home_domain: "outpatient",
      allowed_actions: ["read", "write"], ui_actions: ["metric:create"],
      granted_domains: [], metric_whitelist: [], row_level_restricted: false,
      grants: [], expiring_soon: [],
    });
    mockedUpdateGuide.mockResolvedValue({
      metric_code: "sales_gmv_sum_d",
      name: "销售 GMV", domain: "outpatient", type: "sum", granularity: "day",
      unit: "元", aggregation: "sum", time_semantics: "occurrence", serving_mode: "catalog",
      recommended_usage: ["新推荐用法"], cautions: [], related_metrics: [],
      guide_source: "manual", guide_updated_at: "2026-08-26T00:00:00",
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    fireEvent.click(await screen.findByRole("button", { name: /编辑/ }));
    await waitFor(() => expect(document.querySelector(".ant-modal")).toBeTruthy());
    // 展开「消费指南」Collapse
    fireEvent.click(document.querySelectorAll(".ant-modal .ant-collapse-header")[0]);
    await waitFor(() => expect(screen.getByDisplayValue("旧推荐用法")).toBeInTheDocument());
    // 修改推荐用法 → 触发 editGuideDirty
    fireEvent.change(screen.getByDisplayValue("旧推荐用法"), { target: { value: "新推荐用法" } });
    // 填变更原因并保存
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "同步更新消费指南" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      expect(mockedUpdateGuide).toHaveBeenCalledWith(
        "sales_gmv_sum_d",
        expect.objectContaining({ recommended_usage: ["新推荐用法"], row_version: metric.row_version }),
      );
    });
    await waitFor(() => expect(mockedUpdateMetric).toHaveBeenCalled());
    // 保存顺序：指南先于指标
    const guideOrder = mockedUpdateGuide.mock.invocationCallOrder[0];
    const metricOrder = mockedUpdateMetric.mock.invocationCallOrder[0];
    expect(guideOrder).toBeLessThan(metricOrder);
  });

  it("编辑弹窗保存：指南未修改时不调用 updateConsumptionGuide", async () => {
    mockedUpdateGuide.mockClear();
    mockedUpdateMetric.mockClear();
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      consumption_guide: { recommended_usage: ["旧推荐用法"], cautions: [], related_metrics: [] },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedDomainTree.mockResolvedValue([
      { id: 1, code: "sales", name: "销售域", parent_id: null, level: 1, sort_order: 0, status: "active", metric_count: 0, children: [] },
    ]);
    mockedCurrentUser.mockResolvedValue({
      id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1,
    });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1, role: "metric_owner", home_domain: "outpatient",
      allowed_actions: ["read", "write"], ui_actions: ["metric:create"],
      granted_domains: [], metric_whitelist: [], row_level_restricted: false,
      grants: [], expiring_soon: [],
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    fireEvent.click(await screen.findByRole("button", { name: /编辑/ }));
    await waitFor(() => expect(document.querySelector(".ant-modal")).toBeTruthy());
    // 只改名称，不改指南
    const nameInput = document.querySelector('.ant-modal input[id="name"]') as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "销售 GMV 改名" } });
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "仅改名称" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => expect(mockedUpdateMetric).toHaveBeenCalled());
    expect(mockedUpdateGuide).not.toHaveBeenCalled();
  });

  it("详情页口径定义卡片展示三层口径（业务/技术/数仓SQL空态）", async () => {
    // 三层口径（产品文档 §2.2）：业务口径（definition_json.definition）→ 技术口径（源业务库口径，sql）
    // → 数仓SQL口径（dw_definition）；数仓为空时展示"未填写"引导而非隐藏
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "PUBLISHED",
      definition_json: {
        definition: "当日支付成功订单的成交总额",
        sql: "SELECT SUM(order_amount) AS gmv, dt FROM dwd_order_di GROUP BY dt",
      },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
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
    // 三层口径标签与值
    expect(screen.getByText("业务口径")).toBeTruthy();
    expect(screen.getByText("当日支付成功订单的成交总额")).toBeTruthy();
    expect(screen.getByText("技术口径（源业务库口径）")).toBeTruthy();
    expect(screen.getByText("SELECT SUM(order_amount) AS gmv, dt FROM dwd_order_di GROUP BY dt")).toBeTruthy();
    // 数仓SQL口径空态：标签可见 + "未填写"引导（而非隐藏）
    expect(screen.getByText("数仓SQL口径")).toBeTruthy();
    expect(screen.getByText("未填写（可在编辑弹窗补填）")).toBeTruthy();
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
      items: [{ id: 1, dim_code: "dim_channel", name: "渠道", domain: "outpatient", type: "SCD1", description: "渠道维度", owner_id: 1, status: "PUBLISHED", created_at: "", updated_at: "" }],
      total: 1,
    });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
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
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
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
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
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


  it("SQL 模式指标打开编辑弹窗自动落 SQL 模式，编辑 SQL 保存携带 definition_json.sql", async () => {
    // SQL 模式指标（definition_json 含 sql）→ 打开弹窗自动选中「SQL 模式」并预填 SQL；
    // 修改 SQL 保存 → definition_json.sql 更新且保留原口径非 sql 字段（不丢字段）
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      definition_json: {
        sql: "SELECT SUM(amount) AS gmv FROM dwd_sales",
        expression: "sum(amount)",
        source_tables: ["dwd_sales"],
      },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
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
    // 自动落 SQL 模式：Segmented 选中「SQL 模式」，SQL 框预填原 SQL，JSON 框不渲染
    await waitFor(() => {
      const selected = document.querySelector(".ant-modal .ant-segmented-item-selected");
      expect(selected?.textContent).toContain("SQL 模式");
    });
    const sqlArea = document.querySelector('[data-testid="editSqlText"]') as HTMLTextAreaElement | null;
    expect(sqlArea).toBeTruthy();
    expect((sqlArea as HTMLTextAreaElement).value).toBe("SELECT SUM(amount) AS gmv FROM dwd_sales");
    expect(document.querySelector('.ant-modal textarea[id="definition_json"]')).toBeNull();
    // 修改 SQL 并保存 → definition_json.sql 更新，原非 sql 字段保留（expression/source_tables）
    fireEvent.change(sqlArea as HTMLTextAreaElement, { target: { value: "SELECT SUM(amount) AS gmv FROM dwd_sales WHERE channel = 'APP'" } });
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "口径增加 APP 渠道过滤" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      const lastCall = mockedUpdateMetric.mock.calls[mockedUpdateMetric.mock.calls.length - 1]?.[1];
      expect(lastCall).toMatchObject({
        definition_json: {
          source_tables: ["dwd_sales"],
          sql: "SELECT SUM(amount) AS gmv FROM dwd_sales WHERE channel = 'APP'",
        },
      });
    });
  });

  it("编辑弹窗口径分角色（伪代码/数仓口径）回填 + 修改合入 + 清空移除（dirty 语义）", async () => {
    // 存量指标（口径双字段上线前注册）无 pseudo_definition/dw_definition → 打开弹窗两框为空；
    // 输入伪代码口径保存 → 合入 definition_json；清空数仓口径保存 → 从口径移除（dirty 区分）。
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      definition_json: {
        sql: "SELECT SUM(amount) AS gmv FROM dwd_sales",
        definition: "按渠道汇总的订单成交总额（业务口径）",
        pseudo_definition: "按渠道汇总订单金额（伪 SQL）",
        dw_definition: "SELECT channel, SUM(order_amount) FROM dwd_sales GROUP BY channel",
      },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
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
    // 回填：三个口径 TextArea 均带原值（业务口径 + 伪代码 + 数仓SQL）
    const bizArea = document.querySelector('[data-testid="editBusinessDefinition"]') as HTMLTextAreaElement | null;
    const pseudoArea = document.querySelector('[data-testid="editPseudoDefinition"]') as HTMLTextAreaElement | null;
    const dwArea = document.querySelector('[data-testid="editDwDefinition"]') as HTMLTextAreaElement | null;
    expect(bizArea).toBeTruthy();
    expect(pseudoArea).toBeTruthy();
    expect(dwArea).toBeTruthy();
    expect((bizArea as HTMLTextAreaElement).value).toBe("按渠道汇总的订单成交总额（业务口径）");
    expect((pseudoArea as HTMLTextAreaElement).value).toBe("按渠道汇总订单金额（伪 SQL）");
    expect((dwArea as HTMLTextAreaElement).value).toBe(
      "SELECT channel, SUM(order_amount) FROM dwd_sales GROUP BY channel",
    );
    // 修改业务口径 + 修改伪代码口径 + 清空数仓口径 → 保存：definition/pseudo 更新、dw 移除、sql 保留
    fireEvent.change(bizArea as HTMLTextAreaElement, { target: { value: "按渠道汇总的实付金额（业务口径）" } });
    fireEvent.change(pseudoArea as HTMLTextAreaElement, { target: { value: "按渠道汇总实付金额（伪 SQL）" } });
    fireEvent.change(dwArea as HTMLTextAreaElement, { target: { value: "" } });
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "补填三层口径" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      const lastCall = mockedUpdateMetric.mock.calls[mockedUpdateMetric.mock.calls.length - 1]?.[1];
      expect(lastCall).toMatchObject({
        definition_json: {
          sql: "SELECT SUM(amount) AS gmv FROM dwd_sales",
          definition: "按渠道汇总的实付金额（业务口径）",
          pseudo_definition: "按渠道汇总实付金额（伪 SQL）",
        },
      });
      // dw_definition 被清空移除（不在提交口径中）
      const def = (lastCall as { definition_json?: Record<string, unknown> }).definition_json ?? {};
      expect("dw_definition" in def).toBe(false);
    });
  });

  it("编辑弹窗三层口径 LLM 增强：业务口径 AI 丰富增强回填 + dirty（可保存合入）", async () => {
    // 业务口径已有值 → 点击「AI 丰富增强」→ LLM 回填增强文本，置 dirty 后可保存合入 definition_json
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      definition_json: {
        sql: "SELECT SUM(amount) AS gmv FROM dwd_sales",
        definition: "按渠道汇总的订单成交总额",
      },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    mockedRefine.mockResolvedValue({
      content: "按就诊号去重统计的门诊就诊总人次（含跨院区）",
      source: "llm",
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    fireEvent.click(await screen.findByRole("button", { name: /编辑/ }));
    await waitFor(() => {
      expect(document.querySelector(".ant-modal")).toBeTruthy();
    });
    // 业务口径有值 → 按钮显示「AI 丰富增强」
    const enrichBtn = screen.getByRole("button", { name: /AI 丰富增强/ });
    expect(enrichBtn).toBeTruthy();
    fireEvent.click(enrichBtn);
    // 等待 LLM 回填：textarea 更新为增强文本
    await waitFor(() => {
      const bizArea = document.querySelector('[data-testid="editBusinessDefinition"]') as HTMLTextAreaElement | null;
      expect((bizArea as HTMLTextAreaElement).value).toBe("按就诊号去重统计的门诊就诊总人次（含跨院区）");
    });
    // 请求载荷：field=business + action=enrich + 指标上下文（名称/编码/域/技术口径SQL）
    expect(mockedRefine).toHaveBeenCalledWith(
      expect.objectContaining({
        field: "business",
        action: "enrich",
        current: "按渠道汇总的订单成交总额",
        metric_code: "sales_gmv_sum_d",
        metric_name: "销售 GMV",
        domain: "outpatient",
        sql: "SELECT SUM(amount) AS gmv FROM dwd_sales",
      }),
    );
    // dirty 生效：保存后 LLM 增强文本合入 definition_json.definition
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "AI 丰富增强业务口径" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      const lastCall = mockedUpdateMetric.mock.calls[mockedUpdateMetric.mock.calls.length - 1]?.[1];
      expect(lastCall).toMatchObject({
        definition_json: {
          sql: "SELECT SUM(amount) AS gmv FROM dwd_sales",
          definition: "按就诊号去重统计的门诊就诊总人次（含跨院区）",
        },
      });
    });
  });

  it("编辑弹窗三层口径 LLM 增强：LLM 不可用时提示错误且不回填", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      definition_json: { expression: "sum(amount)" },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
      allowed_actions: ["read", "write"],
      ui_actions: ["metric:create"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    });
    const llmErr = new UnisenseApiError("LLM 不可用：请检查 LLM 配置或稍后重试", "LLM_INFER_UNAVAILABLE", 400, "trace");
    llmErr.code = "LLM_INFER_UNAVAILABLE";
    mockedRefine.mockRejectedValue(llmErr);
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    fireEvent.click(await screen.findByRole("button", { name: /编辑/ }));
    await waitFor(() => {
      expect(document.querySelector(".ant-modal")).toBeTruthy();
    });
    // 业务口径为空 → 按钮显示「AI 生成」（限定在业务口径 Form.Item 内，避免命中伪代码/数仓的同名按钮）
    const bizItem = (document.querySelector('[data-testid="editBusinessDefinition"]') as HTMLElement)
      .closest(".ant-form-item") as HTMLElement;
    const genBtn = within(bizItem).getByRole("button", { name: /AI 生成/ });
    fireEvent.click(genBtn);
    await waitFor(() => {
      const bizArea = document.querySelector('[data-testid="editBusinessDefinition"]') as HTMLTextAreaElement | null;
      expect((bizArea as HTMLTextAreaElement).value).toBe("");
    });
    // 错误提示
    await screen.findByText(/LLM 不可用/);
  });

  it("编辑弹窗口径定义支持表达式模式切 SQL 模式并保存（Segmented 双向切换）", async () => {
    // 表达式模式指标（definition_json 无 sql）→ 打开弹窗默认表达式模式；
    // 切「SQL 模式」出现 SQL 框，输入 SQL 保存 → definition_json 合入 sql（保留原字段）
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      definition_json: { expression: "sum(amount)", source_tables: ["dwd_sales"] },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
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
    // 默认表达式模式：JSON 框存在，SQL 框不渲染
    expect(document.querySelector('.ant-modal textarea[id="definition_json"]')).toBeTruthy();
    expect(document.querySelector('[data-testid="editSqlText"]')).toBeNull();
    // 切到 SQL 模式：无存量 sql 字段 → 空框，用户手输 SQL
    const segItems = Array.from(document.querySelectorAll(".ant-modal .ant-segmented-item")) as HTMLElement[];
    const sqlSeg = segItems.find((el) => (el.textContent ?? "").includes("SQL 模式"));
    fireEvent.click(sqlSeg as HTMLElement);
    const sqlArea = document.querySelector('[data-testid="editSqlText"]') as HTMLTextAreaElement | null;
    await waitFor(() => {
      expect(sqlArea).toBeTruthy();
    });
    fireEvent.change(sqlArea as HTMLTextAreaElement, { target: { value: "SELECT SUM(amount) FROM dwd_sales" } });
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "改为 SQL 口径" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      const lastCall = mockedUpdateMetric.mock.calls[mockedUpdateMetric.mock.calls.length - 1]?.[1];
      expect(lastCall).toMatchObject({
        definition_json: {
          source_tables: ["dwd_sales"],
          sql: "SELECT SUM(amount) FROM dwd_sales",
        },
      });
    });
  });

  it("编辑弹窗治理字段回填（数仓层/时效/分级/币种），未改保存不传（dirty 机制保留原值）", async () => {
    // 指标治理字段（dw_layer/freshness/metric_tier/currency）在 openEdit 回填；
    // 未修改时保存 payload 不含治理字段（dirty 未改不传 → 后端保留原值，防误覆盖）
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      dw_layer: "DWD",
      freshness: "T1",
      metric_tier: "T2",
      currency: "CNY",
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]); // 字典为空 → 遗留治理值必须兜底显示
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
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
    // 币种回填 CNY（治理字段回填；字典 mock 为空 → 兜底 label 带「不在字典中」）
    await waitFor(() => {
      const selected = Array.from(document.querySelectorAll(".ant-modal .ant-select-selection-item"));
      expect(selected.some((el) => el.textContent?.includes("CNY"))).toBeTruthy();
    });
    // 未改治理字段 → 保存 payload 不含治理字段（dirty 未改不传，后端保留原值）
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "仅调整名称，治理字段不动" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      expect(mockedUpdateMetric).toHaveBeenCalledWith(
        "sales_gmv_sum_d",
        expect.objectContaining({ change_reason: expect.any(String) }),
      );
    });
    const lastCall = mockedUpdateMetric.mock.calls[mockedUpdateMetric.mock.calls.length - 1][1];
    expect(lastCall).not.toHaveProperty("dw_layer");
    expect(lastCall).not.toHaveProperty("currency");
  });

  it("编辑弹窗清空币种（allowClear 后保存 currency 传空串，不再静默保留原币种）", async () => {
    // currency 是可选字段（str|None），清空是合法终态（非币种指标）。
    // 修复前：allowClear 清空后被 `&& value` 过滤 → 不提交 → 币种静默保留（清空意图失效）。
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      currency: "CNY",
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
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
    // 币种回填 CNY（Select 选中项；字典 mock 为空 → 兜底 label 带「不在字典中」）
    await waitFor(() => {
      const selected = Array.from(document.querySelectorAll(".ant-modal .ant-select-selection-item"));
      expect(selected.some((el) => el.textContent?.includes("CNY"))).toBeTruthy();
    });
    // 清空币种（allowClear 点击清除图标）→ dirty 标记 currency
    const currencySelect = Array.from(document.querySelectorAll(".ant-modal .ant-select")).find((el) =>
      el.textContent?.includes("CNY"),
    );
    const clearBtn = currencySelect?.querySelector(".ant-select-clear") as HTMLElement;
    expect(clearBtn).toBeTruthy();
    fireEvent.mouseDown(clearBtn);
    fireEvent.click(clearBtn);
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "清空币种（该指标非币种口径）" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      expect(mockedUpdateMetric).toHaveBeenCalled();
    });
    const lastCall = mockedUpdateMetric.mock.calls[mockedUpdateMetric.mock.calls.length - 1][1];
    expect(lastCall).toHaveProperty("currency", ""); // 空串提交 → 后端清空币种（修复核心）
  });

  it("PUBLISHED 指标显示「发起变更申请」入口，弹窗提示破坏性变更进入 PENDING 确认期", async () => {
    // 修复前：编辑按钮仅 DRAFT/REVIEW 显示，已发布指标的治理/口径变更无前端入口
    // （后端 update_metric 支持 PUBLISHED——破坏性→PENDING_VERSION、治理→直接生效，前端不可达）。
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "PUBLISHED",
      currency: "CNY",
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
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
    // PUBLISHED + owner 显示「发起变更申请」（区别于 DRAFT/REVIEW 的「编辑」）
    fireEvent.click(await screen.findByRole("button", { name: /发起变更申请/ }));
    await waitFor(() => {
      expect(document.querySelector(".ant-modal")).toBeTruthy();
    });
    // 弹窗内提示：破坏性变更进入 PENDING 确认期（已发布语义）
    expect(
      screen.getByText(/该指标已发布：变更可能触发口径版本确认/),
    ).toBeTruthy();
    // 保存反馈为已发布场景文案（PENDING 提示而非"已更新"）
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "发起已发布指标的变更申请" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      expect(mockedUpdateMetric).toHaveBeenCalled();
    });
  });

  it("编辑弹窗遗留粒度/单位值兜底（字典未收录时仍显示并保留，防静默清空）", async () => {
    // 存量派生指标粒度 "daily" 不在字典（字典为空），openEdit 应将其作为兜底选项加入，
    // 保存时 granularity/unit 不被静默清空（数据丢失防护）。
    // （S6：原子粒度编辑框已隐藏、粒度锁死 day——遗留粒度兜底只对派生/复合生效）
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      type: "derived",
      granularity: "daily",
      unit: "USD",
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]); // 字典为空 → 遗留值必须兜底
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
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
      items: [{ id: 1, dim_code: "dim_channel", name: "渠道", domain: "outpatient", type: "SCD1", description: "渠道维度", owner_id: 1, status: "PUBLISHED", created_at: "", updated_at: "" }],
      total: 1,
    });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1,
      role: "metric_owner",
      home_domain: "outpatient",
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

  it("H3：编辑弹窗清空口径 JSON 后改子字段 → 提交口径保留原主体（不丢 expression/sql/source_tables）", async () => {
    // 修复前：口径 JSON 文本框留空后，子字段（伪代码/数仓/维度等）合并以 {} 为基底，
    // 原口径主体被静默丢弃（保存后口径变空）。修复后：子字段合并以原 metric.definition_json
    // 为基底，仅叠加改动字段。
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "DRAFT",
      // 无 sql → expression 模式 → 编辑弹窗显示「口径定义（JSON）」文本框
      definition_json: {
        expression: "sum(gmv)",
        definition: "当日支付成功订单的成交总额",
        source_tables: ["dwd_order_di"],
        dependencies: ["user_base_cnt_d"],
      },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1, role: "metric_owner", home_domain: "outpatient",
      allowed_actions: ["read", "write"], ui_actions: ["metric:create"],
      granted_domains: [], metric_whitelist: [], row_level_restricted: false, grants: [], expiring_soon: [],
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    fireEvent.click(await screen.findByRole("button", { name: /编辑/ }));
    await waitFor(() => expect(document.querySelector(".ant-modal")).toBeTruthy());
    // 清空口径 JSON 文本框（UI 提示「留空表示不修改口径」）
    const defArea = document.querySelector('.ant-modal textarea[id="definition_json"]') as HTMLTextAreaElement;
    fireEvent.change(defArea, { target: { value: "" } });
    // 改伪代码口径（子字段 dirty → 触发合并）
    const pseudoArea = document.querySelector('.ant-modal textarea[data-testid="editPseudoDefinition"]') as HTMLTextAreaElement;
    fireEvent.change(pseudoArea, { target: { value: "sum(成交额) 按日汇总" } });
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "补充口径说明" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      const lastCall = mockedUpdateMetric.mock.calls[mockedUpdateMetric.mock.calls.length - 1]?.[1];
      expect(lastCall?.definition_json).toEqual(
        expect.objectContaining({
          expression: "sum(gmv)", // 原主体保留（修复核心断言）
          source_tables: ["dwd_order_di"], // 原 source_tables 保留
          pseudo_definition: "sum(成交额) 按日汇总", // 新子字段叠加
        }),
      );
    });
  });

  it("M2：PUBLISHED 仅改治理属性 → 提示直接生效，不宣称进入消费方确认期", async () => {
    // 修复前：PUBLISHED 保存成功无条件提示「破坏性修改进入消费方确认期」。
    // 修复后：仅实际破坏性（粒度/单位/聚合/口径主体）变化才提示 PENDING，治理属性直接更新。
    // 用纯 SQL 模式口径（无 expression）：避免 SQL 模式重组排除 expression 被误判为破坏性
    mockedGetMetric.mockResolvedValue({
      ...metric,
      status: "PUBLISHED",
      definition_json: {
        sql: "SELECT SUM(order_amount) AS gmv, dt FROM dwd_order_di GROUP BY dt",
        source_tables: ["dwd_order_di"],
        dependencies: ["user_base_cnt_d"],
      },
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1, role: "metric_owner", home_domain: "outpatient",
      allowed_actions: ["read", "write"], ui_actions: ["metric:create"],
      granted_domains: [], metric_whitelist: [], row_level_restricted: false, grants: [], expiring_soon: [],
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    // PUBLISHED 状态编辑入口文案为「发起变更申请」（1666 行，与 DRAFT 的「编辑」区分）
    fireEvent.click(await screen.findByRole("button", { name: /变更申请/ }));
    await waitFor(() => expect(document.querySelector(".ant-modal")).toBeTruthy());
    // 只改名称（治理属性，非破坏性）→ 保存 → 提示「已直接更新，无需消费方确认」
    const nameInput = document.querySelector('.ant-modal input[id="name"]') as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "销售 GMV（改名）" } });
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "治理属性调整" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await screen.findByText("指标已更新（治理属性变更已直接生效，无需消费方确认）");
    expect(mockedUpdateMetric).toHaveBeenCalled();
  });

  it("详情页 atomic 展示已关联逻辑度量名称+编码（backend best-effort 填充）", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      pii_flag: false,
      measure_name: "支付金额",
      measure_code: "pay_amt",
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1, role: "metric_owner", home_domain: "outpatient",
      allowed_actions: ["read", "write"], ui_actions: ["metric:read"],
      granted_domains: [], metric_whitelist: [], row_level_restricted: false, grants: [], expiring_soon: [],
    });
    renderWithPerms(["metric:read"]);
    await screen.findByText("销售 GMV");
    // Descriptions「逻辑度量」栏：名称 + 编码
    expect(screen.getByText("逻辑度量")).toBeInTheDocument();
    expect(screen.getByText("支付金额")).toBeInTheDocument();
    expect(screen.getByText("pay_amt")).toBeInTheDocument();
  });

  it("详情页 atomic 未关联逻辑度量显示引导（存量旧式来源）", async () => {
    mockedGetMetric.mockResolvedValue({ ...metric, pii_flag: false, measure_id: null });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1, role: "metric_owner", home_domain: "outpatient",
      allowed_actions: ["read", "write"], ui_actions: ["metric:read"],
      granted_domains: [], metric_whitelist: [], row_level_restricted: false, grants: [], expiring_soon: [],
    });
    renderWithPerms(["metric:read"]);
    await screen.findByText("销售 GMV");
    expect(screen.getByText(/未关联（存量旧式来源）/)).toBeInTheDocument();
  });

  it("atomic 编辑弹窗显示逻辑度量选择器并提交更换（破坏性口径变更）", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      pii_flag: false,
      measure_id: 1,
      measure_name: "支付金额",
      measure_code: "pay_amt",
    });
    mockedListMeasureCatalogs.mockResolvedValue({
      items: [
        { id: 1, measure_code: "pay_amt", name: "支付金额", status: "PUBLISHED" } as unknown as MeasureCatalog,
        { id: 2, measure_code: "gmv_amt", name: "成交总额", status: "PUBLISHED" } as unknown as MeasureCatalog,
      ],
      total: 2, page: 1, page_size: 200,
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1, role: "metric_owner", home_domain: "outpatient",
      allowed_actions: ["read", "write"], ui_actions: ["metric:create"],
      granted_domains: [], metric_whitelist: [], row_level_restricted: false, grants: [], expiring_soon: [],
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    fireEvent.click(await screen.findByRole("button", { name: /变更申请/ }));
    await waitFor(() => expect(document.querySelector(".ant-modal")).toBeTruthy());
    // 逻辑度量选择器（atomic 专属，位于名称后、粒度前，是弹窗内首个 Select）
    expect(screen.getByText("逻辑度量（原子指标口径库，OneData 原子层）")).toBeInTheDocument();
    // 选择新度量
    fireEvent.mouseDown(document.querySelector(".ant-modal .ant-select-selector") as HTMLElement);
    await screen.findByText("成交总额（gmv_amt）");
    fireEvent.click(screen.getByText("成交总额（gmv_amt）"));
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "更换逻辑度量" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      const lastCall = mockedUpdateMetric.mock.calls[mockedUpdateMetric.mock.calls.length - 1]?.[1];
      expect(lastCall?.measure_id).toBe(2);
    });
  });

  it("atomic 编辑弹窗清空逻辑度量 → 提交 measure_id=null 解除关联", async () => {
    mockedGetMetric.mockResolvedValue({
      ...metric,
      pii_flag: false,
      measure_id: 1,
      measure_name: "支付金额",
      measure_code: "pay_amt",
    });
    mockedListMeasureCatalogs.mockResolvedValue({
      items: [
        { id: 1, measure_code: "pay_amt", name: "支付金额", status: "PUBLISHED" } as unknown as MeasureCatalog,
      ],
      total: 1, page: 1, page_size: 200,
    });
    mockedListVersions.mockResolvedValue([]);
    mockedDictItems.mockResolvedValue([]);
    mockedDimensions.mockResolvedValue({ items: [], total: 0 });
    mockedListMetrics.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100 });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    mockedFavorites.mockResolvedValue([]);
    mockedHealth.mockResolvedValue(null as unknown as MetricHealth);
    mockedUsers.mockResolvedValue([]);
    mockedSubs.mockResolvedValue({ items: [], total: 0 });
    mockedRelated.mockResolvedValue([]);
    mockedMyPerms.mockResolvedValue({
      user_id: 1, role: "metric_owner", home_domain: "outpatient",
      allowed_actions: ["read", "write"], ui_actions: ["metric:create"],
      granted_domains: [], metric_whitelist: [], row_level_restricted: false, grants: [], expiring_soon: [],
    });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    fireEvent.click(await screen.findByRole("button", { name: /变更申请/ }));
    await waitFor(() => expect(document.querySelector(".ant-modal")).toBeTruthy());
    // 回填当前逻辑度量后 allowClear 清除按钮已渲染（首个 Select 为逻辑度量）——直接点击解除关联
    const clearBtn = document.querySelector(".ant-modal .ant-select-clear") as HTMLElement;
    expect(clearBtn).toBeTruthy();
    fireEvent.mouseDown(clearBtn);
    const reasonArea = document.querySelector('.ant-modal textarea[id="change_reason"]') as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "解除逻辑度量关联" } });
    fireEvent.click(document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement);
    await waitFor(() => {
      const lastCall = mockedUpdateMetric.mock.calls[mockedUpdateMetric.mock.calls.length - 1]?.[1];
      expect(lastCall?.measure_id).toBeNull();
    });
  });

  // P1-3：挂载实体可见——详情页展示挂载的物理表/粒度/周期/域（此前无任何页面读取挂载）
  it("详情页展示挂载实体（OneData 挂载层）物理表/粒度/周期/域", async () => {
    mockedListMetricMounts.mockResolvedValue({
      items: [
        {
          id: 7,
          metric_id: 1,
          source_table: "dwd_outpatient_gmv_df",
          source_column: "amount",
          granularity: "day",
          default_period: "day",
          domain: "outpatient",
          created_at: "2026-08-01T00:00:00",
          updated_at: "2026-08-01T00:00:00",
        },
      ],
      total: 1,
    });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    await screen.findByText("挂载实体（OneData 挂载层）");
    expect(screen.getByText("dwd_outpatient_gmv_df")).toBeTruthy();
    expect(screen.getByText(/列：amount · 粒度：day/)).toBeTruthy();
  });

  // 2026-08-27 多变体：详情页挂载卡逐行展示全部变体（不同粒度/限定/周期），业务限定随行展示
  it("详情页逐行展示多变体挂载（粒度/业务限定/周期）", async () => {
    mockedListMetricMounts.mockResolvedValue({
      items: [
        {
          id: 1,
          metric_id: 1,
          source_table: "dwd.doctor_fee_daily",
          source_column: "fee",
          granularity: "医生",
          default_period: "day",
          domain: "medical",
          business_filter: "场景=门诊",
          created_at: "2026-08-01T00:00:00",
          updated_at: "2026-08-01T00:00:00",
        },
        {
          id: 2,
          metric_id: 1,
          source_table: "dwd.hospital_fee",
          source_column: "fee",
          granularity: "医院",
          default_period: "day",
          domain: "medical",
          business_filter: "场景=住院",
          created_at: "2026-08-01T00:00:00",
          updated_at: "2026-08-01T00:00:00",
        },
      ],
      total: 2,
    });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    await screen.findByText("挂载实体（OneData 挂载层）");
    // 两个变体逐行展示，业务限定随行
    expect(screen.getByText("dwd.doctor_fee_daily")).toBeTruthy();
    expect(screen.getByText(/粒度：医生/)).toBeTruthy();
    expect(screen.getByText(/业务限定：场景=门诊/)).toBeTruthy();
    expect(screen.getByText("dwd.hospital_fee")).toBeTruthy();
    expect(screen.getByText(/粒度：医院/)).toBeTruthy();
    expect(screen.getByText(/业务限定：场景=住院/)).toBeTruthy();
    // 两个解除按钮（每行一个）
    expect(screen.getAllByRole("button", { name: /解除挂载/ })).toHaveLength(2);
  });

  // P1-3：挂载实体可管——已发布指标解除挂载 = 破坏性变更，走指标更新接口提交
  // （mounts 去行 + 变更原因），由后端判定 removed 行破坏性 → PENDING_VERSION 消费方
  // 确认流（14 天确认后生效），不再直接调 DELETE /metric-mounts 软删。
  it("已发布指标解除挂载：确认后调 updateMetric（mounts 去行）进入确认流，不直接删除", async () => {
    // 第二个 describe 无 beforeEach：显式清空 spy 防跨测试残留
    mockedUpdateMetric.mockClear();
    mockedDeleteMetricMount.mockClear();
    mockedGetMetric.mockResolvedValue(metric);
    mockedListMetricMounts.mockResolvedValue({
      items: [
        {
          id: 7,
          metric_id: 1,
          source_table: "dwd_outpatient_gmv_df",
          source_column: "amount",
          granularity: "day",
          default_period: "day",
          domain: "outpatient",
          created_at: "2026-08-01T00:00:00",
          updated_at: "2026-08-01T00:00:00",
        },
      ],
      total: 1,
    });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    await screen.findByText("挂载实体（OneData 挂载层）");
    const beforeMountsCalls = mockedListMetricMounts.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: /解除挂载/ }));
    await waitFor(() => expect(document.querySelector(".ant-modal-confirm")).toBeTruthy());
    // 已发布指标：确认弹窗标题明确「需消费方确认」，确认按钮为「提交解除」（危险样式）
    expect(screen.getAllByText("解除挂载（需消费方确认）").length).toBeGreaterThan(0);
    fireEvent.click(document.querySelector(".ant-modal-confirm .ant-btn-dangerous") as HTMLElement);
    await waitFor(() => expect(mockedUpdateMetric).toHaveBeenCalled());
    // mounts 去行（只剩一行 → 空数组）+ 变更原因 + 乐观锁；不直接 DELETE 挂载
    expect(mockedUpdateMetric).toHaveBeenCalledWith(
      "sales_gmv_sum_d",
      expect.objectContaining({
        mounts: [],
        change_reason: "解除挂载变体：dwd_outpatient_gmv_df（day）",
        row_version: 1,
      }),
    );
    expect(mockedDeleteMetricMount).not.toHaveBeenCalled();
    // 提交后刷新挂载列表（解除前已 load 一次，提交后应多一次）
    await waitFor(() =>
      expect(mockedListMetricMounts.mock.calls.length).toBe(beforeMountsCalls + 1),
    );
  });

  // DRAFT/REVIEW 指标无消费方确认期：解除挂载保持直接 DELETE 挂载（软删）
  it("草稿指标解除挂载：确认后直接调 deleteMetricMount 并刷新挂载列表", async () => {
    // 第二个 describe 无 beforeEach：显式清空 spy 防跨测试残留
    mockedUpdateMetric.mockClear();
    mockedDeleteMetricMount.mockClear();
    mockedGetMetric.mockResolvedValue({ ...metric, status: "DRAFT" });
    mockedListMetricMounts.mockResolvedValue({
      items: [
        {
          id: 7,
          metric_id: 1,
          source_table: "dwd_outpatient_gmv_df",
          source_column: "amount",
          granularity: "day",
          default_period: "day",
          domain: "outpatient",
          created_at: "2026-08-01T00:00:00",
          updated_at: "2026-08-01T00:00:00",
        },
      ],
      total: 1,
    });
    mockedDomainTree.mockResolvedValue([]);
    mockedCurrentUser.mockResolvedValue({ id: 1, username: "zhangsan", display_name: "张三", role: "metric_owner", domain: "outpatient", org_id: 1 });
    renderWithPerms(["metric:create"]);
    await screen.findByText("销售 GMV");
    await screen.findByText("挂载实体（OneData 挂载层）");
    const beforeMountsCalls = mockedListMetricMounts.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: /解除挂载/ }));
    await waitFor(() => expect(document.querySelector(".ant-modal-confirm")).toBeTruthy());
    // 草稿指标：确认弹窗为普通「解除挂载」（无确认期提示；按钮+标题两处匹配）
    expect(screen.getAllByText("解除挂载").length).toBeGreaterThan(0);
    expect(screen.queryByText("解除挂载（需消费方确认）")).toBeNull();
    fireEvent.click(document.querySelector(".ant-modal-confirm .ant-btn-dangerous") as HTMLElement);
    await waitFor(() => expect(mockedDeleteMetricMount).toHaveBeenCalledWith(7));
    expect(mockedUpdateMetric).not.toHaveBeenCalled();
    // 解除成功后刷新挂载列表（解除前已 load 一次，解除后应多一次）
    await waitFor(() =>
      expect(mockedListMetricMounts.mock.calls.length).toBe(beforeMountsCalls + 1),
    );
  });

});
