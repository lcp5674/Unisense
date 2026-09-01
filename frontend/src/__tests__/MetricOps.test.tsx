import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { MetricOps, tablePagination } from "../pages/MetricOps";
import type {
  MetricConsistencyStats,
  MetricLedgerStats,
  MetricReuseStats,
} from "../types";

vi.mock("../api", () => ({
  fetchMetricReuseStats: vi.fn(),
  fetchMetricLedger: vi.fn(),
  fetchConsistencyStats: vi.fn(),
  listDomainTree: vi.fn(),
}));

import {
  fetchMetricReuseStats,
  fetchMetricLedger,
  fetchConsistencyStats,
  listDomainTree,
} from "../api";

// 主题域树（含子域，验证筛选下拉来源）
const DOMAIN_TREE = [
  {
    id: 1,
    code: "sales",
    name: "销售",
    parent_id: null,
    level: 1,
    sort_order: 1,
    status: "active",
    metric_count: 2,
    children: [],
  },
  {
    id: 2,
    code: "outpatient",
    name: "门诊",
    parent_id: null,
    level: 1,
    sort_order: 2,
    status: "active",
    metric_count: 2,
    children: [],
  },
];

const REUSE: MetricReuseStats = {
  total: 5,
  referenced: 3,
  zero_reuse: 2,
  items: [
    { metric_code: "sales_gmv_day", name: "日销售额", domain: "sales", type: "ATOMIC", status: "PUBLISHED", derived_by_count: 6, consumed_by_count: 2, reuse_count: 8 },
    { metric_code: "sales_ordercnt_day", name: "日订单量", domain: "sales", type: "ATOMIC", status: "PUBLISHED", derived_by_count: 1, consumed_by_count: 0, reuse_count: 1 },
    { metric_code: "outp_visit_day", name: "日门诊量", domain: "outpatient", type: "ATOMIC", status: "PUBLISHED", derived_by_count: 0, consumed_by_count: 1, reuse_count: 1 },
    { metric_code: "outp_fee_day", name: "日门诊费用", domain: "outpatient", type: "ATOMIC", status: "DRAFT", derived_by_count: 0, consumed_by_count: 0, reuse_count: 0 },
    { metric_code: "yb_settle_day", name: "日医保结算", domain: "medical_insurance", type: "ATOMIC", status: "PUBLISHED", derived_by_count: 0, consumed_by_count: 0, reuse_count: 0 },
  ],
};

const LEDGER: MetricLedgerStats = {
  total: 5,
  active_count: 3,
  zombie_count: 2,
  duplicate_count: 1,
  zombies: [
    { metric_code: "outp_fee_day", name: "日门诊费用", domain: "outpatient", type: "ATOMIC", status: "DRAFT", last_updated_at: "2026-07-01T00:00:00", days_since_update: 58, derived_by_count: 0, consumed_by_count: 0, reuse_count: 0 },
    { metric_code: "yb_settle_day", name: "日医保结算", domain: "medical_insurance", type: "ATOMIC", status: "PUBLISHED", last_updated_at: "2026-07-02T00:00:00", days_since_update: 57, derived_by_count: 0, consumed_by_count: 0, reuse_count: 0 },
  ],
  duplicates: [
    { metric_code: "sales_gmv_sum", name: "销售总额", domain: "sales", conflict_score: 0.96, existing_code: "sales_gmv_day", reason: "口径定义相同，仅名称不同" },
  ],
};

const CONSISTENCY: MetricConsistencyStats = {
  total_definitions: 5,
  total_conflicts: 2,
  conflicted_metrics: 3,
  consistency_rate_pct: 40,
  cross_department_conflicts: 1,
  avg_resolve_hours: 6.5,
};

function renderPage() {
  return render(
    <MemoryRouter>
      <MetricOps />
    </MemoryRouter>
  );
}

