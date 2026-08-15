import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Table } from "antd";
import type { TableProps } from "antd";
import { useResizableColumns, ResizableTitle } from "../components/ResizableTable";

beforeEach(() => {
  localStorage.clear();
});

describe("ResizableTitle", () => {
  it("渲染拖拽手柄（aria-label 供可访问性与测试定位）", () => {
    render(
      <table>
        <thead>
          <tr>
            <ResizableTitle onResize={() => {}}>列A</ResizableTitle>
          </tr>
        </thead>
      </table>,
    );
    expect(screen.getByLabelText("拖拽调整列宽")).toBeTruthy();
  });

  it("拖拽（mousedown→mousemove→mouseup）触发 onResize 且宽度受 minWidth 兜底", () => {
    const onResize = vi.fn();
    render(
      <table>
        <thead>
          <tr>
            <ResizableTitle onResize={onResize} minWidth={60} width={200}>
              列A
            </ResizableTitle>
          </tr>
        </thead>
      </table>,
    );
    const handle = screen.getByLabelText("拖拽调整列宽");
    fireEvent.mouseDown(handle, { clientX: 100 });
    fireEvent.mouseMove(document, { clientX: 30 }); // 左拖 -> jsdom offsetWidth=0，被 minWidth 兜底
    fireEvent.mouseMove(document, { clientX: 180 }); // 右拖
    fireEvent.mouseUp(document);
    expect(onResize).toHaveBeenCalled();
    const widths = onResize.mock.calls.map((c) => c[0] as number);
    // jsdom 下 th.offsetWidth 为 0，最终宽度 = max(minWidth, 0 + delta) = max(60, 80) = 80
    expect(widths[widths.length - 1]).toBe(80);
  });
});

describe("useResizableColumns", () => {
  it("拖拽列头后列宽写入 localStorage（按 storageKey 记忆）", () => {
    const storageKey = "unisense:test-col-widths";
    const cols = [
      { title: "列A", dataIndex: "a", key: "a", width: 100 },
      { title: "列B", dataIndex: "b", key: "b" },
    ];
    function Harness() {
      const { columns, components } = useResizableColumns<Record<string, string>>(
        cols as TableProps<Record<string, string>>["columns"],
        storageKey,
      );
      return (
        <Table
          columns={columns}
          components={components}
          dataSource={[{ a: "1", b: "2" }]}
          rowKey="a"
        />
      );
    }
    render(<Harness />);
    const handles = screen.getAllByLabelText("拖拽调整列宽");
    expect(handles.length).toBeGreaterThanOrEqual(2);
    fireEvent.mouseDown(handles[0], { clientX: 0 });
    fireEvent.mouseMove(document, { clientX: 90 });
    fireEvent.mouseUp(document);
    const saved = JSON.parse(localStorage.getItem(storageKey) ?? "{}");
    expect(saved.a).toBe(90);
  });
});
