import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Card, Tabs, Table, Button, Modal, Form, Input, InputNumber, Space, Tag, Select, Popconfirm, App as AntApp,
} from "antd";
import { PlusOutlined, EditOutlined, DeleteOutlined, StopOutlined, CheckCircleOutlined, ArrowLeftOutlined } from "@ant-design/icons";
import {
  listDictTypes, listAllDictItems, createDictItem, updateDictItem,
  deactivateDictItem, activateDictItem, deleteDictItem,
  batchCreateDictItems, batchToggleDictItems, batchDeleteDictItems,
} from "../api";
import type { DictBatchResult, SystemDictItem } from "../types";
import { slugifyCode, resolveUniqueCode } from "../utils/zhEnDict";
import { usePermission } from "../hooks/usePermission";

const DICT_TYPE_LABELS: Record<string, string> = {
  granularity: "粒度",
  unit: "单位",
  aggregation: "聚合方式",
  time_semantics: "时间语义",
  freshness: "新鲜度",
  dw_layer: "数仓层",
  metric_type: "指标类型",
  additivity: "可加性",
  serving_mode: "服务模式",
  metric_tier: "指标分级",
  currency: "币种",
  // PII 合规增强：敏感规则类别与规则配置（供敏感分级规则引擎 DB 可配置）
  pii_category: "PII 类别",
  pii_rule: "PII 规则",
  // 原子指标口径库：度量分类（dict_type=measure_category，在线增删改/启停用）
  measure_category: "度量分类",
};

// 打开新增弹窗时静默刷新的最小间隔（毫秒）：TTL 内重复打开直接用缓存，避免
// 无并发场景下每次打开都多发一次 listAllDictItems 请求；超 TTL 才刷新，以
// 缩小「他端新增同名编码但本页未刷新」导致的预览滞后窗口。
export const QUIET_REFRESH_TTL_MS = 30_000;

// 批量新增弹窗单行（label 必填，code 由后端自动生成）
interface BatchCreateRow {
  label: string;
  sort_order: number;
  description: string;
}

