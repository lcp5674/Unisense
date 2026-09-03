import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { SystemConfig } from "../pages/SystemConfig";
import { MemoryRouter } from "react-router-dom";
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
    render(<SystemConfig />, { wrapper: MemoryRouter });
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
      { wrapper: MemoryRouter },
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
    render(<SystemConfig />, { wrapper: MemoryRouter });
    const testBtn = await screen.findByText("测试");
    fireEvent.click(testBtn);
    await waitFor(() => {
      expect(mockTest).toHaveBeenCalledWith({ instance_id: 1 });
      expect(screen.getByText(/连通成功/)).toBeTruthy();
    });
  });

  it("新增实例：打开弹窗 → 填写 → 保存 → 调用 createLlmConfig", async () => {
    mockCreate.mockResolvedValue({ id: 2 });
    render(<SystemConfig />, { wrapper: MemoryRouter });
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
    render(<SystemConfig />, { wrapper: MemoryRouter });
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
    render(<SystemConfig />, { wrapper: MemoryRouter });
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

  it("选择火山方舟/腾讯混元提供商 → 预填 Coding Plan 接口地址与默认模型", async () => {
    render(<SystemConfig />, { wrapper: MemoryRouter });
    fireEvent.click(await screen.findByText("新增 LLM 实例"));
    // 选择火山方舟（Coding Plan）
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "提供商" }));
    fireEvent.click(await screen.findByText("火山方舟（Coding Plan）"));
    await waitFor(() => {
      expect(
        screen.getByDisplayValue("https://ark.cn-beijing.volces.com/api/coding/v3"),
      ).toBeTruthy();
      expect(screen.getByDisplayValue("deepseek-v3.1")).toBeTruthy();
    });
    // 切换到腾讯云混元
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "提供商" }));
    fireEvent.click(await screen.findByText("腾讯云混元"));
    await waitFor(() => {
      expect(screen.getByDisplayValue("https://api.hunyuan.cloud.tencent.com/v1")).toBeTruthy();
      expect(screen.getByDisplayValue("hunyuan-turbos-latest")).toBeTruthy();
    });
  });

  it("获取模型：网关不支持 /models 但平台有内置目录 → 展示目录并提示可手动补充", async () => {
    mockFetchModels.mockResolvedValue({
      models: ["deepseek-v3.1", "deepseek-r1-0528", "kimi-k2.5"],
      supported: true,
      error: "",
      latency_ms: 30,
      source: "catalog",
      note: "该网关不支持 GET /models 接口，已列出平台内置常用模型；实际可用模型以订阅套餐/控制台为准，可手动输入补充",
    });
    render(<SystemConfig />, { wrapper: MemoryRouter });
    fireEvent.click(await screen.findByText("新增 LLM 实例"));
    // 选择火山方舟 → 预填 base_url，同时 provider=ark 传给 fetchLlmModels
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "提供商" }));
    fireEvent.click(await screen.findByText("火山方舟（Coding Plan）"));
    await waitFor(() => {
      expect(
        screen.getByDisplayValue("https://ark.cn-beijing.volces.com/api/coding/v3"),
      ).toBeTruthy();
    });
    fireEvent.change(screen.getByPlaceholderText("sk-..."), {
      target: { value: "sk-test" },
    });
    fireEvent.click(screen.getByText("获取模型"));
    await waitFor(() => {
      expect(mockFetchModels).toHaveBeenCalledWith(
        expect.objectContaining({
          provider: "ark",
          base_url: "https://ark.cn-beijing.volces.com/api/coding/v3",
        }),
      );
      // 展示目录来源提示（而非「获取到 N 个可用模型」）
      expect(screen.getByText(/已列出平台内置常用模型/)).toBeTruthy();
      // 下拉被目录模型填充（kimi-k2.5 不在预设/自动选中值中，仅来自目录）
      expect(screen.getByText("kimi-k2.5")).toBeTruthy();
    });
  });

  it("编辑实例：点编辑 → 回填表单 → 保存 → 调用 updateLlmConfig", async () => {
    mockGet.mockResolvedValue(listData({ items: [PRIMARY_ITEM] }) as never);
    mockUpdate.mockResolvedValue({ id: 1 });
    render(<SystemConfig />, { wrapper: MemoryRouter });
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

  it("编辑并停用实例：关掉「启用」→ 保存 → 不自动跑连通性测试", async () => {
    mockGet.mockResolvedValue(listData({ items: [PRIMARY_ITEM] }) as never);
    mockUpdate.mockResolvedValue({ id: 1 });
    render(<SystemConfig />, { wrapper: MemoryRouter });
    fireEvent.click(await screen.findByText("编辑"));
    await waitFor(() => {
      expect(screen.getByDisplayValue("deepseek-chat")).toBeTruthy();
    });
    // 停用：关闭「启用」Switch（PRIMARY_ITEM 初始 enabled=true）
    const sw = screen.getByRole("switch");
    expect(sw.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(sw);
    expect(sw.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith(1, expect.objectContaining({ enabled: false }));
    });
    // 停用 = 下线该实例，不应自动跑连通性测试（测试无意义且会在停用时误报连通失败）
    expect(mockTest).not.toHaveBeenCalled();
  });

  it("删除实例：点删除 → 确认弹窗 → 确定 → 调用 deleteLlmConfig", async () => {
    mockGet.mockResolvedValue(listData({ items: [PRIMARY_ITEM] }) as never);
    mockDelete.mockResolvedValue({ id: 1 });
    render(<SystemConfig />, { wrapper: MemoryRouter });
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
      render(<SystemConfig />, { wrapper: MemoryRouter });
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
    render(<SystemConfig />, { wrapper: MemoryRouter });
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
    render(<SystemConfig />, { wrapper: MemoryRouter });
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
    render(<SystemConfig />, { wrapper: MemoryRouter });
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

  it("P0 保存后一键启用流：网关无 /models（火山方舟/腾讯混元）→ 连通成功并明示已用真实推理验证", async () => {
    mockGet.mockResolvedValue(
      listData({ items: [{ ...PRIMARY_ITEM, id: 1 }] }) as never,
    );
    mockCreate.mockResolvedValue({ id: 2 });
    // 后端两步探测：GET /models 404 → 回落真实 chat 探测通过 → ok=true + models_supported=false
    mockTest.mockResolvedValue({
      ok: true,
      latency_ms: 210,
      model: "deepseek-v3.1",
      error: "",
      models: [],
      chat: true,
      models_supported: false,
    });
    render(<SystemConfig />, { wrapper: MemoryRouter });
    fireEvent.click(await screen.findByText("新增 LLM 实例"));
    fireEvent.change(await screen.findByPlaceholderText("如：主用 DeepSeek / 备用通义"), {
      target: { value: "方舟主用" },
    });
    fireEvent.change(screen.getByPlaceholderText("https://api.deepseek.com"), {
      target: { value: "https://ark.cn-beijing.volces.com/api/coding/v3" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "模型名称" }), {
      target: { value: "deepseek-v3.1" },
    });
    fireEvent.change(screen.getByPlaceholderText("sk-..."), {
      target: { value: "sk-test" },
    });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockTest).toHaveBeenCalledWith({ instance_id: 2 });
    });
    expect(await screen.findByText(/连通成功 · 推理正常 · 210 ms/)).toBeTruthy();
    // 网关无 /models 时明示连通由真实推理验证（不再是「可用模型 0 个」的困惑）
    expect(await screen.findByText(/网关无 \/models，已用真实推理验证连通/)).toBeTruthy();
  });

  it("P0 保存后一键启用流：连通失败 → 展示失败徽标 + 去编辑密钥入口", async () => {
    mockGet.mockResolvedValue(
      listData({ items: [{ ...PRIMARY_ITEM, id: 1 }] }) as never,
    );
    mockCreate.mockResolvedValue({ id: 2 });
    mockTest.mockResolvedValue({ ok: false, latency_ms: 0, model: "qwen-turbo", error: "HTTP 401" });
    render(<SystemConfig />, { wrapper: MemoryRouter });
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
    render(<SystemConfig />, { wrapper: MemoryRouter });
    await screen.findByText("备用");
    fireEvent.click(screen.getByLabelText("上移 备用"));
    await waitFor(() => {
      // 备用(id=2, p1) 与主用(id=1, p0) 交换优先级
      expect(mockUpdate).toHaveBeenCalledWith(2, expect.objectContaining({ priority: 0 }));
      expect(mockUpdate).toHaveBeenCalledWith(1, expect.objectContaining({ priority: 1 }));
    });
  });

  it("P1 同优先级上移（均为 0）：第 2 位点「上移」→ 区间重排：主用 priority+1 推后、备用保持 0，位次严格互换", async () => {
    mockGet.mockResolvedValue(
      listData({
        items: [
          { ...PRIMARY_ITEM, id: 1, name: "主用", priority: 0 },
          { ...PRIMARY_ITEM, id: 2, name: "备用", priority: 0 },
        ],
      }) as never,
    );
    mockUpdate.mockResolvedValue({ id: 2 });
    render(<SystemConfig />, { wrapper: MemoryRouter });
    await screen.findByText("备用");
    // 两个实例 priority 均为 0（新建默认）——旧逻辑上移 newP=max(0,0-1)=0 被钳回，
    // 优先级不变、仍按 ID 并列 → 位次无变化。区间重排：段 [主用,备用] 交换后重写为
    // 主用=1、备用=0，位次严格互换（主用 1→2、备用 2→1），其余不动。
    fireEvent.click(screen.getByLabelText("上移 备用"));
    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledTimes(1);
      expect(mockUpdate).toHaveBeenCalledWith(1, expect.objectContaining({ priority: 1 }));
    });
    // 不再发生「把备用自身 priority 写成 0」的无效更新
    expect(mockUpdate).not.toHaveBeenCalledWith(2, expect.objectContaining({ priority: 0 }));
  });

  it("P1 同优先级下移（均为 0）：第 1 位点「下移」→ 区间重排：自身 priority+1，位次实际下移", async () => {
    mockGet.mockResolvedValue(
      listData({
        items: [
          { ...PRIMARY_ITEM, id: 1, name: "主用", priority: 0 },
          { ...PRIMARY_ITEM, id: 2, name: "备用", priority: 0 },
        ],
      }) as never,
    );
    mockUpdate.mockResolvedValue({ id: 1 });
    render(<SystemConfig />, { wrapper: MemoryRouter });
    await screen.findByText("备用");
    fireEvent.click(screen.getByLabelText("下移 主用"));
    await waitFor(() => {
      // 主用(cur, p0) 下移 → 段 [主用,备用] 交换后重写：备用=0（不变）、主用=1，位次互换
      expect(mockUpdate).toHaveBeenCalledWith(1, expect.objectContaining({ priority: 1 }));
    });
  });

  it("P1 严格相邻交换：3 个同优先级实例，上移第 3 位 → 仅第 2/3 位互换、第 1 位完全不动", async () => {
    mockGet.mockResolvedValue(
      listData({
        items: [
          { ...PRIMARY_ITEM, id: 1, name: "主用", priority: 0 },
          { ...PRIMARY_ITEM, id: 2, name: "备用", priority: 0 },
          { ...PRIMARY_ITEM, id: 3, name: "备用2", priority: 0 },
        ],
      }) as never,
    );
    mockUpdate.mockResolvedValue({ id: 3 });
    render(<SystemConfig />, { wrapper: MemoryRouter });
    await screen.findByText("备用2");
    // 旧逻辑：把目标行(备用,id2)推后到 p1 → 排序 备用2(0)、主用(0)、备用(1)，主用位次被挤动。
    // 区间重排：段 [主用,备用,备用2] 交换后重写为 主用=0、备用2=1、备用=2——
    // 第 1 位主用完全不动，仅备用(2→3) 与 备用2(3→2) 互换。
    fireEvent.click(screen.getByLabelText("上移 备用2"));
    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledTimes(2);
      expect(mockUpdate).toHaveBeenCalledWith(3, expect.objectContaining({ priority: 1 }));
      expect(mockUpdate).toHaveBeenCalledWith(2, expect.objectContaining({ priority: 2 }));
    });
    // 第 1 位（主用 id=1）未被触碰
    expect(mockUpdate).not.toHaveBeenCalledWith(1, expect.anything());
  });

  it("P1 连锁重排：同优先级区间后紧跟更高优先级实例，上移区间末位 → 连锁并入下一区间保位次正确", async () => {
    mockGet.mockResolvedValue(
      listData({
        items: [
          { ...PRIMARY_ITEM, id: 1, name: "主用", priority: 0 },
          { ...PRIMARY_ITEM, id: 2, name: "备用", priority: 0 },
          { ...PRIMARY_ITEM, id: 3, name: "备用2", priority: 0 },
          { ...PRIMARY_ITEM, id: 4, name: "备用3", priority: 1 },
        ],
      }) as never,
    );
    mockUpdate.mockResolvedValue({ id: 3 });
    render(<SystemConfig />, { wrapper: MemoryRouter });
    await screen.findByText("备用3");
    // 段 [主用,备用,备用2] (p0) 重排终点=2 撞上 备用3(p1)，必须连锁并入 →
    // 段 [主用,备用,备用2,备用3] 交换后重写 0,1,2,3：主用=0（跳过）、备用2=1、
    // 备用=2、备用3=3。位次：主用1、备用2 3→2、备用 2→3、备用3 4 不动。
    fireEvent.click(screen.getByLabelText("上移 备用2"));
    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledTimes(3);
      expect(mockUpdate).toHaveBeenCalledWith(3, expect.objectContaining({ priority: 1 }));
      expect(mockUpdate).toHaveBeenCalledWith(2, expect.objectContaining({ priority: 2 }));
      expect(mockUpdate).toHaveBeenCalledWith(4, expect.objectContaining({ priority: 3 }));
    });
    // 第 1 位（主用 id=1）未被触碰
    expect(mockUpdate).not.toHaveBeenCalledWith(1, expect.anything());
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
    render(<SystemConfig />, { wrapper: MemoryRouter });
    await screen.findByText("备用");
    expect(screen.getAllByTestId("priority-conflict").length).toBeGreaterThanOrEqual(1);
  });

  it("P1 删除影响：删除当前路由且唯一启用实例 → 提示将处于未配置状态", async () => {
    mockGet.mockResolvedValue(listData({ items: [{ ...PRIMARY_ITEM, id: 1 }] }) as never);
    render(<SystemConfig />, { wrapper: MemoryRouter });
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
    render(<SystemConfig />, { wrapper: MemoryRouter });
    fireEvent.click(await screen.findByText("全部测试"));
    await waitFor(() => {
      expect(mockTest).toHaveBeenCalledWith({ instance_id: 1 });
      expect(mockTest).toHaveBeenCalledWith({ instance_id: 2 });
    });
    expect(await screen.findByTestId("cluster-health")).toBeTruthy();
  });

  it("获取模型成功：自动选中第一个模型 + 摘要提示 + 下拉展开（可像选项框点选）", async () => {
    mockFetchModels.mockResolvedValue({
      models: ["hy3", "hy3-pro"],
      supported: true,
      error: "",
      latency_ms: 36,
    });
    render(<SystemConfig />, { wrapper: MemoryRouter });
    fireEvent.click(await screen.findByText("新增 LLM 实例"));
    fireEvent.change(screen.getByPlaceholderText("https://api.deepseek.com"), {
      target: { value: "http://127.0.0.1:19091" },
    });
    fireEvent.click(screen.getByText("获取模型"));
    await waitFor(() => {
      // 成功提示带模型列表摘要
      expect(screen.getByText(/获取到 2 个可用模型：hy3、hy3-pro/)).toBeTruthy();
      // 当前模型为空时自动选中第一个
      expect(screen.getByDisplayValue("hy3")).toBeTruthy();
    });
    // 下拉展开（无 hidden class），模型列表可见
    const dropdown = document.querySelector(".ant-select-dropdown") as HTMLElement | null;
    expect(dropdown).toBeTruthy();
    expect(dropdown?.classList.contains("ant-select-dropdown-hidden")).toBe(false);
  });

  it("获取模型成功：已有模型值时不被覆盖（保留当前输入）", async () => {
    mockGet.mockResolvedValue(listData({ items: [PRIMARY_ITEM] }) as never);
    mockFetchModels.mockResolvedValue({
      models: ["hy3", "hy3-pro"],
      supported: true,
      error: "",
      latency_ms: 20,
    });
    render(<SystemConfig />, { wrapper: MemoryRouter });
    fireEvent.click(await screen.findByText("编辑"));
    await waitFor(() => {
      expect(screen.getByDisplayValue("deepseek-chat")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("获取模型"));
    await waitFor(() => {
      // 已有模型值（deepseek-chat）不被覆盖为第一个模型
      expect(screen.getByDisplayValue("deepseek-chat")).toBeTruthy();
    });
    expect(screen.queryByDisplayValue("hy3")).toBeNull();
  });

  it("获取模型成功：下拉展示全部模型（不被自动选中值过滤成子集）", async () => {
    mockFetchModels.mockResolvedValue({
      models: ["hy3", "hy3-pro", "deepseek-chat", "gpt-4o-mini", "glm-4-flash"],
      supported: true,
      error: "",
      latency_ms: 10,
    });
    render(<SystemConfig />, { wrapper: MemoryRouter });
    fireEvent.click(await screen.findByText("新增 LLM 实例"));
    fireEvent.change(screen.getByPlaceholderText("https://api.deepseek.com"), {
      target: { value: "http://127.0.0.1:19091" },
    });
    fireEvent.click(screen.getByText("获取模型"));
    await waitFor(() => {
      // 自动选中第一个，但下拉必须展示全部模型（修复：程序化 value 不过滤）
      expect(screen.getByDisplayValue("hy3")).toBeTruthy();
    });
    const opts = document.querySelectorAll(
      ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option",
    );
    expect(opts.length).toBe(5);
  });

  it("获取模型成功（编辑）：下拉展示全部模型（不被已有模型值过滤）", async () => {
    mockGet.mockResolvedValue(listData({ items: [PRIMARY_ITEM] }) as never);
    mockFetchModels.mockResolvedValue({
      models: ["hy3", "hy3-pro", "deepseek-chat", "gpt-4o-mini", "glm-4-flash"],
      supported: true,
      error: "",
      latency_ms: 10,
    });
    render(<SystemConfig />, { wrapper: MemoryRouter });
    fireEvent.click(await screen.findByText("编辑"));
    await waitFor(() => {
      expect(screen.getByDisplayValue("deepseek-chat")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("获取模型"));
    await waitFor(() => {
      expect(screen.getByDisplayValue("deepseek-chat")).toBeTruthy();
    });
    const opts = document.querySelectorAll(
      ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option",
    );
    expect(opts.length).toBe(5);
  });

  it("获取模型失败（不支持 /models）：不自动选中、不展开下拉", async () => {
    mockFetchModels.mockResolvedValue({
      models: [],
      supported: false,
      error: "HTTP 404: not found",
      latency_ms: 8,
    });
    render(<SystemConfig />, { wrapper: MemoryRouter });
    fireEvent.click(await screen.findByText("新增 LLM 实例"));
    fireEvent.change(screen.getByPlaceholderText("https://api.deepseek.com"), {
      target: { value: "http://127.0.0.1:19091" },
    });
    fireEvent.click(screen.getByText("获取模型"));
    await waitFor(() => {
      expect(screen.getByText(/HTTP 404/)).toBeTruthy();
    });
    // 模型输入框保持空，未被自动填充
    expect(
      (screen.getByRole("combobox", { name: "模型名称" }) as HTMLInputElement).value,
    ).toBe("");
    // 下拉未展开
    expect(document.querySelector(".ant-select-dropdown")).toBeNull();
  });
});