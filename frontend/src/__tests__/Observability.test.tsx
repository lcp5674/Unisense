import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { Observability } from "../pages/Observability";

// Mock API（可观测端点，含质量事件明细）
vi.mock("../api", () => ({
  fetchObsMetricsQuality: vi.fn(),
  fetchObsMetricsApi: vi.fn(),
  fetchObsMetricsNotifications: vi.fn(),
  fetchObsMetricsLineage: vi.fn(),
  fetchObsOverview: vi.fn(),
  fetchObsQualityEvents: vi.fn(),
}));

import {
  fetchObsMetricsQuality,
  fetchObsMetricsApi,
  fetchObsMetricsNotifications,
  fetchObsMetricsLineage,
  fetchObsOverview,
  fetchObsQualityEvents,
} from "../api";

const mockedQuality = vi.mocked(fetchObsMetricsQuality);
const mockedApi = vi.mocked(fetchObsMetricsApi);
const mockedNotif = vi.mocked(fetchObsMetricsNotifications);
const mockedLineage = vi.mocked(fetchObsMetricsLineage);
const mockedOverview = vi.mocked(fetchObsOverview);
const mockedQualityEvents = vi.mocked(fetchObsQualityEvents);

const overview = {
  sources: { by_health: { healthy: 2, unhealthy: 1 }, total: 3 },
  backlog: {
    open_conflicts: 1,
    pending_quality_events: 2,
    review_metrics: 3,
    open_escalations: 0,
  },
  assets: {
    metrics_by_status: { PUBLISHED: 5, DRAFT: 2 },
    terms: 4,
    dimensions: 3,
    domains: 2,
    sources: 3,
  },
  clients: { total: 1, active: 1 },
};

beforeEach(() => {
  vi.clearAllMocks();
  mockedOverview.mockResolvedValue(overview as never);
  mockedQuality.mockResolvedValue({
    by_level: { ERROR: 1, WARN: 2 },
    by_status: { OPEN: 2 },
    total: 3,
  } as never);
  mockedApi.mockResolvedValue({ "metric.created": 5, "metric.approved": 3 } as never);
  mockedNotif.mockResolvedValue({
    by_status: { SENT: 2, FAILED: 1 },
    event_total: 10,
    event_notified: 3,
  } as never);
  mockedLineage.mockResolvedValue({ edges: 7 } as never);
  mockedQualityEvents.mockResolvedValue({ items: [], total: 0 } as never);
});

