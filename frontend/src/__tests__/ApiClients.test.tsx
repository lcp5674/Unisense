import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiClients } from "../pages/ApiClients";

vi.mock("../api", () => {
  class UnisenseApiError extends Error {
    code: string;
    traceId: string;
    status: number;
    constructor(message: string, code: string, status: number, traceId: string) {
      super(message);
      this.name = "UnisenseApiError";
      this.code = code;
      this.status = status;
      this.traceId = traceId;
    }
    get codeZh(): string {
      return this.code;
    }
  }
  return {
    UnisenseApiError,
    createApiClient: vi.fn(),
    listApiClients: vi.fn(),
    mintClientToken: vi.fn(),
    consumeDryRun: vi.fn(),
    listMetrics: vi.fn(),
    setConsumeToken: vi.fn(),
    getConsumeToken: vi.fn(() => null),
    updateApiClient: vi.fn(),
    updateApiClientStatus: vi.fn(),
    deleteApiClient: vi.fn(),
    batchApiClientAction: vi.fn(),
    listDomainTree: vi.fn(() => Promise.resolve([{ code: "outp", name: "门诊", children: [] }])),
  };
});

vi.mock("../hooks/usePermission", () => ({
  usePermission: () => ({ can: () => true }),
}));

import {
  listApiClients,
  mintClientToken,
  consumeDryRun,
  listMetrics,
  getConsumeToken,
  updateApiClient,
  updateApiClientStatus,
  deleteApiClient,
  batchApiClientAction,
} from "../api";
const mockedListApiClients = vi.mocked(listApiClients);
const mockedMintClientToken = vi.mocked(mintClientToken);
const mockedConsumeDryRun = vi.mocked(consumeDryRun);
const mockedListMetrics = vi.mocked(listMetrics);
const mockedGetConsumeToken = vi.mocked(getConsumeToken);
const mockedUpdateApiClient = vi.mocked(updateApiClient);
const mockedUpdateApiClientStatus = vi.mocked(updateApiClientStatus);
const mockedDeleteApiClient = vi.mocked(deleteApiClient);
const mockedBatchApiClientAction = vi.mocked(batchApiClientAction);

const ACTIVE_CLIENT = {
  client_id: "app_abcd1234",
  scope_domain: null,
  metric_whitelist: null,
  qps: 20,
  daily_quota: 100000,
  status: "ACTIVE",
};

