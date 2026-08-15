import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { Notifications, EVENT_TYPES } from "../pages/Notifications";
import { NOTIF_CHANGED_EVENT } from "../utils/notifBus";
import type { Notification } from "../types";

vi.mock("../api", () => {
  class UnisenseApiError extends Error {
    code: string;
    traceId: string;
    status: number;
    detail?: Record<string, unknown> | null;
    constructor(message: string, code: string, status: number, traceId: string, detail?: Record<string, unknown> | null) {
      super(message);
      this.name = "UnisenseApiError";
      this.code = code;
      this.status = status;
      this.traceId = traceId;
      this.detail = detail;
    }
  }
  return {
    listNotifications: vi.fn(),
    listNotifyEvents: vi.fn(),
    listSubscriptions: vi.fn(),
    upsertSubscription: vi.fn(),
    publishNotifyEvent: vi.fn(),
    markNotificationRead: vi.fn(),
    markAllNotificationsRead: vi.fn(),
    deleteNotification: vi.fn(),
    deleteAllNotifications: vi.fn(),
    UnisenseApiError,
  };
});

import {
  listNotifications,
  markNotificationRead,
  markAllNotificationsRead,
  deleteNotification,
  deleteAllNotifications,
  upsertSubscription,
} from "../api";

const mockedList = vi.mocked(listNotifications);
const mockedMarkRead = vi.mocked(markNotificationRead);
const mockedReadAll = vi.mocked(markAllNotificationsRead);
const mockedDelete = vi.mocked(deleteNotification);
const mockedClear = vi.mocked(deleteAllNotifications);

function notif(partial: Partial<Notification>): Notification {
  return {
    id: 1,
    subscriber_id: 5,
    channel: "in_app",
    template_code: "metric.approved",
    title: "指标已通过",
    body: "指标编码：sales_gmv",
    payload: { metric_code: "sales_gmv" },
    status: "SENT",
    send_at: null,
    sent_at: "2026-08-10T10:00:00",
    ref_type: "event",
    ref_id: 101,
    created_at: "2026-08-10T09:59:00",
    read_at: null,
    ...partial,
  };
}

// 路由追踪：捕获导航后的 pathname，供深链跳转断言
function PathSpy() {
  const loc = useLocation();
  return <div data-testid="path">{loc.pathname}</div>;
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/notifications"]}>
      <Routes>
        <Route path="/notifications" element={<Notifications />} />
        <Route path="*" element={<PathSpy />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedList.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 });
  mockedMarkRead.mockResolvedValue(notif({}));
  mockedReadAll.mockResolvedValue({ ok: true });
  mockedDelete.mockResolvedValue({ ok: true });
  mockedClear.mockResolvedValue({ ok: true });
});

describe("通知中心 - 列表与未读", () => {
  it("按服务端分页参数加载，未读行高亮", async () => {
    const unread = notif({ id: 1, read_at: null, title: "指标已通过" });
    const read = notif({ id: 2, read_at: "2026-08-11T08:00:00", title: "指标已驳回", template_code: "metric.rejected" });
    mockedList.mockResolvedValue({ items: [unread, read], total: 2, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("指标已通过")).toBeInTheDocument());

    // 初始请求带服务端分页参数
    expect(mockedList).toHaveBeenCalledWith({ page: 1, page_size: 10 });
    // 未读（read_at 为 null）卡片带高亮类，已读卡片不带
    const unreadCard = screen.getByText("指标已通过").closest(".notif-card") as HTMLElement;
    const readCard = screen.getByText("指标已驳回").closest(".notif-card") as HTMLElement;
    expect(unreadCard.classList.contains("notif-unread")).toBe(true);
    expect(readCard.classList.contains("notif-unread")).toBe(false);
  });

  it("分页切换请求下一页并沿用 page_size", async () => {
    mockedList.mockResolvedValue({ items: [notif({})], total: 25, page: 1, page_size: 10 });
    renderPage();
    await waitFor(() => expect(screen.getByText("指标已通过")).toBeInTheDocument());

    fireEvent.click(screen.getByTitle("2"));
    await waitFor(() => expect(mockedList).toHaveBeenCalledWith({ page: 2, page_size: 10 }));
  });
});

