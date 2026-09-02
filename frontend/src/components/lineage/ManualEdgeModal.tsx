// 手动登记血缘边弹窗（人工治理：自动解析覆盖不到的业务依赖）。
// 支持从当前节点（表/指标）「添加上游」或「添加下游」，目标节点可关键词搜索或手动输入，
// 并在登记时给出清晰的节点类型/所需信息说明。

import { useEffect, useRef, useState } from "react";
import {
  Alert,
  AutoComplete,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Tag,
  message,
} from "antd";
import { LinkOutlined } from "@ant-design/icons";
import { addManualLineageEdge, lineageNodes, UnisenseApiError } from "../../api";
import type { LineageNode } from "../../types";

// 支持人工登记的节点类型：前缀 → 所需信息 / 示例（产品语义：每类节点需哪些信息）。
export const MANUAL_NODE_TYPE_OPTIONS: Array<{
  value: string;
  label: string;
  example: string;
  hint: string;
}> = [
  { value: "table", label: "表", example: "table:wedw_ods.sale_detail", hint: "库名.表名（含 schema）" },
  { value: "metric", label: "指标", example: "metric:gmv_total", hint: "指标编码" },
  { value: "column", label: "字段", example: "column:dws.gmv.amount", hint: "库名.表名.字段名" },
  { value: "dimension", label: "维度", example: "dimension:store", hint: "维度编码" },
  { value: "consumer", label: "消费方", example: "consumer:app_a", hint: "接入方 ID（报表/接口）" },
  { value: "external", label: "外部依赖", example: "external:etl_manual", hint: "外部依赖标识" },
];

// 边类型 → 中文标签 + 适用场景（登记时给用户语义化选择）。
export const MANUAL_EDGE_TYPE_OPTIONS: Array<{ value: string; label: string; hint: string }> = [
  { value: "DERIVED_FROM", label: "派生自（加工依赖）", hint: "A 的数据由 B 加工而来" },
  { value: "CONSUMED_BY", label: "被消费（下游使用）", hint: "报表/接口/应用消费该资产" },
  { value: "USES_DIMENSION", label: "使用维度", hint: "指标按该维度分析" },
  { value: "READS_COLUMN", label: "读取字段", hint: "指标/表读取该字段" },
  { value: "EXTERNAL_BREAK", label: "外部断链", hint: "依赖外部系统/文档（仅登记）" },
];

// 按源/目标前缀推断默认边类型（用户可改）。
function defaultEdgeType(source: string, target: string): string {
  if (source.startsWith("column:") || target.startsWith("column:")) return "READS_COLUMN";
  if (source.startsWith("metric:") && target.startsWith("dimension:")) return "USES_DIMENSION";
  if (source.startsWith("dimension:") && target.startsWith("metric:")) return "USES_DIMENSION";
  if (source.startsWith("metric:") && target.startsWith("consumer:")) return "CONSUMED_BY";
  if (source.startsWith("external:") || target.startsWith("external:")) return "EXTERNAL_BREAK";
  return "DERIVED_FROM";
}

interface ManualEdgeModalProps {
  open: boolean;
  onClose: () => void;
  /** 当前节点（表/指标/维度等，作为固定的上游或下游） */
  baseNode: string;
  baseLabel?: string;
  /** 默认方向：upstream=给当前节点添上游；downstream=给当前节点添下游 */
  defaultDirection?: "upstream" | "downstream";
  onSuccess?: () => void;
}