describe("ApiClients", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedListApiClients.mockResolvedValue([ACTIVE_CLIENT]);
    mockedMintClientToken.mockResolvedValue({ access_token: "consume-token-xyz" });
    mockedGetConsumeToken.mockReturnValue(null);
    mockedListMetrics.mockResolvedValue({
      items: [
        { metric_code: "outp_feeamount_day", name: "门诊收费金额", pii_flag: false },
        { metric_code: "outp_doctor_cnt", name: "医生数", pii_flag: false },
      ],
      total: 2,
      page: 1,
      page_size: 200,
    } as never);
  });

  it("列表渲染客户端并展示接入指南（端点清单 + curl 示例）", async () => {
    render(<ApiClients />);
    expect(await screen.findByText("app_abcd1234")).toBeInTheDocument();
    expect(screen.getByText("接入指南")).toBeInTheDocument();
    expect(screen.getByText("/api/v1/consume/query/dry-run")).toBeInTheDocument();
    expect(screen.getByText("/api/v1/consume/query")).toBeInTheDocument();
    expect(screen.getByText(/curl -X POST/)).toBeInTheDocument();
    expect(screen.getByText("连通性测试")).toBeInTheDocument();
  });

  it("签发令牌弹窗支持选择有效期并透传后端", async () => {
    const user = userEvent.setup();
    render(<ApiClients />);
    await screen.findByText("app_abcd1234");

    await user.click(screen.getByText("签发令牌"));
    expect(await screen.findByText(/签发消费令牌/)).toBeInTheDocument();

    // 默认 60 分钟；切换到 4 小时
    await user.click(screen.getByText("4 小时"));
    await user.click(screen.getByText("签发并复制"));

    await waitFor(() => {
      expect(mockedMintClientToken).toHaveBeenCalledWith("app_abcd1234", 240);
    });
    // 成功提示动态展示实际时长
    await waitFor(() => {
      expect(screen.getByText(/已签发令牌并复制到剪贴板（240 分钟有效）/)).toBeInTheDocument();
    });
  });

  it("连通性测试：签发令牌 → 首个已发布指标 dry-run 验证全链路", async () => {
    const user = userEvent.setup();
    mockedListMetrics.mockResolvedValue({
      items: [{ metric_code: "outp_feeamount_day", name: "门诊收费金额", pii_flag: false }],
      total: 1,
      page: 1,
      page_size: 1,
    } as never);
    mockedConsumeDryRun.mockResolvedValue({
      metric_code: "outp_feeamount_day",
      status: "ok",
      checks: [],
      execution_plan: { elapsed_ms: 12 },
      meta: {},
    } as never);

    render(<ApiClients />);
    await screen.findByText("app_abcd1234");

    await user.click(screen.getByText("连通性测试"));

    await waitFor(() => {
      expect(mockedMintClientToken).toHaveBeenCalledWith("app_abcd1234", 60);
    });
    expect(await screen.findByText(/连通正常：指标 outp_feeamount_day dry-run 通过/)).toBeInTheDocument();
  });

  it("编辑客户端：预填表单 → 修改授权域 → 保存透传 PUT", async () => {
    const user = userEvent.setup();
    mockedUpdateApiClient.mockResolvedValue({ ...ACTIVE_CLIENT, scope_domain: "outp" });
    render(<ApiClients />);
    await screen.findByText("app_abcd1234");

    await user.click(screen.getByText("编辑"));
    expect(await screen.findByText(/编辑 API 客户端/)).toBeInTheDocument();

    // 修改授权域（编辑弹窗有两个下拉：授权域 + 指标白名单多选；取第一个 combobox）
    const combos = screen.getAllByRole("combobox");
    await user.click(combos[0]);
    await user.click(await screen.findByTitle("门诊 (outp)"));
    await user.click(await screen.findByText(/保\s*存/));

    await waitFor(() => {
      expect(mockedUpdateApiClient).toHaveBeenCalledWith(
        "app_abcd1234",
        expect.objectContaining({ scope_domain: "outp", qps: 20, daily_quota: 100000 }),
      );
    });
  });

  it("编辑客户端：指标白名单为下拉多选，追加指标后保存透传数组", async () => {
    const user = userEvent.setup();
    const existing = { ...ACTIVE_CLIENT, metric_whitelist: ["outp_feeamount_day"] };
    mockedListApiClients.mockResolvedValue([existing]);
    mockedUpdateApiClient.mockResolvedValue(existing);
    render(<ApiClients />);
    await screen.findByText("app_abcd1234");

    await user.click(screen.getByText("编辑"));
    expect(await screen.findByText(/编辑 API 客户端/)).toBeInTheDocument();

    // 指标白名单多选：回填已有值，再从下拉追加一个指标
    const combos = screen.getAllByRole("combobox");
    await user.click(combos[1]);
    await user.click(await screen.findByTitle("outp_doctor_cnt（医生数）"));
    await user.click(await screen.findByText(/保\s*存/));

    await waitFor(() => {
      expect(mockedUpdateApiClient).toHaveBeenCalledWith(
        "app_abcd1234",
        expect.objectContaining({ metric_whitelist: ["outp_feeamount_day", "outp_doctor_cnt"] }),
      );
    });
  });

  it("编辑客户端：清空授权域与白名单后保存 → PUT 携带空串/空数组（而非 null，避免后端视为不修改而静默失效）", async () => {
    const user = userEvent.setup();
    const existing = { ...ACTIVE_CLIENT, scope_domain: "outp", metric_whitelist: ["outp_feeamount_day"] };
    mockedListApiClients.mockResolvedValue([existing]);
    mockedUpdateApiClient.mockResolvedValue(existing);
    render(<ApiClients />);
    await screen.findByText("app_abcd1234");

    await user.click(screen.getByText("编辑"));
    const modal = await screen.findByRole("dialog");
    await within(modal).findByText(/编辑 API 客户端/);

    // 清空授权域下拉（allowClear 的清除按钮）与白名单多选（第二个 clear）
    const clears = modal.querySelectorAll(".ant-select-clear");
    expect(clears.length).toBeGreaterThanOrEqual(2);
    await user.click(clears[0]);
    await user.click(clears[1]);

    await user.click(await within(modal).findByText(/保\s*存/));

    await waitFor(() => {
      expect(mockedUpdateApiClient).toHaveBeenCalledWith(
        "app_abcd1234",
        expect.objectContaining({ scope_domain: "", metric_whitelist: [] }),
      );
    });
  });

  it("停用客户端：Popconfirm 确认后透传 PATCH status", async () => {
    const user = userEvent.setup();
    mockedUpdateApiClientStatus.mockResolvedValue({ ...ACTIVE_CLIENT, status: "REVOKED" });
    render(<ApiClients />);
    await screen.findByText("app_abcd1234");

    await user.click(screen.getByText(/停\s*用/));
    await user.click(await screen.findByRole("button", { name: /确\s*认/ }));

    await waitFor(() => {
      expect(mockedUpdateApiClientStatus).toHaveBeenCalledWith("app_abcd1234", "REVOKED");
    });
  });

  it("删除客户端：Popconfirm 确认后透传 DELETE", async () => {
    const user = userEvent.setup();
    mockedDeleteApiClient.mockResolvedValue({ deleted: true });
    render(<ApiClients />);
    await screen.findByText("app_abcd1234");

    await user.click(screen.getByText("删除"));
    await user.click(await screen.findByRole("button", { name: /确\s*认/ }));

    await waitFor(() => {
      expect(mockedDeleteApiClient).toHaveBeenCalledWith("app_abcd1234");
    });
  });

  it("批量操作：勾选 → 批量停用 → 透传 batch 端点", async () => {
    const user = userEvent.setup();
    mockedBatchApiClientAction.mockResolvedValue({ action: "disable", ok_count: 1, fail_count: 0, results: [{ client_id: "app_abcd1234", ok: true, status: "REVOKED" }] });
    render(<ApiClients />);
    await screen.findByText("app_abcd1234");

    // 勾选首行
    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[0]);

    // 打开批量操作菜单
    await user.click(screen.getByText(/批量操作/));
    await user.click(await screen.findByText("批量停用"));

    await waitFor(() => {
      expect(mockedBatchApiClientAction).toHaveBeenCalledWith({ action: "disable", client_ids: ["app_abcd1234"] });
    });
    await waitFor(() => {
      expect(screen.getByText(/批量停用成功：1 个客户端/)).toBeInTheDocument();
    });
  });
});
