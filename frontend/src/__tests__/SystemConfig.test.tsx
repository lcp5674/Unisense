import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { SystemConfig } from "../pages/SystemConfig";

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
    getLlmConfigs: vi.fn(),
    getLlmConfigSecret: vi.fn(),
    createLlmConfig: vi.fn(),
    updateLlmConfig: vi.fn(),
    deleteLlmConfig: vi.fn(),
    testLlmConfig: vi.fn(),
    UnisenseApiError,
  };
});

import {
  createLlmConfig,
  deleteLlmConfig,
  getLlmConfigs,
  getLlmConfigSecret,
  testLlmConfig,
  updateLlmConfig,
} from "../api";

const mockGet = vi.mocked(getLlmConfigs);
const mockSecret = vi.mocked(getLlmConfigSecret);
const mockCreate = vi.mocked(createLlmConfig);
const mockUpdate = vi.mocked(updateLlmConfig);
const mockDelete = vi.mocked(deleteLlmConfig);
const mockTest = vi.mocked(testLlmConfig);

function listData(overrides: {
  canEdit?: boolean;
  items?: Array<Record<string, unknown>>;
} = {}) {
  const { canEdit = true, items = [] } = overrides;
  return {
    items: items as never[],
    strategy: "round_robin",
    effective: { source: "db", provider: "deepseek", base_url: "https://api.deepseek.com", model: "deepseek-chat" },
    can_edit: canEdit,
  };
}

const PRIMARY_ITEM = {
  id: 1,
  name: "主用",
  provider: "deepseek",
  base_url: "https://api.deepseek.com",
  model: "deepseek-chat",
  has_api_key: true,
  timeout: 30,
  enabled: true,
  priority: 0,
  source: "db",
  can_edit: true,
  updated_by: 1,
  updated_at: null,
};

