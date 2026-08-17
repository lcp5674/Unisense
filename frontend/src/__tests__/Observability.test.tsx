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
  system: {
    dependencies: {
      by_status: { HEALTHY: 1, DEGRADED: 2 },
      circuit_open: 1,
      total: 3,
      items: [
        {
          dependency_type: "LLM",
          dependency_id: "llm",
          status: "DEGRADED",
          circuit_state: "HALF_OPEN",
          consecutive_failures: 5,
          latency_p95_ms: null,
          error_rate_pct: 0,
          last_check_at: "2026-08-14T07:05:13",
        },
        {
          dependency_type: "OLAP",
          dependency_id: "olap",
          status: "HEALTHY",
          circuit_state: "CLOSED",
          consecutive_failures: 0,
          latency_p95_ms: null,
          error_rate_pct: 0,
          last_check_at: "2026-08-13T10:58:50",
        },
        {
          dependency_type: "GRAPH",
          dependency_id: "graph",
          status: "DEGRADED",
          circuit_state: "OPEN",
          consecutive_failures: 5,
          latency_p95_ms: null,
          error_rate_pct: 0,
          last_check_at: "2026-08-17T06:58:52",
        },
      ],
    },
    collection: {
      by_status: { COMPLETED: 6, RUNNING: 2 },
      total: 8,
      running: 2,
      failed: 0,
      success_rate_pct: 100.0,
      last_collected_at: "2026-08-17T07:20:13",
    },
  },
  quality: {
    metric_health: {
      by_level: { GOOD: 1, WARNING: 8 },
      total_scored: 9,
      coverage_pct: 81.8,
      avg_score: 66,
      top_risk: [
        { metric_id: 2, metric_name: "低健康GMV日", metric_code: "low_health_gmv_day", score: 55, level: "WARNING", missing_dimensions: ["sla", "lineage_coverage"] },
      ],
    },
    lineage: { edges: 58, stale: 0, ingest_success: 58, last_ingest_at: "2026-08-17T14:31:42" },
  },
  risks: { pii_review_pending: 305, grants_expiring_soon: 0, schema_drift_7d: 3 },
  trends: {
    days: 7,
    metrics_created: [
      { date: "2026-08-11", count: 0 },
      { date: "2026-08-12", count: 0 },
      { date: "2026-08-13", count: 1 },
      { date: "2026-08-14", count: 2 },
      { date: "2026-08-15", count: 0 },
      { date: "2026-08-16", count: 0 },
      { date: "2026-08-17", count: 1 },
    ],
    collections: [
      { date: "2026-08-11", count: 0 },
      { date: "2026-08-12", count: 0 },
      { date: "2026-08-13", count: 0 },
      { date: "2026-08-14", count: 2 },
      { date: "2026-08-15", count: 0 },
      { date: "2026-08-16", count: 0 },
      { date: "2026-08-17", count: 6 },
    ],
  },
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
  it("平台概览支持手动刷新与切回 Tab 自动刷新（时效性）", async () => {
    render(<Observability />);
    await waitFor(() => expect(screen.getByText("数据源健康")).toBeInTheDocument());
    const callsAfterMount = mockedOverview.mock.calls.length;
    expect(callsAfterMount).toBeGreaterThanOrEqual(1);

    // 手动刷新按钮触发重新拉取
    fireEvent.click(screen.getByRole("button", { name: /刷\s*新/ }));
    await waitFor(() => expect(mockedOverview.mock.calls.length).toBeGreaterThan(callsAfterMount));

    // 切到运行指标再切回平台概览 → 自动静默刷新
    const afterManual = mockedOverview.mock.calls.length;
    fireEvent.click(screen.getByText("运行指标"));
    await waitFor(() => expect(screen.getByText("API 动作分布")).toBeInTheDocument());
    fireEvent.click(screen.getByText("平台概览"));
    await waitFor(() => expect(mockedOverview.mock.calls.length).toBeGreaterThan(afterManual));
  });

  it("默认展示平台概览 Tab：系统健康/风险雷达/资产规模，全部业务标签", async () => {
    render(<Observability />);
    await waitFor(() => expect(screen.getAllByText("核心依赖健康").length).toBeGreaterThan(0));

    // 顶部状态条
    expect(screen.getAllByText("熔断开启").length).toBeGreaterThan(0);
    expect(screen.getByText("采集运行中")).toBeInTheDocument();
    expect(screen.getByText("数据新鲜度")).toBeInTheDocument();
    // 降级依赖提示（含熔断开启的 GRAPH）
    expect(screen.getByText(/降级\/不可用 2 个/)).toBeInTheDocument();

    // 核心依赖健康卡：类型/状态/熔断 中文
    expect(screen.getByText("AI 模型")).toBeInTheDocument();
    expect(screen.getByText("图数据库")).toBeInTheDocument();
    expect(screen.getAllByText("降级").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("半开恢复")).toBeInTheDocument();

    // 采集链路健康
    expect(screen.getByText("采集链路健康")).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(screen.getByText("运行中")).toBeInTheDocument();
    expect(screen.getByText("采集成功率")).toBeInTheDocument();

    // 风险雷达（治理积压并入）
    expect(screen.getByText("治理风险雷达")).toBeInTheDocument();
    expect(screen.getByText("待处理冲突")).toBeInTheDocument();
    expect(screen.getByText("未关闭质量事件")).toBeInTheDocument();
    expect(screen.getByText("待审核指标")).toBeInTheDocument();
    expect(screen.getByText("未闭环升级")).toBeInTheDocument();
    expect(screen.getByText("PII 待复核")).toBeInTheDocument();

    // 资产规模：指标生命周期状态转中文 + 数据源健康子块
    expect(screen.getByText("资产规模")).toBeInTheDocument();
    expect(screen.getByText("已发布")).toBeInTheDocument();
    expect(screen.getByText("草稿")).toBeInTheDocument();
    expect(screen.getByText("术语")).toBeInTheDocument();
    expect(screen.getByText("主题域")).toBeInTheDocument();
    expect(screen.getByText("数据源健康")).toBeInTheDocument();
    expect(screen.getByText("不健康")).toBeInTheDocument();
    // 消费接入
    expect(screen.getByText("接入方总数")).toBeInTheDocument();
    expect(screen.getByText("活跃接入方")).toBeInTheDocument();

    // 技术值不应直出
    expect(screen.queryByText("healthy")).not.toBeInTheDocument();
    expect(screen.queryByText("PUBLISHED")).not.toBeInTheDocument();
    expect(screen.queryByText("DRAFT")).not.toBeInTheDocument();
    expect(screen.queryByText("HEALTHY")).not.toBeInTheDocument();
    expect(screen.queryByText("WARNING")).not.toBeInTheDocument();
    expect(screen.queryByText("RUNNING")).not.toBeInTheDocument();
  });

  it("平台概览企业级：指标健康度/血缘健康/趋势 全业务标签展示", async () => {
    render(<Observability />);
    await waitFor(() => expect(screen.getAllByText("核心依赖健康").length).toBeGreaterThan(0));

    // 指标健康度卡
    expect(screen.getByText("指标健康度")).toBeInTheDocument();
    expect(screen.getByText("警告")).toBeInTheDocument();
    expect(screen.getByText("良好")).toBeInTheDocument();
    expect(screen.getByText("健康覆盖率")).toBeInTheDocument();
    expect(screen.getByText(/低健康指标 Top 1/)).toBeInTheDocument();
    // 低健康指标展示指标名（编码）+ 分数 + 缺失维度，而非裸 ID
    expect(screen.getByText("低健康GMV日")).toBeInTheDocument();
    expect(screen.getByText("low_health_gmv_day")).toBeInTheDocument();
    expect(screen.getByText(/55 分/)).toBeInTheDocument();
    expect(screen.getByText(/sla、lineage_coverage/)).toBeInTheDocument();

    // 血缘健康卡
    expect(screen.getByText("血缘健康")).toBeInTheDocument();
    expect(screen.getByText("失效边")).toBeInTheDocument();
    expect(screen.getByText("接入成功")).toBeInTheDocument();
    expect(screen.getByText("最近接入")).toBeInTheDocument();

    // 风险雷达扩展项
    expect(screen.getByText("授权即将到期")).toBeInTheDocument();
    expect(screen.getByText("7 天 Schema 漂移")).toBeInTheDocument();

    // 近 7 天趋势
    expect(screen.getByText("近 7 天指标新增")).toBeInTheDocument();
    expect(screen.getByText("近 7 天采集运行")).toBeInTheDocument();
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
        {
          id: 11,
          level: "P0",
          status: "OPEN",
          rule_type: "ACCURACY",
          obs_value: 85.2,
          threshold: 99.0,
          metric_id: 5,
          metric_name: "销售GMV",
          metric_code: "sales_gmv",
          metric_domain: "交易域",
          ack_by: null,
          ack_at: null,
          resolved_by: null,
          resolved_at: null,
          closed_by: null,
          closed_at: null,
          repair_suggestion: null,
          created_at: "2026-08-12T03:00:00",
        },
        {
          id: 12,
          level: "P2",
          status: "CLOSED",
          rule_type: "TIMELINESS",
          obs_value: null,
          threshold: null,
          metric_id: 6,
          metric_name: null,
          metric_code: null,
          ack_by: 3,
          ack_by_name: "李仲裁",
          ack_at: "2026-08-11T02:30:00",
          resolved_by: 3,
          resolved_by_name: "李仲裁",
          resolved_at: "2026-08-11T03:00:00",
          closed_by: 3,
          closed_by_name: "李仲裁",
          closed_at: "2026-08-11T04:00:00",
          repair_suggestion: null,
          created_at: "2026-08-11T02:00:00",
        },
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
    // 资产归属域
    expect(screen.getByText("交易域")).toBeInTheDocument();
    // 影响风险：严重级影响说明（P0 紧急 → 核心指标异常）
    expect(screen.getByText(/核心指标异常/)).toBeInTheDocument();
    // 观测值/阈值展示
    expect(screen.getByText("85.2 / 99")).toBeInTheDocument();
    // 处理留痕：谁在何时 ACK/RESOLVE/CLOSE（六要素之"谁/何时"）
    expect(screen.getAllByText("李仲裁").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText(/确认：/)).toBeInTheDocument();
    expect(screen.getByText(/解决：/)).toBeInTheDocument();
    expect(screen.getByText(/关闭：/)).toBeInTheDocument();
    // 原始技术值不应直出
    expect(screen.queryByText("OPEN")).not.toBeInTheDocument();
    expect(screen.queryByText("CLOSED")).not.toBeInTheDocument();
    expect(screen.queryByText("ACCURACY")).not.toBeInTheDocument();
  });

  it("质量事件含修复建议时展示解决建议（责任方/处置动作/诊断 SQL）", async () => {
    mockedQualityEvents.mockResolvedValue({
      items: [
        {
          id: 21,
          level: "P1",
          status: "OPEN",
          rule_type: "COMPLETENESS",
          obs_value: 50.0,
          threshold: 90.0,
          metric_id: 1,
          metric_name: "E2E销售额",
          metric_code: "sales_e2e_gmv_day",
          metric_domain: "交易域",
          ack_by: null,
          ack_at: null,
          resolved_by: null,
          resolved_at: null,
          closed_by: null,
          closed_at: null,
          repair_suggestion: {
            pattern: "static_threshold_breach",
            suggested_action: "核查上游采集/ETL 是否成功产出，补跑缺失分区并校验行数",
            owner_hint: "指标 Owner 或上游采集任务责任人",
            suggested_sql: "SELECT COUNT(*) AS cnt FROM {src} WHERE dt = :dt;",
            upstream_task: "collector_job:metric:1",
          },
          created_at: "2026-08-14T06:19:00",
        },
      ],
      total: 1,
    } as never);
    render(<Observability />);
    await waitFor(() => expect(screen.getByText("平台概览")).toBeInTheDocument());
    fireEvent.click(screen.getByText("运行指标"));

    await waitFor(() => expect(screen.getByText("最近质量事件")).toBeInTheDocument());
    // 异常模式中文 + 解决建议
    expect(screen.getByText("静态阈值越界")).toBeInTheDocument();
    expect(screen.getByText("解决建议")).toBeInTheDocument();
    expect(screen.getByText(/核查上游采集/)).toBeInTheDocument();
    expect(screen.getByText(/指标 Owner 或上游采集任务责任人/)).toBeInTheDocument();
    expect(screen.getByText(/SELECT COUNT\(\*\) AS cnt/)).toBeInTheDocument();
    // 原始 pattern/action 英文不应直出
    expect(screen.queryByText("static_threshold_breach")).not.toBeInTheDocument();
  });
});
