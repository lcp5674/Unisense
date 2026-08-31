import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { App as AntApp } from "antd";
import MetricImportModal from "../components/MetricImportModal";
import { importMetricsCsv, downloadMetricImportTemplate, UnisenseApiError } from "../api";

vi.mock("../api", () => ({
  importMetricsCsv: vi.fn(),
  downloadMetricImportTemplate: vi.fn(),
  UnisenseApiError: class UnisenseApiError extends Error {
    code: string;
    status: number;
    detail: string;
    constructor(message: string, code = "HTTP_ERROR", status = 400, detail = "") {
      super(message);
      this.code = code;
      this.status = status;
      this.detail = detail;
    }
  },
}));

const mockedImport = vi.mocked(importMetricsCsv);
const mockedDownload = vi.mocked(downloadMetricImportTemplate);

const DOMAIN_OPTIONS = [
  { value: "online_consultation", label: "在线问诊 (online_consultation)" },
  { value: "outp", label: "门诊 (outp)" },
];

function renderModal(open = true) {
  return render(
    <AntApp>
      <MetricImportModal open={open} onClose={() => {}} domainOptions={DOMAIN_OPTIONS} />
    </AntApp>,
  );
}

/** 打开页面第一个 antd Select 并点选指定 label 的选项（与 MetricCreate 测试同款交互）。 */
async function pickDomainOption(label: string) {
  const selector = document.querySelector(".ant-select-selector") as HTMLElement;
  fireEvent.mouseDown(selector);
  await waitFor(() => {
    const dropdown = document.querySelector(
      ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
    ) as HTMLElement | null;
    const option = dropdown?.querySelector(
      `.ant-select-item-option[title="${label}"]`,
    ) as HTMLElement | null;
    expect(option).toBeTruthy();
    if (option) fireEvent.click(option);
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedDownload.mockResolvedValue(undefined);
  mockedImport.mockResolvedValue({
    batch_id: "b1",
    candidates: [
      { metric_code: "outp_gmv_day", status: "DRAFT" },
      { metric_code: "bad_code", status: "FAILED", validation_errors: ["缺少数仓开发责任方"] },
    ],
  });
});

describe("MetricImportModal 批量导入弹窗（CSV / Excel 共用）", () => {
  it("打开时展示模板下载（CSV/Excel）、域下拉（中文 label）与上传入口（accept 含 .xlsx）", async () => {
    renderModal();
    expect(screen.getByText("批量导入指标")).toBeTruthy();
    expect(screen.getByText("下载 CSV 模板")).toBeTruthy();
    expect(screen.getByText("下载 Excel 模板")).toBeTruthy();
    expect(screen.getByText("选择目标域")).toBeTruthy();
    // 上传入口 accept 同时支持 .csv 与 .xlsx
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).toBeTruthy();
    expect(input.accept).toContain(".xlsx");
    expect(input.accept).toContain(".csv");
    // 域下拉选项显示中文 label（value 仍为域 code）
    await pickDomainOption("门诊 (outp)");
    // 选中项以中文 label 展示（antd selection-item）
    expect(document.querySelector('.ant-select-selection-item[title="门诊 (outp)"]')).toBeTruthy();
  });

  it("下载 CSV / Excel 模板按钮分别调用带 format 的模板接口", () => {
    renderModal();
    fireEvent.click(screen.getByText("下载 CSV 模板"));
    expect(mockedDownload).toHaveBeenCalledWith("csv");
    fireEvent.click(screen.getByText("下载 Excel 模板"));
    expect(mockedDownload).toHaveBeenCalledWith("xlsx");
  });

  it("未选域上传 → 提示先选目标域且不调导入接口", async () => {
    renderModal();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["x"], "m.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(mockedImport).not.toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText(/请先选择目标域/)).toBeTruthy());
  });

  it("选域后上传 xlsx → 调导入接口（FormData 含 file+domain）并展示结果", async () => {
    renderModal();
    await pickDomainOption("门诊 (outp)");
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["x"], "m.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(mockedImport).toHaveBeenCalledTimes(1));
    const fd = mockedImport.mock.calls[0][0] as FormData;
    expect(fd.get("domain")).toBe("outp");
    expect(fd.get("file")).toBeTruthy();
    // 结果表格：成功/失败回显
    await waitFor(() => expect(screen.getByText("outp_gmv_day")).toBeTruthy());
    expect(screen.getByText("已创建（草稿）")).toBeTruthy();
    expect(screen.getByText("bad_code")).toBeTruthy();
  });

  it("上传失败（接口异常）→ 展示错误信息", async () => {
    mockedImport.mockRejectedValue(new UnisenseApiError("服务器未安装 openpyxl", "INVALID_XLSX", 400, ""));
    renderModal();
    await pickDomainOption("门诊 (outp)");
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["x"], "m.xlsx", { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(screen.getByText("服务器未安装 openpyxl")).toBeTruthy());
  });
});
