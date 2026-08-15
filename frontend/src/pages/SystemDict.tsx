import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Card, Tabs, Table, Button, Modal, Form, Input, InputNumber, Space, Tag, Select, App as AntApp,
} from "antd";
import { PlusOutlined, EditOutlined, DeleteOutlined, StopOutlined, CheckCircleOutlined, ArrowLeftOutlined } from "@ant-design/icons";
import {
  listDictTypes, listAllDictItems, createDictItem, updateDictItem,
  deactivateDictItem, activateDictItem, deleteDictItem,
} from "../api";
import type { SystemDictItem } from "../types";
import { slugifyCode, resolveUniqueCode } from "../utils/zhEnDict";

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
};

export function SystemDict() {
  const { message, modal } = AntApp.useApp();
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

  // 静默刷新项列表：不置 loading，仅更新 items（打开新增弹窗时调用）
  function refreshItemsQuietly() {
    if (!activeType) return;
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
          <Button size="small" icon={<EditOutlined />} onClick={() => { setEditItem(record); editForm.setFieldsValue({ label: record.label, sort_order: record.sort_order, description: record.description }); setEditOpen(true); }}>编辑</Button>
          <Button size="small" icon={record.status === "active" ? <StopOutlined /> : <CheckCircleOutlined />} onClick={() => handleToggle(record)}>
            {record.status === "active" ? "停用" : "启用"}
          </Button>
          <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(record)} />
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
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增参照数据项</Button>
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
        rowKey="id"
        loading={loading}
        size="small"
        pagination={false}
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
    </Card>
    </div>
  );
}
