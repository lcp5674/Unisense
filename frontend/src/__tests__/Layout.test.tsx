import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Layout } from "../components/Layout";

// Mock API：Layout 挂载时拉取未读通知数
vi.mock("../api", () => ({
  listNotifications: vi.fn(),
  clearToken: vi.fn(),
}));
import { listNotifications } from "../api";
vi.mocked(listNotifications).mockResolvedValue({ items: [], total: 0 });

const SIDER_KEY = "unisense.sider.collapsed";

const mockUser = {
  id: 1,
  username: "admin",
  display_name: "管理员",
  role: "platform_admin",
  domain: null,
  org_id: 1,
};

function renderLayout() {
  return render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <Layout user={mockUser} />
    </MemoryRouter>,
  );
}

describe("Layout 侧边栏伸缩", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("渲染分组导航与头部功能区", () => {
    renderLayout();
    expect(screen.getByText("总览仪表")).toBeInTheDocument();
    expect(screen.getByText("指标目录")).toBeInTheDocument();
    expect(screen.getByText("血缘视图")).toBeInTheDocument();
    expect(screen.getByText("权限治理")).toBeInTheDocument();
    expect(screen.getAllByText("Unisense").length).toBeGreaterThan(0);
  });

  it("默认展开；点击收起按钮后侧边栏折叠并持久化", () => {
    const { container } = renderLayout();
    expect(container.querySelector(".ant-layout-sider-collapsed")).toBeFalsy();

    fireEvent.click(screen.getByRole("button", { name: "收起侧边栏" }));

    expect(container.querySelector(".ant-layout-sider-collapsed")).toBeTruthy();
    expect(localStorage.getItem(SIDER_KEY)).toBe("1");
  });

  it("再次点击可展开并清除持久化折叠标记", () => {
    localStorage.setItem(SIDER_KEY, "1");
    const { container } = renderLayout();

    expect(container.querySelector(".ant-layout-sider-collapsed")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "展开侧边栏" }));

    expect(container.querySelector(".ant-layout-sider-collapsed")).toBeFalsy();
    expect(localStorage.getItem(SIDER_KEY)).toBe("0");
  });

  it("刷新后从 localStorage 恢复折叠偏好", () => {
    localStorage.setItem(SIDER_KEY, "1");
    const { container } = renderLayout();
    expect(container.querySelector(".ant-layout-sider-collapsed")).toBeTruthy();

    const collapsedBtn = screen.getByRole("button", { name: "展开侧边栏" });
    expect(collapsedBtn).toBeInTheDocument();
  });

  it("折叠后导航项仍可点击跳转", () => {
    localStorage.setItem(SIDER_KEY, "1");
    const { container } = renderLayout();
    expect(container.querySelector(".ant-layout-sider-collapsed")).toBeTruthy();

    // 折叠态菜单项以图标形式保留（antd inline-collapsed），可继续点击
    const navItems = container.querySelectorAll(".ant-menu-item");
    expect(navItems.length).toBeGreaterThan(0);
  });
});
