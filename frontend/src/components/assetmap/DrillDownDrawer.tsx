import { Table } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { GetComponentProps } from "rc-table/lib/interface";
import { ResizableDrawer } from "../ResizableDrawer";
import { PAGE_SIZE_OPTIONS, usePersistentPageSize } from "../../hooks/usePersistentPageSize";

/**
 * 通用明细下钻抽屉：概览指标点击值后展示明细表。
 *
 * 列与数据由调用方提供（目录 / 指标 / 孤儿等不同口径），
 * 抽屉仅负责承载与分页展示，保持单一职责。
 * onRow 可选：行点击进一步下钻（如从指标明细跳转到单条实体详情）。
 * 宽度可拖拽调整（左边缘手柄），按 storageKey 持久化。
 */
export interface DrillDownDrawerProps<T extends Record<string, unknown>> {
  open: boolean;
  title: string;
  columns: ColumnsType<T>;
  rows: T[];
  loading: boolean;
  onClose: () => void;
  onRow?: GetComponentProps<T>;
  /** 宽度持久化 key（不同口径传不同值，互不干扰） */
  storageKey?: string;
}

export function DrillDownDrawer<T extends Record<string, unknown>>({
  open,
  title,
  columns,
  rows,
  loading,
  onClose,
  onRow,
  storageKey,
}: DrillDownDrawerProps<T>) {
  const drillStorageKey = storageKey ?? "unisense.drawer.drill.width";
  const { pageSize, onShowSizeChange } = usePersistentPageSize(
    `${drillStorageKey}.pageSize`,
    20,
  );
  return (
    <ResizableDrawer
      title={title}
      open={open}
      onClose={onClose}
      storageKey={drillStorageKey}
      defaultWidth={860}
      minWidth={560}
      destroyOnClose
    >
      <Table<T>
        dataSource={rows}
        columns={columns}
        rowKey={(_, i) => String(i)}
        size="middle"
        loading={loading}
        onRow={onRow}
        pagination={{
          pageSize,
          showSizeChanger: true,
          pageSizeOptions: [...PAGE_SIZE_OPTIONS],
          onShowSizeChange,
          showTotal: (t) => `共 ${t} 条`,
        }}
      />
    </ResizableDrawer>
  );
}
