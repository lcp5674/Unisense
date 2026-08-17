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
}));

import {
  listSensitiveRules, listSensitiveRuleCategories, createSensitiveRule,
  updateSensitiveRule, setSensitiveRuleStatus,
  validateSensitiveRegex, testSensitiveRule,
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

  it("规则测试台：输入字段名识别出 PII 并展示命中明细", async () => {
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
    fireEvent.change(screen.getByPlaceholderText("如 mobile / 手机号"), { target: { value: "mobile" } });
    fireEvent.change(screen.getByPlaceholderText("如 13812345678"), { target: { value: "13812345678" } });
    fireEvent.click(screen.getByRole("button", { name: /运行识别/ }));
    await screen.findByText("PII（个人可识别）");
    expect(screen.getByText("命中规则 phone")).toBeInTheDocument();
    expect(screen.getByText("字段名+样本命中")).toBeInTheDocument();
    expect(mockedTest).toHaveBeenCalledWith(
      expect.objectContaining({ column_name: "mobile", sample_value: "13812345678" }),
    );
  });

  it("测试台未命中时显示内部数据", async () => {
    mockedTest.mockResolvedValue({ sensitivity_level: "INTERNAL", hits: [] });
    renderPage();
    await screen.findByText("身份证号规则");
    fireEvent.click(screen.getByRole("button", { name: /规则测试台/ }));
    await screen.findByText("运行识别");
    fireEvent.change(screen.getByPlaceholderText("如 mobile / 手机号"), { target: { value: "amount" } });
    fireEvent.click(screen.getByRole("button", { name: /运行识别/ }));
    await screen.findByText("内部");
  });
});
