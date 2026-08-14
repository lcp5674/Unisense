import { Drawer, Table } from "antd";
import type { ColumnsType } from "antd/es/table";

/**
 * 通用明细下钻抽屉：概览指标点击值后展示明细表。
 *
 * 列与数据由调用方提供（目录 / 指标 / 孤儿等不同口径），
 * 抽屉仅负责承载与分页展示，保持单一职责。
 */
export interface DrillDownDrawerProps<T extends Record<string, unknown>> {
  open: boolean;
  title: string;
  columns: ColumnsType<T>;
  rows: T[];
  loading: boolean;
  onClose: () => void;
}

export function DrillDownDrawer<T extends Record<string, unknown>>({
  open,
  title,
  columns,
  rows,
  loading,
  onClose,
}: DrillDownDrawerProps<T>) {
  return (
    <Drawer title={title} open={open} onClose={onClose} width={760} destroyOnClose>
      <Table<T>
        dataSource={rows}
        columns={columns}
        rowKey={(_, i) => String(i)}
        size="small"
        loading={loading}
        pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
      />
    </Drawer>
  );
}
