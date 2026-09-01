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
} from "../api";
const mockedListApiClients = vi.mocked(listApiClients);
const mockedMintClientToken = vi.mocked(mintClientToken);
const mockedConsumeDryRun = vi.mocked(consumeDryRun);
const mockedListMetrics = vi.mocked(listMetrics);
const mockedGetConsumeToken = vi.mocked(getConsumeToken);

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
});
