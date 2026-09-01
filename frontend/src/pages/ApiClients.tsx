import { useEffect, useState } from "react";
import {
  Card,
  Table,
  Tag,
  Button,
  Modal,
  Form,
  Input,
  InputNumber,
  message,
  Space,
  Typography,
  Radio,
  Alert,
  Select,
  Dropdown,
} from "antd";
import type { FormInstance } from "antd";
import {
  PlusOutlined,
  KeyOutlined,
  CopyOutlined,
  ReloadOutlined,
  ExperimentOutlined,
  ApiOutlined,
  EditOutlined,
  DeleteOutlined,
  DownOutlined,
  StopOutlined,
  CheckCircleOutlined,
} from "@ant-design/icons";
import {
  createApiClient,
  listApiClients,
  mintClientToken,
  consumeQuery,
  listMetrics,
  setConsumeToken,
  getConsumeToken,
  UnisenseApiError,
  updateApiClient,
  updateApiClientStatus,
  deleteApiClient,
  batchApiClientAction,
  listDomainTree,
} from "../api";
import { usePermission } from "../hooks/usePermission";
import type { ClientResponse, ClientUpdateRequest, SubjectDomainTreeNode } from "../types";

const { Paragraph } = Typography;

// 消费令牌有效期选项（分钟）——后端签发端点支持 5~1440，前端提供常用档位
const TOKEN_TTL_OPTIONS = [
  { value: 60, label: "60 分钟（默认）" },
  { value: 240, label: "4 小时" },
  { value: 720, label: "12 小时" },
  { value: 1440, label: "24 小时" },
];

// 批量操作菜单项
const BATCH_ACTIONS = [
  { key: "enable", label: "批量启用" },
  { key: "disable", label: "批量停用" },
  { key: "delete", label: "批量删除", danger: true },
];

