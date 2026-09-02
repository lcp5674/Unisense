import { useEffect, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, Space, Popconfirm, message } from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  createOrganization,
  listDomainTree,
  listOrganizations,
  updateOrganization,
  UnisenseApiError,
} from "../api";
import type { OrganizationView, SubjectDomainTreeNode } from "../types";
import { formatCnTime } from "../utils/timeCn";
import { usePermission } from "../hooks/usePermission";

const ORG_STATUS_LABEL: Record<string, { text: string; color: string }> = {
  active: { text: "正常", color: "success" },
  suspended: { text: "已停用", color: "warning" },
  deleted: { text: "已删除", color: "default" },
};

// 主题域树 → 扁平化下拉选项（与数据源页「业务域」下拉同款实现）
function flattenDomains(
  nodes: SubjectDomainTreeNode[],
  depth = 0,
  out: Array<{ value: string; label: string }> = [],
): Array<{ value: string; label: string }> {
  for (const n of nodes) {
    const indent = depth > 0 ? `${"　".repeat(depth)}` : "";
    out.push({ value: n.code, label: `${indent}${n.name}（${n.code}）` });
    if (n.children?.length) flattenDomains(n.children, depth + 1, out);
  }
  return out;
}

export function OrgManagement() {
  const { can } = usePermission();
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
  const [domainOptions, setDomainOptions] = useState<Array<{ value: string; label: string }>>([]);

  useEffect(() => {
    // 业务域下拉：仅展示启用中的主题域（团队可绑定业务域，成员自动继承）
    listDomainTree("active")
      .then((tree) => setDomainOptions(flattenDomains(tree)))
      .catch(() => setDomainOptions([]));
  }, []);

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
      const domain = values.domain ? String(values.domain) : null;
      if (editing) {
        await updateOrganization(editing.id, { name: String(values.name), domain });
        message.success("团队信息已更新");
      } else {
        await createOrganization({ name: String(values.name), code: String(values.code), domain });
        message.success("团队已创建");
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
    form.setFieldsValue({ name: org.name, code: org.code, domain: org.domain ?? undefined });
    setModalOpen(true);
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "团队名称", dataIndex: "name", key: "name", render: (v: string) => <strong>{v}</strong> },
    { title: "团队编码", dataIndex: "code", key: "code", render: (v: string) => <span className="mono">{v}</span> },
    {
      title: "业务域",
      dataIndex: "domain",
      key: "domain",
      render: (v: string | null) => (v ? <Tag color="blue">{v}</Tag> : <span className="muted">不限域</span>),
    },
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
    { title: "成员数", dataIndex: "user_count", key: "users", width: 90 },
    { title: "创建时间", dataIndex: "created_at", key: "created", width: 170, render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span className="muted">—</span>) },
    {
      title: "操作",
      key: "ops",
      width: 240,
      render: (_: unknown, org: OrganizationView) => (
        <Space>
          {can("org:edit") && <Button size="small" onClick={() => openEdit(org)}>编辑</Button>}
          {can("org:disable") && (org.status === "active" ? (
            <Popconfirm title={`确认停用组织「${org.name}」？停用后其下用户将无法登录`} onConfirm={() => handleStatus(org, "suspended")}>
              <Button size="small">停用</Button>
            </Popconfirm>
          ) : (
            <Button size="small" onClick={() => handleStatus(org, "active")}>启用</Button>
          ))}
          {can("org:disable") && org.status !== "deleted" && org.user_count === 0 && (
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
          <div className="page-kicker">团队管理 / 组织与权限域</div>
          <h2>团队管理</h2>
          <p>团队（组织）为顶级数据隔离单元，可绑定业务域——成员自动继承团队域（权限域隔离）；停用后其下用户无法登录，删除前须先迁移或回收用户。</p>
        </div>
      </div>

      <Card
        extra={
          <Space>
            {can("org:create") && (
              <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>创建团队</Button>
            )}
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
          <Select showSearch
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
          pagination={{ current: page, pageSize, total, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100], onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showTotal: (t) => `共 ${t} 个团队` }}
          locale={{ emptyText: "暂无团队" }}
          size="small"
        />
      </Card>

      <Modal
        title={editing ? "编辑团队" : "创建团队"}
        open={modalOpen}
        onCancel={() => { setModalOpen(false); setEditing(null); }}
        onOk={() => form.submit()}
        okText={editing ? "保存" : "创建"}
      >
        <Form form={form} layout="vertical" onFinish={handleSubmit} style={{ marginTop: 8 }}>
          <Form.Item name="name" label="团队名称" rules={[{ required: true, message: "请输入团队名称" }]}>
            <Input maxLength={128} />
          </Form.Item>
          <Form.Item name="code" label="团队编码" rules={[
            { required: true, message: "请输入团队编码" },
            { pattern: /^[a-z0-9][a-z0-9_-]*$/, message: "小写字母/数字开头，可含 _ -" },
          ]}>
            <Input disabled={!!editing} maxLength={64} className="mono" placeholder="如 finance_dept" />
          </Form.Item>
          <Form.Item
            name="domain"
            label="绑定业务域（可选）"
            extra="绑定后其成员自动继承该域（权限域隔离）；不绑定则成员无默认域，需经授权访问指标。"
          >
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="选择业务域（可留空=不限域）"
              options={domainOptions}
            />
          </Form.Item>
          {editing && (
            <p className="muted" style={{ fontSize: 12 }}>团队编码创建后不可修改（作为唯一标识）。停用/删除请使用列表操作；变更绑定域将同步到全部成员。</p>
          )}
        </Form>
      </Modal>
    </div>
  );
}
