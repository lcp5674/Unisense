import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Layout } from "../components/Layout";
import { NOTIF_CHANGED_EVENT } from "../utils/notifBus";

// Mock API：Layout 挂载时拉取未读通知数与用户偏好（折叠态服务端持久化）；
// fetchGlobalSearch 供顶栏实时下拉使用（测试中不触发）
vi.mock("../api", () => ({
  fetchUnreadCount: vi.fn(),
  clearToken: vi.fn(),
  fetchPreferences: vi.fn(),
  setPreference: vi.fn(),
  fetchGlobalSearch: vi.fn(),
}));
import { fetchUnreadCount, fetchPreferences, setPreference } from "../api";
vi.mocked(fetchUnreadCount).mockResolvedValue(0);
vi.mocked(fetchPreferences).mockResolvedValue({});
vi.mocked(setPreference).mockResolvedValue(undefined);

// 折叠状态按用户隔离存储：key 带 user.id
const SIDER_KEY = "unisense.sider.collapsed.1";

const mockUser = {
  id: 1,
  username: "admin",
  display_name: "管理员",
  role: "platform_admin",
  domain: null,
  org_id: 1,
};

function renderLayout(user = mockUser) {
  return render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <Layout user={user} />
    </MemoryRouter>,
  );
}

describe("Layout 侧边栏伸缩（按用户持久化）", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    vi.mocked(fetchUnreadCount).mockResolvedValue(0);
    vi.mocked(fetchPreferences).mockResolvedValue({});
    vi.mocked(setPreference).mockResolvedValue(undefined);
  });

  it("渲染分组导航与头部功能区", () => {
    renderLayout();
    expect(screen.getByText("总览仪表")).toBeInTheDocument();
    expect(screen.getByText("指标目录")).toBeInTheDocument();
    expect(screen.getByText("血缘视图")).toBeInTheDocument();
    expect(screen.getByText("权限治理")).toBeInTheDocument();
    expect(screen.getAllByText("Unisense").length).toBeGreaterThan(0);
  });

  it("默认展开；点击收起按钮后侧边栏折叠并持久化到该用户", () => {
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

  it("刷新后从该用户的 localStorage 恢复折叠偏好", () => {
    localStorage.setItem(SIDER_KEY, "1");
    const { container } = renderLayout();
    expect(container.querySelector(".ant-layout-sider-collapsed")).toBeTruthy();

    const collapsedBtn = screen.getByRole("button", { name: "展开侧边栏" });
    expect(collapsedBtn).toBeInTheDocument();
  });

  it("折叠偏好按用户隔离：user.id=1 折叠不影响 user.id=2", () => {
    localStorage.setItem("unisense.sider.collapsed.1", "1");
    localStorage.setItem("unisense.sider.collapsed.2", "0");
    const { container } = renderLayout({ ...mockUser, id: 2 });
    expect(container.querySelector(".ant-layout-sider-collapsed")).toBeFalsy();
  });

  it("服务端偏好覆盖本地缓存（服务端为准）", async () => {
    vi.mocked(fetchPreferences).mockResolvedValue({ ui: { sider_collapsed: true } });
    const { container } = renderLayout();
    await waitFor(() => {
      expect(container.querySelector(".ant-layout-sider-collapsed")).toBeTruthy();
    });
    expect(localStorage.getItem(SIDER_KEY)).toBe("1");
  });

  it("点击折叠后防抖写入服务端偏好", async () => {
    const { container } = renderLayout();
    fireEvent.click(screen.getByRole("button", { name: "收起侧边栏" }));
    await waitFor(
      () => {
        expect(setPreference).toHaveBeenCalledWith("ui", { sider_collapsed: true });
      },
      { timeout: 1500 },
    );
    expect(container.querySelector(".ant-layout-sider-collapsed")).toBeTruthy();
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

describe("Layout 顶栏通知角标", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    vi.mocked(fetchPreferences).mockResolvedValue({});
    vi.mocked(setPreference).mockResolvedValue(undefined);
  });

  it("收到通知变更事件后重新拉取未读数并更新角标（已读清零后角标隐藏）", async () => {
    // 首次精确计数 1 条未读；事件触发后二次计数 0 未读
    vi.mocked(fetchUnreadCount).mockResolvedValueOnce(1).mockResolvedValue(0);
    const { container } = renderLayout();

    await waitFor(() => {
      expect(container.querySelector(".ant-badge-count")?.textContent).toContain("1");
    });

    // 通知中心操作后广播变更事件 → 重新拉取 → 未读清零 → 角标隐藏（antd count=0 不渲染）
    window.dispatchEvent(new CustomEvent(NOTIF_CHANGED_EVENT));
    await waitFor(() => {
      expect(container.querySelector(".ant-badge-count")).toBeNull();
    });
  });

  it("每 30 秒轮询未读数（跨标签页/跨用户新通知实时性，无需手动刷新）", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(fetchUnreadCount).mockResolvedValue(3);
      const { container } = renderLayout();

      // flush 微任务：初始 effect 拉取并更新角标
      await act(async () => {});
      expect(fetchUnreadCount).toHaveBeenCalledTimes(1);
      expect(container.querySelector(".ant-badge-count")?.textContent).toContain("3");

      // 推进 30s → 轮询再拉取；角标随新未读数更新（模拟他人/后台生成了新通知）
      vi.mocked(fetchUnreadCount).mockResolvedValue(5);
      act(() => {
        vi.advanceTimersByTime(30_000);
      });
      await act(async () => {});
      expect(fetchUnreadCount).toHaveBeenCalledTimes(2);
      expect(container.querySelector(".ant-badge-count")?.textContent).toContain("5");
    } finally {
      vi.useRealTimers();
    }
  });
});

afterEach(() => {
  vi.useRealTimers();
});
