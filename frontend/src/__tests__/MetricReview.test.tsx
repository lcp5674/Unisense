import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { MetricReview } from "../pages/MetricReview";
import type { MetricResponse } from "../types";

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
    get codeZh(): string {
      return this.code;
    }
  }
  return {
    listMetrics: vi.fn(),
    reviewMetric: vi.fn(),
    approveMetric: vi.fn(),
    fetchCurrentUser: vi.fn(),
    listUsers: vi.fn(),
    listDomainTree: vi.fn(),
    batchApproveMetrics: vi.fn(),
    batchRejectMetrics: vi.fn(),
    listVersions: vi.fn(),
    UnisenseApiError,
  };
});

import {
  approveMetric,
  batchRejectMetrics,
  fetchCurrentUser,
  listDomainTree,
  listMetrics,
  listUsers,
  listVersions,
} from "../api";
import type { MetricVersionResponse } from "../types";
const mockedList = vi.mocked(listMetrics);
const mockedApprove = vi.mocked(approveMetric);
const mockedBatchReject = vi.mocked(batchRejectMetrics);
const mockedListVersions = vi.mocked(listVersions);

const metric: MetricResponse = {
  id: 1,
  metric_code: "sales_gmv_day",
  name: "日销售额",
  domain: "finance",
  type: "atomic",
  granularity: "day",
  unit: "元",
  currency: null,
  aggregation: "SUM",
  time_semantics: "event",
  freshness: "T+1",
  sla: null,
  dw_layer: "DWS",
  metric_tier: "T1",
  serving_mode: "api",
  additivity: "additive",
  non_additive_dimensions: null,
  definition_json: {},
  version: 1,
  row_version: 0,
  status: "REVIEW",
  owner_id: 1,
  backup_owner_id: null,
  approver_id: null,
  submitted_by: null,
  pii_flag: false,
  compliance_reviewed: true,
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
  created_at: "2026-08-01T00:00:00",
  updated_at: "2026-08-01T00:00:00",
};

function renderReview() {
  return render(
    <MemoryRouter initialEntries={["/metrics/review"]}>
      <MetricReview />
    </MemoryRouter>,
  );
}

