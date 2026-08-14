import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { AiAssistant } from "../pages/AiAssistant";

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
  }
  return {
    aiNl2Sql: vi.fn(),
    getLlmConfig: vi.fn(),
    saveLlmConfig: vi.fn(),
    testLlmConfig: vi.fn(),
    UnisenseApiError,
  };
});

import { getLlmConfig, saveLlmConfig, testLlmConfig } from "../api";

const mockGet = vi.mocked(getLlmConfig);
const mockSave = vi.mocked(saveLlmConfig);
const mockTest = vi.mocked(testLlmConfig);

describe("AiAssistant LLM 配置", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockSave.mockReset();
    mockTest.mockReset();
  });

  it("平台管理员：展示可编辑配置表单并回填已存配置", async () => {
    mockGet.mockResolvedValue({
      provider: "deepseek",
      base_url: "https://api.deepseek.com",
      model: "deepseek-chat",
      has_api_key: true,
      timeout: 30,
      enabled: true,
      source: "db",
      can_edit: true,
      updated_by: 1,
      updated_at: null,
    });
    render(<AiAssistant />);
    expect(await screen.findByText("LLM 配置")).toBeTruthy();
    expect(await screen.findByText("已启用")).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByDisplayValue("https://api.deepseek.com")).toBeTruthy();
    });
  });

  it("普通用户：只读展示，无编辑按钮", async () => {
    mockGet.mockResolvedValue({
      provider: "deepseek",
      base_url: "https://api.deepseek.com",
      model: "deepseek-chat",
      has_api_key: true,
      timeout: 30,
      enabled: true,
      source: "env",
      can_edit: false,
      updated_by: null,
      updated_at: null,
    });
    render(<AiAssistant />);
    await screen.findByText("LLM 配置");
    await waitFor(() => {
      expect(screen.queryByText("保存配置")).toBeNull();
      expect(screen.queryByText("测试连通性")).toBeNull();
    });
  });

  it("测试连通性：成功时展示连通成功徽标", async () => {
    mockGet.mockResolvedValue({
      provider: "deepseek",
      base_url: "https://api.deepseek.com",
      model: "deepseek-chat",
      has_api_key: true,
      timeout: 30,
      enabled: true,
      source: "db",
      can_edit: true,
      updated_by: 1,
      updated_at: null,
    });
    mockTest.mockResolvedValue({
      ok: true,
      latency_ms: 123,
      model: "deepseek-chat",
      error: "",
    });
    render(<AiAssistant />);
    const testBtn = await screen.findByText("测试连通性");
    fireEvent.click(testBtn);
    await waitFor(() => {
      expect(screen.getByText(/连通成功/)).toBeTruthy();
    });
  });

  it("保存配置：调用 saveLlmConfig 并刷新", async () => {
    mockGet
      .mockResolvedValueOnce({
        provider: "custom",
        base_url: "",
        model: "",
        has_api_key: false,
        timeout: 30,
        enabled: false,
        source: "none",
        can_edit: true,
        updated_by: null,
        updated_at: null,
      })
      .mockResolvedValueOnce({
        provider: "deepseek",
        base_url: "https://api.deepseek.com",
        model: "deepseek-chat",
        has_api_key: true,
        timeout: 30,
        enabled: true,
        source: "db",
        can_edit: true,
        updated_by: 1,
        updated_at: null,
      });
    mockSave.mockResolvedValue({ id: 1 });
    render(<AiAssistant />);
    const baseUrl = (await screen.findByPlaceholderText("https://api.deepseek.com")) as HTMLInputElement;
    fireEvent.change(baseUrl, { target: { value: "https://api.deepseek.com" } });
    const modelInput = screen.getByPlaceholderText("deepseek-chat") as HTMLInputElement;
    fireEvent.change(modelInput, { target: { value: "deepseek-chat" } });
    const saveBtn = screen.getByText("保存配置");
    fireEvent.click(saveBtn);
    await waitFor(() => {
      expect(mockSave).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(screen.getByText("已启用")).toBeTruthy();
    });
  });
});
