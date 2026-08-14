import { useEffect, useRef, useState } from "react";
import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  ApiOutlined,
  CheckCircleOutlined,
  CloudDownloadOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  PlusOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  createLlmConfig,
  deleteLlmConfig,
  fetchLlmModels,
  getLlmConfigs,
  getLlmConfigSecret,
  testLlmConfig,
  UnisenseApiError,
  updateLlmConfig,
} from "../api";
import type {
  LlmConfigItem,
  LlmConfigList,
  LlmConfigPayload,
  LlmConfigTestResult,
} from "../types";

// OpenAI 协议兼容提供商的预设（对齐后端 services/llm/config_service.py PROVIDER_DEFAULTS）
const PROVIDER_PRESETS: Record<string, { label: string; base_url: string; model: string }> = {
  openai: { label: "OpenAI", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  deepseek: { label: "DeepSeek", base_url: "https://api.deepseek.com", model: "deepseek-chat" },
  qwen: {
    label: "通义千问",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode",
    model: "qwen-turbo",
  },
  ernie: {
    label: "文心一言",
    base_url: "https://aip.baidubce.com/rpc/2.0/ai_custom",
    model: "ernie-bot-turbo",
  },
  kilo: {
    label: "kilo.ai",
    base_url: "https://api.kilo.ai/api/gateway",
    model: "poolside/laguna-m.1:free",
  },
  custom: { label: "自定义（任意 OpenAI 兼容端点）", base_url: "", model: "" },
};

const SOURCE_LABEL: Record<string, string> = {
  db: "数据库配置",
  env: "环境变量",
  none: "未配置",
};

function TestResultBadge({ result }: { result: LlmConfigTestResult }) {
  if (result.ok) {
    const modelInfo = result.models?.length
      ? ` · 可用模型 ${result.models.length} 个`
      : "";
    return (
      <Tag icon={<CheckCircleOutlined />} color="success">
        连通成功 · {result.latency_ms} ms · {result.model}
        {modelInfo}
      </Tag>
    );
  }
  return (
    <Tag icon={<CloseCircleOutlined />} color="error">
      <span>
        连通失败：{result.error || "未知错误"}
        {result.detail?.request_url ? (
          <span className="muted" style={{ fontSize: 11, marginLeft: 8 }}>
            （{String(result.detail.request_url)}）
          </span>
        ) : null}
      </span>
    </Tag>
  );
}

export function SystemConfig() {
  const [form] = Form.useForm();
  const [data, setData] = useState<LlmConfigList | null>(null);
  const [loading, setLoading] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<LlmConfigItem | null>(null);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState<number | null>(null);
  const [testResults, setTestResults] = useState<Record<number, LlmConfigTestResult>>({});
  const [deleteTarget, setDeleteTarget] = useState<LlmConfigItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [revealingKey, setRevealingKey] = useState(false);
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [modelOptions, setModelOptions] = useState<{ value: string }[]>([]);
  const [fetchingModels, setFetchingModels] = useState(false);
  const hideTimerRef = useRef<number | null>(null);

  function clearReveal() {
    if (hideTimerRef.current != null) {
      window.clearTimeout(hideTimerRef.current);
      hideTimerRef.current = null;
    }
    setRevealedKey(null);
    form.setFieldValue("api_key", "");
  }

  function load() {
    setLoading(true);
    getLlmConfigs()
      .then(setData)
      .catch((err) => {
        message.error(
          err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败",
        );
      })
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, []);

  function handleProviderChange(provider: string) {
    const preset = PROVIDER_PRESETS[provider];
    if (preset) {
      form.setFieldsValue({ base_url: preset.base_url, model: preset.model });
    }
  }

  function openCreate() {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ provider: "custom", timeout: 30, enabled: true, priority: 0 });
    clearReveal();
    setModelOptions([]);
    setModalOpen(true);
  }

  function openEdit(item: LlmConfigItem) {
    setEditing(item);
    form.resetFields();
    form.setFieldsValue({
      name: item.name,
      provider: item.provider || "custom",
      base_url: item.base_url,
      model: item.model,
      timeout: item.timeout,
      enabled: item.enabled,
      priority: item.priority,
      // api_key 不回填明文：留空表示保持原密钥，按需经「显示密钥」按钮解密回显
    });
    clearReveal();
    setModelOptions([]);
    setModalOpen(true);
  }

  async function handleFetchModels() {
    try {
      const values = await form.validateFields(["base_url"]);
      const apiKey = (form.getFieldValue("api_key") as string) || "";
      setFetchingModels(true);
      setModelOptions([]);
      const res = await fetchLlmModels({
        instance_id: editing?.id ?? undefined,
        base_url: values.base_url,
        api_key: apiKey || undefined,
        timeout: (form.getFieldValue("timeout") as number) ?? 30,
      });
      if (res.supported && res.models.length > 0) {
        setModelOptions(res.models.map((m) => ({ value: m })));
        message.success(`获取到 ${res.models.length} 个可用模型（${res.latency_ms}ms）`);
      } else {
        message.warning(
          res.error || "该网关不支持 /models 接口，请手动输入模型名称",
        );
      }
    } catch (err) {
      if (err instanceof Error && "errorFields" in err) return; // 表单校验错误，已高亮
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "获取模型失败",
      );
    } finally {
      setFetchingModels(false);
    }
  }

  async function handleRevealKey() {
    if (editing?.id == null) return;
    setRevealingKey(true);
    try {
      const secret = await getLlmConfigSecret(editing.id);
      form.setFieldValue("api_key", secret.api_key);
      setRevealedKey(secret.api_key);
      // 自动隐藏：15 秒后清空字段，防止密钥停留在表单/内存
      if (hideTimerRef.current != null) window.clearTimeout(hideTimerRef.current);
      hideTimerRef.current = window.setTimeout(() => {
        form.setFieldValue("api_key", "");
        setRevealedKey(null);
        message.info("密钥已自动隐藏（未保存则不会改动）");
      }, 15000);
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "读取密钥失败",
      );
    } finally {
      setRevealingKey(false);
    }
  }

  async function handleSave() {
    try {
      const values = await form.validateFields();
      setSaving(true);
      const payload: LlmConfigPayload = {
        name: values.name || "",
        provider: values.provider,
        base_url: values.base_url,
        model: values.model,
        api_key: values.api_key || "",
        timeout: values.timeout,
        enabled: values.enabled,
        priority: values.priority ?? 0,
      };
      if (editing && editing.id != null) {
        await updateLlmConfig(editing.id, payload);
        message.success("LLM 实例已更新");
      } else {
        await createLlmConfig(payload);
        message.success("LLM 实例已新增");
      }
      setModalOpen(false);
      clearReveal();
      load();
    } catch (err) {
      if (err instanceof Error && "errorFields" in err) return; // 表单校验错误，已高亮
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "保存失败",
      );
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (deleteTarget?.id == null) return;
    setDeleting(true);
    try {
      await deleteLlmConfig(deleteTarget.id);
      message.success(`已删除实例「${deleteTarget.name || deleteTarget.provider}」`);
      setDeleteTarget(null);
      load();
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败",
      );
    } finally {
      setDeleting(false);
    }
  }

  async function handleTest(item: LlmConfigItem) {
    if (item.id == null) return;
    const id: number = item.id;
    setTestingId(id);
    setTestResults((prev) => ({ ...prev, [id]: undefined as unknown as LlmConfigTestResult }));
    try {
      const res = await testLlmConfig({ instance_id: id });
      setTestResults((prev) => ({ ...prev, [id]: res }));
    } catch (err) {
      setTestResults((prev) => ({
        ...prev,
        [id]: {
          ok: false,
          latency_ms: 0,
          model: item.model || "",
          error: err instanceof UnisenseApiError ? err.message : "测试失败",
        },
      }));
    } finally {
      setTestingId(null);
    }
  }

  const canEdit = data?.can_edit ?? false;
  const columns: ColumnsType<LlmConfigItem> = [
    {
      title: "名称",
      dataIndex: "name",
      key: "name",
      width: 140,
      render: (v: string, r) =>
        v || (r.source === "env" ? <Tag>环境变量</Tag> : <span className="muted">未命名</span>),
    },
    {
      title: "提供商",
      dataIndex: "provider",
      key: "provider",
      width: 130,
      render: (v: string) => PROVIDER_PRESETS[v]?.label ?? v,
    },
    {
      title: "接口地址",
      dataIndex: "base_url",
      key: "base_url",
      render: (v: string) => <span className="mono">{v || "—"}</span>,
    },
    {
      title: "模型",
      dataIndex: "model",
      key: "model",
      width: 160,
      render: (v: string) => <span className="mono">{v || "—"}</span>,
    },
    {
      title: "优先级",
      dataIndex: "priority",
      key: "priority",
      width: 80,
      render: (v: number, r) => (r.source === "env" ? "—" : <span className="mono">{v}</span>),
    },
    {
      title: "启用",
      dataIndex: "enabled",
      key: "enabled",
      width: 90,
      render: (v: boolean, r) =>
        r.source === "env" ? (
          <Tag color="blue">env</Tag>
        ) : v ? (
          <Tag color="green">已启用</Tag>
        ) : (
          <Tag>未启用</Tag>
        ),
    },
    ...(canEdit
      ? [
          {
            title: "操作",
            key: "actions",
            width: 240,
            render: (_: unknown, r: LlmConfigItem) => (
              <Space size={4}>
                <Button
                  size="small"
                  icon={<ThunderboltOutlined />}
                  loading={testingId === r.id}
                  onClick={() => handleTest(r)}
                >
                  测试
                </Button>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>
                  编辑
                </Button>
                <Button
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => setDeleteTarget(r)}
                >
                  删除
                </Button>
              </Space>
            ),
          },
        ]
      : []),
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">系统管理 / Settings</div>
          <h2>系统配置</h2>
          <p>平台级配置项：配置多个 LLM 实例后按优先级轮询路由，单实例不可用时自动切换。</p>
        </div>
      </div>

      <Card
        title={
          <Space>
            <ApiOutlined />
            <span>LLM 路由配置</span>
            <Tag color={data?.effective?.source === "none" ? "default" : "green"}>
              {SOURCE_LABEL[data?.effective?.source ?? "none"]}
            </Tag>
            <Tag color="geekblue">轮询 · 故障转移</Tag>
          </Space>
        }
        size="small"
        style={{ marginBottom: 16 }}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="多实例高可用"
          description="请求按优先级轮询选择实例；某实例调用失败时自动切换到下一个可用实例，连续失败的实例进入冷却（约 30 秒）自动恢复，避免单个 LLM 不可用导致 AI 问数 / 指标命名推断不可用。"
        />
        <Table<LlmConfigItem>
          rowKey={(r) => String(r.id ?? `env-${r.base_url}`)}
          columns={columns}
          dataSource={data?.items ?? []}
          loading={loading}
          pagination={false}
          size="small"
          locale={{ emptyText: "尚未配置 LLM 实例" }}
        />
        {Object.entries(testResults).map(([id, res]) =>
          res ? (
            <div key={id} style={{ marginTop: 8 }}>
              <TestResultBadge result={res} />
            </div>
          ) : null,
        )}
        <div style={{ marginTop: 12 }}>
          {canEdit ? (
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              新增 LLM 实例
            </Button>
          ) : (
            <span className="muted" style={{ fontSize: 12 }}>
              仅平台管理员 / 域管理员可配置 LLM 实例。
            </span>
          )}
        </div>
      </Card>

      <Modal
        title={editing ? `编辑 LLM 实例${editing.name ? `（${editing.name}）` : ""}` : "新增 LLM 实例"}
        open={modalOpen}
        onCancel={() => {
          clearReveal();
          setModalOpen(false);
        }}
        onOk={handleSave}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        width={560}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="实例名称" style={{ marginBottom: 12 }}>
            <Input placeholder="如：主用 DeepSeek / 备用通义" maxLength={64} />
          </Form.Item>
          <Form.Item
            name="provider"
            label="提供商"
            rules={[{ required: true, message: "请选择提供商" }]}
            style={{ marginBottom: 12 }}
          >
            <Select
              options={Object.entries(PROVIDER_PRESETS).map(([value, p]) => ({
                value,
                label: p.label,
              }))}
              onChange={handleProviderChange}
            />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="接口地址（OpenAI 兼容）"
            rules={[{ required: true, message: "请输入接口地址" }]}
            style={{ marginBottom: 12 }}
          >
            <Input placeholder="https://api.deepseek.com" className="mono" />
          </Form.Item>
          <Form.Item label="模型名称" required style={{ marginBottom: 12 }}>
            <Space.Compact style={{ width: "100%" }}>
              <Form.Item
                name="model"
                noStyle
                rules={[{ required: true, message: "请输入模型名称" }]}
              >
                <AutoComplete
                  aria-label="模型名称"
                  options={modelOptions}
                  placeholder="deepseek-chat"
                  className="mono"
                  style={{ width: "100%" }}
                  filterOption={(inputValue, option) =>
                    String(option?.value ?? "")
                      .toLowerCase()
                      .includes(inputValue.toLowerCase())
                  }
                />
              </Form.Item>
              <Button
                icon={<CloudDownloadOutlined />}
                loading={fetchingModels}
                onClick={handleFetchModels}
              >
                获取模型
              </Button>
            </Space.Compact>
          </Form.Item>
          <div style={{ marginTop: -8, marginBottom: 12 }}>
            <span className="muted" style={{ fontSize: 12 }}>
              点击「获取模型」从当前接口拉取可用模型列表；网关不支持 /models 时请手动输入。
            </span>
          </div>
          <Form.Item
            name="api_key"
            label={editing ? "API Key（留空保持原密钥）" : "API Key"}
            rules={editing ? [] : [{ required: true, message: "请输入 API Key" }]}
            style={{ marginBottom: 12 }}
          >
            <Input.Password
              placeholder={
                editing && editing.has_api_key ? "已配置（留空保持不变）" : "sk-..."
              }
              autoComplete="new-password"
            />
          </Form.Item>
          {editing?.has_api_key ? (
            <div style={{ marginTop: -8, marginBottom: 12 }}>
              <Space size={8}>
                <Button
                  size="small"
                  icon={<EyeOutlined />}
                  loading={revealingKey}
                  onClick={handleRevealKey}
                >
                  {revealedKey ? "已显示（15 秒后自动隐藏）" : "显示密钥"}
                </Button>
                <span className="muted" style={{ fontSize: 12 }}>
                  密钥加密存储，仅按需解密显示（查看记录会写入审计日志）
                </span>
              </Space>
            </div>
          ) : null}
          <Space size={24}>
            <Form.Item
              name="timeout"
              label="超时（秒）"
              rules={[{ required: true, message: "请输入超时" }]}
              style={{ marginBottom: 12 }}
            >
              <InputNumber min={1} max={300} />
            </Form.Item>
            <Form.Item
              name="priority"
              label="路由优先级"
              tooltip="数值小者优先轮询（0 为最高优先级）"
              style={{ marginBottom: 12 }}
            >
              <InputNumber min={0} max={100} />
            </Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked" style={{ marginBottom: 12 }}>
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      <Modal
        title="删除 LLM 实例"
        open={deleteTarget != null}
        onCancel={() => setDeleteTarget(null)}
        onOk={handleDelete}
        confirmLoading={deleting}
        okText="删除"
        okButtonProps={{ danger: true }}
        cancelText="取消"
        width={440}
      >
        <p>
          确认删除实例「
          <strong>{deleteTarget ? deleteTarget.name || deleteTarget.provider : ""}</strong>
          」？
        </p>
        <p className="muted" style={{ fontSize: 13 }}>
          删除后该实例不再参与 LLM 轮询路由；此操作将保留审计记录。
        </p>
      </Modal>

      <div className="muted" style={{ fontSize: 12 }}>
        配置优先使用数据库存储；未配置任何实例时回落到环境变量（UNISENSE_LLM_*）。保存后 AI
        问数与指标命名推断立即使用新配置（轮询路由）。
      </div>
    </div>
  );
}
