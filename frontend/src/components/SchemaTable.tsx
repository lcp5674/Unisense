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

/** 样本敏感类别标签（sample_rule → 中文，与后端 classify_sample 的 rule_id 对齐） */
const SAMPLE_RULE_LABEL: Record<string, string> = {
  phone: "手机",
  id_card: "身份证",
  email: "邮箱",
  bank_card: "银行卡",
};

/** 样本值最多横向铺开的列数（对应数据源配额 sample_rows，超出部分不额外建列） */
const MAX_SAMPLE_COLS = 8;

/** 归一化样本值：新采样落库为 string[]，存量兼容单值 string */
function normalizeSamples(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw.filter((v): v is string => typeof v === "string" && !!v);
  if (typeof raw === "string" && raw) return [raw];
  return [];
}

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

  // 样本值横向铺开为「样本 1…N」多列（每格一个值），列数取当前表实际最多条数
  const sampleCount = Math.min(
    MAX_SAMPLE_COLS,
    data.reduce((max, c) => Math.max(max, normalizeSamples(c.sample).length), 0),
  );
  // 仅当存在敏感类别命中时才插入「类别」列
  const hasSampleRule = data.some((c) => !!SAMPLE_RULE_LABEL[c.sample_rule ?? ""]);

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
    // 敏感类别（由样本明文判定命中的规则，采样时记录 rule_id）
    ...(hasSampleRule
      ? [
          {
            title: "类别",
            key: "sample_rule",
            width: 90,
            render: (_: unknown, record: SchemaColumn) => {
              const label = SAMPLE_RULE_LABEL[record.sample_rule ?? ""];
              return label ? <Tag color="orange">{label}</Tag> : <span className="muted">-</span>;
            },
          },
        ]
      : []),
    // 样本 1…N：每列一个脱敏值，值过长时 Tooltip 展示完整内容
    ...Array.from({ length: sampleCount }, (_, i) => ({
      title: `样本 ${i + 1}`,
      key: `sample_${i}`,
      width: 150,
      ellipsis: true,
      render: (_: unknown, record: SchemaColumn) => {
        const value = normalizeSamples(record.sample)[i];
        if (!value) return <span className="muted">-</span>;
        return (
          <Tooltip title={value}>
            <span className="mono">{value}</span>
          </Tooltip>
        );
      },
    })),
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
