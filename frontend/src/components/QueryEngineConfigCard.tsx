import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Space,
  Switch,
  Tag,
  message,
} from "antd";
import {
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  EditOutlined,
  HistoryOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  getQueryEngineConfig,
  saveQueryEngineConfig,
  testQueryEngineConfig,
  UnisenseApiError,
} from "../api";
import type { QueryEnginePayload, QueryEngineTestResult, QueryEngineView } from "../types";

const SOURCE_LABEL: Record<string, string> = {
  db: "数据库配置",
  env: "环境变量",
  none: "未配置",
};

const SOURCE_COLOR: Record<string, string> = {
  db: "green",
  env: "geekblue",
  none: "default",
};

function TestBadge({ result }: { result: QueryEngineTestResult }) {
  if (!result) return null;
  return result.ok ? (
    <Space size={4}>
      <CheckCircleOutlined style={{ color: "#52c41a" }} />
      <span style={{ color: "#52c41a" }}>
        {result.engine === "olap" ? "OLAP" : "MySQL 降级"}连通正常（{result.latency_ms}ms）
      </span>
    </Space>
  ) : (
    <Space size={4}>
      <CloseCircleOutlined style={{ color: "#ff4d4f" }} />
      <span style={{ color: "#ff4d4f" }}>
        {result.engine === "olap" ? "OLAP" : "MySQL 降级"}连通失败：{result.error || "未知错误"}
      </span>
    </Space>
  );
}

