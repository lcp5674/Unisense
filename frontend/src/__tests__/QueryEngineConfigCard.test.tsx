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
    getQueryEngineSecrets: vi.fn(),
    saveQueryEngineConfig: vi.fn(),
    testQueryEngineConfig: vi.fn(),
    UnisenseApiError,
  };
});

import {
  getQueryEngineConfig,
  getQueryEngineSecrets,
  saveQueryEngineConfig,
  testQueryEngineConfig,
} from "../api";

const mockGet = vi.mocked(getQueryEngineConfig);
const mockSecrets = vi.mocked(getQueryEngineSecrets);
const mockSave = vi.mocked(saveQueryEngineConfig);
const mockTest = vi.mocked(testQueryEngineConfig);

function defaultSecrets(overrides: Record<string, unknown> = {}) {
  return {
    source: "db",
    doris_user: "root",
    doris_password: "doris-secret",
    mysql_fallback_url: "mysql+aiomysql://root:mysql-secret@mysql:3306/unisense",
    has_doris_password: true,
    has_mysql_fallback: true,
    ...overrides,
  };
}

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
      mysql_fallback_url_masked: "mysql+aiomysql://root:***@mysql:3306/unisense",
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
    mockSecrets.mockResolvedValue(defaultSecrets() as never);
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

  it("编辑并保存：回填 DB 行与密钥 → 修改 olap_url → 保存调用 saveQueryEngineConfig", async () => {
    renderCard();
    fireEvent.click(await screen.findByText("编辑配置"));
    const urlInput = (await screen.findByPlaceholderText(
      "http://doris-fe:8030",
    )) as HTMLInputElement;
    fireEvent.change(urlInput, { target: { value: "http://doris-new:9030" } });
    // 平台管理员打开编辑 → 密钥回填（密文显示，值在表单中）
    expect(await screen.findByDisplayValue("doris-secret")).toBeTruthy();
    fireEvent.click(screen.getByText("保 存") ?? screen.getByText("保存"));
    await waitFor(() => expect(mockSave).toHaveBeenCalledTimes(1));
    const payload = mockSave.mock.calls[0][0] as { olap_url: string };
    expect(payload.olap_url).toBe("http://doris-new:9030");
    // 已回填的当前密码随表单提交（后端同值重加密，不覆盖丢失）
    expect((mockSave.mock.calls[0][0] as { doris_password: string }).doris_password).toBe(
      "doris-secret",
    );
  });

  it("平台管理员打开编辑：回填已存密钥（密文），点击眼睛后明文展示", async () => {
    renderCard();
    fireEvent.click(await screen.findByText("编辑配置"));
    await waitFor(() => expect(mockSecrets).toHaveBeenCalledTimes(1));
    const pwdInput = (await screen.findByDisplayValue("doris-secret")) as HTMLInputElement;
    // 默认密文（type=password）
    expect(pwdInput.type).toBe("password");
    // 点击右侧眼睛 → 明文展示（type=text）
    const wrapper = pwdInput.closest(".ant-input-affix-wrapper");
    const eye = wrapper?.querySelector(".ant-input-password-icon");
    expect(eye).toBeTruthy();
    fireEvent.click(eye as HTMLElement);
    expect((pwdInput as HTMLInputElement).type).toBe("text");
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

  it("env 配置来源（无 DB 行）：编辑弹窗按生效值回填 + 接管提示 + 连接串回填", async () => {
    mockGet.mockResolvedValue({
      row: null,
      effective: {
        source: "env",
        olap_url: "http://doris-not-configured:8030/unisense",
        doris_host: "doris-not-configured",
        doris_port: 8030,
        doris_database: "unisense",
        doris_user: "readonly",
        has_doris_password: false,
        has_mysql_fallback: true,
        olap_configured: true,
        mysql_fallback_configured: true,
        mysql_fallback_url_masked:
          "mysql+aiomysql://e2e:***@mysql:3306/e2e_biz?charset=utf8mb4",
        updated_by: null,
        updated_at: null,
        note: "环境变量配置生效中（DB 未启用或未配置对应段）",
      },
      can_edit: true,
    } as never);
    mockSecrets.mockResolvedValue({
      source: "env",
      doris_user: "readonly",
      doris_password: "",
      mysql_fallback_url: "mysql+aiomysql://e2e:e2e_pass@mysql:3306/e2e_biz?charset=utf8mb4",
      has_doris_password: false,
      has_mysql_fallback: true,
    } as never);
    renderCard();
    fireEvent.click(await screen.findByText("编辑配置"));
    // 接管提示：当前来自环境变量、尚未写库
    expect(screen.getByText(/当前配置来自环境变量/)).toBeTruthy();
    // OLAP 主机按生效值回填（不再全空）
    const hostInput = (await screen.findByDisplayValue(
      "doris-not-configured",
    )) as HTMLInputElement;
    expect(hostInput.value).toBe("doris-not-configured");
    // 卡片/弹窗展示脱敏连接串（不含密码明文）
    expect(
      screen.getAllByText((c: string) =>
        c.includes("mysql+aiomysql://e2e:***@mysql:3306/e2e_biz"),
      ).length,
    ).toBeGreaterThanOrEqual(1);
    // 打开编辑即回填当前生效的降级连接串（Input.Password 密文，值为完整 URL）
    const urlField = (await screen.findByDisplayValue(
      "mysql+aiomysql://e2e:e2e_pass@mysql:3306/e2e_biz?charset=utf8mb4",
    )) as HTMLInputElement;
    expect(urlField.type).toBe("password");
    // 保存不改动 → payload 携带回填主机与降级连接串（接管为 DB 配置，无需重抄）
    fireEvent.click(screen.getByText("保 存") ?? screen.getByText("保存"));
    await waitFor(() => expect(mockSave).toHaveBeenCalledTimes(1));
    const payload = mockSave.mock.calls[0][0] as {
      doris_host: string;
      mysql_fallback_url: string;
    };
    expect(payload.doris_host).toBe("doris-not-configured");
    expect(payload.mysql_fallback_url).toBe(
      "mysql+aiomysql://e2e:e2e_pass@mysql:3306/e2e_biz?charset=utf8mb4",
    );
  });
});