// R8（审查修复）：最小授权范围——作用域与指标白名单至少填一个。
// 双空时后端四级闸门四个条件全不触发，将获得全部非 PII 指标消费权（越权缺口）。
// allowEmptyInitial=true 时放行「初始即双空」的历史客户端（保持存量，仅标记警告），
// 但「原本有授权 → 清成双空」仍拦截。
function validateScopeRequired(
  form: FormInstance,
  _: unknown,
  value: unknown,
  allowEmptyInitial = false,
) {
  const wl = form.getFieldValue("metric_whitelist");
  const hasDomain = typeof value === "string" && value.length > 0;
  const hasWhitelist = Array.isArray(wl) && wl.length > 0;
  if (hasDomain || hasWhitelist || allowEmptyInitial) {
    return Promise.resolve();
  }
  return Promise.reject(new Error("作用域与指标白名单至少填一个（最小授权范围）"));
}

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
  // 编辑弹窗状态
  const [editOpen, setEditOpen] = useState(false);
  const [editingClient, setEditingClient] = useState<ClientResponse | null>(null);
  const [savingEdit, setSavingEdit] = useState(false);
  // 批量选择状态
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [batchBusy, setBatchBusy] = useState(false);
  // 主题域选项（scope_domain 下拉，避免手填错值导致 403）
  const [domainOptions, setDomainOptions] = useState<{ value: string; label: string }[]>([]);
  // 指标白名单选项（供新建/编辑弹窗下拉多选，替代手动逗号输入；与 Governance 授权弹窗同源）
  const [metricOptions, setMetricOptions] = useState<{ value: string; label: string }[]>([]);
  const [editForm] = Form.useForm();

  useEffect(() => {
    listDomainTree()
      .then((tree: SubjectDomainTreeNode[]) => {
        const opts: { value: string; label: string }[] = [];
        const walk = (nodes: SubjectDomainTreeNode[]) => {
          for (const n of nodes) {
            if (n.code) opts.push({ value: n.code, label: `${n.name} (${n.code})` });
            if (n.children?.length) walk(n.children);
          }
        };
        walk(tree);
        setDomainOptions(opts);
      })
      .catch(() => {
        /* 主题域加载失败不阻断页面，scope_domain 仍可手填 */
      });
    // 指标白名单选项：拉取已发布指标（编码 + 名称，供搜索多选）
    listMetrics({ status: "PUBLISHED", page: 1, page_size: 200 })
      .then((res) =>
        setMetricOptions(res.items.map((m) => ({ value: m.metric_code, label: `${m.metric_code}（${m.name}）` }))),
      )
      .catch(() => {
        /* 指标加载失败不阻断页面，白名单仍可手动输入已有值 */
      });
  }, []);

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
        metric_whitelist: Array.isArray(values.metric_whitelist) && values.metric_whitelist.length
          ? values.metric_whitelist
          : null,
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
      const { items: metrics } = await listMetrics({ status: "PUBLISHED", page_size: 50 });
      if (!metrics.length) {
        setTestResult({
          ok: true,
          message: `令牌已签发（客户端 ${active.client_id}），但平台暂无已发布指标，无法执行查询连通性验证`,
        });
        return;
      }
      // 逐个尝试已发布指标（最多前 10 个）：跳过「未声明来源表」等不可查询的指标，
      // 找到第一个真实执行成功的即证明令牌/授权/查询链路完整连通（含真实执行 + 数据返回）。
      const today = new Date().toISOString().slice(0, 10);
      const dateRange = `${today}~${today}`;
      const candidates = metrics.slice(0, 10);
      let lastErr: unknown = null;
      for (const m of candidates) {
        try {
          // 真实执行：POST /consume/query 走完整链路（鉴权 → 口径校验 → SQL 构建 → 引擎执行 → 结果返回），
          // data.elapsed_ms 是后端真实执行耗时，往返耗时由前端实测。
          const t0 = performance.now();
          const res = await consumeQuery({ metric_code: m.metric_code, date_range: dateRange });
          const roundTrip = Math.round(performance.now() - t0);
          const rows = Array.isArray(res.data?.rows) ? res.data.rows : [];
          const execMs = typeof res.data?.elapsed_ms === "number" ? res.data.elapsed_ms : null;
          const engine = res.data?.engine ?? "?";
          setTestResult({
            ok: true,
            message:
              `客户端 ${active.client_id} 连通正常：指标 ${m.metric_code} 查询成功` +
              `（返回 ${res.data?.total ?? rows.length} 行 · 引擎 ${engine}` +
              `${execMs != null ? ` · 执行耗时 ${execMs} ms` : ""}` +
              ` · 链路往返 ${roundTrip} ms）`,
          });
          return;
        } catch (err) {
          lastErr = err;
          // 校验/依赖类失败（未声明来源表、挂载缺失、数据源不可达等）→ 该指标不可查询，继续试下一个
          if (err instanceof UnisenseApiError && (err.message || "").includes("未声明来源表")) {
            continue;
          }
          continue;
        }
      }
      setTestResult({
        ok: false,
        message: `已尝试 ${candidates.length} 个已发布指标均无法完成查询连通性验证（可能未配置挂载/来源表或底层数据源不可达）：${lastErr instanceof UnisenseApiError ? lastErr.message : "真实查询被拒绝"}`,
      });
    } catch (err) {
      setTestResult({ ok: false, message: err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "连通性测试失败" });
    } finally {
      setTesting(false);
    }
  }

  function openEdit(r: ClientResponse) {
    setEditingClient(r);
    // 白名单中不在当前已发布选项列表的旧编码（停用/下架/删除）也并入选项，避免回填显示裸编码
    setMetricOptions((prev) => {
      const existing = new Set(prev.map((o) => o.value));
      const missing = (r.metric_whitelist ?? []).filter((c) => !existing.has(c));
      if (!missing.length) return prev;
      return [...prev, ...missing.map((c) => ({ value: c, label: `${c}（已下线或非发布态）` }))];
    });
    editForm.setFieldsValue({
      scope_domain: r.scope_domain ?? undefined,
      metric_whitelist: r.metric_whitelist ?? undefined,
      qps: r.qps,
      daily_quota: r.daily_quota,
    });
    setEditOpen(true);
  }

  async function handleEditSave() {
    if (!editingClient) return;
    setSavingEdit(true);
    try {
      const values = await editForm.validateFields();
      const hasDomain = typeof values.scope_domain === "string" && values.scope_domain.length > 0;
      const hasWhitelist = Array.isArray(values.metric_whitelist) && values.metric_whitelist.length > 0;
      // R8：历史双空客户端（初始即无授权范围）且本次仍未补充任何授权 → 不传授权字段，
      // 后端视为「不修改」而保留存量（仅标记警告，不强制破坏）；否则显式同步当前值
      //（后端校验：合并后双空将被拒绝，防止把已有授权清成双空）。
      const initialBothEmpty = !editingClient.scope_domain && !(editingClient.metric_whitelist?.length);
      const payload: ClientUpdateRequest =
        initialBothEmpty && !hasDomain && !hasWhitelist
          ? {
              qps: values.qps != null ? Number(values.qps) : null,
              daily_quota: values.daily_quota != null ? Number(values.daily_quota) : null,
            }
          : {
              scope_domain: hasDomain ? String(values.scope_domain) : "",
              metric_whitelist: hasWhitelist ? values.metric_whitelist : [],
              qps: values.qps != null ? Number(values.qps) : null,
              daily_quota: values.daily_quota != null ? Number(values.daily_quota) : null,
            };
      await updateApiClient(editingClient.client_id, payload);
      message.success(`客户端 ${editingClient.client_id} 已更新`);
      setEditOpen(false);
      load();
    } catch (err) {
      if (err instanceof Error && "errorFields" in err) return; // 表单校验错误，静默
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    } finally {
      setSavingEdit(false);
    }
  }

  async function handleToggleStatus(r: ClientResponse) {
    const target = r.status === "ACTIVE" ? "REVOKED" : "ACTIVE";
    try {
      await updateApiClientStatus(r.client_id, target);
      message.success(`客户端 ${r.client_id} 已${target === "ACTIVE" ? "启用" : "停用"}`);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  async function handleDelete(r: ClientResponse) {
    try {
      await deleteApiClient(r.client_id);
      message.success(`客户端 ${r.client_id} 已删除（软删，可追溯）`);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败");
    }
  }

  // 「更多」下拉中的危险操作二次确认（停用/删除）——Modal.confirm 与 Dropdown menu 搭配的标准做法
  function confirmToggle(r: ClientResponse) {
    Modal.confirm({
      title: r.status === "ACTIVE" ? "确认停用该客户端？" : "确认启用该客户端？",
      content: r.status === "ACTIVE" ? "停用后 X-Api-Key 与已签短效令牌将立即失效。" : "启用后即可恢复消费访问。",
      okText: "确认",
      cancelText: "取消",
      okButtonProps: r.status === "ACTIVE" ? { danger: true } : undefined,
      onOk: () => handleToggleStatus(r),
    });
  }

  function confirmDelete(r: ClientResponse) {
    Modal.confirm({
      title: "确认删除该客户端？",
      content: "软删除（保留审计追溯），删除后不可恢复消费访问。",
      okText: "确认",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => handleDelete(r),
    });
  }

  async function handleBatch(action: string) {
    if (!selectedKeys.length) {
      message.warning("请先勾选客户端");
      return;
    }
    setBatchBusy(true);
    try {
      const res = await batchApiClientAction({ action: action as "enable" | "disable" | "delete", client_ids: selectedKeys });
      const actionLabel = action === "enable" ? "启用" : action === "disable" ? "停用" : "删除";
      if (res.fail_count > 0) {
        message.warning(
          `批量${actionLabel}完成：成功 ${res.ok_count} / 失败 ${res.fail_count}（${res.results.filter((r) => !r.ok).map((r) => r.client_id).join(", ")}）`,
        );
      } else {
        message.success(`批量${actionLabel}成功：${res.ok_count} 个客户端`);
      }
      setSelectedKeys([]);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量操作失败");
    } finally {
      setBatchBusy(false);
    }
  }

  const unscopedCount = items.filter((c) => !c.scope_domain && !(c.metric_whitelist?.length)).length;

  const columns = [
    { title: "Client ID", dataIndex: "client_id", key: "client_id", render: (v: string) => <span className="mono">{v}</span> },
    {
      title: "作用域域",
      dataIndex: "scope_domain",
      key: "scope_domain",
      render: (v: string | null, r: ClientResponse) => {
        // R8：双空=未配置授权范围（可消费全部非 PII 指标）→ 醒目警告标记
        if (!v && !(r.metric_whitelist?.length)) {
          return <Tag color="warning">未配置授权范围</Tag>;
        }
        return v ?? <span className="muted">全部</span>;
      },
    },
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
      width: 200,
      render: (_: unknown, r: ClientResponse) => (
        <Space size={8}>
          <Button
            size="small"
            type="primary"
            icon={<KeyOutlined />}
            disabled={r.status !== "ACTIVE" || !canManage}
            onClick={() => openMint(r)}
          >
            签发令牌
          </Button>
          <Dropdown
            menu={{
              items: [
                { key: "edit", icon: <EditOutlined />, label: "编辑", disabled: !canManage },
                {
                  key: "toggle",
                  icon: r.status === "ACTIVE" ? <StopOutlined /> : <CheckCircleOutlined />,
                  label: r.status === "ACTIVE" ? "停用" : "启用",
                  danger: r.status === "ACTIVE",
                  disabled: !canManage,
                },
                { type: "divider" },
                { key: "delete", icon: <DeleteOutlined />, label: "删除", danger: true, disabled: !canManage },
              ],
              onClick: ({ key }) => {
                if (key === "edit") openEdit(r);
                else if (key === "toggle") confirmToggle(r);
                else if (key === "delete") confirmDelete(r);
              },
            }}
          >
            <Button size="small" disabled={!canManage}>
              更多 <DownOutlined />
            </Button>
          </Dropdown>
        </Space>
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

      <Card
        extra={
          <Space>
            <Dropdown
              menu={{
                items: BATCH_ACTIONS,
                onClick: ({ key }) => handleBatch(key),
              }}
              disabled={!selectedKeys.length || !canManage || batchBusy}
            >
              <Button loading={batchBusy}>
                批量操作 <DownOutlined />
              </Button>
            </Dropdown>
            <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
              刷新
            </Button>
          </Space>
        }
      >
        {unscopedCount > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={`${unscopedCount} 个客户端未配置授权范围`}
            description="作用域与指标白名单均为空的客户端将可消费全部非 PII 指标的历史数据值（最小授权原则）。请尽快编辑补充授权范围；新建客户端已强制至少填其一。"
          />
        )}
        <Table
          dataSource={items}
          columns={columns}
          rowKey="client_id"
          loading={loading}
          pagination={false}
          rowSelection={
            canManage
              ? {
                  selectedRowKeys: selectedKeys,
                  onChange: (keys) => setSelectedKeys(keys as string[]),
                }
              : undefined
          }
          locale={{ emptyText: "暂无 API 客户端" }}
        />
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
          <Form.Item
            name="scope_domain"
            label="作用域（业务域）"
            dependencies={["metric_whitelist"]}
            rules={[{ validator: (_, v) => validateScopeRequired(form, _, v) }]}
            extra="与指标白名单至少填一个（最小授权范围）；双空 = 全部非 PII 指标消费权，已禁止"
          >
            <Select
              allowClear
              showSearch
              placeholder="选择业务域（可留空，但白名单必填）"
              options={domainOptions}
              optionFilterProp="label"
              notFoundContent={domainOptions.length ? undefined : "主题域加载中或为空"}
            />
          </Form.Item>
          <Form.Item
            name="metric_whitelist"
            label="指标白名单（可多选）"
            extra="从已发布指标中选择，避免手填编码与指标域不符导致查询 403；与作用域至少填一个"
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              placeholder="选择已发布指标（编码）"
              options={metricOptions}
              optionFilterProp="label"
              notFoundContent={metricOptions.length ? undefined : "已发布指标加载中或为空"}
            />
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
          <Button
            icon={<ExperimentOutlined />}
            onClick={handleConnectivityTest}
            loading={testing}
            disabled={!canManage}
          >
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
  -d '{"metric_code":"outp_doctor_active_cnt_month","date_range":"2026-08-01~2026-08-31"}'

# 方式二：Bearer 消费令牌（短效调试，本页签发）
curl -X POST http://<host>:8180/api/v1/consume/query \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <令牌>" \\
  -d '{"metric_code":"outp_doctor_active_cnt_month","date_range":"2026-08-01~2026-08-31"}'`}</pre>
      </Card>

      <Modal
        title={`编辑 API 客户端：${editingClient?.client_id ?? ""}`}
        open={editOpen}
        onCancel={() => setEditOpen(false)}
        onOk={handleEditSave}
        confirmLoading={savingEdit}
        okText="保存"
      >
        <Form form={editForm} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item
            name="scope_domain"
            label="作用域（业务域）"
            dependencies={["metric_whitelist"]}
            rules={[
              {
                validator: (_, v) =>
                  validateScopeRequired(
                    editForm,
                    _,
                    v,
                    !editingClient?.scope_domain && !(editingClient?.metric_whitelist?.length),
                  ),
              },
            ]}
            extra="与指标白名单至少保留一个（最小授权范围），双空将无法提交；密钥不可修改（如需换密钥请删除重建）"
          >
            <Select
              allowClear
              showSearch
              placeholder="选择业务域（可留空，但白名单必填）"
              options={domainOptions}
              optionFilterProp="label"
              notFoundContent={domainOptions.length ? undefined : "主题域加载中或为空"}
            />
          </Form.Item>
          <Form.Item
            name="metric_whitelist"
            label="指标白名单（可多选）"
            extra="从已发布指标中选择，已下线/删除的存量编码会保留显示；与作用域至少填一个"
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              placeholder="选择已发布指标（编码）"
              options={metricOptions}
              optionFilterProp="label"
              notFoundContent={metricOptions.length ? undefined : "已发布指标加载中或为空"}
            />
          </Form.Item>
          <Space size={16}>
            <Form.Item name="qps" label="QPS">
              <InputNumber min={1} max={1000} style={{ width: 120 }} />
            </Form.Item>
            <Form.Item name="daily_quota" label="日配额">
              <InputNumber min={1} style={{ width: 160 }} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

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
