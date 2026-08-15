import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { TrackingProvider, useTrackingContext } from "../components/TrackingProvider";
import { trackEvent } from "../api";
import type { CurrentUser } from "../types";

const addEventMock = vi.fn();
vi.mock("../api", () => ({ trackEvent: vi.fn().mockResolvedValue(undefined) }));
vi.mock("../store", () => ({
  useAppStore: (sel: (s: { addEvent: typeof addEventMock; recentEvents: unknown[] }) => unknown) =>
    sel({ addEvent: addEventMock, recentEvents: [] }),
}));

const user: CurrentUser = {
  id: 3,
  username: "admin",
  display_name: "管理员",
  role: "platform_admin",
  domain: null,
  org_id: 1,
};

function Probe({ type, targetId }: { type: string; targetId?: string }) {
  const { track } = useTrackingContext();
  return <button onClick={() => track(type, targetId, "metric")}>go</button>;
}

function renderProbe(props: { type: string; targetId?: string }) {
  return render(
    <TrackingProvider user={user}>
      <Probe {...props} />
    </TrackingProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  addEventMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("TrackingProvider 同事件节流", () => {
  it("窗口内重复触发同一事件只上报一次（后端 + 本地）", () => {
    renderProbe({ type: "case_dup" });
    const btn = screen.getByText("go");
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);
    expect(trackEvent).toHaveBeenCalledTimes(1);
    expect(addEventMock).toHaveBeenCalledTimes(1);
  });

  it("不同 target 的同一事件类型互不影响，各自上报", () => {
    render(
      <TrackingProvider user={user}>
        <Probe type="case_multi" targetId="m1" />
        <Probe type="case_multi" targetId="m2" />
      </TrackingProvider>,
    );
    const btns = screen.getAllByText("go");
    fireEvent.click(btns[0]);
    fireEvent.click(btns[1]);
    expect(trackEvent).toHaveBeenCalledTimes(2);
  });

  it("窗口过后再次触发允许上报", () => {
    vi.useFakeTimers();
    renderProbe({ type: "case_window" });
    const btn = screen.getByText("go");

    fireEvent.click(btn);
    expect(trackEvent).toHaveBeenCalledTimes(1);

    // 推进 11 秒（超过 10s 窗口）
    vi.setSystemTime(Date.now() + 11_000);
    fireEvent.click(btn);
    expect(trackEvent).toHaveBeenCalledTimes(2);
  });
});

describe("TrackingProvider 超长字段钳制", () => {
  it("超长 target_id 发送前截断到 36（对齐后端 schema，避免 422）", () => {
    const longId = `table:${"x".repeat(60)}`; // 66 字符 > 36
    renderProbe({ type: "clip_id", targetId: longId });
    fireEvent.click(screen.getByText("go"));
    const sent = vi.mocked(trackEvent).mock.calls[0][0];
    expect(sent.target_id?.length).toBe(36);
    expect(sent.target_id).toBe(longId.slice(0, 36));
    // 本地事件同样截断
    const localEvent = addEventMock.mock.calls[0][0];
    expect(localEvent.target_id?.length).toBe(36);
  });

  it("超长 event_type 截断到 32", () => {
    const longType = `lineage_${"y".repeat(40)}`;
    renderProbe({ type: longType });
    fireEvent.click(screen.getByText("go"));
    const sent = vi.mocked(trackEvent).mock.calls[0][0];
    expect(sent.event_type.length).toBe(32);
  });

  it("正常长度字段原样发送", () => {
    renderProbe({ type: "ok_type", targetId: "table:orders" });
    fireEvent.click(screen.getByText("go"));
    const sent = vi.mocked(trackEvent).mock.calls[0][0];
    expect(sent.event_type).toBe("ok_type");
    expect(sent.target_id).toBe("table:orders");
  });
});
