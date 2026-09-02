import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QualitySnapshot } from "../pages/metric/QualitySnapshot";
import { listSnapshots, listQualityEvents, listQualityRules } from "../api";

vi.mock("../api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../api")>();
  return {
    ...mod,
    listSnapshots: vi.fn(),
    listQualityEvents: vi.fn().mockResolvedValue({ items: [] }),
    listQualityRules: vi.fn().mockResolvedValue({ items: [] }),
    qualityEventAck: vi.fn(),
    qualityEventResolve: vi.fn(),
    qualityEventClose: vi.fn(),
  };
});

let mockRoles: string[] = ["analyst"];
vi.mock("../hooks/usePermission", () => ({
  usePermission: () => ({ snapshot: { roles: mockRoles, role: mockRoles[0] } }),
}));

describe("QualitySnapshot 消费快照前置拦截", () => {
  beforeEach(() => {
    mockRoles = ["analyst"];
    vi.mocked(listSnapshots).mockReset();
    vi.mocked(listSnapshots).mockResolvedValue([]);
  });

  it("DEPRECATED 且非管理员：不发快照请求，提示仅管理员可审计", async () => {
    render(<QualitySnapshot metricId={1} metricCode="outp_e2e_drugfee_day" status="DEPRECATED" />);
    await screen.findByText("消费快照（不可用）");
    fireEvent.click(screen.getByRole("tab", { name: "消费快照（不可用）" }));
    expect(listSnapshots).not.toHaveBeenCalled();
    expect(await screen.findByText(/指标已废弃（DEPRECATED）/)).toBeTruthy();
  });

  it("DRAFT：不发快照请求，提示未发布", async () => {
    render(<QualitySnapshot metricId={1} metricCode="x_day" status="DRAFT" />);
    await screen.findByText("消费快照（不可用）");
    fireEvent.click(screen.getByRole("tab", { name: "消费快照（不可用）" }));
    expect(listSnapshots).not.toHaveBeenCalled();
    expect(await screen.findByText(/未发布，暂无消费快照/)).toBeTruthy();
  });

  it("PUBLISHED：正常发起快照请求", async () => {
    render(<QualitySnapshot metricId={1} metricCode="fee_day" status="PUBLISHED" />);
    await waitFor(() => expect(listSnapshots).toHaveBeenCalledWith("fee_day", 10));
  });

  it("DEPRECATED 且平台管理员：仍发起快照请求（审计回溯）", async () => {
    mockRoles = ["platform_admin"];
    render(<QualitySnapshot metricId={1} metricCode="outp_e2e_drugfee_day" status="DEPRECATED" />);
    await waitFor(() => expect(listSnapshots).toHaveBeenCalled());
  });
});
