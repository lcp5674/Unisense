import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { AuditLog } from "../pages/AuditLog";
import { PermissionProvider } from "../hooks/usePermission";
import type { AuditEntry } from "../types";

vi.mock("../api", () => ({
  listAudit: vi.fn(),
  exportAudit: vi.fn(),
  UnisenseApiError: class extends Error {
    code = "";
    codeZh = "";
    status = 0;
    traceId = "";
  },
}));

import { listAudit, exportAudit } from "../api";

const mockedListAudit = vi.mocked(listAudit);
const mockedExportAudit = vi.mocked(exportAudit);

const ENTRY: AuditEntry = {
  id: 1,
  actor_id: 7,
  actor_display: "张伟",
  action: "data_source.create",
  entity_type: "data_source",
  entity_id: "batch:5",
  detail_json: { source_type: "mysql", name: "财务库" },
  ip: "10.0.0.1",
  trace_id: "trace-abc123",
  pii_access: false,
  archived: false,
  created_at: "2026-08-17T01:31:20",
  action_desc: "创建了数据源（名称=财务库）",
};

function renderPage() {
  return render(
    <PermissionProvider user={{ id: 1, username: "admin", display_name: "管理员", role: "platform_admin", domain: null, org_id: 1 } as never}>
      <AuditLog />
    </PermissionProvider>,
  );
}

describe("AuditLog 表格页", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedListAudit.mockResolvedValue({ items: [ENTRY], total: 1, page: 1, page_size: 20 });
    mockedExportAudit.mockResolvedValue();
  });

  it("优先展示后端 enrich 的中文描述，而非裸露英文 action", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("创建了数据源（名称=财务库）")).toBeTruthy();
    });
    expect(screen.queryByText("data_source.create")).toBeNull();
  });

  it("操作人按姓名模糊搜索（传 actor_keyword 而非数字 ID）", async () => {
    renderPage();
    await waitFor(() => {
      expect(mockedListAudit).toHaveBeenCalled();
    });
    const input = screen.getByPlaceholderText("按操作人姓名搜索");
    fireEvent.change(input, { target: { value: "张伟" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      const calls = mockedListAudit.mock.calls;
      const lastCall = calls[calls.length - 1]?.[0];
      expect(lastCall?.actor_keyword).toBe("张伟");
    });
  });

  it("支持按追踪编号搜索（trace_id 跨服务追踪入口）", async () => {
    renderPage();
    await waitFor(() => {
      expect(mockedListAudit).toHaveBeenCalled();
    });
    const input = screen.getByPlaceholderText("按追踪编号搜索");
    fireEvent.change(input, { target: { value: "trace-abc123" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      const calls = mockedListAudit.mock.calls;
      const lastCall = calls[calls.length - 1]?.[0];
      expect(lastCall?.trace_id).toBe("trace-abc123");
    });
  });

  it("entity_id 技术前缀剥离展示（batch:5 → 5）", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("5")).toBeTruthy();
    });
    expect(screen.queryByText("batch:5")).toBeNull();
  });

  it("实体类型筛选器含后端实际复数类型 grants（修复 grant 永远匹配不到的 bug）", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("全部操作对象类型")).toBeTruthy();
    });
    // 全量实体类型常量（auditI18n.AUDIT_ENTITY_TYPES）覆盖后端全部 entity_type
    const { AUDIT_ENTITY_TYPES } = await import("../utils/auditI18n");
    expect(AUDIT_ENTITY_TYPES).toContain("grants");
    expect(AUDIT_ENTITY_TYPES).toContain("grant");
    expect(AUDIT_ENTITY_TYPES).toContain("sensitive_rule");
    expect(AUDIT_ENTITY_TYPES).toContain("api_client");
    expect(AUDIT_ENTITY_TYPES.length).toBeGreaterThanOrEqual(40);
  });

  it("分页文案不含技术话术（后端返回近似值）", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("共 1 条")).toBeTruthy();
    });
    expect(screen.queryByText(/后端返回近似值/)).toBeNull();
  });

  it("导出按钮走 exportAudit 并携带当前筛选", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("导出 CSV")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("导出 CSV"));
    await waitFor(() => {
      expect(mockedExportAudit).toHaveBeenCalledWith(
        expect.objectContaining({ format: "csv", limit: 5000 }),
      );
    });
  });
});