export function SystemDict() {
  const { message, modal } = AntApp.useApp();
  const { can } = usePermission();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // 启用状态下钻（?status=，总览仪表「数据字典」资产卡片）作为初始筛选
  const urlStatus = searchParams.get("status") ?? "";
  const [dictTypes, setDictTypes] = useState<string[]>([]);
  const [activeType, setActiveType] = useState<string>("");
  const [items, setItems] = useState<SystemDictItem[]>([]);
  const [status, setStatus] = useState<string>(urlStatus);
  const [loading, setLoading] = useState(false);

  // 统一返回上一入口：优先回退浏览器历史（总览资产卡片等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  const [createOpen, setCreateOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [editItem, setEditItem] = useState<SystemDictItem | null>(null);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();
  // 批量操作（按当前 tab 的 dict_type 作用域）：行选 + 批量新增/启停/删除
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchRows, setBatchRows] = useState<BatchCreateRow[]>([
    { label: "", sort_order: 0, description: "" },
  ]);
  const [batchSubmitting, setBatchSubmitting] = useState(false);
  // 静默刷新的上次执行时间（TTL 防抖：QUIET_REFRESH_TTL_MS 内不重复请求）
  const lastQuietRefreshRef = useRef(0);
  // 编码自动生成预览：监听显示名，与后端 codegen 规则对齐——
  // slugify_code 生成 base（纯标点/空白名回退 item），再对当前类型已加载
  // （非软删）项编码做冲突自增（resolveUniqueCode，与 generate_unique_code
  // 逐字节一致），预览即后端将生成的最终编码。
  const watchLabel = Form.useWatch("label", createForm);
  const usedCodes = useMemo(() => items.map((i) => i.code), [items]);
  const codePreview = useMemo(() => {
    const base = slugifyCode(watchLabel ?? "") || "item";
    return resolveUniqueCode(base, usedCodes);
  }, [watchLabel, usedCodes]);
  // 超上限回退 base（resolveUniqueCode 返回的 base 必然仍被占用）→ 无法自动
  // 生成唯一编码，切换为手动指定（后端对应抛 DICT_CODE_EXHAUSTED）。
  const codeExhausted = usedCodes.includes(codePreview);

  // 状态筛选为客户端过滤（数据字典按类型 Tabs 一次性加载全部项）
  const visibleItems = useMemo(
    () => (status ? items.filter((i) => i.status === status) : items),
    [items, status],
  );

  useEffect(() => {
    listDictTypes().then((types) => {
      setDictTypes(types);
      if (types.length > 0 && !activeType) setActiveType(types[0]);
    }).catch(() => message.error("加载参照数据类型失败"));
  }, []);

  useEffect(() => {
    if (!activeType) return;
    setLoading(true);
    listAllDictItems(activeType)
      .then(setItems)
      .catch(() => message.error("加载参照数据项失败"))
      .finally(() => setLoading(false));
  }, [activeType]);

  function loadItems() {
    if (!activeType) return;
    setLoading(true);
    listAllDictItems(activeType)
      .then(setItems)
      .finally(() => setLoading(false));
  }

  // 静默刷新项列表：不置 loading，仅更新 items（打开新增弹窗时调用）。
  // TTL 防抖——QUIET_REFRESH_TTL_MS 内重复打开直接用缓存，超 TTL 才刷新。
  function refreshItemsQuietly() {
    if (!activeType) return;
    const now = Date.now();
    if (now - lastQuietRefreshRef.current < QUIET_REFRESH_TTL_MS) return;
    lastQuietRefreshRef.current = now;
    listAllDictItems(activeType).then(setItems).catch(() => {});
  }

  function openCreate() {
    setCreateOpen(true);
    // 打开弹窗时基于最新项列表重算编码预览，缩小「他端新增同名编码但本页未
    // 刷新」导致的预览滞后窗口（提交仍以后端权威判定为准）。
    refreshItemsQuietly();
  }

  async function handleCreate(values: { code?: string; label: string; sort_order?: number; description?: string }) {
    try {
      // code 不传由后端按显示名自动生成英文编码（冲突自动追加序号）；
      // 仅「无法自动生成」时手动指定 code 才随表单透传。
      await createDictItem(activeType, { ...values, sort_order: values.sort_order ?? 0 });
      message.success("新增成功");
      setCreateOpen(false);
      createForm.resetFields();
      loadItems();
    } catch (err: any) {
      message.error(err?.message || "新增失败");
    }
  }

  async function handleEdit(values: { label?: string; sort_order?: number; description?: string }) {
    if (!editItem) return;
    try {
      await updateDictItem(activeType, editItem.code, values);
      message.success("更新成功");
      setEditOpen(false);
      editForm.resetFields();
      setEditItem(null);
      loadItems();
    } catch (err: any) {
      message.error(err?.message || "更新失败");
    }
  }

  async function handleToggle(item: SystemDictItem) {
    try {
      if (item.status === "active") await deactivateDictItem(activeType, item.code);
      else await activateDictItem(activeType, item.code);
      message.success(item.status === "active" ? "已停用" : "已启用");
      loadItems();
    } catch (err: any) {
      message.error(err?.message || "操作失败");
    }
  }

  function handleDelete(item: SystemDictItem) {
    modal.confirm({
      title: "确认删除",
      content: `确定要删除 "${item.label}" 吗？被引用的参照数据项不可删除。`,
      okText: "删除",
      okType: "danger",
      onOk: async () => {
        try {
          await deleteDictItem(activeType, item.code);
          message.success("删除成功");
          loadItems();
        } catch (err: any) {
          message.error(err?.message || "删除失败");
        }
      },
    });
  }

  // 批量失败清单：label/code（原因）——批量 207 语义下逐项标注，便于治理定位
  function batchFailSummary(result: DictBatchResult): string {
    return result.failed
      .map((f) => `${f.label || f.code || "?"}（${f.message ?? f.error_code ?? "失败"}）`)
      .join("、");
  }

  async function handleBatchToggle(enabled: boolean) {
    if (selectedRowKeys.length === 0) return;
    const action = enabled ? "启用" : "停用";
    setBatchSubmitting(true);
    try {
      const codes = selectedRowKeys.map(String);
      const result = await batchToggleDictItems(
        activeType,
        codes,
        enabled ? "activate" : "deactivate",
      );
      if (result.failed.length > 0) {
        message.warning(`${action}完成 ${result.succeeded.length} 个，失败 ${result.failed.length} 个：${batchFailSummary(result)}`);
      } else {
        message.success(`已${action} ${result.succeeded.length} 个参照数据项`);
      }
      setSelectedRowKeys([]);
      loadItems();
    } catch (err: any) {
      message.error(err?.message || `批量${action}失败`);
    } finally {
      setBatchSubmitting(false);
    }
  }

  async function handleBatchDelete() {
    if (selectedRowKeys.length === 0) return;
    setBatchSubmitting(true);
    try {
      const codes = selectedRowKeys.map(String);
      const result = await batchDeleteDictItems(activeType, codes);
      if (result.failed.length > 0) {
        message.warning(`删除完成 ${result.succeeded.length} 个，失败 ${result.failed.length} 个：${batchFailSummary(result)}`);
      } else {
        message.success(`已删除 ${result.succeeded.length} 个参照数据项`);
      }
      setSelectedRowKeys([]);
      loadItems();
    } catch (err: any) {
      message.error(err?.message || "批量删除失败");
    } finally {
      setBatchSubmitting(false);
    }
  }

  function addBatchRow() {
    setBatchRows([...batchRows, { label: "", sort_order: 0, description: "" }]);
  }

  function updateBatchRow(idx: number, patch: Partial<BatchCreateRow>) {
    setBatchRows(batchRows.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  }

  function removeBatchRow(idx: number) {
    setBatchRows(batchRows.filter((_, i) => i !== idx));
  }

  async function handleBatchCreate() {
    const valid = batchRows
      .map((r) => ({ ...r, label: r.label.trim() }))
      .filter((r) => r.label.length > 0);
    if (valid.length === 0) {
      message.warning("请至少填写一行的显示名");
      return;
    }
    setBatchSubmitting(true);
    try {
      const result = await batchCreateDictItems(activeType, valid);
      if (result.failed.length > 0) {
        message.warning(`新增成功 ${result.succeeded.length} 个，失败 ${result.failed.length} 个：${batchFailSummary(result)}`);
      } else {
        message.success(`已新增 ${result.succeeded.length} 个参照数据项`);
      }
      setBatchOpen(false);
      setBatchRows([{ label: "", sort_order: 0, description: "" }]);
      loadItems();
    } catch (err: any) {
      message.error(err?.message || "批量新增失败");
    } finally {
      setBatchSubmitting(false);
    }
  }

  const columns = [
    { title: "编码", dataIndex: "code", key: "code", width: 140 },
    { title: "显示名", dataIndex: "label", key: "label", width: 140 },
    { title: "排序", dataIndex: "sort_order", key: "sort_order", width: 60 },
    {
      title: "状态", dataIndex: "status", key: "status", width: 80,
      render: (s: string) => <Tag color={s === "active" ? "green" : "red"}>{s === "active" ? "启用" : "停用"}</Tag>,
    },
    { title: "引用数", dataIndex: "ref_count", key: "ref_count", width: 80 },
    { title: "描述", dataIndex: "description", key: "description", ellipsis: true },
    {
      title: "操作", key: "action", width: 200,
      render: (_: unknown, record: SystemDictItem) => (
        <Space size="small">
          {can("dict:create") && <Button size="small" icon={<EditOutlined />} onClick={() => { setEditItem(record); editForm.setFieldsValue({ label: record.label, sort_order: record.sort_order, description: record.description }); setEditOpen(true); }}>编辑</Button>}
          {can("dict:create") && (
            <Button size="small" icon={record.status === "active" ? <StopOutlined /> : <CheckCircleOutlined />} onClick={() => handleToggle(record)}>
              {record.status === "active" ? "停用" : "启用"}
            </Button>
          )}
          {can("dict:create") && <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(record)} />}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 8 }}>
        返回
      </Button>
      <Card title="参照数据管理">
      <Tabs
        activeKey={activeType}
        onChange={setActiveType}
        items={dictTypes.map((t) => ({
          key: t,
          label: DICT_TYPE_LABELS[t] || t,
        }))}
      />
      <div style={{ marginBottom: 16, display: "flex", gap: 12, alignItems: "center" }}>
        {can("dict:create") && (
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增参照数据项</Button>
        )}
        {can("dict:create") && (
          <Button icon={<PlusOutlined />} onClick={() => setBatchOpen(true)}>批量新增</Button>
        )}
        {can("dict:create") && selectedRowKeys.length > 0 && (
          <>
            <Button onClick={() => handleBatchToggle(true)} disabled={batchSubmitting}>
              批量启用
            </Button>
            <Popconfirm
              title="批量停用"
              description={`确定停用选中的 ${selectedRowKeys.length} 个参照数据项？停用后新指标无法再选用该取值。`}
              okText="确认停用"
              onConfirm={() => handleBatchToggle(false)}
              disabled={batchSubmitting}
            >
              <Button icon={<StopOutlined />} disabled={batchSubmitting}>
                批量停用
              </Button>
            </Popconfirm>
            <Popconfirm
              title="批量删除"
              description={`确定删除选中的 ${selectedRowKeys.length} 个参照数据项？被指标引用的项不可删除。`}
              okText="确认删除"
              okButtonProps={{ danger: true }}
              onConfirm={handleBatchDelete}
              disabled={batchSubmitting}
            >
              <Button danger icon={<DeleteOutlined />} disabled={batchSubmitting}>
                批量删除
              </Button>
            </Popconfirm>
          </>
        )}
        {selectedRowKeys.length > 0 && (
          <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 13 }}>
            已选 {selectedRowKeys.length} 项
          </span>
        )}
        <Select
          allowClear
          placeholder="全部状态"
          style={{ width: 140 }}
          value={status || undefined}
          onChange={(v?: string) => setStatus(v ?? "")}
          options={[
            { value: "active", label: "启用" },
            { value: "inactive", label: "停用" },
          ]}
        />
      </div>
      <Table
        columns={columns}
        dataSource={visibleItems}
        rowKey="code"
        loading={loading}
        size="small"
        pagination={false}
        rowSelection={{
          selectedRowKeys,
          onChange: (keys) => setSelectedRowKeys(keys),
        }}
      />

      {/* 新增弹窗 */}
      <Modal title={`新增 ${DICT_TYPE_LABELS[activeType] || activeType} 参照数据项`} open={createOpen} onCancel={() => setCreateOpen(false)} onOk={() => createForm.submit()}>
        <Form form={createForm} onFinish={handleCreate} layout="vertical">
          <Form.Item name="label" label="显示名" rules={[{ required: true }]}>
            <Input placeholder="如 人民币元" />
          </Form.Item>
          {codeExhausted ? (
            <Form.Item
              name="code"
              preserve={false}
              label="编码（需手动指定）"
              tooltip="已存在大量同名编码（如 x、x_2 … x_100），无法自动生成唯一编码；请手动指定一个未占用的编码，或修改显示名后重试"
              rules={[{ required: true, pattern: /^[A-Za-z0-9_]+$/, message: "编码仅支持字母、数字、下划线" }]}
            >
              <Space.Compact style={{ width: "100%" }}>
                <Input placeholder="如 item_101" data-testid="dict-code-manual" />
                <Tag color="orange" style={{ lineHeight: "30px", margin: 0 }}>需手动指定</Tag>
              </Space.Compact>
            </Form.Item>
          ) : (
            <Form.Item label="编码（自动生成）" tooltip="系统根据显示名自动生成英文编码，与已有编码冲突时自动追加序号（如 minute_2）；若无法自动生成可手动指定编码后重试">
              <Space.Compact style={{ width: "100%" }}>
                <Input value={codePreview} disabled data-testid="dict-code-preview" />
                <Tag color="blue" style={{ lineHeight: "30px", margin: 0 }}>自动生成</Tag>
              </Space.Compact>
            </Form.Item>
          )}
          <Form.Item name="sort_order" label="排序" initialValue={0}>
            <InputNumber min={0} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑弹窗 */}
      <Modal title="编辑参照数据项" open={editOpen} onCancel={() => { setEditOpen(false); setEditItem(null); }} onOk={() => editForm.submit()}>
        <Form form={editForm} onFinish={handleEdit} layout="vertical">
          <Form.Item name="label" label="显示名" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="sort_order" label="排序">
            <InputNumber min={0} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 批量新增弹窗：多行显示名/排序/描述，编码由后端逐条自动生成 */}
      <Modal
        title={`批量新增 ${DICT_TYPE_LABELS[activeType] || activeType} 参照数据项`}
        open={batchOpen}
        onCancel={() => setBatchOpen(false)}
        onOk={handleBatchCreate}
        okText="批量新增"
        confirmLoading={batchSubmitting}
        width={680}
      >
        <div style={{ marginBottom: 12 }}>
          <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={addBatchRow}>
            添加一行
          </Button>
          <span style={{ marginLeft: 8, color: "rgba(0,0,0,0.45)", fontSize: 13 }}>
            每行显示名必填；编码自动生成，与既有项冲突的条目会标记为失败
          </span>
        </div>
        {batchRows.map((row, idx) => (
          <Space key={idx} style={{ display: "flex", marginBottom: 8 }} align="baseline">
            <Input
              placeholder="显示名（必填）"
              value={row.label}
              data-testid={`dict-batch-label-${idx}`}
              onChange={(e) => updateBatchRow(idx, { label: e.target.value })}
              style={{ width: 180 }}
            />
            <InputNumber
              placeholder="排序"
              min={0}
              value={row.sort_order}
              data-testid={`dict-batch-sort-${idx}`}
              onChange={(v) => updateBatchRow(idx, { sort_order: v ?? 0 })}
              style={{ width: 90 }}
            />
            <Input
              placeholder="描述"
              value={row.description}
              onChange={(e) => updateBatchRow(idx, { description: e.target.value })}
              style={{ width: 240 }}
            />
            <Button
              type="text"
              danger
              icon={<DeleteOutlined />}
              onClick={() => removeBatchRow(idx)}
              disabled={batchRows.length === 1}
            />
          </Space>
        ))}
      </Modal>
    </Card>
    </div>
  );
}
