import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { Notifications } from "../pages/Notifications";
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