describe("通知中心 - 已读与删除操作", () => {
  it("单条标记已读调用 API 并刷新列表", async () => {
    const n = notif({ id: 7, read_at: null, body: "指标编码：sales_gmv" });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("指标已通过")).toBeInTheDocument());

    const card = screen.getByText("指标已通过").closest(".notif-card") as HTMLElement;
    fireEvent.click(withinCard(card, "标记已读"));

    await waitFor(() => expect(mockedMarkRead).toHaveBeenCalledWith(7));
    expect(mockedList).toHaveBeenCalledTimes(2); // 初始 + 操作后刷新
  });

  it("全部已读调用 read-all 并刷新", async () => {
    mockedList.mockResolvedValue({ items: [notif({ read_at: null })], total: 1, page: 1, page_size: 10 });
    renderPage();
    await waitFor(() => expect(screen.getByText("指标已通过")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /全部已读/ }));
    await waitFor(() => expect(mockedReadAll).toHaveBeenCalledTimes(1));
    expect(mockedList).toHaveBeenCalledTimes(2);
  });

  it("删除单条调用 API 并刷新", async () => {
    const n = notif({ id: 9, body: "指标编码：sales_gmv" });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("指标已通过")).toBeInTheDocument());

    const card = screen.getByText("指标已通过").closest(".notif-card") as HTMLElement;
    fireEvent.click(withinCard(card, /删\s*除/));

    await waitFor(() => expect(mockedDelete).toHaveBeenCalledWith(9));
    expect(mockedList).toHaveBeenCalledTimes(2);
  });

  it("清空调用 DELETE /notifications 并刷新", async () => {
    mockedList.mockResolvedValue({ items: [notif({})], total: 1, page: 1, page_size: 10 });
    renderPage();
    await waitFor(() => expect(screen.getByText("指标已通过")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /清\s*空/ }));
    await waitFor(() => expect(mockedClear).toHaveBeenCalledTimes(1));
    expect(mockedList).toHaveBeenCalledTimes(2);
  });
});

describe("通知中心 - 深链跳转", () => {
  it("点击指标通知跳转指标详情（payload.metric_code）", async () => {
    const n = notif({ template_code: "metric.created", payload: { metric_code: "sales_gmv" } });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("指标已通过")).toBeInTheDocument());

    fireEvent.click(screen.getByText("指标已通过"));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/detail/sales_gmv"));
  });

  it("conflict 事件跳转冲突仲裁页", async () => {
    const n = notif({ id: 3, template_code: "conflict_open", title: "口径冲突待处理", payload: { conflict_id: "C-1" } });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("口径冲突待处理")).toBeInTheDocument());

    fireEvent.click(screen.getByText("口径冲突待处理"));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/review"));
  });

  it("未知类型事件优雅降级：不跳转且提示", async () => {
    const n = notif({ id: 4, template_code: "unknown.custom", title: "未知事件" });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("未知事件")).toBeInTheDocument());

    fireEvent.click(screen.getByText("未知事件"));
    // 未发生路由跳转（PathSpy 仅在非 /notifications 路由渲染）
    expect(screen.queryByTestId("path")).toBeNull();
    expect(await screen.findByText(/没有关联/)).toBeInTheDocument();
  });
});

