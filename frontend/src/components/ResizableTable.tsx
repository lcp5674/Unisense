import { forwardRef, useCallback, useMemo, useRef, useState } from "react";
import type { CSSProperties, HTMLAttributes, Key, MouseEvent as ReactMouseEvent } from "react";
import type { TableProps } from "antd";

/**
 * 可拖拽调整列宽的通用表格能力（antd v5，零第三方依赖）。
 *
 * 用法（配合 tableLayout="fixed" + scroll={{ x: "max" }} 使用）：
 *   const { columns: resizableColumns, components } = useResizableColumns(columns, "unisense:xxx-col-widths");
 *   <Table columns={resizableColumns} components={components} tableLayout="fixed" scroll={{ x: "max" }} ... />
 *
 * - 每列标题右侧提供拖拽手柄，hover 显示、按下拖动实时改宽；
 * - 用户调整的宽度持久化到 localStorage（按 storageKey 记忆，下次进入保持）；
 * - 不设 width 的列（弹性列）在 tableLayout="fixed" 下吸收表格剩余宽度，配合 ellipsis 让内容自适应；
 * - 列默认宽度由调用方 columns 的 width 提供，minWidth 兜底防止拖拽过窄。
 */

type ColumnsType<T> = NonNullable<TableProps<T>["columns"]>;

export interface ResizableColumnType {
  key?: Key;
  dataIndex?: unknown;
  width?: number;
  minWidth?: number;
  [k: string]: unknown;
}

const MIN_COL_WIDTH = 60;
const MAX_COL_WIDTH = 1600;
const HANDLE_CLASS = "unisense-col-resize-handle";

/** 拖拽手柄样式：惰性注入 <style>，避免依赖全局 styles.css（仓库常被并行会话编辑）。 */
const HANDLE_CSS = `
.${HANDLE_CLASS} {
  position: absolute; top: 0; right: 0; bottom: 0; width: 7px;
  cursor: col-resize; z-index: 1; opacity: 0;
  transition: opacity 0.15s ease; touch-action: none;
}
.${HANDLE_CLASS}::after {
  content: ""; position: absolute; top: 10%; bottom: 10%; right: 3px;
  width: 1px; background: rgba(12, 22, 38, 0.22); border-radius: 1px;
}
th:hover .${HANDLE_CLASS} { opacity: 1; }
.${HANDLE_CLASS}:active::after { background: var(--signal, #e8862d); width: 2px; right: 2.5px; }
`;

let styleInjected = false;
function ensureHandleStyle(): void {
  if (styleInjected || typeof document === "undefined") return;
  styleInjected = true;
  const el = document.createElement("style");
  el.textContent = HANDLE_CSS;
  document.head.appendChild(el);
}

interface ResizableTitleProps extends HTMLAttributes<HTMLTableCellElement> {
  width?: number;
  minWidth?: number;
  onResize?: (width: number) => void;
}

/**
 * 替换 antd Table 的 header cell（components.header.cell）。
 * 通过 onHeaderCell 注入的 onResize 回调驱动列宽更新。
 */
export const ResizableTitle = forwardRef<HTMLTableCellElement, ResizableTitleProps>(
  function ResizableTitle({ width, minWidth = MIN_COL_WIDTH, onResize, children, ...restProps }, ref) {
    const thRef = useRef<HTMLTableCellElement | null>(null);

    const setRefs = useCallback(
      (el: HTMLTableCellElement | null) => {
        thRef.current = el;
        if (typeof ref === "function") ref(el);
        else if (ref) ref.current = el;
      },
      [ref],
    );

    const handleMouseDown = useCallback(
      (e: ReactMouseEvent<HTMLSpanElement>) => {
        e.preventDefault();
        e.stopPropagation();
        if (!onResize) return;
        const startX = e.clientX;
        const startW = thRef.current?.offsetWidth ?? 0;
        const body = document.body;
        body.style.cursor = "col-resize";
        body.style.userSelect = "none";

        const onMove = (ev: globalThis.MouseEvent) => {
          const next = startW + (ev.clientX - startX);
          onResize(Math.max(minWidth, Math.min(next, MAX_COL_WIDTH)));
        };
        const onUp = () => {
          body.style.cursor = "";
          body.style.userSelect = "";
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      },
      [minWidth, onResize],
    );

    const style: CSSProperties = { position: "relative", ...restProps.style };

    return (
      <th {...restProps} ref={setRefs} style={style}>
        {children}
        {onResize ? (
          <span
            role="separator"
            aria-label="拖拽调整列宽"
            className={HANDLE_CLASS}
            onMouseDown={handleMouseDown}
          />
        ) : null}
      </th>
    );
  },
);

function loadWidths(storageKey?: string): Record<string, number> {
  if (!storageKey || typeof localStorage === "undefined") return {};
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null
      ? (parsed as Record<string, number>)
      : {};
  } catch {
    return {};
  }
}

function columnKey(col: ResizableColumnType): string {
  const di = col.dataIndex;
  return String(
    col.key ??
      (Array.isArray(di) ? di.join(".") : di != null ? String(di) : ""),
  );
}

export interface ResizableColumnsResult<T> {
  columns: ColumnsType<T>;
  components: TableProps<T>["components"];
}

/**
 * 为列注入 width（记忆用户拖拽值）+ onHeaderCell（连接 ResizableTitle 手柄）。
 * 必须在组件顶层调用（hooks 规则）；storageKey 缺省时不持久化（仅会话内记忆）。
 */
export function useResizableColumns<T extends object>(
  columns: ColumnsType<T> | undefined,
  storageKey?: string,
): ResizableColumnsResult<T> {
  const [widths, setWidths] = useState<Record<string, number>>(() => loadWidths(storageKey));
  const storageKeyRef = useRef(storageKey);
  storageKeyRef.current = storageKey;

  ensureHandleStyle();

  const handleResize = useCallback((key: string, width: number) => {
    setWidths((prev) => {
      const next = { ...prev, [key]: width };
      const sk = storageKeyRef.current;
      if (sk) {
        try {
          localStorage.setItem(sk, JSON.stringify(next));
        } catch {
          // 忽略隐私模式/配额等写入失败，仅本次会话生效
        }
      }
      return next;
    });
  }, []);

  const resizableColumns = useMemo<ColumnsType<T>>(() => {
    if (!columns) return [];
    return columns.map((col) => {
      const base = col as unknown as ResizableColumnType;
      const key = columnKey(base);
      const width = widths[key] ?? base.width;
      const minWidth = base.minWidth ?? MIN_COL_WIDTH;
      return {
        ...base,
        width,
        minWidth,
        onHeaderCell: () => ({
          width,
          minWidth,
          onResize: (w: number) => handleResize(key, w),
        }),
      } as ColumnsType<T>[number];
    });
  }, [columns, widths, handleResize]);

  const components = useMemo<TableProps<T>["components"]>(
    () => ({ header: { cell: ResizableTitle } }),
    [],
  );

  return { columns: resizableColumns, components };
}
