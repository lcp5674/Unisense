import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { AuditLog } from "../pages/AuditLog";
import { MemoryRouter } from "react-router-dom";
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
    <MemoryRouter>
      <PermissionProvider
        user={{ id: 1, username: "admin", display_name: "管理员", role: "platform_admin", domain: null, org_id: 1 } as never}
      >
        <AuditLog />
      </PermissionProvider>
    </MemoryRouter>,
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

describe("AuditLog 合规报告", () => {
  it("「合规报告」Tab 一键聚合：敏感访问按操作人汇总（同人多次合并计数）", async () => {
    mockedListAudit.mockImplementation(async (params) => {
      if (params?.pii_access) {
        return {
          items: [
            { ...ENTRY, id: 10, actor_id: 9, actor_display: "李敏", pii_access: true, action: "metric.read", action_desc: "查看了敏感指标", created_at: "2026-08-18T02:00:00" },
            { ...ENTRY, id: 11, actor_id: 9, actor_display: "李敏", pii_access: true, action: "consume.query", action_desc: "查询了敏感指标", created_at: "2026-08-18T05:00:00" },
          ],
          total: 2,
          page: 1,
          page_size: 100,
        };
      }
      return {
        items: [{ ...ENTRY, id: 20, action: "audit.export", entity_type: "audit_log", entity_id: "2026-08-18", action_desc: "导出了审计日志" }, ENTRY],
        total: 2,
        page: 1,
        page_size: 100,
      };
    });
    renderPage();
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "合规报告" })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("tab", { name: "合规报告" }));
    // 敏感访问聚合：李敏 2 次
    await waitFor(() => {
      expect(screen.getByText("敏感数据访问（1 位操作人）")).toBeTruthy();
    });
    const accessCard = screen.getByText("敏感数据访问（1 位操作人）").closest(".ant-card") as HTMLElement;
    expect(within(accessCard).getByText("李敏")).toBeTruthy();
    expect(within(accessCard).getByText("2")).toBeTruthy();
  });

  it("「合规报告」聚合审计导出记录（action 含 export），非导出条目不混入", async () => {
    mockedListAudit.mockImplementation(async (params) => {
      if (params?.pii_access) {
        return { items: [], total: 0, page: 1, page_size: 100 };
      }
      return {
        items: [
          { ...ENTRY, id: 20, action: "audit.export", entity_type: "audit_log", entity_id: "2026-08-18", action_desc: "导出了审计日志" },
          ENTRY,
        ],
        total: 2,
        page: 1,
        page_size: 100,
      };
    });
    renderPage();
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "合规报告" })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("tab", { name: "合规报告" }));
    await waitFor(() => {
      expect(screen.getByText("审计导出记录（1）")).toBeTruthy();
    });
    const exportCard = screen.getByText("审计导出记录（1）").closest(".ant-card") as HTMLElement;
    // 导出人 + 导出内容实体类型中文
    expect(within(exportCard).getByText("张伟")).toBeTruthy();
    expect(within(exportCard).getByText("审计日志")).toBeTruthy();
    // 非导出条目（data_source.create）不进入导出记录
    expect(within(exportCard).queryByText("创建了数据源（名称=财务库）")).toBeNull();
  });

  it("敏感访问 API 失败时合规报告静默降级为空态（不崩溃）", async () => {
    mockedListAudit.mockImplementation(async (params) => {
      if (params?.pii_access) throw new Error("audit 不可用");
      return { items: [ENTRY], total: 1, page: 1, page_size: 100 };
    });
    renderPage();
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "合规报告" })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("tab", { name: "合规报告" }));
    await waitFor(() => {
      expect(screen.getByText("敏感数据访问（0 位操作人）")).toBeTruthy();
    });
    expect(screen.getByText("暂无敏感数据访问记录")).toBeTruthy();
  });
});

describe("AuditLog 行点击详情弹窗", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedListAudit.mockResolvedValue({ items: [ENTRY], total: 1, page: 1, page_size: 20 });
    mockedExportAudit.mockResolvedValue();
  });

  it("点击操作日志行打开弹窗并展示完整详情（detail_json 全字段 + 元信息）", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("创建了数据源（名称=财务库）")).toBeTruthy();
    });
    // 点击操作者单元格，冒泡到行 onClick
    fireEvent.click(screen.getByText("张伟"));
    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeTruthy();
    });
    const modal = within(screen.getByRole("dialog"));
    // 元信息：来源地址 / 完整追踪编号（非截断）/ 原始动作码 / 完整 entity_id（技术前缀不剥离）
    expect(modal.getByText("10.0.0.1")).toBeTruthy();
    expect(modal.getByText("trace-abc123")).toBeTruthy();
    expect(modal.getByText("data_source.create")).toBeTruthy();
    expect(modal.getByText("batch:5")).toBeTruthy();
    // detail_json 全字段：字段名中文映射 + 值
    expect(modal.getByText("源类型")).toBeTruthy();
    expect(modal.getByText("mysql")).toBeTruthy();
    expect(modal.getByText("名称")).toBeTruthy();
    expect(modal.getByText("财务库")).toBeTruthy();
  });

  it("detail_json 为空的条目弹窗显示无附加详情", async () => {
    mockedListAudit.mockResolvedValue({
      items: [{ ...ENTRY, id: 2, detail_json: null }],
      total: 1,
      page: 1,
      page_size: 20,
    });
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("创建了数据源（名称=财务库）")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("张伟"));
    await waitFor(() => {
      expect(screen.getByText("无附加详情")).toBeTruthy();
    });
  });

  it("点击关闭按钮关闭弹窗", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("创建了数据源（名称=财务库）")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("张伟"));
    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: /关\s*闭/ }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("合规报告导出记录行点击同样打开详情弹窗", async () => {
    mockedListAudit.mockImplementation(async (params) => {
      if (params?.pii_access) return { items: [], total: 0, page: 1, page_size: 100 };
      return {
        items: [
          {
            ...ENTRY,
            id: 20,
            action: "audit.export",
            entity_type: "audit_log",
            entity_id: "2026-08-18",
            detail_json: { format: "csv", rows: 5 },
            action_desc: "导出了审计日志",
          },
        ],
        total: 1,
        page: 1,
        page_size: 100,
      };
    });
    renderPage();
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "合规报告" })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("tab", { name: "合规报告" }));
    await waitFor(() => {
      expect(screen.getByText("审计导出记录（1）")).toBeTruthy();
    });
    // 限定在导出记录卡片内点击（操作日志 Tab 仍留在 DOM 中，全局匹配会命中多个"张伟"）
    const exportCard = screen.getByText("审计导出记录（1）").closest(".ant-card") as HTMLElement;
    fireEvent.click(within(exportCard).getByText("张伟"));
    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeTruthy();
    });
    const modal = within(screen.getByRole("dialog"));
    expect(modal.getByText("导出了审计日志")).toBeTruthy();
    expect(modal.getByText("csv")).toBeTruthy(); // detail_json 的 format 字段
  });
});
