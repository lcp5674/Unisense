import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Layout } from "../components/Layout";
import { NOTIF_CHANGED_EVENT } from "../utils/notifBus";

// Mock API：Layout 挂载时拉取未读通知数与用户偏好（折叠态服务端持久化）；
// fetchGlobalSearch 供顶栏实时下拉使用（测试中不触发）；listBatchInferTasks 供
// 批量任务中心挂载探测使用（条件渲染用例断言「无读角色不调用」）
vi.mock("../api", () => ({
  fetchUnreadCount: vi.fn(),
  clearToken: vi.fn(),
  fetchPreferences: vi.fn(),
  setPreference: vi.fn(),
  fetchGlobalSearch: vi.fn(),
  listBatchInferTasks: vi.fn(),
}));
import { fetchUnreadCount, fetchPreferences, setPreference, listBatchInferTasks } from "../api";
vi.mocked(fetchUnreadCount).mockResolvedValue(0);
vi.mocked(fetchPreferences).mockResolvedValue({});
vi.mocked(setPreference).mockResolvedValue(undefined);
vi.mocked(listBatchInferTasks).mockResolvedValue([]);

// Mock usePermission：Layout 依赖权限快照做菜单过滤与批量任务中心条件渲染。
// 默认返回与真实 PermissionContext 默认一致的 fail-open 形态（can 全 true、snapshot=null），
// 使既有用例行为不变；条件渲染用例通过 setPerm 注入指定角色的 snapshot。
const { __perm } = vi.hoisted(() => {
  const state: { value: any } = {
    value: {
      can: () => true,
      canAny: () => true,
      canAll: () => true,
      snapshot: null,
      loading: false,
      error: false,
      refresh: async () => undefined,
    },
  };
  return { __perm: state };
});
vi.mock("../hooks/usePermission", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../hooks/usePermission")>();
  return { ...actual, usePermission: () => __perm.value };
});

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
    expect(screen.getAllByText("WeSemantics").length).toBeGreaterThan(0);
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

describe("Layout 批量任务中心按读角色条件渲染", () => {
  /** 注入指定角色的权限快照（roles=null 模拟快照未就绪 fail-open） */
  function setPerm(roles: string[] | null) {
    __perm.value = {
      can: () => true,
      canAny: () => true,
      canAll: () => true,
      snapshot: roles === null ? null : ({ role: roles[0], roles } as never),
      loading: false,
      error: false,
      refresh: async () => undefined,
    };
  }

  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    vi.mocked(fetchPreferences).mockResolvedValue({});
    vi.mocked(setPreference).mockResolvedValue(undefined);
    vi.mocked(listBatchInferTasks).mockResolvedValue([]);
  });

  it("无读角色（viewer）不挂载任务中心 → 零请求（不调用列表接口）", () => {
    setPerm(["viewer"]);
    renderLayout({ ...mockUser, role: "viewer" });
    expect(listBatchInferTasks).not.toHaveBeenCalled();
  });

  it("读角色（domain_admin）挂载任务中心 → 挂载探测调用一次列表接口", async () => {
    setPerm(["domain_admin"]);
    renderLayout({ ...mockUser, role: "domain_admin" });
    await waitFor(() => {
      expect(listBatchInferTasks).toHaveBeenCalledTimes(1);
    });
  });

  it("权限快照未就绪（null）fail-open 挂载（与全局 can 语义一致，后端强制兜底）", async () => {
    setPerm(null);
    renderLayout();
    await waitFor(() => {
      expect(listBatchInferTasks).toHaveBeenCalledTimes(1);
    });
  });
});
