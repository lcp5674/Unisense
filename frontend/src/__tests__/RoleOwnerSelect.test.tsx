import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import RoleOwnerSelect from "../components/RoleOwnerSelect";

const users = [
  { id: 1, username: "alice", display_name: "爱丽丝", role: "metric_owner", domain: "sales", status: "active" },
  { id: 2, username: "bob", display_name: "鲍勃", role: "metric_owner", domain: "sales", status: "active" },
];

describe("RoleOwnerSelect（责任方：平台用户 + 外部人员名称兜底）", () => {
  it("选择平台用户 → onChange 返回 { id, name: null }", async () => {
    const onChange = vi.fn();
    render(<RoleOwnerSelect users={users} value={undefined} onChange={onChange} />);
    const input = document.querySelector(".ant-select-selection-search-input") as HTMLInputElement;
    fireEvent.mouseDown(input);
    await waitFor(() => {
      const option = document.querySelector(".ant-select-item-option[title='爱丽丝（1）']") as HTMLElement;
      expect(option).toBeTruthy();
      fireEvent.click(option);
    });
    expect(onChange).toHaveBeenCalledWith({ id: 1, name: null });
  });

  it("自由输入外部人员名称 → 出现「外部人员」项，选中返回 { id: null, name }", async () => {
    const onChange = vi.fn();
    render(<RoleOwnerSelect users={users} value={undefined} onChange={onChange} />);
    const input = document.querySelector(".ant-select-selection-search-input") as HTMLInputElement;
    fireEvent.mouseDown(input);
    fireEvent.change(input, { target: { value: "张外部" } });
    await waitFor(() => {
      const option = document.querySelector(
        ".ant-select-item-option[title='外部人员：张外部']",
      ) as HTMLElement;
      expect(option).toBeTruthy();
      fireEvent.click(option);
    });
    expect(onChange).toHaveBeenCalledWith({ id: null, name: "张外部" });
  });

  it("回显外部人员名称：value={name} 展示文本而非原始 token", () => {
    render(<RoleOwnerSelect users={users} value={{ id: null, name: "张外部" }} onChange={() => {}} />);
    expect(screen.getByText("张外部")).toBeTruthy();
    expect(screen.queryByText("text:张外部")).toBeNull();
  });

  it("回显平台用户：value={id} 解析为用户名", () => {
    render(<RoleOwnerSelect users={users} value={{ id: 2, name: null }} onChange={() => {}} />);
    expect(screen.getByText("鲍勃（2）")).toBeTruthy();
  });

  it("清空 → onChange undefined（解除责任方）", async () => {
    const onChange = vi.fn();
    render(<RoleOwnerSelect users={users} value={{ id: 1, name: null }} onChange={onChange} />);
    const clearBtn = document.querySelector(".ant-select-clear") as HTMLElement;
    expect(clearBtn).toBeTruthy();
    fireEvent.mouseDown(clearBtn);
    expect(onChange).toHaveBeenCalledWith(undefined);
  });
});
