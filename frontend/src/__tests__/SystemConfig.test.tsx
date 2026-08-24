import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { SystemConfig } from "../pages/SystemConfig";
import { PermissionProvider } from "../hooks/usePermission";

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
    fetchLlmModels: vi.fn(),
    fetchMyPermissions: vi.fn(),
    UnisenseApiError,
  };
});

import {
  createLlmConfig,
  deleteLlmConfig,
  fetchLlmModels,
  fetchMyPermissions,
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
const mockFetchModels = vi.mocked(fetchLlmModels);
const mockPerms = vi.mocked(fetchMyPermissions);

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
    mockFetchModels.mockReset();
    mockGet.mockResolvedValue(listData() as never);
  });

  it("平台管理员：展示实例列表（名称/接口/模型/优先级/启用）+ 新增按钮", async () => {
    mockGet.mockResolvedValue(
      listData({ items: [PRIMARY_ITEM] }) as never,
    );
    render(<SystemConfig />);
    expect(await screen.findByText("LLM 路由配置")).toBeTruthy();
    // 概览条显示「当前路由：主用」+ 表格行也含「主用」→ 至少两处
    expect((await screen.findAllByText("主用")).length).toBeGreaterThanOrEqual(1);
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
    // 无 system-config:edit 权限点 → 即使后端 can_edit=false 且 fail-open 也保持只读
    mockPerms.mockResolvedValue({
      user_id: 2,
      role: "viewer",
      home_domain: null,
      allowed_actions: ["read"],
      ui_actions: ["system-config:view", "dashboard:view"],
      granted_domains: [],
      metric_whitelist: [],
      row_level_restricted: false,
      grants: [],
      expiring_soon: [],
    } as never);
    render(
      <PermissionProvider user={{ id: 2, username: "viewer", display_name: "访客", role: "viewer", domain: null, org_id: 1 } as never}>
        <SystemConfig />
      </PermissionProvider>,
    );
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
    fireEvent.change(screen.getByRole("combobox", { name: "模型名称" }), {
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

  it("新增实例：点获取模型 → 调 fetchLlmModels → 提示可用模型数", async () => {
    mockFetchModels.mockResolvedValue({
      models: ["hy3", "hy3-pro"],
      supported: true,
      error: "",
      latency_ms: 36,
    });
    render(<SystemConfig />);
    fireEvent.click(await screen.findByText("新增 LLM 实例"));
    fireEvent.change(screen.getByPlaceholderText("https://api.deepseek.com"), {
      target: { value: "http://127.0.0.1:19091" },
    });
    fireEvent.change(screen.getByPlaceholderText("sk-..."), {
      target: { value: "sk-test" },
    });
    fireEvent.click(screen.getByText("获取模型"));
    await waitFor(() => {
      expect(mockFetchModels).toHaveBeenCalledWith(
        expect.objectContaining({ base_url: "http://127.0.0.1:19091" }),
      );
      expect(screen.getByText(/获取到 2 个可用模型/)).toBeTruthy();
    });
  });

  it("获取模型：网关不支持 /models → 提示错误并保留手动输入", async () => {
    mockFetchModels.mockResolvedValue({
      models: [],
      supported: false,
      error: "HTTP 404: not found",
      latency_ms: 8,
    });
    render(<SystemConfig />);
    fireEvent.click(await screen.findByText("新增 LLM 实例"));
    fireEvent.change(screen.getByPlaceholderText("https://api.deepseek.com"), {
      target: { value: "http://127.0.0.1:19091" },
    });
    fireEvent.click(screen.getByText("获取模型"));
    await waitFor(() => {
      expect(mockFetchModels).toHaveBeenCalledWith(
        expect.objectContaining({ base_url: "http://127.0.0.1:19091" }),
      );
      expect(screen.getByText(/HTTP 404/)).toBeTruthy();
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
    // AutoComplete 必须回填已保存的模型名（回归：被 Space.Compact 包裹时 Form.Item
    // 的 value 注入不到 AutoComplete 上，导致编辑时模型显示为空）
    expect(screen.getByDisplayValue("deepseek-chat")).toBeTruthy();
    fireEvent.change(screen.getByDisplayValue("https://api.deepseek.com"), {
      target: { value: "https://new.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ base_url: "https://new.example.com", model: "deepseek-chat" }),
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

  it("P0 概览条：展示当前路由、启用数与已验证连通数", async () => {
    mockGet.mockResolvedValue(listData({ items: [PRIMARY_ITEM] }) as never);
    render(<SystemConfig />);
    const overview = await screen.findByTestId("routing-overview");
    expect(within(overview).getByText(/当前路由/)).toBeTruthy();
    expect(within(overview).getByText("主用")).toBeTruthy();
    expect(within(overview).getByText(/启用/)).toBeTruthy();
    expect(within(overview).getByText(/已验证连通/)).toBeTruthy();
  });

  it("P0 轮询位次：多实例按 priority 排序展示第 N 位", async () => {
    mockGet.mockResolvedValue(
      listData({
        items: [
          { ...PRIMARY_ITEM, id: 1, name: "主用", priority: 0 },
          { ...PRIMARY_ITEM, id: 2, name: "备用", priority: 1 },
        ],
      }) as never,
    );
    render(<SystemConfig />);
    await screen.findByText("备用");
    expect(screen.getByText("第 1 位")).toBeTruthy();
    expect(screen.getByText("第 2 位")).toBeTruthy();
  });

  it("P0 保存后一键启用流：保存成功 → 自动测试 → 展示连通徽标", async () => {
    mockGet.mockResolvedValue(
      listData({ items: [{ ...PRIMARY_ITEM, id: 1 }] }) as never,
    );
    mockCreate.mockResolvedValue({ id: 2 });
    mockTest.mockResolvedValue({ ok: true, latency_ms: 88, model: "qwen-turbo", error: "" });
    render(<SystemConfig />);
    fireEvent.click(await screen.findByText("新增 LLM 实例"));
    fireEvent.change(await screen.findByPlaceholderText("如：主用 DeepSeek / 备用通义"), {
      target: { value: "备用通义" },
    });
    fireEvent.change(screen.getByPlaceholderText("https://api.deepseek.com"), {
      target: { value: "https://dashscope.aliyuncs.com/compatible-mode" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "模型名称" }), {
      target: { value: "qwen-turbo" },
    });
    fireEvent.change(screen.getByPlaceholderText("sk-..."), {
      target: { value: "sk-test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    // 保存后应自动对该新实例（id=2）发起连通性测试
    await waitFor(() => {
      expect(mockCreate).toHaveBeenCalled();
      expect(mockTest).toHaveBeenCalledWith({ instance_id: 2 });
    });
    expect(await screen.findByText(/连通成功 · 推理正常 · 88 ms/)).toBeTruthy();
  });

  it("P0 保存后一键启用流：连通失败 → 展示失败徽标 + 去编辑密钥入口", async () => {
    mockGet.mockResolvedValue(
      listData({ items: [{ ...PRIMARY_ITEM, id: 1 }] }) as never,
    );
    mockCreate.mockResolvedValue({ id: 2 });
    mockTest.mockResolvedValue({ ok: false, latency_ms: 0, model: "qwen-turbo", error: "HTTP 401" });
    render(<SystemConfig />);
    fireEvent.click(await screen.findByText("新增 LLM 实例"));
    fireEvent.change(await screen.findByPlaceholderText("如：主用 DeepSeek / 备用通义"), {
      target: { value: "备用通义" },
    });
    fireEvent.change(screen.getByPlaceholderText("https://api.deepseek.com"), {
      target: { value: "https://dashscope.aliyuncs.com/compatible-mode" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "模型名称" }), {
      target: { value: "qwen-turbo" },
    });
    fireEvent.change(screen.getByPlaceholderText("sk-..."), {
      target: { value: "sk-test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(screen.getByText(/连通失败/)).toBeTruthy();
    });
    // 失败时给「去编辑密钥」快捷入口
    expect(await screen.findByText("去编辑密钥")).toBeTruthy();
  });

  it("P1 上移位次：点上移 → 与相邻实例交换优先级（调用 updateLlmConfig ×2）", async () => {
    mockGet.mockResolvedValue(
      listData({
        items: [
          { ...PRIMARY_ITEM, id: 1, name: "主用", priority: 0 },
          { ...PRIMARY_ITEM, id: 2, name: "备用", priority: 1 },
        ],
      }) as never,
    );
    mockUpdate.mockResolvedValue({ id: 2 });
    render(<SystemConfig />);
    await screen.findByText("备用");
    fireEvent.click(screen.getByLabelText("上移 备用"));
    await waitFor(() => {
      // 备用(id=2, p1) 与主用(id=1, p0) 交换优先级
      expect(mockUpdate).toHaveBeenCalledWith(2, expect.objectContaining({ priority: 0 }));
      expect(mockUpdate).toHaveBeenCalledWith(1, expect.objectContaining({ priority: 1 }));
    });
  });

  it("P1 同优先级冲突：两个实例优先级相同 → 显示冲突警告图标", async () => {
    mockGet.mockResolvedValue(
      listData({
        items: [
          { ...PRIMARY_ITEM, id: 1, name: "主用", priority: 0 },
          { ...PRIMARY_ITEM, id: 2, name: "备用", priority: 0 },
        ],
      }) as never,
    );
    render(<SystemConfig />);
    await screen.findByText("备用");
    expect(screen.getAllByTestId("priority-conflict").length).toBeGreaterThanOrEqual(1);
  });

  it("P1 删除影响：删除当前路由且唯一启用实例 → 提示将处于未配置状态", async () => {
    mockGet.mockResolvedValue(listData({ items: [{ ...PRIMARY_ITEM, id: 1 }] }) as never);
    render(<SystemConfig />);
    fireEvent.click(await screen.findByText("删除"));
    const modal = await screen.findByText("删除 LLM 实例");
    const modalBox = modal.closest(".ant-modal") as HTMLElement;
    expect(within(modalBox).getByTestId("delete-effective")).toBeTruthy();
    expect(within(modalBox).getByText(/LLM 将处于未配置状态/)).toBeTruthy();
  });

  it("P1 批量测试：全部测试 → 并行调用 testLlmConfig ×2 + 集群健康报告", async () => {
    mockGet.mockResolvedValue(
      listData({
        items: [
          { ...PRIMARY_ITEM, id: 1, name: "主用" },
          { ...PRIMARY_ITEM, id: 2, name: "备用" },
        ],
      }) as never,
    );
    mockTest.mockImplementation((body?: { instance_id?: number }) => {
      if (body?.instance_id === 1) {
        return Promise.resolve({ ok: true, latency_ms: 30, model: "deepseek-chat", error: "" });
      }
      return Promise.resolve({ ok: false, latency_ms: 0, model: "qwen-turbo", error: "HTTP 401" });
    });
    render(<SystemConfig />);
    fireEvent.click(await screen.findByText("全部测试"));
    await waitFor(() => {
      expect(mockTest).toHaveBeenCalledWith({ instance_id: 1 });
      expect(mockTest).toHaveBeenCalledWith({ instance_id: 2 });
    });
    expect(await screen.findByTestId("cluster-health")).toBeTruthy();
    const health = screen.getByTestId("cluster-health");
    expect(within(health).getByText(/集群健康：1 可用 \/ 1 失败/)).toBeTruthy();
  });
});