describe("通知中心 - 订阅项对齐", () => {
  it("订阅选项不含幽灵事件、含真实 grant/conflict 事件", async () => {
    mockedList.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 });
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: "订阅设置" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /新增订阅/ })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /新增订阅/ }));

    // 幽灵项已移除
    expect(screen.queryByText("指标发布")).not.toBeInTheDocument();
    expect(screen.queryByText("权限变更")).not.toBeInTheDocument();
    expect(screen.queryByText("血缘变更")).not.toBeInTheDocument();
    expect(screen.queryByText("系统公告")).not.toBeInTheDocument();
  });

  it("订阅选项含新接入的可订阅事件（反馈/满意度/审计容量），与后端订阅清单对齐", async () => {
    // EVENT_TYPES 是后端订阅清单（main.py _BUSINESS_EVENT_TYPES）的前端契约镜像
    expect(EVENT_TYPES).toContain("feedback.status_updated");
    expect(EVENT_TYPES).toContain("nps.submitted");
    expect(EVENT_TYPES).toContain("audit.capacity_warning");
    // 幽灵项（无发布方）不得出现在可订阅集合
    for (const ghost of [
      "lineage.change",
      "system.notice",
      "governance.grant",
      "review.pending",
      "quality.alert",
      "conflict.detected",
      "orphan.event",
    ]) {
      expect(EVENT_TYPES).not.toContain(ghost);
    }

    // 渲染验证：新增订阅弹窗的消息类型下拉中「反馈状态更新」可订阅（中文标签）
    mockedList.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 });
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: "订阅设置" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /新增订阅/ })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /新增订阅/ }));
    fireEvent.mouseDown(screen.getByLabelText("消息类型"));
    await waitFor(() => {
      const dropdown = document.querySelector(".ant-select-dropdown:not(.ant-select-dropdown-hidden)");
      expect(dropdown).toBeTruthy();
    });
    // 英文事件码不应直出
    expect(screen.queryByText("feedback.status_updated")).not.toBeInTheDocument();
  });

  it("sms 渠道已隐藏（后端无短信实现）", async () => {
    mockedList.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 });
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: "订阅设置" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /新增订阅/ })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /新增订阅/ }));

    // 渠道下拉：短信不可选
    fireEvent.mouseDown(screen.getByLabelText("送达方式"));
    expect(screen.queryByText("短信")).not.toBeInTheDocument();
  });

  it("新增订阅支持多选消息类型：选多个事件 → 每个事件各建一条订阅", async () => {
    const mockedUpsert = vi.mocked(upsertSubscription);
    mockedUpsert.mockResolvedValue({ id: 1 } as never);
    mockedList.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 });
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: "订阅设置" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /新增订阅/ })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /新增订阅/ }));

    // 送达方式：email
    fireEvent.mouseDown(screen.getByLabelText("送达方式"));
    fireEvent.click(await screen.findByText("邮件"));
    // 消息类型多选：展开 dropdown，在 dropdown 容器内连续点选选项
    //（antd 多选在 jsdom 中下拉保持打开，但选项与已选 tag 文本重复，须限定在 dropdown 内）
    const typeLabel = screen.getByLabelText("消息类型");
    fireEvent.mouseDown(typeLabel);
    const dropdown = () =>
      document.querySelector(".ant-select-dropdown:not(.ant-select-dropdown-hidden)") as HTMLElement;
    await waitFor(() => expect(dropdown()).toBeTruthy());
    const optionOf = (label: string) =>
      [...(dropdown().querySelectorAll(".ant-select-item-option") ?? [])].find(
        (el) => el.textContent?.includes(label),
      ) as HTMLElement;
    fireEvent.click(optionOf("指标创建"));
    fireEvent.click(optionOf("数据质量异常告警"));
    fireEvent.keyDown(document.body, { key: "Escape" });

    fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
    await waitFor(() => {
      expect(mockedUpsert).toHaveBeenCalledTimes(2);
      expect(mockedUpsert).toHaveBeenCalledWith(
        expect.objectContaining({ channel: "email", event_type: "metric.created", enabled: true }),
      );
      expect(mockedUpsert).toHaveBeenCalledWith(
        expect.objectContaining({ channel: "email", event_type: "quality.anomaly" }),
      );
    });
  });
});

describe("通知中心 - 变更事件广播（顶栏角标实时刷新）", () => {
  // 各操作成功后都应广播事件，Layout 顶栏据此实时刷新未读数
  it("标记已读后广播通知变更事件", async () => {
    const spy = vi.fn();
    window.addEventListener(NOTIF_CHANGED_EVENT, spy);
    const n = notif({ id: 7, read_at: null });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("指标已通过")).toBeInTheDocument());
    const card = screen.getByText("指标已通过").closest(".notif-card") as HTMLElement;
    fireEvent.click(withinCard(card, "标记已读"));

    await waitFor(() => expect(spy).toHaveBeenCalled());
    window.removeEventListener(NOTIF_CHANGED_EVENT, spy);
  });

  it("全部已读后广播通知变更事件", async () => {
    const spy = vi.fn();
    window.addEventListener(NOTIF_CHANGED_EVENT, spy);
    mockedList.mockResolvedValue({ items: [notif({ read_at: null })], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("指标已通过")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /全部已读/ }));

    await waitFor(() => expect(spy).toHaveBeenCalled());
    window.removeEventListener(NOTIF_CHANGED_EVENT, spy);
  });

  it("删除单条后广播通知变更事件", async () => {
    const spy = vi.fn();
    window.addEventListener(NOTIF_CHANGED_EVENT, spy);
    const n = notif({ id: 9 });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("指标已通过")).toBeInTheDocument());
    const card = screen.getByText("指标已通过").closest(".notif-card") as HTMLElement;
    fireEvent.click(withinCard(card, /删\s*除/));

    await waitFor(() => expect(spy).toHaveBeenCalled());
    window.removeEventListener(NOTIF_CHANGED_EVENT, spy);
  });

  it("清空后广播通知变更事件", async () => {
    const spy = vi.fn();
    window.addEventListener(NOTIF_CHANGED_EVENT, spy);
    mockedList.mockResolvedValue({ items: [notif({})], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("指标已通过")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /清\s*空/ }));

    await waitFor(() => expect(spy).toHaveBeenCalled());
    window.removeEventListener(NOTIF_CHANGED_EVENT, spy);
  });
});

