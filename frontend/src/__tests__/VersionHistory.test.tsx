import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { VersionHistory } from "../pages/metric/VersionHistory";
import type { MetricVersionResponse } from "../types";

vi.mock("../api", () => ({
  confirmMetricVersion: vi.fn(),
  extendMetricVersion: vi.fn(),
  rejectMetricVersion: vi.fn(),
}));

function makeVersion(overrides: Partial<MetricVersionResponse> = {}): MetricVersionResponse {
  return {
    id: 1,
    version: 2,
    metric_id: 1,
    change_type: "UPDATE",
    change_reason: "破坏性口径变更，待消费方确认",
    definition_json: { expression: "sum(amount) * 2" },
    diff_json: null,
    status: "PENDING_CONFIRMATION",
    created_by: 1,
    published_at: null,
    created_at: "2026-08-01T00:00:00",
    ...overrides,
  };
}

function renderHistory(
  canConfirm: boolean | undefined,
  status = "PENDING_CONFIRMATION",
  overrides: Partial<MetricVersionResponse> = {},
) {
  return render(
    <VersionHistory
      metricCode="sales_gmv_sum_d"
      versions={[makeVersion({ status, ...overrides }) as MetricVersionResponse]}
      effectiveVersion={1}
      onChanged={() => {}}
      canConfirm={canConfirm}
    />,
  );
}

describe("VersionHistory 确认/拒绝权限门禁", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("写角色（canConfirm=true）：PENDING 版本显示确认/拒绝/延期按钮", async () => {
    renderHistory(true);
    // antd Button 会在两汉字间插入空格（"确 认"），用空格容忍正则
    expect(await screen.findByRole("button", { name: /确\s*认/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /拒\s*绝/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /延\s*期/ })).toBeTruthy();
  });

  it("非写角色（canConfirm=false）：PENDING 版本不显示确认/拒绝按钮（防 viewer 点击后 403）", async () => {
    renderHistory(false);
    await screen.findByText("v2");
    expect(screen.queryByRole("button", { name: /确认/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /拒绝/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /延期/ })).toBeNull();
  });

  it("未传 canConfirm（默认 undefined，fail-closed）：不显示确认按钮", async () => {
    renderHistory(undefined);
    await screen.findByText("v2");
    expect(screen.queryByRole("button", { name: /确认/ })).toBeNull();
  });

  it("非 PENDING 版本：即使写角色也不显示确认/拒绝按钮", async () => {
    renderHistory(true, "PUBLISHED");
    await screen.findByText("v2");
    expect(screen.queryByRole("button", { name: /确认/ })).toBeNull();
  });

  it("PENDING 版本展示确认截止时间（超时自动接受语义）", async () => {
    renderHistory(true, "PENDING_CONFIRMATION", {
      pending_deadline: "2026-08-15T00:00:00",
    });
    // fixture 带 pending_deadline → 状态列下方展示截止提示（formatCnTime 输出中文格式）
    expect(await screen.findByText(/超时自动接受/)).toBeTruthy();
    expect(screen.getByText(/将于.*8月15日/)).toBeTruthy();
  });

  it("PENDING 版本无 pending_deadline（旧数据）：不显示截止提示", async () => {
    renderHistory(true);
    await screen.findByText("v2");
    // 版本无 deadline → 无超时提示
    expect(screen.queryByText(/超时自动接受/)).toBeNull();
  });
});