describe("Observability 可观测中心", () => {
  it("默认展示平台概览 Tab：数据源健康/治理积压/资产规模/消费接入，全部业务标签", async () => {
    render(<Observability />);
    await waitFor(() => expect(screen.getByText("数据源健康")).toBeInTheDocument());

    // 数据源健康：技术值 healthy/unhealthy 转中文
    expect(screen.getByText("健康")).toBeInTheDocument();
    expect(screen.getByText("不健康")).toBeInTheDocument();
    expect(screen.getByText("数据源总数")).toBeInTheDocument();

    // 治理积压
    expect(screen.getByText("治理积压")).toBeInTheDocument();
    expect(screen.getByText("待处理冲突")).toBeInTheDocument();
    expect(screen.getByText("未关闭质量事件")).toBeInTheDocument();
    expect(screen.getByText("待审核指标")).toBeInTheDocument();
    expect(screen.getByText("未闭环升级")).toBeInTheDocument();

    // 资产规模：指标生命周期状态转中文
    expect(screen.getByText("资产规模")).toBeInTheDocument();
    expect(screen.getByText("已发布")).toBeInTheDocument();
    expect(screen.getByText("草稿")).toBeInTheDocument();
    expect(screen.getByText("术语")).toBeInTheDocument();
    expect(screen.getByText("主题域")).toBeInTheDocument();

    // 消费接入
    expect(screen.getByText("消费接入")).toBeInTheDocument();
    expect(screen.getByText("接入方总数")).toBeInTheDocument();
    expect(screen.getByText("活跃接入方")).toBeInTheDocument();

    // 技术值不应直出
    expect(screen.queryByText("healthy")).not.toBeInTheDocument();
    expect(screen.queryByText("PUBLISHED")).not.toBeInTheDocument();
    expect(screen.queryByText("DRAFT")).not.toBeInTheDocument();
  });

  it("运行指标 Tab 的 API 动作分布用中文标签而非技术 action", async () => {
    render(<Observability />);
    await waitFor(() => expect(screen.getByText("平台概览")).toBeInTheDocument());
    fireEvent.click(screen.getByText("运行指标"));

    await waitFor(() => expect(screen.getByText("API 动作分布")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("创建指标")).toBeInTheDocument());
    expect(screen.getByText("审核通过")).toBeInTheDocument();
    // 技术 action 不应直出
    expect(screen.queryByText("metric.created")).not.toBeInTheDocument();
  });

  it("质量事件级别分布：P0/P1/P2 显示中文标签（对齐后端 QualitySeverity）而非原始级别", async () => {
    mockedQuality.mockResolvedValue({
      by_level: { P0: 2, P1: 3, P2: 1 },
      by_status: { OPEN: 2 },
      total: 6,
    } as never);
    render(<Observability />);
    await waitFor(() => expect(screen.getByText("平台概览")).toBeInTheDocument());
    fireEvent.click(screen.getByText("运行指标"));

    await waitFor(() => expect(screen.getByText("质量事件级别分布")).toBeInTheDocument());
    expect(screen.getByText("P0 紧急")).toBeInTheDocument();
    expect(screen.getByText("P1 严重")).toBeInTheDocument();
    expect(screen.getByText("P2 一般")).toBeInTheDocument();
    // 原始级别不应直出（ERROR/WARN/INFO 是消息重要度，非质量事件严重级）
    expect(screen.queryByText("ERROR")).not.toBeInTheDocument();
  });

  it("运行指标 Tab 展示最近质量事件明细：级别/规则/状态中文 + 指标名 + 观测值/阈值", async () => {
    mockedQualityEvents.mockResolvedValue({
      items: [
        { id: 11, level: "P0", status: "OPEN", rule_type: "ACCURACY", obs_value: 85.2, threshold: 99.0, metric_id: 5, metric_name: "销售GMV", metric_code: "sales_gmv", created_at: "2026-08-12T03:00:00" },
        { id: 12, level: "P2", status: "CLOSED", rule_type: "TIMELINESS", obs_value: null, threshold: null, metric_id: 6, metric_name: null, metric_code: null, created_at: "2026-08-11T02:00:00" },
      ],
      total: 2,
    } as never);
    render(<Observability />);
    await waitFor(() => expect(screen.getByText("平台概览")).toBeInTheDocument());
    fireEvent.click(screen.getByText("运行指标"));

    await waitFor(() => expect(screen.getByText("最近质量事件")).toBeInTheDocument());
    // 严重级别中文
    expect(screen.getByText("P0 紧急")).toBeInTheDocument();
    expect(screen.getByText("P2 一般")).toBeInTheDocument();
    // 规则类型中文
    expect(screen.getByText("准确性")).toBeInTheDocument();
    expect(screen.getByText("及时性")).toBeInTheDocument();
    // 状态中文
    expect(screen.getByText("待处理")).toBeInTheDocument();
    expect(screen.getByText("已关闭")).toBeInTheDocument();
    // 指标名（有名称显示名称，无名称回退 ID）
    expect(screen.getByText("销售GMV")).toBeInTheDocument();
    expect(screen.getByText("指标 #6")).toBeInTheDocument();
    // 观测值/阈值展示
    expect(screen.getByText("85.2 / 99")).toBeInTheDocument();
    // 原始技术值不应直出
    expect(screen.queryByText("OPEN")).not.toBeInTheDocument();
    expect(screen.queryByText("CLOSED")).not.toBeInTheDocument();
    expect(screen.queryByText("ACCURACY")).not.toBeInTheDocument();
  });
});