export default function QueryEngineConfigCard() {
  const navigate = useNavigate();
  const [view, setView] = useState<QueryEngineView | null>(null);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<null | "olap" | "mysql">(null);
  const [testResult, setTestResult] = useState<QueryEngineTestResult | null>(null);
  const [form] = Form.useForm<QueryEnginePayload>();

  const load = useCallback(async () => {
    try {
      const d = await getQueryEngineConfig();
      setView(d);
    } catch {
      // 读失败不打扰（仅展示状态，失败通常为权限/网络）
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function openEdit() {
    // 回填源：优先 DB 行；DB 未配置（配置来自环境变量/未配置）时回填当前生效值，
    // 让管理员在「接管为 DB 配置」时无需重抄一遍已生效的连接参数。
    const src = view?.row ?? view?.effective;
    form.setFieldsValue({
      olap_url: src?.olap_url ?? "",
      doris_host: src?.doris_host ?? "",
      doris_port: src?.doris_port ?? 8030,
      doris_database: src?.doris_database ?? "",
      doris_user: src?.doris_user ?? "",
      doris_password: "",
      mysql_fallback_url: "",
      enabled: view?.row?.enabled ?? true,
    });
    setTestResult(null);
    setEditing(true);
  }

  async function handleSave() {
    const values = await form.validateFields();
    setSaving(true);
    try {
      await saveQueryEngineConfig(values);
      message.success("查询引擎配置已保存（最长 30s 全量生效）");
      setEditing(false);
      await load();
    } catch (e) {
      const err = e as UnisenseApiError;
      message.error(err.codeZh ? `${err.message}（${err.codeZh}）` : err.message);
    } finally {
      setSaving(false);
    }
  }

  async function handleTest(engine: "olap" | "mysql") {
    setTesting(engine);
    setTestResult(null);
    try {
      const payload: Partial<QueryEnginePayload> | undefined = editing
        ? await form.validateFields()
        : undefined;
      const res = await testQueryEngineConfig({ engine, payload });
      setTestResult(res);
      if (res.ok) {
        message.success(
          engine === "olap" ? "OLAP 引擎连通正常" : "MySQL 降级引擎连通正常",
        );
      }
    } catch (e) {
      const err = e as UnisenseApiError;
      message.error(err.codeZh ? `${err.message}（${err.codeZh}）` : err.message);
    } finally {
      setTesting(null);
    }
  }

  const eff = view?.effective;
  const hasDbRow = Boolean(view?.row);
  const rowHasDorisPwd = Boolean(view?.row?.has_doris_password);
  const rowHasMysql = Boolean(view?.row?.has_mysql_fallback);
  // 无 DB 行且环境变量生效中 → 编辑=「接管为 DB 配置」，密码/URL 需重填才能保留
  const envTakeover = !hasDbRow && eff?.source === "env";
  const dorisPwdLabel = rowHasDorisPwd
    ? "Doris 密码（已配置，留空保持原值）"
    : !hasDbRow && eff?.has_doris_password
      ? "Doris 密码（环境变量已配置，密码不回显；如需保留请重新输入）"
      : "Doris 密码（可空=无认证）";
  const mysqlLabel = rowHasMysql
    ? "MySQL 降级引擎 URL（已配置，留空保持原值）"
    : envTakeover && eff?.mysql_fallback_configured
      ? "MySQL 降级引擎 URL（当前生效来自环境变量；留空保存后将停用）"
      : "MySQL 降级引擎 URL（可空=不启用）";
  const mysqlPlaceholder = rowHasMysql
    ? "已配置，留空保持"
    : envTakeover && eff?.mysql_fallback_configured
      ? "粘贴完整连接串以保留（如 mysql+aiomysql://user:pass@host:3306/db）"
      : "mysql+aiomysql://...";
  const mysqlMasked = eff?.mysql_fallback_url_masked || "";
  return (
    <Card
      title={
        <Space>
          <ApiOutlined />
          <span>查询引擎配置</span>
          <Tag color={SOURCE_COLOR[eff?.source ?? "none"]}>
            {SOURCE_LABEL[eff?.source ?? "none"]}
          </Tag>
          {eff?.olap_configured && <Tag color="geekblue">OLAP 已配置</Tag>}
          {eff?.mysql_fallback_configured && <Tag color="purple">MySQL 降级已配置</Tag>}
        </Space>
      }
      size="small"
      style={{ marginBottom: 16 }}
      extra={
        view?.can_edit ? (
          <Space>
            <Button size="small" icon={<ThunderboltOutlined />} loading={testing === "olap"} onClick={() => handleTest("olap")}>
              测试 OLAP
            </Button>
            <Button size="small" icon={<ThunderboltOutlined />} loading={testing === "mysql"} onClick={() => handleTest("mysql")}>
              测试 MySQL 降级
            </Button>
            <Button size="small" type="primary" icon={<EditOutlined />} onClick={openEdit}>
              编辑配置
            </Button>
            <Button size="small" icon={<HistoryOutlined />} onClick={() => navigate("/audit?entity_type=query_engine_config")}>
              变更记录
            </Button>
          </Space>
        ) : (
          <span className="muted" style={{ fontSize: 12 }}>
            仅平台管理员可配置
          </span>
        )
      }
    >
      {eff?.note ? (
        <Alert
          type={eff.source === "none" && !eff.olap_configured && !eff.mysql_fallback_configured ? "warning" : "info"}
          showIcon
          style={{ marginBottom: 12 }}
          message="引擎状态"
          description={eff.note}
        />
      ) : null}
      {testResult ? (
        <Alert
          type={testResult.ok ? "success" : "error"}
          showIcon
          style={{ marginBottom: 12 }}
          message={<TestBadge result={testResult} />}
        />
      ) : null}
      <Descriptions
        size="small"
        column={2}
        items={[
          {
            key: "olap",
            label: "OLAP 引擎",
            children: eff?.olap_configured ? (
              <span>
                {eff.doris_host}:{eff.doris_port}
                {eff.doris_database ? `（库 ${eff.doris_database}）` : ""}
                {eff.doris_user ? ` · 用户 ${eff.doris_user}` : ""}
              </span>
            ) : (
              <span className="muted">未配置</span>
            ),
          },
          {
            key: "mysql",
            label: "MySQL 降级引擎",
            children: eff?.mysql_fallback_configured ? (
              <span style={{ wordBreak: "break-all" }}>
                {mysqlMasked || "已配置"}
              </span>
            ) : (
              <span className="muted">未配置</span>
            ),
          },
          {
            key: "updated",
            label: "最近更新",
            children: eff?.updated_at ? (
              <span>
                用户 #{eff.updated_by ?? "-"} · {String(eff.updated_at).slice(0, 19).replace("T", " ")}
              </span>
            ) : (
              <span className="muted">—</span>
            ),
          },
        ]}
      />
      <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
        查询引擎连接（Doris / StarRocks OLAP + MySQL 只读降级）可在页面配置并立即生效，
        无需修改 .env 或重启；配置优先使用数据库存储，未配置时回落到环境变量（UNISENSE_OLAP_URL
        等）。仅平台管理员可修改，变更写入审计日志。
      </div>

      <Modal
        title="查询引擎配置"
        open={editing}
        onOk={handleSave}
        onCancel={() => setEditing(false)}
        confirmLoading={saving}
        okText="保存"
        width={640}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="配置优先级：数据库 > 环境变量"
            description="保存即生效（最长 30s 全量生效）。olap_url 与 doris_host 二选一即可（填写 olap_url 时自动派生主机/端口/库）；密码与 MySQL 降级 URL 留空表示保持已保存值。"
          />
          {envTakeover ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="当前配置来自环境变量（尚未写入数据库）"
              description="OLAP 连接已按当前生效值预填，保存后将写入数据库并接管。密码与 MySQL 降级 URL 属敏感信息不回显——如需保留 MySQL 降级，请在此粘贴完整连接串；留空保存后该项将停用（不再回落环境变量）。"
            />
          ) : null}
          <Form.Item label="OLAP 基础 URL（可选，自动派生连接参数）" name="olap_url">
            <Input placeholder="http://doris-fe:8030" />
          </Form.Item>
          <Space.Compact block>
            <Form.Item label="Doris 主机（显式直连优先）" name="doris_host" style={{ flex: 1 }}>
              <Input placeholder="doris-fe" />
            </Form.Item>
            <Form.Item label="端口" name="doris_port" style={{ width: 110, marginLeft: 8 }}>
              <InputNumber min={1} max={65535} style={{ width: "100%" }} />
            </Form.Item>
          </Space.Compact>
          <Space.Compact block>
            <Form.Item label="默认库（可空）" name="doris_database" style={{ flex: 1 }}>
              <Input placeholder="unisense" />
            </Form.Item>
            <Form.Item label="用户名（可空=无认证）" name="doris_user" style={{ flex: 1, marginLeft: 8 }}>
              <Input placeholder="root" />
            </Form.Item>
          </Space.Compact>
          <Form.Item label={dorisPwdLabel} name="doris_password">
            <Input.Password
              placeholder={
                rowHasDorisPwd || (!hasDbRow && eff?.has_doris_password)
                  ? "已配置，留空保持"
                  : "密码"
              }
            />
          </Form.Item>
          <Form.Item
            label={mysqlLabel}
            name="mysql_fallback_url"
            extra={
              mysqlMasked
                ? `当前生效：${mysqlMasked}${rowHasMysql ? "（留空保持原值）" : envTakeover ? "（来自环境变量，留空保存后停用）" : ""}`
                : "完整连接串：mysql+aiomysql://user:pass@host:3306/db"
            }
          >
            <Input placeholder={mysqlPlaceholder} />
          </Form.Item>
          <Form.Item label="启用数据库配置（关闭则回落环境变量）" name="enabled" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}