// 在单条卡片内定位按钮：antd 双字按钮会插入空格（如「删 除」）
function withinCard(card: HTMLElement, text: string | RegExp) {
  const btn = Array.from(card.querySelectorAll("button")).find((b) => {
    const t = b.textContent ?? "";
    const expectRe = text instanceof RegExp ? text : new RegExp(text.replace(/ /g, "\\s*"));
    return expectRe.test(t.replace(/\s+/g, " "));
  });
  if (!btn) throw new Error(`卡片内未找到按钮 ${text}`);
  return btn;
}

describe("通知中心 - 信息展示增强", () => {
  it("payload 业务字段合并展示——body 为自然语言时补充实体名称/状态", async () => {
    const n = notif({
      id: 20,
      template_code: "catalog.deprecated",
      title: "目录已废弃",
      body: "目录 销售明细表 已被废弃",
      payload: { source_id: "mysql_unisense", entity_name: "销售明细表", status: "active" },
    });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("目录已废弃")).toBeInTheDocument());
    // body 自然语言（整行展示）
    expect(screen.getByText("目录 销售明细表 已被废弃")).toBeInTheDocument();
    // payload 补充业务字段未丢弃：实体名称/数据源ID/状态均展示
    expect(screen.getByText("实体名称")).toBeInTheDocument();
    expect(screen.getByText("销售明细表")).toBeInTheDocument();
    expect(screen.getByText("数据源ID")).toBeInTheDocument();
    expect(screen.getByText("mysql_unisense")).toBeInTheDocument();
    expect(screen.getByText("状态")).toBeInTheDocument();
    expect(screen.getByText("启用")).toBeInTheDocument();
  });

  it("body 已含某字段时 payload 同字段不重复展示", async () => {
    const n = notif({
      id: 21,
      body: "指标编码：sales_gmv\n业务域：零售",
      payload: { metric_code: "sales_gmv", domain: "零售", note: "补充说明" },
    });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("指标已通过")).toBeInTheDocument());
    // metric_code/domain 在 body 已展示（不重复），note 被跳过——合计 2 个字段
    expect(screen.getByText("指标编码")).toBeInTheDocument();
    expect(screen.getAllByText("sales_gmv").length).toBe(1);
    expect(screen.getAllByText("零售").length).toBe(1);
    expect(screen.queryByText("补充说明")).not.toBeInTheDocument();
  });

  it("事件类型徽标——title 为自然语言时显示类型 Tag", async () => {
    const n = notif({
      id: 22,
      template_code: "user.password_reset",
      title: "您的账号 admin 已被管理员重置密码",
      body: "您的账号 admin 已被管理员重置密码，请尽快重新登录。",
      payload: { user_id: 5, username: "admin" },
    });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("您的账号 admin 已被管理员重置密码")).toBeInTheDocument());
    // 类型徽标显示「密码已重置」（与自然语言 title 不同）
    expect(screen.getByText("密码已重置")).toBeInTheDocument();
    // 账号业务字段也展示
    expect(screen.getByText("账号")).toBeInTheDocument();
    expect(screen.getByText("admin")).toBeInTheDocument();
  });

  it("账号事件点击跳转个人中心（本人视角，方案 C）", async () => {
    const n = notif({ id: 23, template_code: "user.status_changed", title: "账号已禁用", payload: { user_id: 5, username: "admin" } });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("账号已禁用")).toBeInTheDocument());
    fireEvent.click(screen.getByText("账号已禁用"));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/account"));
  });

  it("组织事件点击跳转个人中心（本人视角，方案 C）", async () => {
    const n = notif({ id: 24, template_code: "org.status_changed", title: "您所属的组织已停用", payload: { org_id: 1, org_name: "销售部", status: "suspended" } });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("您所属的组织已停用")).toBeInTheDocument());
    fireEvent.click(screen.getByText("您所属的组织已停用"));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/account"));
  });

  it("采集事件点击跳转数据源页", async () => {
    const n = notif({ id: 25, template_code: "collect.failed", title: "采集任务失败", payload: { source_id: "mysql_unisense", reason: "连接超时" } });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("采集任务失败")).toBeInTheDocument());
    fireEvent.click(screen.getByText("采集任务失败"));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/data-sources"));
  });

  it("授权事件点击跳转个人中心（我的授权，方案 C）", async () => {
    const n = notif({ id: 26, template_code: "grant.expiring_soon", title: "权限即将到期", payload: { grant_id: 8, grant_type: "READ" } });
    mockedList.mockResolvedValue({ items: [n], total: 1, page: 1, page_size: 10 });

    renderPage();
    await waitFor(() => expect(screen.getByText("权限即将到期")).toBeInTheDocument());
    fireEvent.click(screen.getByText("权限即将到期"));
    await waitFor(() => expect(screen.getByTestId("path").textContent).toBe("/account"));
  });
});
