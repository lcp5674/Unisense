import { useEffect, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, InputNumber, message, Space, Typography, Radio, Alert } from "antd";
import { PlusOutlined, KeyOutlined, CopyOutlined, ReloadOutlined, ExperimentOutlined, ApiOutlined } from "@ant-design/icons";
import {
  createApiClient,
  listApiClients,
  mintClientToken,
  consumeDryRun,
  listMetrics,
  setConsumeToken,
  getConsumeToken,
  UnisenseApiError,
} from "../api";
import { usePermission } from "../hooks/usePermission";
import type { ClientResponse } from "../types";

const { Paragraph } = Typography;

// 消费令牌有效期选项（分钟）——后端签发端点支持 5~1440，前端提供常用档位
const TOKEN_TTL_OPTIONS = [
  { value: 60, label: "60 分钟（默认）" },
  { value: 240, label: "4 小时" },
  { value: 720, label: "12 小时" },
  { value: 1440, label: "24 小时" },
];

// consume 查询端点清单（供接入指南展示，与 backend/app/api/consume.py 对齐）
const CONSUME_ENDPOINTS = [
  { method: "POST", path: "/api/v1/consume/query/dry-run", desc: "语义校验（不执行/不计费）" },
  { method: "POST", path: "/api/v1/consume/query", desc: "执行指标查询" },
  { method: "GET", path: "/api/v1/consume/metrics/{code}/semantic", desc: "指标语义（只读）" },
  { method: "GET", path: "/api/v1/consume/metrics/{code}/snapshots", desc: "结果快照" },
  { method: "GET", path: "/api/v1/consume/stats/response-time", desc: "查询响应时效" },
];

export function ApiClients() {
  const { can } = usePermission();
  const canManage = can("api-clients:manage");
  const [items, setItems] = useState<ClientResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [form] = Form.useForm();
  // 签发令牌弹窗状态
  const [mintOpen, setMintOpen] = useState(false);
  const [mintClient, setMintClient] = useState<ClientResponse | null>(null);
  const [mintTtl, setMintTtl] = useState(60);
  const [minting, setMinting] = useState(false);
  // 连通性测试状态
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null);

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

  function openMint(r: ClientResponse) {
    setMintClient(r);
    setMintTtl(60);
    setTestResult(null);
    setMintOpen(true);
  }

  async function handleMintConfirm() {
    if (!mintClient) return;
    setMinting(true);
    try {
      const { access_token } = await mintClientToken(mintClient.client_id, mintTtl);
      navigator.clipboard?.writeText(access_token).catch(() => {});
      setConsumeToken(access_token);
      message.success(`已签发令牌并复制到剪贴板（${mintTtl} 分钟有效）`);
      setMintOpen(false);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "签发失败");
    } finally {
      setMinting(false);
    }
  }

  // 连通性测试：签发令牌 → 用首个已发布指标调 dry-run 验证 consume 全链路
  async function handleConnectivityTest() {
    setTesting(true);
    setTestResult(null);
    try {
      const clients = await listApiClients();
      const active = clients.find((c) => c.status === "ACTIVE");
      if (!active) {
        setTestResult({ ok: false, message: "没有 ACTIVE 的 API 客户端，请先创建客户端" });
        return;
      }
      if (!getConsumeToken()) {
        const { access_token } = await mintClientToken(active.client_id, 60);
        setConsumeToken(access_token);
      }
      const { items: metrics } = await listMetrics({ status: "PUBLISHED", page_size: 1 });
      if (!metrics.length) {
        setTestResult({
          ok: true,
          message: `令牌已签发（客户端 ${active.client_id}），但平台暂无已发布指标，无法执行查询连通性验证`,
        });
        return;
      }
      const code = metrics[0].metric_code;
      const res = await consumeDryRun({ metric_code: code, date_range: "today,today" });
      setTestResult({
        ok: res.status === "ok",
        message: `客户端 ${active.client_id} 连通正常：指标 ${code} dry-run ${res.status === "ok" ? "通过" : "被拒绝"}（耗时 ${String((res as { execution_plan?: { elapsed_ms?: number } }).execution_plan?.elapsed_ms ?? "-")} ms）`,
      });
    } catch (err) {
      setTestResult({ ok: false, message: err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "连通性测试失败" });
    } finally {
      setTesting(false);
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
        <Button size="small" icon={<KeyOutlined />} disabled={r.status !== "ACTIVE" || !canManage} onClick={() => openMint(r)}>
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

      <Card
        style={{ marginTop: 16 }}
        title={
          <span>
            <ApiOutlined /> 接入指南
          </span>
        }
        extra={
          <Button icon={<ExperimentOutlined />} onClick={handleConnectivityTest} loading={testing}>
            连通性测试
          </Button>
        }
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="两种消费鉴权方式"
          description="方式一：X-Api-Key: client_id:密钥（长期接入、无过期，密钥仅创建时展示一次）；方式二：行内「签发令牌」换短效 Bearer 令牌（调试用，有效期可选，最长 24 小时）。"
        />
        {testResult && (
          <Alert
            type={testResult.ok ? "success" : "error"}
            showIcon
            style={{ marginBottom: 12 }}
            message="连通性测试结果"
            description={testResult.message}
            closable
            onClose={() => setTestResult(null)}
          />
        )}
        <Table
          size="small"
          dataSource={CONSUME_ENDPOINTS}
          columns={[
            { title: "方法", dataIndex: "method", key: "method", width: 80, render: (v: string) => <Tag color={v === "GET" ? "blue" : "green"}>{v}</Tag> },
            { title: "路径", dataIndex: "path", key: "path", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
            { title: "说明", dataIndex: "desc", key: "desc" },
          ]}
          pagination={false}
          rowKey="path"
          style={{ marginBottom: 12 }}
        />
        <Paragraph strong>查询示例（curl）</Paragraph>
        <pre className="code-block">{`# 方式一：X-Api-Key（长期接入，无过期）
curl -X POST http://<host>:8180/api/v1/consume/query/dry-run \\
  -H "Content-Type: application/json" \\
  -H "X-Api-Key: app_xxxx:你的密钥" \\
  -d '{"metric_code":"outp_doctor_active_cnt_month","date_range":"2026-08-01,2026-08-31"}'

# 方式二：Bearer 消费令牌（短效调试，本页签发）
curl -X POST http://<host>:8180/api/v1/consume/query \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <令牌>" \\
  -d '{"metric_code":"outp_doctor_active_cnt_month","date_range":"2026-08-01,2026-08-31"}'`}</pre>
      </Card>

      <Modal
        title={`签发消费令牌：${mintClient?.client_id ?? ""}`}
        open={mintOpen}
        onCancel={() => setMintOpen(false)}
        onOk={handleMintConfirm}
        confirmLoading={minting}
        okText="签发并复制"
      >
        <Paragraph type="secondary">
          令牌将复制到剪贴板，并同步为查询工作台的当前消费令牌（5~1440 分钟有效）。
        </Paragraph>
        <div style={{ marginBottom: 8 }}>有效期</div>
        <Radio.Group
          value={mintTtl}
          onChange={(e) => setMintTtl(e.target.value)}
          options={TOKEN_TTL_OPTIONS}
        />
        <Alert
          type="info"
          showIcon
          style={{ marginTop: 12 }}
          message="有效期越长泄露风险越高，建议按需选择最短时长；外部长期接入推荐 X-Api-Key（无过期）。"
        />
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
