import { useEffect, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, Space, Popconfirm, message } from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { createOrganization, listOrganizations, updateOrganization, UnisenseApiError } from "../api";
import type { OrganizationView } from "../types";
import { formatCnTime } from "../utils/timeCn";

const ORG_STATUS_LABEL: Record<string, { text: string; color: string }> = {
  active: { text: "正常", color: "success" },
  suspended: { text: "已停用", color: "warning" },
  deleted: { text: "已删除", color: "default" },
};

export function OrgManagement() {
  const [items, setItems] = useState<OrganizationView[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [keyword, setKeyword] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<OrganizationView | null>(null);
  const [form] = Form.useForm();

  async function load() {
    setLoading(true);
    try {
      const res = await listOrganizations({ keyword: keyword || undefined, status: status || undefined, page, page_size: pageSize });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, keyword, status]);

  async function handleSubmit(values: Record<string, unknown>) {
    try {
      if (editing) {
        await updateOrganization(editing.id, { name: String(values.name) });
        message.success("组织名称已更新");
      } else {
        await createOrganization({ name: String(values.name), code: String(values.code) });
        message.success("组织已创建");
      }
      setModalOpen(false);
      form.resetFields();
      setEditing(null);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "保存失败");
    }
  }

  async function handleStatus(org: OrganizationView, nextStatus: string) {
    try {
      await updateOrganization(org.id, { status: nextStatus });
      message.success(`组织已${nextStatus === "suspended" ? "停用" : nextStatus === "active" ? "启用" : "删除"}`);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  function openCreate() {
    setEditing(null);
    form.resetFields();
    setModalOpen(true);
  }

  function openEdit(org: OrganizationView) {
    setEditing(org);
    form.setFieldsValue({ name: org.name, code: org.code });
    setModalOpen(true);
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "组织名称", dataIndex: "name", key: "name", render: (v: string) => <strong>{v}</strong> },
    { title: "组织编码", dataIndex: "code", key: "code", render: (v: string) => <span className="mono">{v}</span> },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (v: string) => {
        const s = ORG_STATUS_LABEL[v] ?? { text: v, color: "default" };
        return <Tag color={s.color}>{s.text}</Tag>;
      },
    },
    { title: "用户数", dataIndex: "user_count", key: "users", width: 90 },
    { title: "创建时间", dataIndex: "created_at", key: "created", width: 170, render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span className="muted">—</span>) },
    {
      title: "操作",
      key: "ops",
      width: 240,
      render: (_: unknown, org: OrganizationView) => (
        <Space>
          <Button size="small" onClick={() => openEdit(org)}>编辑</Button>
          {org.status === "active" ? (
            <Popconfirm title={`确认停用组织「${org.name}」？停用后其下用户将无法登录`} onConfirm={() => handleStatus(org, "suspended")}>
              <Button size="small">停用</Button>
            </Popconfirm>
          ) : (
            <Button size="small" onClick={() => handleStatus(org, "active")}>启用</Button>
          )}
          {org.status !== "deleted" && org.user_count === 0 && (
            <Popconfirm title={`确认删除组织「${org.name}」？`} onConfirm={() => handleStatus(org, "deleted")}>
              <Button size="small" danger>删除</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">组织管理 / 多租户</div>
          <h2>组织管理</h2>
          <p>组织（租户）为顶级数据隔离单元——停用后其下用户无法登录，删除前须先迁移或回收用户。</p>
        </div>
      </div>

      <Card
        extra={
          <Space>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>创建组织</Button>
            <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button>
          </Space>
        }
      >
        <Space style={{ marginBottom: 12 }} wrap>
          <Input
            placeholder="按名称 / 编码搜索"
            style={{ width: 220 }}
            value={keyword}
            onChange={(e) => { setKeyword(e.target.value); setPage(1); }}
            allowClear
          />
          <Select
            allowClear
            placeholder="全部状态"
            style={{ width: 140 }}
            value={status || undefined}
            onChange={(v) => { setStatus(v || ""); setPage(1); }}
            options={[{ value: "active", label: "正常" }, { value: "suspended", label: "已停用" }, { value: "deleted", label: "已删除" }]}
          />
        </Space>

        <Table
          dataSource={items}
          columns={columns}
          rowKey="id"
          loading={loading}
          pagination={{ current: page, pageSize, total, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100], onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showTotal: (t) => `共 ${t} 个组织` }}
          locale={{ emptyText: "暂无组织" }}
          size="small"
        />
      </Card>

      <Modal
        title={editing ? "编辑组织" : "创建组织"}
        open={modalOpen}
        onCancel={() => { setModalOpen(false); setEditing(null); }}
        onOk={() => form.submit()}
        okText={editing ? "保存" : "创建"}
      >
        <Form form={form} layout="vertical" onFinish={handleSubmit} style={{ marginTop: 8 }}>
          <Form.Item name="name" label="组织名称" rules={[{ required: true, message: "请输入组织名称" }]}>
            <Input maxLength={128} />
          </Form.Item>
          <Form.Item name="code" label="组织编码" rules={[
            { required: true, message: "请输入组织编码" },
            { pattern: /^[a-z0-9][a-z0-9_-]*$/, message: "小写字母/数字开头，可含 _ -" },
          ]}>
            <Input disabled={!!editing} maxLength={64} className="mono" placeholder="如 finance_dept" />
          </Form.Item>
          {editing && (
            <p className="muted" style={{ fontSize: 12 }}>组织编码创建后不可修改（作为唯一标识）。停用/删除请使用列表操作。</p>
          )}
        </Form>
      </Modal>
    </div>
  );
}
