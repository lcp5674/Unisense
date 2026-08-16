import { useState } from "react";
import { Button, Input, Space, Spin, Table, Tag, Tooltip, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { EditOutlined, CheckOutlined, CloseOutlined, ThunderboltOutlined } from "@ant-design/icons";
import type { SchemaColumn, DescriptionSource } from "../types";
import { PAGE_SIZE_OPTIONS, usePersistentPageSize } from "../hooks/usePersistentPageSize";

/** 描述来源 Tag 配置 */
const SOURCE_TAG_CONFIG: Record<string, { label: string; color: string }> = {
  manual: { label: "人工编辑", color: "blue" },
  llm: { label: "LLM 推断", color: "purple" },
  schema: { label: "采集原始", color: "default" },
};

function descriptionSourceTag(source?: DescriptionSource | null) {
  if (!source) return null;
  const cfg = SOURCE_TAG_CONFIG[source];
  if (!cfg) return <Tag>{source}</Tag>;
  return <Tag color={cfg.color}>{cfg.label}</Tag>;
}

interface SchemaTableProps {
  columns: SchemaColumn[];
  loading?: boolean;
  /** 是否可编辑（人工编辑描述） */
  editable?: boolean;
  /** 是否可推断（LLM 推断按钮） */
  inferable?: boolean;
  /** 是否可 LLM 推断（权限点控制按钮显隐，默认 true 兼容既有调用/测试） */
  canInfer?: boolean;
  /** 编辑回调 */
  onEdit?: (col: SchemaColumn, newDesc: string) => void | Promise<void>;
  /** 单字段推断回调 */
  onInfer?: (col: SchemaColumn) => void | Promise<void>;
  /** 批量推断回调 */
  onBatchInfer?: () => void | Promise<void>;
}

export function SchemaTable({
  columns: data,
  loading = false,
  editable = false,
  inferable = false,
  canInfer = true,
  onEdit,
  onInfer,
  onBatchInfer,
}: SchemaTableProps) {
  // 编辑态：记录正在编辑的字段名
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [saving, setSaving] = useState(false);

  // 推断中
  const [inferringColumn, setInferringColumn] = useState<string | null>(null);
  const [batchInferring, setBatchInferring] = useState(false);

  // 每页条数可切换并持久化（实体详情抽屉字段表）
  const { pageSize, onShowSizeChange } = usePersistentPageSize(
    "unisense.schema-table.pageSize",
    20,
  );

  /** 空描述字段数量（用于批量推断按钮） */
  const emptyDescCount = data.filter(
    (c) => !c.description && !c.comment
  ).length;

  async function handleSave(col: SchemaColumn) {
    if (!editValue.trim()) {
      message.warning("描述不能为空");
      return;
    }
    setSaving(true);
    try {
      await onEdit?.(col, editValue.trim());
      setEditingName(null);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  function handleCancelEdit() {
    setEditingName(null);
    setEditValue("");
  }

  async function handleInfer(col: SchemaColumn) {
    setInferringColumn(col.name);
    try {
      await onInfer?.(col);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "推断失败");
    } finally {
      setInferringColumn(null);
    }
  }

  async function handleBatchInfer() {
    setBatchInferring(true);
    try {
      await onBatchInfer?.();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "批量推断失败");
    } finally {
      setBatchInferring(false);
    }
  }

  const tableColumns: ColumnsType<SchemaColumn> = [
    {
      title: "字段名",
      dataIndex: "name",
      key: "name",
      width: 180,
      render: (v: string) => <span className="mono">{v}</span>,
    },
    {
      title: "类型",
      dataIndex: "type",
      key: "type",
      width: 120,
      render: (v: string) => v ? <Tag>{v}</Tag> : <span className="muted">-</span>,
    },
    {
      title: "描述",
      key: "description",
      render: (_: unknown, record: SchemaColumn) => {
        const desc = record.description || record.comment;
        const source = record.description_source;

        // 编辑态
        if (editingName === record.name) {
          return (
            <Space.Compact style={{ width: "100%" }}>
              <Input
                size="small"
                value={editValue}
                onChange={(e) => setEditValue(e.target.value)}
                onPressEnter={() => handleSave(record)}
                disabled={saving}
                style={{ flex: 1 }}
              />
              <Button
                size="small"
                type="primary"
                icon={<CheckOutlined />}
                loading={saving}
                onClick={() => handleSave(record)}
              />
              <Button
                size="small"
                icon={<CloseOutlined />}
                onClick={handleCancelEdit}
                disabled={saving}
              />
            </Space.Compact>
          );
        }

        return (
          <Space size={4} wrap>
            <span>{desc || <span className="muted" style={{ fontStyle: "italic" }}>暂无描述</span>}</span>
            {descriptionSourceTag(source)}
            {editable && (
              <Tooltip title="编辑描述">
                <Button
                  type="text"
                  size="small"
                  icon={<EditOutlined />}
                  onClick={() => {
                    setEditingName(record.name);
                    setEditValue(desc || "");
                  }}
                />
              </Tooltip>
            )}
            {inferable && canInfer && !desc && onInfer && (
              <Tooltip title="LLM 推断描述">
                <Button
                  type="text"
                  size="small"
                  icon={<ThunderboltOutlined />}
                  loading={inferringColumn === record.name}
                  onClick={() => handleInfer(record)}
                >
                  推断
                </Button>
              </Tooltip>
            )}
          </Space>
        );
      },
    },
  ];

  return (
    <Spin spinning={loading}>
      {inferable && canInfer && onBatchInfer && emptyDescCount > 0 && (
        <div style={{ marginBottom: 8 }}>
          <Button
            icon={<ThunderboltOutlined />}
            loading={batchInferring}
            onClick={handleBatchInfer}
            size="small"
          >
            批量推断缺失描述（{emptyDescCount} 个字段）
          </Button>
        </div>
      )}
      <Table
        dataSource={data}
        rowKey={(r) => r.name}
        columns={tableColumns}
        size="small"
        pagination={
          data.length > 20
            ? {
                pageSize,
                showSizeChanger: true,
                pageSizeOptions: [...PAGE_SIZE_OPTIONS],
                onShowSizeChange,
              }
            : false
        }
        locale={{ emptyText: "暂无字段信息" }}
      />
    </Spin>
  );
}
