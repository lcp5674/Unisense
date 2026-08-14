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

  async function handleCreate(values: { code: string; label: string; sort_order?: number; description?: string }) {
    try {
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
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新增参照数据项</Button>
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
          <Form.Item name="code" label="编码" rules={[{ required: true }, { pattern: /^[A-Za-z0-9_]+$/, message: "仅字母数字下划线" }]}>
            <Input placeholder="如 CNY" />
          </Form.Item>
          <Form.Item name="label" label="显示名" rules={[{ required: true }]}>
            <Input placeholder="如 人民币元" />
          </Form.Item>
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
