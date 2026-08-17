import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { AuditTimeline } from "../pages/metric/AuditTimeline";
import type { AuditEntry } from "../types";

vi.mock("../api", () => ({
  listAudit: vi.fn(),
}));

import { listAudit } from "../api";

const mockedListAudit = vi.mocked(listAudit);

const ENTRY: AuditEntry = {
  id: 1,
  actor_id: 7,
  actor_display: "张伟",
  action: "COLLECT",
  entity_type: "data_source",
  entity_id: "mysql_finance",
  detail_json: { scanned: 270, registered: 260, failed_count: 0 },
  ip: "10.0.0.1",
  trace_id: "trace-abc123",
  pii_access: false,
  archived: false,
  created_at: "2026-08-17T01:31:20",
  action_desc: "采集了数据源元数据（扫描数=270；注册数=260；失败数=0）",
};

describe("AuditTimeline", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedListAudit.mockResolvedValue({ items: [ENTRY], total: 1, page: 1, page_size: 30 });
  });

  it("按 entity_type + entity_id 查询审计并渲染业务中文描述（不暴露英文 action）", async () => {
    render(<AuditTimeline entityType="data_source" entityId="mysql_finance" />);
    await waitFor(() => {
      expect(mockedListAudit).toHaveBeenCalledWith({
        entity_type: "data_source",
        entity_id: "mysql_finance",
        page_size: 30,
      });
    });
    // 后端 enrich 的中文描述作为标题，而非原始英文 action
    expect(screen.getByText("采集了数据源元数据（扫描数=270；注册数=260；失败数=0）")).toBeTruthy();
    // 业务分类 Tag（采集）替代英文 COLLECT
    expect(screen.getByText("采集")).toBeTruthy();
    expect(screen.queryByText("COLLECT")).toBeNull();
  });

  it("展示操作人、实体标识与相对/绝对时间，不出现英文 trace 技术串", async () => {
    render(<AuditTimeline entityType="data_source" entityId="mysql_finance" />);
    await waitFor(() => {
      expect(screen.getByText(/张伟/)).toBeTruthy();
    });
    expect(screen.getByText("数据源")).toBeTruthy();
    expect(screen.getByText("mysql_finance")).toBeTruthy();
    expect(screen.queryByText(/trace/)).toBeNull();
  });

  it("无 action_desc 时回退 auditI18n 中文动作，且时间线节点可点开查看结构化详情", async () => {
    mockedListAudit.mockResolvedValue({
      items: [{ ...ENTRY, id: 2, action: "TEST_CONNECTION", action_desc: undefined, detail_json: { ok: true, latency_ms: 12 } }],
      total: 1,
      page: 1,
      page_size: 30,
    });
    render(<AuditTimeline entityType="data_source" entityId="mysql_finance" />);
    await waitFor(() => {
      // 回退 auditActionLabel("TEST_CONNECTION") = 测试连接
      expect(screen.getByText("测试连接")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("查看详情"));
    await waitFor(() => {
      expect(screen.getByText("是否成功:")).toBeTruthy();
      expect(screen.getByText("是")).toBeTruthy();
    });
  });

  it("空记录展示业务空态文案", async () => {
    mockedListAudit.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 30 });
    render(<AuditTimeline entityType="data_source" entityId="mysql_finance" emptyText="暂无该数据源的操作记录" />);
    await waitFor(() => {
      expect(screen.getByText("暂无该数据源的操作记录")).toBeTruthy();
    });
  });
});
