import { describe, it, expect, vi, beforeEach, type MockInstance } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { message } from "antd";
import { CodeValue, ellipsizeCode, CODE_ELLIPSIS_MAX, CODE_EXTREME_LONG } from "../components/CodeValue";

// 公共组件 CodeValue：全站超长标识（指标编码/表名/节点 ID）统一「单行中间省略 + hover 完整值 + 一键复制 + 可点击」。
// 视觉变体：code（等宽浅底）/ tag（边框 Tag）/ link（点击直达）。
const LONG_CODE = "outp_e2e_fee_day_2026_q3_retail_amount_sum_by_store_region";

describe("ellipsizeCode 纯函数", () => {
  it("不超过阈值返回原值", () => {
    expect(ellipsizeCode("abc")).toBe("abc");
    expect(ellipsizeCode("x".repeat(CODE_ELLIPSIS_MAX))).toBe("x".repeat(CODE_ELLIPSIS_MAX));
  });
  it("超过阈值保留首尾段（首段占比 60%）", () => {
    const out = ellipsizeCode(LONG_CODE);
    expect(out).toContain("…");
    expect(out.length).toBe(CODE_ELLIPSIS_MAX);
    // 首段保留前缀模式，尾段保留结尾特征
    expect(out.startsWith(LONG_CODE.slice(0, 10))).toBe(true);
    expect(out.endsWith(LONG_CODE.slice(-5))).toBe(true);
  });
});

describe("CodeValue 基础渲染", () => {
  it("短编码完整展示（不省略），aria-label 保留完整值", () => {
    render(<CodeValue value="gmv" />);
    const el = screen.getByText("gmv");
    expect(el.getAttribute("aria-label")).toBe("gmv");
    expect(el.classList.contains("code-value-long")).toBe(false);
  });
  it("长编码单行中间省略：DOM 文本非完整值，aria-label 为完整值", () => {
    const { container } = render(<CodeValue value={LONG_CODE} />);
    const el = container.querySelector(".code-value-long") as HTMLElement;
    expect(el).not.toBeNull();
    expect(el.getAttribute("aria-label")).toBe(LONG_CODE);
    expect(el.textContent).toContain("…");
    expect(el.textContent).not.toBe(LONG_CODE);
  });
  it("code 变体：等宽浅底样式类", () => {
    render(<CodeValue value="abc" code />);
    expect(screen.getByText("abc").classList.contains("code-value-code")).toBe(true);
  });
  it("link 变体：点击触发 onNavigate（含 stopPropagation）", () => {
    const onNav = vi.fn();
    render(<CodeValue value="gmv" target="/detail/gmv" onNavigate={onNav} />);
    const el = screen.getByText("gmv");
    expect(el.classList.contains("code-value-link")).toBe(true);
    fireEvent.click(el);
    expect(onNav).toHaveBeenCalledWith("/detail/gmv");
  });
});

describe("CodeValue tag 变体", () => {
  it("渲染为 Tag 容器（code-value-tag），短编码完整展示", () => {
    const { container } = render(<CodeValue value="gmv" tag />);
    const tag = container.querySelector(".code-value-tag");
    expect(tag).not.toBeNull();
    expect(tag?.textContent).toBe("gmv");
  });
  it("长编码省略 + hover 完整值 Tooltip", async () => {
    const { container } = render(<CodeValue value={LONG_CODE} tag />);
    const tag = container.querySelector(".code-value-tag") as HTMLElement;
    fireEvent.mouseEnter(tag);
    await waitFor(() => expect(screen.getByText(LONG_CODE)).toBeTruthy());
  });
});

describe("CodeValue 复制与极端长值", () => {
  let writeText: ReturnType<typeof vi.fn>;
  let msgSpy: MockInstance;
  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    msgSpy = vi.spyOn(message, "success").mockReturnValue({} as unknown as never);
  });
  it("长编码 hover 出 Tooltip，复制按钮一键复制完整值 + message 反馈", async () => {
    const { container } = render(<CodeValue value={LONG_CODE} />);
    const el = container.querySelector(".code-value-long") as HTMLElement;
    fireEvent.mouseEnter(el);
    const copyBtn = await screen.findByText(/复\s*制/);
    fireEvent.click(copyBtn);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(LONG_CODE));
    expect(msgSpy).toHaveBeenCalledWith(expect.stringContaining(LONG_CODE.slice(0, 20)));
    await waitFor(() => expect(screen.getByText("已复制")).toBeTruthy());
  });
  it("极端长值（>100 字符）Tooltip 加宽 + 内容可滚动兜底", async () => {
    const extreme = `${LONG_CODE}_${"x".repeat(CODE_EXTREME_LONG)}_2026`;
    const { container } = render(<CodeValue value={extreme} />);
    const el = container.querySelector(".code-value-long") as HTMLElement;
    fireEvent.mouseEnter(el);
    await waitFor(() => {
      const overlay = document.querySelector(".code-value-tip-overlay-wide");
      expect(overlay).not.toBeNull();
      expect(document.querySelector(".code-value-tip-text-extreme")).not.toBeNull();
    });
  });
});