describe("SystemConfig LLM 路由配置", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockSecret.mockReset();
    mockCreate.mockReset();
    mockUpdate.mockReset();
    mockDelete.mockReset();
    mockTest.mockReset();
    mockGet.mockResolvedValue(listData() as never);
  });

  it("平台管理员：展示实例列表（名称/接口/模型/优先级/启用）+ 新增按钮", async () => {
    mockGet.mockResolvedValue(
      listData({ items: [PRIMARY_ITEM] }) as never,
    );
    render(<SystemConfig />);
    expect(await screen.findByText("LLM 路由配置")).toBeTruthy();
    expect(await screen.findByText("主用")).toBeTruthy();
    expect(await screen.findByText("https://api.deepseek.com")).toBeTruthy();
    expect(await screen.findByText("已启用")).toBeTruthy();
    expect(screen.getByText("新增 LLM 实例")).toBeTruthy();
    expect(screen.getByText("测试")).toBeTruthy();
    expect(screen.getByText("编辑")).toBeTruthy();
    expect(screen.getByText("删除")).toBeTruthy();
  });

  it("普通用户：只读展示，无操作列与新增按钮", async () => {
    mockGet.mockResolvedValue(
      listData({ canEdit: false, items: [{ ...PRIMARY_ITEM, source: "env" }] }) as never,
    );
    render(<SystemConfig />);
    await screen.findByText("LLM 路由配置");
    await waitFor(() => {
      expect(screen.queryByText("新增 LLM 实例")).toBeNull();
      expect(screen.queryByText("测试")).toBeNull();
      expect(screen.queryByText("编辑")).toBeNull();
      expect(screen.queryByText("删除")).toBeNull();
    });
  });

  it("测试连通性：点行内测试按钮 → 展示连通成功徽标", async () => {
    mockGet.mockResolvedValue(listData({ items: [PRIMARY_ITEM] }) as never);
    mockTest.mockResolvedValue({
      ok: true,
      latency_ms: 123,
      model: "deepseek-chat",
      error: "",
    });
    render(<SystemConfig />);
    const testBtn = await screen.findByText("测试");
    fireEvent.click(testBtn);
    await waitFor(() => {
      expect(mockTest).toHaveBeenCalledWith({ instance_id: 1 });
      expect(screen.getByText(/连通成功/)).toBeTruthy();
    });
  });

  it("新增实例：打开弹窗 → 填写 → 保存 → 调用 createLlmConfig", async () => {
    mockCreate.mockResolvedValue({ id: 2 });
    render(<SystemConfig />);
    fireEvent.click(await screen.findByText("新增 LLM 实例"));
    // 弹窗表单
    fireEvent.change(await screen.findByPlaceholderText("如：主用 DeepSeek / 备用通义"), {
      target: { value: "备用通义" },
    });
    fireEvent.change(screen.getByPlaceholderText("https://api.deepseek.com"), {
      target: { value: "https://dashscope.aliyuncs.com/compatible-mode" },
    });
    fireEvent.change(screen.getByPlaceholderText("deepseek-chat"), {
      target: { value: "qwen-turbo" },
    });
    fireEvent.change(screen.getByPlaceholderText("sk-..."), {
      target: { value: "sk-test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({
          name: "备用通义",
          base_url: "https://dashscope.aliyuncs.com/compatible-mode",
          model: "qwen-turbo",
          api_key: "sk-test",
        }),
      );
    });
  });

  it("编辑实例：点编辑 → 回填表单 → 保存 → 调用 updateLlmConfig", async () => {
    mockGet.mockResolvedValue(listData({ items: [PRIMARY_ITEM] }) as never);
    mockUpdate.mockResolvedValue({ id: 1 });
    render(<SystemConfig />);
    fireEvent.click(await screen.findByText("编辑"));
    await waitFor(() => {
      expect(screen.getByDisplayValue("https://api.deepseek.com")).toBeTruthy();
    });
    fireEvent.change(screen.getByDisplayValue("https://api.deepseek.com"), {
      target: { value: "https://new.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ base_url: "https://new.example.com" }),
      );
    });
  });

  it("删除实例：点删除 → 确认弹窗 → 确定 → 调用 deleteLlmConfig", async () => {
    mockGet.mockResolvedValue(listData({ items: [PRIMARY_ITEM] }) as never);
    mockDelete.mockResolvedValue({ id: 1 });
    render(<SystemConfig />);
    fireEvent.click(await screen.findByText("删除"));
    // 确认弹窗（可控 Modal）出现，限定在弹窗内点击确认按钮
    const modal = await screen.findByText("删除 LLM 实例");
    const modalBox = modal.closest(".ant-modal") as HTMLElement;
    expect(within(modalBox).getByText(/确认删除实例/)).toBeTruthy();
    fireEvent.click(within(modalBox).getByRole("button", { name: /删\s*除/ }));
    await waitFor(() => {
      expect(mockDelete).toHaveBeenCalledWith(1);
    });
  });

  it("编辑时按需显示密钥：点显示密钥 → 调 getLlmConfigSecret → 回填 → 15 秒后自动隐藏", async () => {
    mockGet.mockResolvedValue(listData({ items: [PRIMARY_ITEM] }) as never);
    mockSecret.mockResolvedValue({ id: 1, api_key: "sk-revealed" });
    vi.useFakeTimers();
    try {
      render(<SystemConfig />);
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.click(screen.getByText("编辑"));
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.click(screen.getByText("显示密钥"));
      await act(async () => {
        await Promise.resolve();
      });
      expect(mockSecret).toHaveBeenCalledWith(1);
      expect(screen.getByDisplayValue("sk-revealed")).toBeTruthy();
      expect(screen.getByText("已显示（15 秒后自动隐藏）")).toBeTruthy();
      // 自动隐藏：推进 15 秒后字段被清空
      act(() => {
        vi.advanceTimersByTime(16000);
      });
      expect(screen.queryByDisplayValue("sk-revealed")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