describe("MetricOps 指标运营分析", () => {
  beforeEach(() => {
    vi.mocked(fetchMetricReuseStats).mockResolvedValue(REUSE);
    vi.mocked(fetchMetricLedger).mockResolvedValue(LEDGER);
    vi.mocked(fetchConsistencyStats).mockResolvedValue(CONSISTENCY);
    vi.mocked(listDomainTree).mockResolvedValue(DOMAIN_TREE);
  });

  it("渲染 6 张统计卡（总数/活跃/僵尸/重复/被引用/零复用）", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("指标总数")).toBeTruthy());
    expect(screen.getByText("活跃指标")).toBeTruthy();
    expect(screen.getByText("僵尸指标")).toBeTruthy();
    expect(screen.getByText("重复建设")).toBeTruthy();
    expect(screen.getByText("被引用指标")).toBeTruthy();
    expect(screen.getByText("零复用指标")).toBeTruthy();
    const totalCard = screen.getByText("指标总数").closest(".ant-card") as HTMLElement;
    expect(within(totalCard).getByText("5")).toBeTruthy();
    const zombieCard = screen.getByText("僵尸指标").closest(".ant-card") as HTMLElement;
    expect(within(zombieCard).getByText("2")).toBeTruthy();
  });

  it("口径一致率正确读取后端字段（consistency_rate_pct 等）并展示统计口径说明", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("口径一致率")).toBeTruthy());
    // 环形一致率 40%
    expect(screen.getByText("40%")).toBeTruthy();
    // 后端返回的真实字段（此前前端误读 total/rate 永远显示 0）
    expect(screen.getByText("参与统计指标")).toBeTruthy();
    expect(screen.getByText("卷入冲突指标")).toBeTruthy();
    expect(screen.getByText("冲突记录数")).toBeTruthy();
    expect(screen.getByText("部门间冲突")).toBeTruthy();
    expect(screen.getByText("平均解决时长")).toBeTruthy();
    // 统计口径 Tooltip 图标存在
    expect(document.querySelector(".anticon-info-circle")).toBeTruthy();
    // 部门间冲突 > 0 显示治理提醒
    await waitFor(() => expect(screen.getByText(/部门间口径冲突/)).toBeTruthy());
  });

  it("表格分页支持跳页码与切换条数（showQuickJumper / showSizeChanger）", async () => {
    // 25 条数据制造多页，验证 quickJumper 渲染（单页时 antd 会隐藏跳页输入框）
    const many: MetricReuseStats = {
      ...REUSE,
      total: 25,
      items: Array.from({ length: 25 }, (_, i) => ({
        metric_code: `m_${String(i + 1).padStart(3, "0")}_day`,
        name: `指标 ${i + 1}`,
        domain: "outpatient",
        type: "ATOMIC",
        status: "PUBLISHED",
        derived_by_count: i % 3,
        consumed_by_count: i % 2,
        reuse_count: i % 4,
      })),
    };
    vi.mocked(fetchMetricReuseStats).mockResolvedValue(many);
    const { container } = renderPage();
    await waitFor(() => expect(screen.getByText("复用度分析（25）")).toBeTruthy());
    await waitFor(() => {
      const quickJumper = container.querySelector(".ant-pagination-options-quick-jumper");
      const sizeChanger = container.querySelector(".ant-pagination-options-size-changer");
      expect(quickJumper).toBeTruthy();
      expect(sizeChanger).toBeTruthy();
    });
  });

  it("分页配置非受控（defaultPageSize 而非受控 pageSize，保证切换条数不重置）", () => {
    const p = tablePagination(25) as Record<string, unknown>;
    expect(p.defaultPageSize).toBe(10);
    expect("pageSize" in p).toBe(false); // 受控 pageSize 会导致切换条数被重置（本次修复点）
    expect(p.showSizeChanger).toBe(true);
    expect(p.showQuickJumper).toBe(true);
    expect(p.showTotal).toBeTypeOf("function");
    expect(p.total).toBe(25);
  });

  it("复用度分析展示分桶分布（零复用/低/中/高）", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("零复用")).toBeTruthy());
    expect(screen.getByText("低复用（1-2）")).toBeTruthy();
    expect(screen.getByText("中复用（3-5）")).toBeTruthy();
    expect(screen.getByText("高复用（>5）")).toBeTruthy();
    // 零复用 2 个（outp_fee_day / yb_settle_day）
    const zeroStat = screen.getByText("零复用").closest(".ant-card") as HTMLElement;
    expect(within(zeroStat).getByText("2")).toBeTruthy();
  });

  it("表格行渲染指标链接与零复用红色 Tag", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("sales_gmv_day")).toBeTruthy());
    expect(screen.getAllByText(/零复用/).length).toBeGreaterThan(0);
  });

  it("按业务域筛选：选择域后三个端点带筛选参数重拉，并展示已筛选 Tag", async () => {
    const { container } = renderPage();
    await waitFor(() => expect(screen.getByText("指标总数")).toBeTruthy());
    // 打开业务域下拉并选择「销售」（精确定位下拉选项，避免命中表格同名字段）
    const selector = container.querySelector(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(selector);
    const opt = screen.getAllByText("销售").find((e) => e.className.includes("ant-select-item-option-content"));
    fireEvent.click(opt as HTMLElement);
    await waitFor(() => expect(screen.getByText("已筛选")).toBeTruthy());
    expect(vi.mocked(fetchMetricReuseStats)).toHaveBeenLastCalledWith({ domain: "sales" });
    expect(vi.mocked(fetchMetricLedger)).toHaveBeenLastCalledWith({ domain: "sales" });
    expect(vi.mocked(fetchConsistencyStats)).toHaveBeenLastCalledWith({ domain: "sales" });
    // 口径一致率卡片标注「筛选范围」
    expect(screen.getByText("筛选范围")).toBeTruthy();
  });

  it("重置筛选：清空三个端点筛选参数并移除已筛选 Tag", async () => {
    const { container } = renderPage();
    await waitFor(() => expect(screen.getByText("指标总数")).toBeTruthy());
    // 先选择类型「原子指标」（表格类型列也显示该中文，需精确取下拉选项）
    const selector = container.querySelectorAll(".ant-select-selector")[1] as HTMLElement;
    fireEvent.mouseDown(selector);
    const opt = screen.getAllByText("原子指标").find((e) => e.className.includes("ant-select-item-option-content"));
    fireEvent.click(opt as HTMLElement);
    await waitFor(() => expect(screen.getByText("已筛选")).toBeTruthy());
    expect(vi.mocked(fetchMetricReuseStats)).toHaveBeenLastCalledWith({ type: "atomic" });
    // 点重置（antd 两字按钮自动加空格「重 置」）
    fireEvent.click(screen.getByText(/重\s*置/));
    await waitFor(() => expect(screen.queryByText("已筛选")).toBeNull());
    expect(vi.mocked(fetchMetricReuseStats)).toHaveBeenLastCalledWith({});
  });
});
