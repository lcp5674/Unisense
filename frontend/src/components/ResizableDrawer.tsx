import { Drawer } from "antd";
import type { DrawerProps } from "antd";
import { useEffect, useRef, useState } from "react";

/**
 * 可拖拽调整宽度的详情抽屉。
 *
 * 基于 antd Drawer：抽屉从右侧滑出，左边缘有一条拖拽手柄，
 * 按住左右拖动即可自由调整宽度（clamp 到 [minWidth, maxWidth]），
 * 松手后按 storageKey 持久化到 localStorage，下次打开保持宽度。
 *
 * 相比固定 width 的 Drawer，解决详情内容过宽/过窄无法自适应的问题。
 */
export interface ResizableDrawerProps extends DrawerProps {
  /** 宽度持久化 key（不同抽屉传不同值，互不干扰） */
  storageKey?: string;
  /** 最小宽度 px（默认 520） */
  minWidth?: number;
  /** 最大宽度 px（默认 1440） */
  maxWidth?: number;
  /** 默认宽度 px（无持久化记录时，默认 820） */
  defaultWidth?: number;
  /** 是否允许拖拽调整宽度（默认 true） */
  resizable?: boolean;
}

function clamp(v: number, min: number, max: number) {
  return Math.max(min, Math.min(max, v));
}

export function ResizableDrawer({
  storageKey,
  minWidth = 520,
  maxWidth = 1440,
  defaultWidth = 820,
  resizable = true,
  width: widthProp,
  children,
  ...rest
}: ResizableDrawerProps) {
  const [width, setWidth] = useState<number>(() => {
    if (widthProp != null) return widthProp as number;
    if (storageKey) {
      try {
        const v = Number(localStorage.getItem(storageKey));
        if (Number.isFinite(v) && v >= minWidth) return clamp(v, minWidth, maxWidth);
      } catch {
        /* localStorage 不可用（隐私模式等）时忽略，回退默认 */
      }
    }
    return defaultWidth;
  });
  const widthRef = useRef(width);
  const startX = useRef(0);
  const startW = useRef(width);

  useEffect(() => {
    if (widthProp != null) setWidth(widthProp as number);
  }, [widthProp]);

  useEffect(() => {
    widthRef.current = width;
  }, [width]);

  function onResizeStart(e: React.MouseEvent) {
    if (!resizable) return;
    e.preventDefault();
    startX.current = e.clientX;
    startW.current = widthRef.current;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", onResizeMove);
    window.addEventListener("mouseup", onResizeEnd);
  }

  function onResizeMove(e: MouseEvent) {
    // 抽屉从右侧滑出：鼠标向左拖（clientX 减小）→ 宽度增大
    const next = clamp(startW.current + (startX.current - e.clientX), minWidth, maxWidth);
    widthRef.current = next;
    setWidth(next);
  }

  function onResizeEnd() {
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    window.removeEventListener("mousemove", onResizeMove);
    window.removeEventListener("mouseup", onResizeEnd);
    if (storageKey) {
      try {
        localStorage.setItem(storageKey, String(widthRef.current));
      } catch {
        /* 持久化失败不阻断使用 */
      }
    }
  }

  return (
    <Drawer {...rest} width={width} rootClassName="resizable-drawer">
      {resizable && (
        <div
          className="drawer-resize-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label="拖动调整详情宽度"
          onMouseDown={onResizeStart}
        />
      )}
      {children}
    </Drawer>
  );
}