export function ManualEdgeModal({
  open,
  onClose,
  baseNode,
  baseLabel,
  defaultDirection = "downstream",
  onSuccess,
}: ManualEdgeModalProps) {
  const [form] = Form.useForm();
  const [direction, setDirection] = useState<"upstream" | "downstream">(defaultDirection);
  const [options, setOptions] = useState<LineageNode[]>([]);
  const [searching, setSearching] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  // 跟踪用户是否手动改过边类型（避免自动推断覆盖手选）
  const [edgeTypeChanged, setEdgeTypeChanged] = useState(false);

  useEffect(() => {
    if (!open) return;
    setDirection(defaultDirection);
    setEdgeTypeChanged(false);
    form.resetFields();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, baseNode, defaultDirection]);

  /** 目标节点关键词搜索（无关键词加载 top-N 候选），供 AutoComplete 惰性选择。 */
  async function loadNodes(kw: string) {
    setSearching(true);
    try {
      setOptions(await lineageNodes(kw || undefined, 30));
    } catch {
      // 搜索失败不阻断（仍可手动输入节点）
    } finally {
      setSearching(false);
    }
  }
  // 目标节点输入防抖：AutoComplete onSearch 每次击键 300ms 静默后直查（onFocus 空载不走防抖）
  const nodeSearchTimer = useRef<number | null>(null);
  const loadNodesDebounced = (kw: string) => {
    if (nodeSearchTimer.current) window.clearTimeout(nodeSearchTimer.current);
    nodeSearchTimer.current = window.setTimeout(() => void loadNodes(kw), 300);
  };

  function onTargetChange(target: string) {
    if (edgeTypeChanged) return;
    form.setFieldValue(
      "edge_type",
      defaultEdgeType(
        direction === "upstream" ? target : baseNode,
        direction === "upstream" ? baseNode : target,
      ),
    );
  }

  async function handleSubmit() {
    try {
      const values = await form.validateFields();
      const target = (values.target_node as string).trim();
      const source = direction === "upstream" ? target : baseNode;
      const tgt = direction === "upstream" ? baseNode : target;
      if (source === tgt) {
        message.warning("上游与下游不能是同一节点");
        return;
      }
      setSubmitting(true);
      await addManualLineageEdge({
        source_node: source,
        target_node: tgt,
        edge_type: values.edge_type,
        note: values.note,
      });
      message.success("血缘边已登记（来源：手动登记）");
      onClose();
      onSuccess?.();
    } catch (err) {
      if (err instanceof Error && "errorFields" in err) return; // 表单校验错误，已高亮
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "登记血缘边失败",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      title={
        <Space>
          <LinkOutlined />
          {direction === "upstream" ? "添加上游（被依赖方）" : "添加下游（消费/加工方）"}
        </Space>
      }
      open={open}
      onCancel={onClose}
      onOk={handleSubmit}
      confirmLoading={submitting}
      okText="登记血缘边"
      width={620}
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="人工登记血缘边"
        description={
          <Space direction="vertical" size={2}>
            <span>
              当前节点：<span className="mono">{baseNode}</span>
              {baseLabel ? <span className="muted">（{baseLabel}）</span> : null}
            </span>
            <span>
              将登记「{direction === "upstream" ? "新节点 → 当前节点" : "当前节点 → 新节点"}」的血缘关系，
              用于补全自动解析覆盖不到的业务依赖（外部报表/文档记载/手工关联）。
            </span>
          </Space>
        }
      />
      <Form form={form} layout="vertical" initialValues={{ edge_type: "DERIVED_FROM" }}>
        <Form.Item label="登记方向">
          <Select showSearch
            value={direction}
            onChange={(v) => setDirection(v as "upstream" | "downstream")}
            options={[
              { value: "upstream", label: `添加上游（${baseNode} 的下游）` },
              { value: "downstream", label: `添加下游（${baseNode} 的上游）` },
            ]}
          />
        </Form.Item>
        <Form.Item
          name="target_node"
          label="目标节点"
          rules={[
            { required: true, message: "请输入目标节点" },
            {
              pattern: /^[a-z]+:/,
              message: "目标节点须带类型前缀（如 table: / metric: / column: / dimension: / consumer: / external:）",
            },
          ]}
          extra="可输入关键词搜索已有节点，或直接按「前缀:标识」手动输入。"
        >
          <AutoComplete
            options={options.map((n) => ({ value: n.id, label: n.label }))}
            onSearch={loadNodesDebounced}
            onSelect={(v) => onTargetChange(v)}
            onFocus={() => void loadNodes("")}
            placeholder="输入关键词搜索，或手动输入 table:db.orders"
            notFoundContent={searching ? "搜索中…" : null}
            style={{ width: "100%" }}
          />
        </Form.Item>
        <Form.Item name="edge_type" label="边类型" required>
          <Select showSearch
            options={MANUAL_EDGE_TYPE_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
            onChange={() => setEdgeTypeChanged(true)}
          />
        </Form.Item>
        <Form.Item name="note" label="登记说明（选填）">
          <Input.TextArea rows={2} maxLength={500} placeholder="补充该血缘关系的业务背景，便于后续追溯" />
        </Form.Item>
      </Form>
      <Alert
        type="info"
        showIcon
        style={{ marginTop: 8 }}
        message="节点类型与所需信息"
        description={
          <Space direction="vertical" size={4} style={{ width: "100%" }}>
            {MANUAL_NODE_TYPE_OPTIONS.map((o) => (
              <div key={o.value} style={{ fontSize: 12 }}>
                <Tag color="blue" style={{ marginRight: 6 }}>{o.label}</Tag>
                <span className="mono">{o.example}</span>
                <span className="muted" style={{ marginLeft: 8 }}>{o.hint}</span>
              </div>
            ))}
          </Space>
        }
      />
    </Modal>
  );
}
