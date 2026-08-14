import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { Observability } from "../pages/Observability";

// Mock API（5 个可观测端点）
vi.mock("../api", () => ({
  fetchObsMetricsQuality: vi.fn(),
  fetchObsMetricsApi: vi.fn(),
  fetchObsMetricsNotifications: vi.fn(),
  fetchObsMetricsLineage: vi.fn(),
  fetchObsOverview: vi.fn(),
}));

import {
  fetchObsMetricsQuality,
  fetchObsMetricsApi,
  fetchObsMetricsNotifications,
  fetchObsMetricsLineage,
  fetchObsOverview,
} from "../api";

const mockedQuality = vi.mocked(fetchObsMetricsQuality);
const mockedApi = vi.mocked(fetchObsMetricsApi);
const mockedNotif = vi.mocked(fetchObsMetricsNotifications);
const mockedLineage = vi.mocked(fetchObsMetricsLineage);
const mockedOverview = vi.mocked(fetchObsOverview);

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
});
