import { useEffect, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, InputNumber, message, Space, Typography } from "antd";
import { PlusOutlined, KeyOutlined, CopyOutlined, ReloadOutlined } from "@ant-design/icons";
import { createApiClient, listApiClients, mintClientToken, UnisenseApiError } from "../api";
import { usePermission } from "../hooks/usePermission";
import type { ClientResponse } from "../types";

const { Paragraph } = Typography;

export function ApiClients() {
  const { can } = usePermission();
  const canManage = can("api-clients:manage");
  const [items, setItems] = useState<ClientResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [form] = Form.useForm();

  async function load() {
    setLoading(true);
    try {
      setItems(await listApiClients());
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCreate(values: Record<string, unknown>) {
    setLoading(true);
    try {
      const created = await createApiClient({
        client_id: values.client_id ? String(values.client_id) : undefined,
        secret: String(values.secret),
        scope_domain: values.scope_domain ? String(values.scope_domain) : null,
        metric_whitelist: values.metric_whitelist ? String(values.metric_whitelist).split(",").map((s) => s.trim()).filter(Boolean) : null,
        qps: Number(values.qps ?? 20),
        daily_quota: Number(values.daily_quota ?? 100000),
      });
      setSecret(created.secret);
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    } finally {
      setLoading(false);
    }
  }

  async function handleMint(clientId: string) {
    try {
      const { access_token } = await mintClientToken(clientId);
      navigator.clipboard?.writeText(access_token).catch(() => {});
      message.success(`已签发令牌并复制到剪贴板（60 分钟有效）`);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "签发失败");
    }
  }

  const columns = [
    { title: "Client ID", dataIndex: "client_id", key: "client_id", render: (v: string) => <span className="mono">{v}</span> },
    { title: "作用域域", dataIndex: "scope_domain", key: "scope_domain", render: (v: string | null) => v ?? <span className="muted">全部</span> },
    {
      title: "指标白名单",
      dataIndex: "metric_whitelist",
      key: "whitelist",
      ellipsis: true,
      render: (v: string[] | null) => (v?.length ? v.join(", ") : <span className="muted">全部</span>),
    },
    { title: "QPS", dataIndex: "qps", key: "qps", width: 90 },
    { title: "日配额", dataIndex: "daily_quota", key: "daily_quota", width: 120 },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (v: string) => <Tag color={v === "ACTIVE" ? "success" : "default"}>{v === "ACTIVE" ? "启用" : "已吊销"}</Tag>,
    },
    {
      title: "操作",
      key: "actions",
      width: 160,
      render: (_: unknown, r: ClientResponse) => (
        <Button size="small" icon={<KeyOutlined />} disabled={r.status !== "ACTIVE" || !canManage} onClick={() => handleMint(r.client_id)}>
          签发令牌
        </Button>
      ),
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Consumption / API Clients</div>
          <h2>API 客户端</h2>
          <p>管理消费查询的 API 客户端——每个客户端持有独立密钥、域与指标白名单。</p>
        </div>
        {canManage && <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
          新建客户端
        </Button>}
      </div>

      <Card extra={<Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button>}>
        <Table dataSource={items} columns={columns} rowKey="client_id" loading={loading} pagination={false} locale={{ emptyText: "暂无 API 客户端" }} />
      </Card>

      <Modal
        title="新建 API 客户端"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={loading}
        okText="创建"
      >
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="client_id" label="Client ID" extra={<span className="mono" style={{ color: "#0E7C86" }}>留空则由系统生成 app_ 前缀 ID</span>}>
            <Input className="mono" placeholder="留空自动生成（app_ 前缀）" />
          </Form.Item>
          <Form.Item name="secret" label="密钥" rules={[{ required: true, min: 8 }]}>
            <Input.Password placeholder="至少 8 位" />
          </Form.Item>
          <Form.Item name="scope_domain" label="作用域（业务域，留空为全部）">
            <Input placeholder="如 finance" />
          </Form.Item>
          <Form.Item name="metric_whitelist" label="指标白名单（逗号分隔，留空为全部）">
            <Input placeholder="finance_revenue_sum_d, finance_cost_sum_d" />
          </Form.Item>
          <Space size={16}>
            <Form.Item name="qps" label="QPS" initialValue={20}>
              <InputNumber min={1} max={1000} style={{ width: 120 }} />
            </Form.Item>
            <Form.Item name="daily_quota" label="日配额" initialValue={100000}>
              <InputNumber min={1} style={{ width: 160 }} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      <Modal
        title="客户端创建成功 — 请立即保存密钥"
        open={!!secret}
        onCancel={() => setSecret(null)}
        footer={<Button type="primary" onClick={() => setSecret(null)}>我已保存</Button>}
      >
        <Paragraph type="secondary">
          明文密钥只在此展示一次。请复制保存，后续无法再次查看。
        </Paragraph>
        <pre className="code-block" style={{ userSelect: "all" }}>{secret}</pre>
        <Button
          icon={<CopyOutlined />}
          onClick={() => {
            navigator.clipboard?.writeText(secret ?? "").catch(() => {});
            message.success("已复制");
          }}
        >
          复制密钥
        </Button>
      </Modal>
    </div>
  );
}
