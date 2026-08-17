import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { App as AntApp } from "antd";
import { SensitiveRules } from "../pages/SensitiveRules";
import type { SensitiveRuleItem } from "../types";

vi.mock("../api", () => ({
  listSensitiveRules: vi.fn(),
  listSensitiveRuleCategories: vi.fn(),
  createSensitiveRule: vi.fn(),
  updateSensitiveRule: vi.fn(),
  setSensitiveRuleStatus: vi.fn(),
  deleteSensitiveRule: vi.fn(),
  validateSensitiveRegex: vi.fn(),
  testSensitiveRule: vi.fn(),
  classificationRescan: vi.fn(),
  batchSetSensitiveRuleStatus: vi.fn(),
  batchSetSensitiveRuleConfidence: vi.fn(),
  listDataSources: vi.fn(),
  fetchAssetTables: vi.fn(),
  fetchAssetEntityDetail: vi.fn(),
}));

import {
  listSensitiveRules, listSensitiveRuleCategories, createSensitiveRule,
  updateSensitiveRule, setSensitiveRuleStatus,
  validateSensitiveRegex, testSensitiveRule, classificationRescan,
  listDataSources, fetchAssetTables, fetchAssetEntityDetail,
} from "../api";
const mockedList = vi.mocked(listSensitiveRules);
const mockedCats = vi.mocked(listSensitiveRuleCategories);
const mockedCreate = vi.mocked(createSensitiveRule);
const mockedUpdate = vi.mocked(updateSensitiveRule);
const mockedStatus = vi.mocked(setSensitiveRuleStatus);
const mockedRegex = vi.mocked(validateSensitiveRegex);
const mockedTest = vi.mocked(testSensitiveRule);

const CATS = [
  { category: "ID_CARD", label: "身份证号", pii: true },
  { category: "PHONE", label: "手机/电话", pii: true },
  { category: "HEALTH", label: "健康医疗", pii: true },
  { category: "CREDENTIAL", label: "密码/密钥", pii: false },
];

const RULES: SensitiveRuleItem[] = [
  {
    rule_id: "id_card", label: "身份证号规则", category: "ID_CARD", category_label: "身份证号",
    name_re: "(id_?card|sfz|身份证)", sample_re: "^\\d{17}[\\dXx]$", confidence: 0.95,
    pii: true, source: "builtin", status: "active", updated_at: null,
  },
  {
    rule_id: "phone", label: "手机号规则", category: "PHONE", category_label: "手机/电话",
    name_re: "(phone|mobile|手机)", sample_re: null, confidence: 0.9,
    pii: true, source: "custom", status: "active", updated_at: "2026-08-17T00:00:00",
  },
  {
    rule_id: "password", label: "密码/密钥规则", category: "CREDENTIAL", category_label: "密码/密钥",
    name_re: "(password|pwd|密钥)", sample_re: null, confidence: 0.95,
    pii: false, source: "builtin", status: "inactive", updated_at: null,
  },
  {
    rule_id: "clinic", label: "诊所规则", category: "HEALTH", category_label: "健康医疗",
    name_re: "(clinic|hospital)", sample_re: null, confidence: 0.9,
    pii: true, source: "custom", status: "active", updated_at: "2026-08-17T00:00:00",
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue(RULES);
  mockedCats.mockResolvedValue(CATS);
  mockedRegex.mockResolvedValue({ valid: true, error: null });
  vi.mocked(listDataSources).mockResolvedValue({
    items: [], total: 0, page: 1, page_size: 20,
  });
});

function renderPage() {
  return render(
    <AntApp>
      <MemoryRouter initialEntries={["/sensitive-rules"]}>
        <SensitiveRules />
      </MemoryRouter>
    </AntApp>,
  );
}

// antd Select 可见选项（role=option 会匹配到隐藏无障碍 listbox，必须点 .ant-select-item-option-content）
async function pickVisibleOption(text: string) {
  const el = await waitFor(() => {
    const opts = Array.from(document.querySelectorAll<HTMLElement>(".ant-select-item-option-content"));
    const found = opts.find((e) => e.textContent?.trim() === text);
    expect(found).toBeTruthy();
    return found as HTMLElement;
  });
  fireEvent.click(el);
}