describe("MetricReview 指标审批", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedList.mockResolvedValue({ items: [metric], total: 1, page: 1, page_size: 100 });
    // 审批页需当前用户身份 + 用户映射（默认平台管理员，可评审任意指标）
    vi.mocked(fetchCurrentUser).mockResolvedValue({
      id: 1,
      username: "admin",
      display_name: "管理员",
      role: "platform_admin",
      domain: null,
      org_id: 1,
    });
    vi.mocked(listUsers).mockResolvedValue([]);
    vi.mocked(listDomainTree).mockResolvedValue([]);
    // 变更上下文默认无版本记录（详情/确认弹窗的 listVersions 均走默认空）
    vi.mocked(listVersions).mockResolvedValue([]);
  });

  it("加载并展示待审核指标", async () => {
    renderReview();
    await screen.findByText("sales_gmv_day");
    // 分页参数（默认第 1 页、每页 20 条）
    expect(mockedList).toHaveBeenCalledWith({
      status: "REVIEW",
      page: 1,
      page_size: 20,
      sort_by: "updated_at",
      sort_order: "asc",
    });
  });

  it("编码列长编码用 CodeValue 中间省略（不渲染为 Button 撑破列宽覆盖相邻列）", async () => {
    const longCode = "uncategorized_doctor_currentmonthactivedoctorcnt_month";
    mockedList.mockResolvedValue({
      items: [{ ...metric, id: 99, metric_code: longCode, batch_id: "sqlbatch_xyz" }],
      total: 1,
      page: 1,
      page_size: 100,
    });
    const { container } = renderReview();
    // CodeValue 长编码：class 锚定 + aria-label 含完整值（屏幕阅读器可达，避免回退到 Button nowrap）
    const codeEl = await waitFor(() => {
      const el = container.querySelector<HTMLElement>(".code-value.code-value-long");
      if (!el) throw new Error("code-value-long not yet rendered");
      return el;
    });
    expect(codeEl.getAttribute("aria-label")).toBe(longCode);
    expect(codeEl.textContent).toContain("…");
    expect(codeEl.textContent).not.toBe(longCode);
    // 重要：不能再渲染为 Button（nowrap 会横向覆盖名称列）
    expect(screen.queryByRole("button", { name: longCode })).toBeNull();
    // 名称列内容完整可读，不被覆盖
    expect(screen.getByText("日销售额")).toBeTruthy();
  });

  it("提供统一的返回按钮（返回上一入口）", async () => {
    renderReview();
    await screen.findByText("sales_gmv_day");
    expect(screen.getByRole("button", { name: /返\s*回/ })).toBeTruthy();
  });

  it("点击返回：历史栈有上一页时回退到上一入口（不限于总览仪表）", async () => {
    const lengthSpy = vi.spyOn(window.history, "length", "get").mockReturnValue(3);
    render(
      <MemoryRouter initialEntries={["/dashboard", "/metrics/review"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/metrics/review" element={<MetricReview />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("sales_gmv_day");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await screen.findByText("dashboard-page");
    lengthSpy.mockRestore();
  });

  it("点击返回：无上一页（URL 直达）时兜底跳转总览仪表", async () => {
    render(
      <MemoryRouter initialEntries={["/metrics/review"]}>
        <Routes>
          <Route path="/dashboard" element={<div>dashboard-page</div>} />
          <Route path="/metrics/review" element={<MetricReview />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("sales_gmv_day");
    fireEvent.click(screen.getByRole("button", { name: /返\s*回/ }));
    await waitFor(() => expect(screen.getByText("dashboard-page")).toBeInTheDocument());
  });

  it("PII 待复核指标：禁用『通过』并展示待复核标签（对齐后端 COMPLIANCE_BLOCKED 拦截）", async () => {
    const piiPending: MetricResponse = {
      ...metric,
      metric_code: "pii_metric_day",
      pii_flag: true,
      compliance_reviewed: false,
    };
    mockedList.mockResolvedValue({ items: [piiPending], total: 1, page: 1, page_size: 100 });
    renderReview();
    await screen.findByText("pii_metric_day");
    // 「通过」按钮应禁用
    const approveBtn = screen.getAllByRole("button", { name: /通\s*过/ })[0];
    expect((approveBtn as HTMLButtonElement).disabled).toBe(true);
    // PII 待复核标签可见（PII 列，与指标目录一致）
    expect(screen.getAllByText(/PII 待复核/).length).toBeGreaterThan(0);
  });

  it("「我审过的」视图：按 reviewed_by 查询（含驳回历史）、展示处理结果、无操作按钮、无批量按钮", async () => {
    const reviewed: MetricResponse = {
      ...metric,
      status: "PUBLISHED",
      approver_id: 1,
      approved_at: "2026-08-10T10:00:00",
    };
    mockedList.mockResolvedValue({ items: [reviewed], total: 1, page: 1, page_size: 20 });
    renderReview();
    // 切换到「我审过的」
    fireEvent.click(await screen.findByRole("radio", { name: /我审过的/ }));
    await screen.findByText("sales_gmv_day");
    // 携带 reviewed_by 查询（命中审批通过或驳回——评审历史完整）
    expect(mockedList).toHaveBeenCalledWith(
      expect.objectContaining({ reviewed_by: 1 }),
    );
    // 无「通过」操作、无批量通过按钮
    expect(screen.queryAllByRole("button", { name: /通\s*过/ }).length).toBe(0);
    // 处理结果列：已通过 Tag
    expect(screen.getByText("已通过")).toBeTruthy();
    // 操作列：查看详情按钮（替代旧的"已处理"标签）
    expect(screen.getByRole("button", { name: /查看详情/ })).toBeTruthy();
  });

  it("「我审过的」驳回场景：展示已驳回 + 原因 + 时间", async () => {
    const rejected: MetricResponse = {
      ...metric,
      status: "DRAFT",
      reject_reviewer_id: 1,
      reject_reason: "口径缺少过滤条件，请补充后重提",
      rejected_at: "2026-08-11T09:30:00",
    };
    mockedList.mockResolvedValue({ items: [rejected], total: 1, page: 1, page_size: 20 });
    renderReview();
    fireEvent.click(await screen.findByRole("radio", { name: /我审过的/ }));
    await screen.findByText("sales_gmv_day");
    // 处理结果列：已驳回 Tag + 原因（截断展示仍可读）
    expect(screen.getByText("已驳回")).toBeTruthy();
    expect(screen.getByText(/口径缺少过滤条件/)).toBeTruthy();
  });

  it("「我审过的」查看详情弹窗：处理结论 + 完整口径", async () => {
    const reviewed: MetricResponse = {
      ...metric,
      status: "PUBLISHED",
      approver_id: 1,
      approved_at: "2026-08-10T10:00:00",
      definition_json: {
        expression: "SUM(amount)",
        source_tables: ["demo.sales_order"],
        source_fields: ["amount"],
      },
    };
    mockedList.mockResolvedValue({ items: [reviewed], total: 1, page: 1, page_size: 20 });
    renderReview();
    fireEvent.click(await screen.findByRole("radio", { name: /我审过的/ }));
    await screen.findByText("sales_gmv_day");
    fireEvent.click(screen.getByRole("button", { name: /查看详情/ }));
    // 弹窗：处理结论 + 完整口径
    expect(await screen.findByText(/评审记录：sales_gmv_day/)).toBeTruthy();
    expect(screen.getByText("已通过评审")).toBeTruthy();
    // 表达式与依赖表在口径卡片和完整 JSON 中均出现，用 getAllByText 断言
    expect(screen.getAllByText(/SUM\(amount\)/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("demo.sales_order").length).toBeGreaterThan(0);
  });

  it("评审记录弹窗：长口径 SQL/JSON 自动换行不撑破弹窗（pre-wrap + 宽度约束）", async () => {
    // 超长单行 SQL（无空格的长 token）与超长 JSON 字符串：若 white-space=pre 会按内容宽度撑破弹窗
    const longToken = "SUM(COALESCE(COUNT(DISTINCT CASE WHEN status='active' THEN doctor_code END),0)) OVER (PARTITION BY month_id ORDER BY hosp_code)";
    const reviewed: MetricResponse = {
      ...metric,
      status: "PUBLISHED",
      approver_id: 1,
      approved_at: "2026-08-10T10:00:00",
      definition_json: {
        expression: longToken,
        etl_sql: `SELECT ${longToken} AS m FROM dwd_t -- ${"x".repeat(300)}`,
        source_tables: ["demo.sales_order"],
      },
    };
    mockedList.mockResolvedValue({ items: [reviewed], total: 1, page: 1, page_size: 20 });
    renderReview();
    fireEvent.click(await screen.findByRole("radio", { name: /我审过的/ }));
    await screen.findByText("sales_gmv_day");
    fireEvent.click(screen.getByRole("button", { name: /查看详情/ }));
    await screen.findByText(/评审记录：sales_gmv_day/);
    // 口径 SQL 与完整 JSON 的 pre 均须 pre-wrap + 宽度约束（长行换行而非横向撑开弹窗）
    const pres = Array.from(document.querySelectorAll(".ant-modal pre"));
    expect(pres.length).toBeGreaterThanOrEqual(2);
    for (const p of pres) {
      const st = (p as HTMLElement).style;
      expect(st.whiteSpace).toBe("pre-wrap");
      expect(st.wordBreak).toBe("break-word");
      expect(st.maxWidth).toBe("100%");
    }
  });

  it("深页空结果自动回退上一页（审批后列表缩短不致空页）", async () => {
    // 默认第 1 页有数据、共 200 条（多页可分页）；第 2 页请求返回空（total>0 → 触发回退重查）
    mockedList.mockResolvedValue({ items: [metric], total: 200, page: 1, page_size: 20 });
    renderReview();
    await screen.findByText("sales_gmv_day");
    // 翻到第 2 页：该页请求返回空（total>0）→ 自动回退第 1 页重查
    mockedList.mockResolvedValueOnce({ items: [], total: 1, page: 2, page_size: 20 });
    fireEvent.click(document.querySelector(".ant-pagination-item-2") as HTMLElement);
    await waitFor(() => {
      const pages = mockedList.mock.calls.map((c) => c[0]?.page);
      expect(pages).toContain(2); // 发起过第 2 页查询
      expect(pages.filter((p) => p === 1).length).toBeGreaterThanOrEqual(2); // 回退后再次查第 1 页
    });
    // 回退后列表仍显示数据（非空页）
    expect(await screen.findByText("sales_gmv_day")).toBeTruthy();
  });

  it("审批通过默认标准发布，弹窗可选灰度发布并携带灰度租户", async () => {
    mockedApprove.mockResolvedValue(metric);
    renderReview();
    await screen.findByText("sales_gmv_day");
    // 点「通过」→ 弹窗含发布模式选择（标准/灰度）
    fireEvent.click(screen.getAllByRole("button", { name: /^通\s*过$/ })[0]);
    await waitFor(() => {
      expect(screen.getByText("标准发布（全部消费方）")).toBeTruthy();
      expect(screen.getByText("灰度发布（仅指定租户）")).toBeTruthy();
    });
    // 默认标准发布提交 → approveMetric 带 mode=standard、无灰度租户
    const confirmBtn = document.querySelector(
      ".ant-modal-confirm-btns .ant-btn-primary",
    ) as HTMLElement;
    expect(confirmBtn).toBeTruthy();
    fireEvent.click(confirmBtn);
    await waitFor(() => {
      expect(mockedApprove).toHaveBeenCalledWith("sales_gmv_day", {
        mode: "standard",
        gray_tenant_ids: undefined,
      });
    });
  });

  it("灰度发布输入非数字租户 ID 时提示且不提交", async () => {
    mockedApprove.mockResolvedValue(metric);
    renderReview();
    await screen.findByText("sales_gmv_day");
    fireEvent.click(screen.getAllByRole("button", { name: /^通\s*过$/ })[0]);
    await waitFor(() => {
      expect(screen.getByText("灰度发布（仅指定租户）")).toBeTruthy();
    });
    // 切换为灰度发布
    fireEvent.click(screen.getByText("灰度发布（仅指定租户）"));
    // 输入含非数字租户 ID
    const input = document.querySelector(
      ".ant-modal-confirm-content input",
    ) as HTMLInputElement;
    expect(input).toBeTruthy();
    fireEvent.change(input, { target: { value: "101,abc,102" } });
    const confirmBtn = document.querySelector(
      ".ant-modal-confirm-btns .ant-btn-primary",
    ) as HTMLElement;
    fireEvent.click(confirmBtn);
    // 非数字被拒绝 → 不提交、弹窗不关闭
    await waitFor(() => {
      expect(mockedApprove).not.toHaveBeenCalled();
    });
    expect(
      document.querySelector(".ant-modal-confirm-btns .ant-btn-primary"),
    ).toBeTruthy();
  });

  it("L1：批量驳回弹窗须填写原因（对齐单条驳回，不再硬编码）", async () => {
    mockedBatchReject.mockResolvedValue({
      ok_count: 1,
      fail_count: 0,
      results: [{ code: "sales_gmv_day", ok: true, message: "" }],
    } as never);
    renderReview();
    await screen.findByText("sales_gmv_day");
    // 勾选首行
    const checkbox = document.querySelector(
      ".ant-table-tbody .ant-checkbox-input",
    ) as HTMLInputElement;
    fireEvent.click(checkbox);
    // 点「批量驳回」→ 打开原因弹窗（修复前直接硬编码原因提交，无弹窗）
    fireEvent.click(await screen.findByRole("button", { name: /批量驳回/ }));
    await waitFor(() => expect(document.querySelector(".ant-modal")).toBeTruthy());
    // 不填原因点确认 → 拦截并提示
    fireEvent.click(
      document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement,
    );
    await screen.findByText("驳回原因至少 4 字，请补充说明");
    expect(mockedBatchReject).not.toHaveBeenCalled();
    // 填写原因后确认 → batchRejectMetrics 携带评审人填写的原因
    const reasonArea = document.querySelector(".ant-modal textarea") as HTMLTextAreaElement;
    fireEvent.change(reasonArea, { target: { value: "口径与同名指标冲突，请修正后重提" } });
    fireEvent.click(
      document.querySelector(".ant-modal .ant-btn-primary") as HTMLElement,
    );
    await waitFor(() => {
      expect(mockedBatchReject).toHaveBeenCalledWith(
        ["sales_gmv_day"],
        "口径与同名指标冲突，请修正后重提",
      );
    });
  });

  // ---- 审批变更上下文（新增/变更/破坏性变更/重评审 + 前后对比）----

  function reviewedMetric(over: Partial<MetricResponse> = {}): MetricResponse {
    return {
      ...metric,
      status: "PUBLISHED",
      approver_id: 1,
      approved_at: "2026-08-10T10:00:00",
      ...over,
    };
  }

  function version(v: Partial<MetricVersionResponse>): MetricVersionResponse {
    return {
      id: 1,
      metric_id: 1,
      version: 1,
      change_type: "CREATE",
      definition_json: {},
      diff_json: null,
      status: "DRAFT",
      change_reason: "",
      created_by: 1,
      published_at: null,
      created_at: "2026-08-01T00:00:00",
      ...v,
    };
  }

  async function openDetail(metricObj: MetricResponse) {
    mockedList.mockResolvedValue({ items: [metricObj], total: 1, page: 1, page_size: 20 });
    renderReview();
    fireEvent.click(await screen.findByRole("radio", { name: /我审过的/ }));
    await screen.findByText("sales_gmv_day");
    fireEvent.click(screen.getByRole("button", { name: /查看详情/ }));
    await screen.findByText(/评审记录：sales_gmv_day/);
  }

  it("评审记录：新增指标（CREATE 无已发布版本）展示新增标签 + 无历史对比", async () => {
    const reviewed = reviewedMetric({ version: 1, effective_version: null });
    mockedListVersions.mockResolvedValue([
      version({ version: 1, change_type: "CREATE", status: "DRAFT" }),
    ]);
    await openDetail(reviewed);
    // 变更上下文：新增指标 + 首次提交评审说明；不渲染变更前后对比
    expect(await screen.findByText("新增指标")).toBeTruthy();
    expect(screen.getByText("首次提交评审，无历史口径可对比")).toBeTruthy();
    expect(screen.queryByText("变更前")).toBeNull();
    // 类型/状态中文（原为 atomic/PUBLISHED 英文裸显）
    expect(screen.getByText("原子指标")).toBeTruthy();
    expect(screen.getByText("已发布")).toBeTruthy();
  });

  it("评审记录：变更指标（UPDATE + 已发布）展示 v{prev}→v{cur} + 前后对比值", async () => {
    const reviewed = reviewedMetric({ version: 2, effective_version: 1 });
    mockedListVersions.mockResolvedValue([
      version({
        version: 2,
        change_type: "UPDATE",
        status: "DRAFT",
        diff_json: {
          expression: { before: "SUM(amount) * 1.1", after: "SUM(amount) * 1.2", change_type: "UPDATE" },
        },
      }),
      version({ version: 1, change_type: "CREATE", status: "PUBLISHED", published_at: "2026-08-01T00:00:00" }),
    ]);
    await openDetail(reviewed);
    // 变更标签带版本区间 + 字段中文标签 + before/after 值
    expect(await screen.findByText("变更指标 v1→v2")).toBeTruthy();
    expect(screen.getByText("表达式")).toBeTruthy();
    expect(screen.getByText("变更前")).toBeTruthy();
    expect(screen.getByText("变更后")).toBeTruthy();
    expect(screen.getByText("SUM(amount) * 1.1")).toBeTruthy();
    expect(screen.getByText("SUM(amount) * 1.2")).toBeTruthy();
  });

  it("评审记录：破坏性变更（BREAKING）展示破坏性变更标签", async () => {
    const reviewed = reviewedMetric({ version: 2, effective_version: 1 });
    mockedListVersions.mockResolvedValue([
      version({
        version: 2,
        change_type: "BREAKING",
        status: "DRAFT",
        diff_json: { measure_id: { before: 1, after: 2, change_type: "BREAKING" } },
      }),
      version({ version: 1, change_type: "CREATE", status: "PUBLISHED", published_at: "2026-08-01T00:00:00" }),
    ]);
    await openDetail(reviewed);
    // 顶部标签（带 v2 后缀）+ 字段级 Tag 均显示「破坏性变更」
    await waitFor(() => {
      expect(screen.getAllByText(/破坏性变更/).length).toBeGreaterThanOrEqual(2);
    });
  });

  it("评审记录：回看视图已发布变更版本仍按版本记录判定为变更（不误判重评审）", async () => {
    const reviewed = reviewedMetric({ version: 2, effective_version: 1 });
    // approve 后 cur 已转正 PUBLISHED——判定须基于 change_type（UPDATE→变更）；
    // gating 生效：metric.status 已非 REVIEW，不会误判为「废弃恢复重评审」
    mockedListVersions.mockResolvedValue([
      version({
        version: 2,
        change_type: "UPDATE",
        status: "PUBLISHED",
        published_at: "2026-08-02T00:00:00",
        diff_json: { name: { before: "旧名", after: "新名", change_type: "UPDATE" } },
      }),
      version({ version: 1, change_type: "CREATE", status: "PUBLISHED", published_at: "2026-08-01T00:00:00" }),
    ]);
    await openDetail(reviewed);
    expect(await screen.findByText("变更指标 v1→v2")).toBeTruthy();
    expect(screen.queryByText("废弃恢复重评审")).toBeNull();
    expect(screen.getByText("旧名")).toBeTruthy();
    expect(screen.getByText("新名")).toBeTruthy();
  });

  it("待我审确认弹窗：废弃恢复重评审（REVIEW + 当前版本已发布）展示重评审标签", async () => {
    mockedList.mockResolvedValue({
      items: [{ ...metric, status: "REVIEW", version: 2, effective_version: 1 }],
      total: 1,
      page: 1,
      page_size: 20,
    });
    // 废弃后重提不新建版本：当前版本记录仍 PUBLISHED（published_at 非空）
    mockedListVersions.mockResolvedValue([
      version({ version: 2, change_type: "UPDATE", status: "PUBLISHED", published_at: "2026-08-01T00:00:00" }),
    ]);
    renderReview();
    await screen.findByText("sales_gmv_day");
    fireEvent.click(screen.getAllByRole("button", { name: /^通\s*过$/ })[0]);
    // 确认弹窗内变更摘要：废弃恢复重评审 + 说明（Tag 带版本后缀 v2）
    expect(await screen.findByText(/废弃恢复重评审/)).toBeTruthy();
    expect(screen.getByText("该指标此前已发布，本次为废弃后重新提交评审")).toBeTruthy();
  });

  it("评审记录：listVersions 失败静默降级，处理结论与口径展示不受影响", async () => {
    const reviewed = reviewedMetric({
      definition_json: { expression: "SUM(amount)", source_tables: ["demo.sales_order"] },
    });
    mockedListVersions.mockRejectedValue(new Error("network"));
    await openDetail(reviewed);
    // 弹窗正常展示处理结论与口径；变更上下文不渲染、不崩
    expect(screen.getByText("已通过评审")).toBeTruthy();
    expect(screen.getAllByText(/SUM\(amount\)/).length).toBeGreaterThan(0);
    expect(screen.queryByText("变更上下文")).toBeNull();
  });

  it("评审记录：object/array diff 值渲染不崩（JSON pre 与 Tag 列表）", async () => {
    const reviewed = reviewedMetric({ version: 2, effective_version: 1 });
    mockedListVersions.mockResolvedValue([
      version({
        version: 2,
        change_type: "UPDATE",
        status: "DRAFT",
        diff_json: {
          source_tables: { before: ["a"], after: ["a", "b"], change_type: "UPDATE" },
          metric_mount: { before: { table: "t1" }, after: { table: "t2" }, change_type: "UPDATE" },
        },
      }),
      version({ version: 1, change_type: "CREATE", status: "PUBLISHED", published_at: "2026-08-01T00:00:00" }),
    ]);
    await openDetail(reviewed);
    expect(await screen.findByText("变更指标 v1→v2")).toBeTruthy();
    // array → Tag 列表（before/after 各一）；object → JSON pre
    expect(screen.getByText("依赖表（上游）")).toBeTruthy();
    expect(screen.getAllByText("a").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("b")).toBeTruthy();
    expect(screen.getByText(/"t1"/)).toBeTruthy();
    expect(screen.getByText(/"t2"/)).toBeTruthy();
  });

  it("批次筛选：输入批次 ID → listMetrics 收到 batch_id 精确匹配（按\"这一批\"收敛）", async () => {
    renderReview();
    await screen.findByText("sales_gmv_day");
    const input = screen.getByPlaceholderText(/批次 ID/) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "sqlbatch_abc" } });
    // 点击 Input.Search 的搜索按钮触发 onSearch
    const searchBtn = (input.closest(".ant-input-search") as HTMLElement).querySelector(
      ".ant-input-search-button",
    ) as HTMLElement;
    fireEvent.click(searchBtn);
    await waitFor(() => {
      expect(mockedList).toHaveBeenCalledWith(
        expect.objectContaining({ batch_id: "sqlbatch_abc" }),
      );
    });
  });
});
