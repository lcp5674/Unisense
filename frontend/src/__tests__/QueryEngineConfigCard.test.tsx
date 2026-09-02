import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import QueryEngineConfigCard from "../components/QueryEngineConfigCard";

vi.mock("../api", () => {
  class UnisenseApiError extends Error {
    code: string;
    traceId: string;
    status: number;
    detail?: Record<string, unknown> | null;
    constructor(
      message: string,
      code: string,
      status: number,
      traceId: string,
      detail?: Record<string, unknown> | null,
    ) {
      super(message);
      this.name = "UnisenseApiError";
      this.code = code;
      this.status = status;
      this.traceId = traceId;
      this.detail = detail;
    }
  }
  return {
    getQueryEngineConfig: vi.fn(),
    saveQueryEngineConfig: vi.fn(),
    testQueryEngineConfig: vi.fn(),
    UnisenseApiError,
  };
});

import {
  getQueryEngineConfig,
  saveQueryEngineConfig,
  testQueryEngineConfig,
} from "../api";

const mockGet = vi.mocked(getQueryEngineConfig);
const mockSave = vi.mocked(saveQueryEngineConfig);
const mockTest = vi.mocked(testQueryEngineConfig);

function viewData(overrides: Record<string, unknown> = {}) {
  return {
    row: {
      id: 1,
      olap_url: "",
      doris_host: "doris",
      doris_port: 8030,
      doris_database: "unisense",
      doris_user: "root",
      has_doris_password: true,
      has_mysql_fallback: true,
      enabled: true,
      updated_by: 1,
      updated_at: "2026-09-02T08:00:00",
    },
    effective: {
      source: "db",
      olap_url: "",
      doris_host: "doris",
      doris_port: 8030,
      doris_database: "unisense",
      doris_user: "root",
      has_doris_password: true,
      has_mysql_fallback: true,
      olap_configured: true,
      mysql_fallback_configured: true,
      updated_by: 1,
      updated_at: "2026-09-02T08:00:00",
      note: "数据库配置生效中（保存后无需重启，最长 30s 全量生效）",
    },
    can_edit: true,
    ...overrides,
  };
}

function renderCard() {
  return render(
    <MemoryRouter>
      <QueryEngineConfigCard />
    </MemoryRouter>,
  );
}

describe("QueryEngineConfigCard 查询引擎配置", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue(viewData() as never);
    mockSave.mockResolvedValue({ id: 1 } as never);
    mockTest.mockResolvedValue({
      ok: true,
      engine: "olap",
      latency_ms: 12,
      error: "",
    } as never);
  });

  it("渲染生效状态：来源 Tag + OLAP/MySQL 摘要 + 状态说明", async () => {
    renderCard();
    expect(await screen.findByText("查询引擎配置")).toBeTruthy();
    expect(screen.getByText("数据库配置")).toBeTruthy();
    expect(await screen.findByText(/doris:8030/)).toBeTruthy();
    expect(screen.getByText(/数据库配置生效中/)).toBeTruthy();
    // 平台管理员可编辑：出现编辑按钮
    expect(screen.getByText("编辑配置")).toBeTruthy();
  });

  it("编辑并保存：回填 DB 行 → 修改 olap_url → 保存调用 saveQueryEngineConfig", async () => {
    renderCard();
    fireEvent.click(await screen.findByText("编辑配置"));
    const urlInput = (await screen.findByPlaceholderText(
      "http://doris-fe:8030",
    )) as HTMLInputElement;
    fireEvent.change(urlInput, { target: { value: "http://doris-new:9030" } });
    fireEvent.click(screen.getByText("保 存") ?? screen.getByText("保存"));
    await waitFor(() => expect(mockSave).toHaveBeenCalledTimes(1));
    const payload = mockSave.mock.calls[0][0] as { olap_url: string };
    expect(payload.olap_url).toBe("http://doris-new:9030");
    // 密码留空 = 保持原值（payload 不覆盖）
    expect((mockSave.mock.calls[0][0] as { doris_password: string }).doris_password).toBe("");
  });

  it("测试 OLAP 连通：调用 testQueryEngineConfig（测生效配置）", async () => {
    renderCard();
    fireEvent.click(await screen.findByText("测试 OLAP"));
    await waitFor(() => expect(mockTest).toHaveBeenCalledTimes(1));
    const arg = mockTest.mock.calls[0][0] as { engine: string; payload?: unknown };
    expect(arg.engine).toBe("olap");
    expect(arg.payload).toBeUndefined();
    expect(await screen.findByText(/OLAP连通正常/)).toBeTruthy();
  });
});