describe("SensitiveRules 配置台", () => {
  it("渲染规则列表：类别/来源/状态/统计卡", async () => {
    renderPage();
    await screen.findByText("身份证号规则");
    expect(screen.getByText("手机号规则")).toBeInTheDocument();
    // 类别 Tag
    expect(screen.getByText("身份证号")).toBeInTheDocument();
    expect(screen.getByText("手机/电话")).toBeInTheDocument();
    // 来源 / 状态
    expect(screen.getAllByText("内置").length).toBeGreaterThan(0);
    expect(screen.getAllByText("自定义").length).toBe(2);
    expect(screen.getAllByText("停用").length).toBeGreaterThan(0);
    // 统计卡：生效 PII 3 条（id_card/phone/clinic），生效机密 0 条（password 停用）
    expect(screen.getByText("3 条")).toBeInTheDocument();
    expect(mockedList).toHaveBeenCalledTimes(1);
  });

  it("新增规则：打开弹窗、填写、提交调用 createSensitiveRule", async () => {
    renderPage();
    await screen.findByText("身份证号规则");
    fireEvent.click(screen.getByRole("button", { name: /新增规则/ }));
    await screen.findByText("新增敏感规则");
    // 填规则名 + 类别 + 正则
    fireEvent.change(screen.getByPlaceholderText("如 手机号规则"), { target: { value: "邮箱规则" } });
    fireEvent.mouseDown(screen.getByRole("combobox"));
    // 下拉选项与表格类别 Tag 同名：取最后匹配（下拉浮层在文档末尾渲染）
    const catOptions = await screen.findAllByText("手机/电话");
    fireEvent.click(catOptions[catOptions.length - 1]);
    fireEvent.change(
      screen.getByPlaceholderText("如 (phone|mobile|手机|电话) —— 匹配字段名或注释"),
      { target: { value: "(email|邮箱)" } },
    );
    mockedCreate.mockResolvedValue({
      rule_id: "email", label: "邮箱规则", category: "PHONE", category_label: "手机/电话",
      name_re: "(email|邮箱)", sample_re: null, confidence: 0.85, pii: true,
      source: "custom", status: "active", updated_at: null,
    });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockedCreate).toHaveBeenCalledWith(
        expect.objectContaining({ label: "邮箱规则", name_re: "(email|邮箱)", pii: true }),
      );
    });
  });

  it("正则非法时即时提示错误", async () => {
    mockedRegex.mockResolvedValue({ valid: false, error: "missing ), unterminated subpattern" });
    renderPage();
    await screen.findByText("身份证号规则");
    fireEvent.click(screen.getByRole("button", { name: /新增规则/ }));
    await screen.findByText("新增敏感规则");
    fireEvent.change(
      screen.getByPlaceholderText("如 (phone|mobile|手机|电话) —— 匹配字段名或注释"),
      { target: { value: "(phone" } },
    );
    await screen.findByText(/语法错误/);
  });

  it("编辑规则预填并提交调用 updateSensitiveRule", async () => {
    renderPage();
    await screen.findByText("身份证号规则");
    const rows = screen.getAllByRole("row");
    const phoneRow = rows.find((r) => within(r).queryByText("手机号规则"));
    expect(phoneRow).toBeTruthy();
    fireEvent.click(within(phoneRow!).getByRole("button", { name: /编\s*辑/ }));
    await screen.findByText("编辑规则：手机号规则");
    // 预填值
    expect(screen.getByDisplayValue("手机号规则")).toBeInTheDocument();
    mockedUpdate.mockResolvedValue({ ...RULES[1], name_re: "(mobile2)" });
    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockedUpdate).toHaveBeenCalledWith(
        "phone",
        expect.objectContaining({ name_re: "(phone|mobile|手机)", pii: true }),
      );
    });
  });

  it("停用规则调用 setSensitiveRuleStatus(deactivate)", async () => {
    renderPage();
    await screen.findByText("身份证号规则");
    const rows = screen.getAllByRole("row");
    const phoneRow = rows.find((r) => within(r).queryByText("手机号规则"));
    mockedStatus.mockResolvedValue({ ...RULES[1], status: "inactive" });
    fireEvent.click(within(phoneRow!).getByRole("button", { name: /停\s*用/ }));
    await waitFor(() => {
      expect(mockedStatus).toHaveBeenCalledWith("phone", "deactivate");
    });
  });

  it("自定义规则可删除，内置规则无删除按钮", async () => {
    renderPage();
    await screen.findByText("身份证号规则");
    const rows = screen.getAllByRole("row");
    const clinicRow = rows.find((r) => within(r).queryByText("诊所规则"));
    const customDanger = within(clinicRow!).getAllByRole("button")
      .filter((b) => b.classList.contains("ant-btn-dangerous"));
    expect(customDanger.length).toBe(1);
    // 内置规则行无删除（danger 图标按钮）
    const builtinRow = rows.find((r) => within(r).queryByText("身份证号规则"));
    const builtinDanger = within(builtinRow!).getAllByRole("button")
      .filter((b) => b.classList.contains("ant-btn-dangerous"));
    expect(builtinDanger.length).toBe(0);
  });

  it("规则测试台：选择表→字段联动识别出 PII 并展示命中明细", async () => {
    vi.mocked(fetchAssetTables).mockResolvedValue({
      items: [{ id: 1, entity_name: "ods_user", source_name: "MySQL主库" }],
      total: 1,
    } as never);
    vi.mocked(fetchAssetEntityDetail).mockResolvedValue({
      schema_summary: [{ name: "mobile", comment: "客户手机号" }],
    } as never);
    mockedTest.mockResolvedValue({
      sensitivity_level: "PII",
      hits: [
        {
          column: "mobile", category: "PHONE", category_label: "手机/电话", rule: "phone",
          confidence: 0.9, matched_by: "name+sample", pii: true,
        },
      ],
    });
    renderPage();
    await screen.findByText("身份证号规则");
    fireEvent.click(screen.getByRole("button", { name: /规则测试台/ }));
    await screen.findByText("运行识别");
    // 表/视图下拉：选表 → 触发字段加载
    const modal = screen.getByRole("dialog");
    const combos = within(modal).getAllByRole("combobox");
    fireEvent.mouseDown(combos[0]);
    await pickVisibleOption("ods_user（MySQL主库）");
    await waitFor(() => expect(fetchAssetEntityDetail).toHaveBeenCalledWith(1));
    // 等字段 Select 变为可用再点开（React 状态刷新时序）
    await waitFor(() => {
      expect(within(modal).getAllByRole("combobox")[1]).not.toBeDisabled();
    });
    fireEvent.mouseDown(within(modal).getAllByRole("combobox")[1]);
    await pickVisibleOption("mobile（客户手机号）");
    // 取值样本
    fireEvent.change(within(modal).getByPlaceholderText("如 13812345678"), { target: { value: "13812345678" } });
    fireEvent.click(screen.getByRole("button", { name: /运行识别/ }));
    await screen.findByText("PII（个人可识别）");
    expect(screen.getByText("命中规则 phone")).toBeInTheDocument();
    expect(screen.getByText("字段名+样本命中")).toBeInTheDocument();
    expect(mockedTest).toHaveBeenCalledWith(
      expect.objectContaining({ entity_name: "ods_user", column_name: "mobile", sample_value: "13812345678" }),
    );
  });

  it("测试台未命中时显示内部数据", async () => {
    vi.mocked(fetchAssetTables).mockResolvedValue({
      items: [{ id: 1, entity_name: "ods_user", source_name: "MySQL主库" }],
      total: 1,
    } as never);
    vi.mocked(fetchAssetEntityDetail).mockResolvedValue({
      schema_summary: [{ name: "amount", comment: null }],
    } as never);
    mockedTest.mockResolvedValue({ sensitivity_level: "INTERNAL", hits: [] });
    renderPage();
    await screen.findByText("身份证号规则");
    fireEvent.click(screen.getByRole("button", { name: /规则测试台/ }));
    await screen.findByText("运行识别");
    const modal = screen.getByRole("dialog");
    const combos = within(modal).getAllByRole("combobox");
    fireEvent.mouseDown(combos[0]);
    await pickVisibleOption("ods_user（MySQL主库）");
    await waitFor(() => expect(fetchAssetEntityDetail).toHaveBeenCalledWith(1));
    await waitFor(() => {
      expect(within(modal).getAllByRole("combobox")[1]).not.toBeDisabled();
    });
    fireEvent.mouseDown(within(modal).getAllByRole("combobox")[1]);
    await pickVisibleOption("amount");
    fireEvent.click(screen.getByRole("button", { name: /运行识别/ }));
    await screen.findByText("内部");
    expect(mockedTest).toHaveBeenCalledWith(
      expect.objectContaining({ entity_name: "ods_user", column_name: "amount" }),
    );
  });

  it("规则搜索：按规则名/标识/类别/正则过滤", async () => {
    renderPage();
    await screen.findByText("身份证号规则");
    // 全部 4 条规则可见
    expect(screen.getByText("诊所规则")).toBeInTheDocument();
    // 搜索「诊所」→ 只留 1 条
    fireEvent.change(screen.getByPlaceholderText("搜索规则名 / 标识 / 类别 / 正则"), {
      target: { value: "诊所" },
    });
    await waitFor(() => {
      expect(screen.queryByText("身份证号规则")).not.toBeInTheDocument();
    });
    expect(screen.getByText("诊所规则")).toBeInTheDocument();
    // 按标识搜 phone（匹配规则名 phone 的标识列）
    fireEvent.change(screen.getByPlaceholderText("搜索规则名 / 标识 / 类别 / 正则"), {
      target: { value: "phone" },
    });
    await waitFor(() => {
      expect(screen.getByText("手机号规则")).toBeInTheDocument();
    });
    expect(screen.queryByText("诊所规则")).not.toBeInTheDocument();
  });

  it("批量停用：勾选多行 → 调用 batchSetSensitiveRuleStatus", async () => {
    const mockedBatchStatus = vi.mocked(
      (await import("../api")).batchSetSensitiveRuleStatus,
    );
    mockedBatchStatus.mockResolvedValue({ action: "deactivate", succeeded: ["phone", "clinic"], failed: [] });
    renderPage();
    await screen.findByText("身份证号规则");
    // 勾选 phone 与 clinic 两行
    const rows = screen.getAllByRole("row");
    const phoneRow = rows.find((r) => within(r).queryByText("手机号规则"));
    const clinicRow = rows.find((r) => within(r).queryByText("诊所规则"));
    fireEvent.click(within(phoneRow!).getByRole("checkbox"));
    fireEvent.click(within(clinicRow!).getByRole("checkbox"));
    await screen.findByText("已选 2 条");
    fireEvent.click(screen.getByRole("button", { name: /批量停用/ }));
    await waitFor(() => {
      expect(mockedBatchStatus).toHaveBeenCalledWith(["phone", "clinic"], "deactivate");
    });
  });

  it("批量启用：勾选停用行 → 调用 batchSetSensitiveRuleStatus(activate)", async () => {
    const mockedBatchStatus = vi.mocked(
      (await import("../api")).batchSetSensitiveRuleStatus,
    );
    mockedBatchStatus.mockResolvedValue({ action: "activate", succeeded: ["password"], failed: [] });
    renderPage();
    await screen.findByText("身份证号规则");
    const rows = screen.getAllByRole("row");
    const pwdRow = rows.find((r) => within(r).queryByText("密码/密钥规则"));
    fireEvent.click(within(pwdRow!).getByRole("checkbox"));
    await screen.findByText("已选 1 条");
    fireEvent.click(screen.getByRole("button", { name: /批量启用/ }));
    await waitFor(() => {
      expect(mockedBatchStatus).toHaveBeenCalledWith(["password"], "activate");
    });
  });

  it("批量置信度：弹窗设置 → 调用 batchSetSensitiveRuleConfidence", async () => {
    const mockedBatchConf = vi.mocked(
      (await import("../api")).batchSetSensitiveRuleConfidence,
    );
    mockedBatchConf.mockResolvedValue({ confidence: 0.85, succeeded: ["phone"], failed: [] });
    renderPage();
    await screen.findByText("身份证号规则");
    const rows = screen.getAllByRole("row");
    const phoneRow = rows.find((r) => within(r).queryByText("手机号规则"));
    fireEvent.click(within(phoneRow!).getByRole("checkbox"));
    await screen.findByText("已选 1 条");
    fireEvent.click(screen.getByRole("button", { name: /批量置信度/ }));
    await screen.findByText("批量设置置信度（1 条）");
    fireEvent.click(screen.getByRole("button", { name: /应\s*用/ }));
    await waitFor(() => {
      expect(mockedBatchConf).toHaveBeenCalledWith(["phone"], 0.85);
    });
  });

  it("按新规则重扫：数据源多选 → 调用 classificationRescan(source_ids)", async () => {
    const mockedRescan = vi.mocked(classificationRescan);
    vi.mocked(listDataSources).mockResolvedValue({
      items: [
        { source_id: "ds1", name: "MySQL主库", source_type: "mysql" },
        { source_id: "ds2", name: "PG仓", source_type: "postgres" },
      ],
      total: 2,
    } as never);
    mockedRescan.mockResolvedValue({ scanned: 10, changed: 2, pii_found: 3, degraded: 0 });
    renderPage();
    await screen.findByText("身份证号规则");
    fireEvent.click(screen.getByRole("button", { name: /按新规则重扫/ }));
    await screen.findByText("数据源（可多选，留空重扫全部）");
    const modal = screen.getByRole("dialog");
    fireEvent.mouseDown(within(modal).getAllByRole("combobox")[0]);
    await pickVisibleOption("MySQL主库（mysql）");
    await pickVisibleOption("PG仓（postgres）");
    fireEvent.click(screen.getByRole("button", { name: /开始重扫/ }));
    await waitFor(() => {
      expect(mockedRescan).toHaveBeenCalledWith(
        expect.objectContaining({ source_ids: ["ds1", "ds2"] }),
      );
    });
    await screen.findByText(/扫描 10/);
  });
});
