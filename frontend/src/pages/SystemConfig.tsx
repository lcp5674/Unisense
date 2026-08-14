import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Tag,
  message,
} from "antd";
import {
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SaveOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { getLlmConfig, saveLlmConfig, testLlmConfig, UnisenseApiError } from "../api";
import type { LlmConfig as LlmConfigType, LlmConfigTestResult } from "../types";

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
  kilo: { label: "kilo.ai", base_url: "https://api.kilo.ai/api/gateway", model: "poolside/laguna-m.1:free" },
  custom: { label: "自定义（任意 OpenAI 兼容端点）", base_url: "", model: "" },
};

const SOURCE_LABEL: Record<string, string> = {
  db: "数据库配置",
  env: "环境变量",
  none: "未配置",
};

export function SystemConfig() {
  const [form] = Form.useForm();
  const [llmConfig, setLlmConfig] = useState<LlmConfigType | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<LlmConfigTestResult | null>(null);

  useEffect(() => {
    getLlmConfig()
      .then((cfg) => {
        setLlmConfig(cfg);
        if (cfg.can_edit) {
          form.setFieldsValue({
            provider: cfg.provider || "custom",
            base_url: cfg.base_url,
            model: cfg.model,
            timeout: cfg.timeout,
            enabled: cfg.enabled,
            // api_key 不回填明文：留空表示保持原密钥
          });
        }
      })
      .catch(() => setLlmConfig(null));
  }, [form]);

  function handleProviderChange(provider: string) {
    const preset = PROVIDER_PRESETS[provider];
    if (preset) {
      form.setFieldsValue({ base_url: preset.base_url, model: preset.model });
    }
  }

  async function handleSaveConfig() {
    try {
      const values = await form.validateFields();
      setSaving(true);
      setTestResult(null);
      await saveLlmConfig({
        provider: values.provider,
        base_url: values.base_url,
        model: values.model,
        api_key: values.api_key || "",
        timeout: values.timeout,
        enabled: values.enabled,
      });
      message.success("LLM 配置已保存");
      const cfg = await getLlmConfig();
      setLlmConfig(cfg);
    } catch (err) {
      if (err instanceof Error && "errorFields" in err) return; // 表单校验错误，已高亮
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleTestConfig() {
    try {
      const values = await form.validateFields();
      setTesting(true);
      setTestResult(null);
      try {
        const res = await testLlmConfig({
          base_url: values.base_url,
          model: values.model,
          api_key: values.api_key || undefined,
          timeout: values.timeout,
        });
        setTestResult(res);
      } catch (err) {
        setTestResult({
          ok: false,
          latency_ms: 0,
          model: values.model || "",
          error: err instanceof UnisenseApiError ? err.message : "测试失败",
        });
      } finally {
        setTesting(false);
      }
    } catch {
      // 表单校验失败，忽略
    }
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">系统管理 / Settings</div>
          <h2>系统配置</h2>
          <p>平台级配置项：连接 LLM 服务后，AI 问数与指标命名推断将使用该配置。</p>
        </div>
      </div>

      <Card
        title={
          <Space>
            <ApiOutlined />
            <span>LLM 配置</span>
            {llmConfig && (
              <Tag color={llmConfig.source === "none" ? "default" : "green"}>
                {SOURCE_LABEL[llmConfig.source] ?? llmConfig.source}
              </Tag>
            )}
          </Space>
        }
        size="small"
        style={{ marginBottom: 16 }}
        extra={
          llmConfig?.enabled ? <Tag color="green">已启用</Tag> : <Tag>未启用</Tag>
        }
      >
        {!llmConfig ? (
          <Alert type="info" message="加载配置中…" showIcon />
        ) : !llmConfig.can_edit ? (
          <Space direction="vertical" size={4}>
            <div>接口地址：<span className="mono">{llmConfig.base_url || "—"}</span></div>
            <div>模型：<span className="mono">{llmConfig.model || "—"}</span></div>
            <div>
              密钥：{llmConfig.has_api_key ? "已配置" : "未配置"}
              {llmConfig.source !== "none" && (
                <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                  （当前为{SOURCE_LABEL[llmConfig.source]}，仅平台管理员可修改）
                </span>
              )}
            </div>
          </Space>
        ) : (
          <Form form={form} layout="vertical">
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              <Form.Item name="provider" label="提供商" initialValue="custom" style={{ marginBottom: 8 }}>
                <Select
                  options={Object.entries(PROVIDER_PRESETS).map(([value, p]) => ({
                    value,
                    label: p.label,
                  }))}
                  onChange={handleProviderChange}
                  style={{ maxWidth: 360 }}
                />
              </Form.Item>
              <Form.Item
                name="base_url"
                label="接口地址（OpenAI 兼容）"
                rules={[{ required: true, message: "请输入接口地址" }]}
                style={{ marginBottom: 8 }}
              >
                <Input placeholder="https://api.deepseek.com" className="mono" />
              </Form.Item>
              <Form.Item
                name="model"
                label="模型名称"
                rules={[{ required: true, message: "请输入模型名称" }]}
                style={{ marginBottom: 8 }}
              >
                <Input placeholder="deepseek-chat" className="mono" />
              </Form.Item>
              <Form.Item name="api_key" label="API Key（留空保持不变）" style={{ marginBottom: 8 }}>
                <Input.Password
                  placeholder={llmConfig.has_api_key ? "已配置（留空保持不变）" : "sk-..."}
                  autoComplete="new-password"
                  style={{ maxWidth: 480 }}
                />
              </Form.Item>
              <Space size={16} style={{ marginBottom: 8 }}>
                <Form.Item name="timeout" label="超时（秒）" initialValue={30} style={{ marginBottom: 0 }}>
                  <InputNumber min={1} max={300} />
                </Form.Item>
                <Form.Item name="enabled" label="启用" valuePropName="checked" initialValue={false} style={{ marginBottom: 0 }}>
                  <Switch />
                </Form.Item>
              </Space>
              <Space>
                <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSaveConfig}>
                  保存配置
                </Button>
                <Button icon={<ThunderboltOutlined />} loading={testing} onClick={handleTestConfig}>
                  测试连通性
                </Button>
                {testResult && (
                  <TestResultBadge result={testResult} />
                )}
              </Space>
              <div className="muted" style={{ fontSize: 12 }}>
                配置优先使用数据库存储；未配置时回落到环境变量（UNISENSE_LLM_*）。保存后 AI 问数与指标命名推断立即使用新配置。
              </div>
            </Space>
          </Form>
        )}
      </Card>

      <Collapse
        ghost
        items={[
          {
            key: "config-help",
            label: "支持哪些提供商？",
            children: (
              <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.8 }}>
                <li>任意「OpenAI 协议兼容」端点：OpenAI / DeepSeek / 通义千问 / 文心一言 / kilo.ai / 自建网关等</li>
                <li>选择提供商后自动填充默认接口地址与模型，也可手动修改为自定义端点</li>
                <li>API Key 仅加密存储在服务端（Fernet），接口响应与前端均不回显明文</li>
              </ul>
            ),
          },
        ]}
      />
    </div>
  );
}

function TestResultBadge({ result }: { result: LlmConfigTestResult }) {
  if (result.ok) {
    return (
      <Tag icon={<CheckCircleOutlined />} color="success">
        连通成功 · {result.latency_ms} ms · {result.model}
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
