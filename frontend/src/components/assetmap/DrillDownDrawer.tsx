import { Drawer, Table } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { GetComponentProps } from "rc-table/lib/interface";

/**
 * 通用明细下钻抽屉：概览指标点击值后展示明细表。
 *
 * 列与数据由调用方提供（目录 / 指标 / 孤儿等不同口径），
 * 抽屉仅负责承载与分页展示，保持单一职责。
 * onRow 可选：行点击进一步下钻（如从指标明细跳转到单条实体详情）。
 */
export interface DrillDownDrawerProps<T extends Record<string, unknown>> {
  open: boolean;
  title: string;
  columns: ColumnsType<T>;
  rows: T[];
  loading: boolean;
  onClose: () => void;
  onRow?: GetComponentProps<T>;
}

export function DrillDownDrawer<T extends Record<string, unknown>>({
  open,
  title,
  columns,
  rows,
  loading,
  onClose,
  onRow,
}: DrillDownDrawerProps<T>) {
  return (
    <Drawer title={title} open={open} onClose={onClose} width={760} destroyOnClose>
      <Table<T>
        dataSource={rows}
        columns={columns}
        rowKey={(_, i) => String(i)}
        size="small"
        loading={loading}
        onRow={onRow}
        pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
      />
    </Drawer>
  );
}
