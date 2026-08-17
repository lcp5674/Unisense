import { useCallback, useEffect, useMemo, useState } from "react";
import {
  App as AntApp, Button, Card, Col, Form, Input, Modal, Progress,
  Radio, Row, Select, Slider, Space, Table, Tag, Typography,
} from "antd";
import {
  ApiOutlined, CheckCircleOutlined, DeleteOutlined,
  EditOutlined, ExperimentOutlined, PlusOutlined, ReloadOutlined,
  SafetyCertificateOutlined, StopOutlined,
} from "@ant-design/icons";
import {
  createSensitiveRule, deleteSensitiveRule, listSensitiveRuleCategories,
  listSensitiveRules, setSensitiveRuleStatus, testSensitiveRule,
  updateSensitiveRule, validateSensitiveRegex, classificationRescan,
} from "../api";
import type {
  SensitiveRuleCategory, SensitiveRuleItem, SensitiveRuleTestResponse,
} from "../types";
import { usePermission } from "../hooks/usePermission";

const { Text } = Typography;

//: 类别 → 专属色（与 PII 合规 Tab 一致）
const CATEGORY_COLORS: Record<string, string> = {
  ID_CARD: "magenta", PHONE: "volcano", EMAIL: "orange", NAME: "gold",
  ADDRESS: "lime", BANK_CARD: "green", DOCUMENT: "cyan", PASSPORT: "blue",
  GPS: "geekblue", HEALTH: "purple", BIOMETRIC: "magenta", FINANCIAL: "red",
  CREDENTIAL: "red", TAX: "orange", BUSINESS: "purple",
};

//: 命中途径 → 中文标签
const MATCHED_BY_LABEL: Record<string, string> = {
  name: "字段名命中",
  comment: "字段注释命中",
  "name+sample": "字段名+样本命中",
};

const SENSITIVITY_META: Record<string, { color: string; label: string }> = {
  PII: { color: "red", label: "PII（个人可识别）" },
  CONFIDENTIAL: { color: "orange", label: "机密" },
  INTERNAL: { color: "default", label: "内部" },
  UNKNOWN: { color: "default", label: "未知" },
};

function confidenceColor(conf: number) {
  if (conf >= 0.85) return "#52c41a";
  if (conf >= 0.7) return "#1677ff";
  return "#faad14";
}

export function SensitiveRules() {
  const { message, modal } = AntApp.useApp();
  const { can } = usePermission();
  const canEdit = can("sensitive-rules:edit");
  const canRescan = can("classification:rescan");

  const [rules, setRules] = useState<SensitiveRuleItem[]>([]);
  const [categories, setCategories] = useState<SensitiveRuleCategory[]>([]);
  const [loading, setLoading] = useState(false);

  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<SensitiveRuleItem | null>(null);
  const [editorForm] = Form.useForm();
  const [regexStatus, setRegexStatus] = useState<{ valid: boolean; error: string | null } | null>(null);

  const [testOpen, setTestOpen] = useState(false);
  const [testLoading, setTestLoading] = useState(false);
  const [testResult, setTestResult] = useState<SensitiveRuleTestResponse | null>(null);
  const [testForm] = Form.useForm();

  const [rescanOpen, setRescanOpen] = useState(false);
  const [rescanLoading, setRescanLoading] = useState(false);
  const [rescanResult, setRescanResult] = useState<Record<string, unknown> | null>(null);
  const [rescanForm] = Form.useForm();

  const loadRules = useCallback(() => {
    setLoading(true);
    listSensitiveRules()
      .then(setRules)
      .catch(() => message.error("加载敏感规则失败"))
      .finally(() => setLoading(false));
  }, [message]);

  useEffect(() => {
    loadRules();
    listSensitiveRuleCategories()
      .then(setCategories)
      .catch(() => {});
  }, [loadRules]);

  // 类别选项按 PII / 机密分组
  const piiCategories = useMemo(() => categories.filter((c) => c.pii), [categories]);
  const confCategories = useMemo(() => categories.filter((c) => !c.pii), [categories]);
  const editorPii = Form.useWatch("pii", editorForm) ?? true;

  // ---- 编辑弹窗 ----
  function openCreate() {
    setEditing(null);
    setRegexStatus(null);
    editorForm.resetFields();
    editorForm.setFieldsValue({ pii: true, confidence: 0.85, sample_re: null });
    setEditorOpen(true);
  }

  function openEdit(record: SensitiveRuleItem) {
    setEditing(record);
    setRegexStatus(null);
    editorForm.setFieldsValue({
      label: record.label,
      category: record.category,
      name_re: record.name_re,
      sample_re: record.sample_re ?? undefined,
      confidence: record.confidence,
      pii: record.pii,
    });
    setEditorOpen(true);
  }

  // 正则合法性即时校验（防写坏正则导致采集误判）
  async function checkRegex(pattern?: string) {
    const value = pattern ?? editorForm.getFieldValue("name_re");
    if (!value) {
      setRegexStatus(null);
      return;
    }
    try {
      const res = await validateSensitiveRegex(value);
      setRegexStatus({ valid: res.valid, error: res.error });
    } catch {
      setRegexStatus(null);
    }
  }

  async function handleEditorSubmit() {
    const values = await editorForm.validateFields().catch(() => null);
    if (!values) return;
    const payload = {
      label: values.label,
      category: values.category,
      name_re: values.name_re,
      sample_re: values.sample_re || null,
      confidence: values.confidence,
      pii: values.pii,
    };
    try {
      if (editing) {
        await updateSensitiveRule(editing.rule_id, payload);
        message.success("规则已更新，下次采集/重扫生效");
      } else {
        await createSensitiveRule({ ...payload, rule_id: values.rule_id || null });
        message.success("规则已创建，下次采集/重扫生效");
      }
      setEditorOpen(false);
      editorForm.resetFields();
      loadRules();
    } catch (err: any) {
      message.error(err?.message || "保存失败");
    }
  }

  // ---- 启停 / 删除 ----
  async function handleToggle(record: SensitiveRuleItem) {
    try {
      await setSensitiveRuleStatus(record.rule_id, record.status === "active" ? "deactivate" : "activate");
      message.success(record.status === "active" ? "已停用（采集/重扫不再使用）" : "已启用");
      loadRules();
    } catch (err: any) {
      message.error(err?.message || "操作失败");
    }
  }

  function handleDelete(record: SensitiveRuleItem) {
    modal.confirm({
      title: "确认删除该规则？",
      content: `删除 "${record.label}" 的自定义配置后，将回退到内置默认规则。`,
      okText: "删除",
      okType: "danger",
      onOk: async () => {
        try {
          await deleteSensitiveRule(record.rule_id);
          message.success("已删除，回退内置默认");
          loadRules();
        } catch (err: any) {
          message.error(err?.message || "删除失败");
        }
      },
    });
  }

  // ---- 测试台 ----
  async function handleTest() {
    const values = await testForm.validateFields().catch(() => null);
    if (!values) return;
    setTestLoading(true);
    setTestResult(null);
    try {
      const result = await testSensitiveRule({
        entity_name: values.entity_name || "",
        column_name: values.column_name,
        sample_value: values.sample_value || null,
        comment: values.comment || null,
      });
      setTestResult(result);
    } catch (err: any) {
      message.error(err?.message || "测试失败");
    } finally {
      setTestLoading(false);
    }
  }

  // ---- 重扫 ----
  async function handleRescan() {
    const values = await rescanForm.validateFields().catch(() => null);
    if (!values) return;
    setRescanLoading(true);
    setRescanResult(null);
    try {
      const result = await classificationRescan({
        source_id: values.source_id || null,
        limit: 1000,
      }) as Record<string, unknown>;
      setRescanResult(result);
      message.success("重扫完成");
    } catch (err: any) {
      message.error(err?.message || "重扫失败");
    } finally {
      setRescanLoading(false);
    }
  }

  const columns = [
    {
      title: "类别",
      dataIndex: "category_label",
      key: "category_label",
      width: 120,
      render: (label: string, r: SensitiveRuleItem) => (
        <Tag color={CATEGORY_COLORS[r.category] || "default"}>{label}</Tag>
      ),
    },
    {
      title: "规则",
      key: "rule",
      width: 200,
      render: (_: unknown, r: SensitiveRuleItem) => (
        <Space direction="vertical" size={0}>
          <Text strong>{r.label}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{r.rule_id}</Text>
        </Space>
      ),
    },
    {
      title: "匹配关键字正则",
      dataIndex: "name_re",
      key: "name_re",
      ellipsis: true,
      render: (v: string) => <Text code style={{ fontSize: 12 }}>{v}</Text>,
    },
    {
      title: "样本正则",
      dataIndex: "sample_re",
      key: "sample_re",
      width: 160,
      ellipsis: true,
      render: (v: string | null) => (v ? <Text code style={{ fontSize: 12 }}>{v}</Text> : <Text type="secondary">—</Text>),
    },
    {
      title: "置信度",
      dataIndex: "confidence",
      key: "confidence",
      width: 130,
      render: (v: number) => (
        <Progress percent={Math.round(v * 100)} size="small" strokeColor={confidenceColor(v)} />
      ),
    },
    {
      title: "类型",
      key: "kind",
      width: 80,
      render: (_: unknown, r: SensitiveRuleItem) => (
        <Tag color={r.pii ? "red" : "purple"}>{r.pii ? "PII" : "机密"}</Tag>
      ),
    },
    {
      title: "来源",
      dataIndex: "source",
      key: "source",
      width: 80,
      render: (s: string) => (
        <Tag color={s === "builtin" ? "blue" : "geekblue"}>{s === "builtin" ? "内置" : "自定义"}</Tag>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 80,
      render: (s: string) => (
        <Tag color={s === "active" ? "green" : "red"}>{s === "active" ? "启用" : "停用"}</Tag>
      ),
    },
    {
      title: "操作",
      key: "action",
      width: 170,
      render: (_: unknown, r: SensitiveRuleItem) => (
        <Space size="small">
          {canEdit && (
            <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>编辑</Button>
          )}
          {canEdit && (
            <Button
              size="small"
              icon={r.status === "active" ? <StopOutlined /> : <CheckCircleOutlined />}
              onClick={() => handleToggle(r)}
            >
              {r.status === "active" ? "停用" : "启用"}
            </Button>
          )}
          {canEdit && r.source === "custom" && (
            <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(r)} />
          )}
        </Space>
      ),
    },
  ];

  const piiCount = rules.filter((r) => r.pii && r.status === "active").length;
  const confCount = rules.filter((r) => !r.pii && r.status === "active").length;

  return (
    <Card
      title={
        <Space>
          <SafetyCertificateOutlined />
          <span>敏感规则配置台</span>
        </Space>
      }
      extra={
        <Space>
          <Button icon={<ExperimentOutlined />} onClick={() => { setTestResult(null); testForm.resetFields(); setTestOpen(true); }}>
            规则测试台
          </Button>
          {canRescan && (
            <Button icon={<ReloadOutlined />} onClick={() => { setRescanResult(null); rescanForm.resetFields(); setRescanOpen(true); }}>
              按新规则重扫
            </Button>
          )}
          {canEdit && (
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增规则</Button>
          )}
        </Space>
      }
    >
      {/* 生效概览 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <Card size="small">
            <Row justify="space-between" align="middle">
              <Col><Text type="secondary">生效 PII 规则</Text></Col>
              <Col><Tag color="red">{piiCount} 条</Tag></Col>
            </Row>
            <Text type="secondary" style={{ fontSize: 12 }}>识别身份证/手机/邮箱/姓名等个人可识别信息</Text>
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small">
            <Row justify="space-between" align="middle">
              <Col><Text type="secondary">生效机密规则</Text></Col>
              <Col><Tag color="purple">{confCount} 条</Tag></Col>
            </Row>
            <Text type="secondary" style={{ fontSize: 12 }}>识别密码/税务/商业敏感，判 CONFIDENTIAL 不计 PII</Text>
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small">
            <Row justify="space-between" align="middle">
              <Col><Text type="secondary">规则覆盖语义</Text></Col>
              <Col><Tag color="geekblue">DB 覆盖内置</Tag></Col>
            </Row>
            <Text type="secondary" style={{ fontSize: 12 }}>自定义按 rule_id 覆盖同 ID 内置，其余回退内置，不吞规则</Text>
          </Card>
        </Col>
      </Row>

      <Table
        columns={columns}
        dataSource={rules}
        rowKey="rule_id"
        loading={loading}
        size="small"
        pagination={{ pageSize: 20, showSizeChanger: false }}
      />

      {/* 新增 / 编辑弹窗 */}
      <Modal
        title={editing ? `编辑规则：${editing.label}` : "新增敏感规则"}
        open={editorOpen}
        onCancel={() => setEditorOpen(false)}
        onOk={() => editorForm.submit()}
        okText="保存"
        cancelText="取消"
        width={620}
        destroyOnClose
      >
        <Form form={editorForm} onFinish={handleEditorSubmit} layout="vertical">
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="pii" label="规则类型" rules={[{ required: true }]}>
                <Radio.Group
                  optionType="button"
                  buttonStyle="solid"
                  onChange={() => {
                    editorForm.setFieldsValue({ category: undefined });
                  }}
                >
                  <Radio.Button value={true}>PII 规则</Radio.Button>
                  <Radio.Button value={false}>机密规则</Radio.Button>
                </Radio.Group>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="label"
                label="规则名"
                rules={[{ required: true, message: "请输入规则名" }]}
              >
                <Input placeholder="如 手机号规则" maxLength={128} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="category"
                label="命中类别"
                rules={[{ required: true, message: "请选择命中类别" }]}
              >
                <Select
                  placeholder="选择命中类别"
                  options={(editorPii ? piiCategories : confCategories).map((c) => ({
                    value: c.category,
                    label: (
                      <Space>
                        <Tag color={CATEGORY_COLORS[c.category] || "default"} style={{ marginRight: 0 }}>{c.label}</Tag>
                        <Text type="secondary" style={{ fontSize: 12 }}>{c.category}</Text>
                      </Space>
                    ),
                  }))}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              {!editing && (
                <Form.Item
                  name="rule_id"
                  label="规则标识"
                  tooltip="留空由系统按规则名自动生成英文编码（如 mobile_rule）"
                >
                  <Input placeholder="留空自动生成" maxLength={64} />
                </Form.Item>
              )}
              {editing && (
                <Form.Item label="规则标识">
                  <Input value={editing.rule_id} disabled />
                </Form.Item>
              )}
            </Col>
          </Row>
          <Form.Item
            name="name_re"
            label="匹配关键字正则"
            extra={
              <Space size={4}>
                <span>匹配字段名或字段注释关键字（大小写不敏感），保存前即时校验语法。</span>
                {regexStatus &&
                  (regexStatus.valid ? (
                    <Tag color="green" style={{ marginRight: 0 }}>
                      <CheckCircleOutlined /> 正则合法
                    </Tag>
                  ) : (
                    <Text type="danger" style={{ fontSize: 12 }}>语法错误：{regexStatus.error}</Text>
                  ))}
              </Space>
            }
            rules={[
              { required: true, message: "请输入匹配正则" },
              {
                validator: async (_rule, value) => {
                  if (!value) return;
                  const res = await validateSensitiveRegex(value).catch(() => null);
                  if (res && !res.valid) throw new Error(res.error || "正则语法错误");
                },
              },
            ]}
            hasFeedback
          >
            <Input.TextArea
              rows={2}
              placeholder={'如 (phone|mobile|手机|电话) —— 匹配字段名或注释'}
              style={{ fontFamily: "monospace" }}
              onChange={(e) => checkRegex(e.target.value)}
            />
          </Form.Item>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="sample_re"
                label="样本正则（可选）"
                extra="命中样本值可提升置信度"
              >
                <Input placeholder={'如 ^1[3-9]\\d{9}$'} style={{ fontFamily: "monospace" }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="confidence" label="基础置信度" initialValue={0.85}>
                <Slider min={0} max={1} step={0.05} marks={{ 0.5: "0.5", 0.7: "阈值 0.7", 1: "1" }} />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      {/* 规则测试台 */}
      <Modal
        title="规则测试台"
        open={testOpen}
        onCancel={() => setTestOpen(false)}
        footer={null}
        width={680}
        destroyOnClose
      >
        <Form form={testForm} layout="vertical">
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="entity_name" label="表 / 视图名（可选）">
                <Input placeholder="如 ods_user" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="column_name" label="字段名" rules={[{ required: true, message: "请输入字段名" }]}>
                <Input placeholder="如 mobile / 手机号" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="sample_value" label="取值样本（可选）">
                <Input placeholder="如 13812345678" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="comment" label="字段注释（可选）">
                <Input placeholder="如 客户手机号" />
              </Form.Item>
            </Col>
          </Row>
          <Button type="primary" icon={<ExperimentOutlined />} loading={testLoading} onClick={handleTest} block>
            运行识别
          </Button>
        </Form>

        {testResult && (
          <Card
            size="small"
            style={{ marginTop: 16 }}
            title={
              <Space>
                <ApiOutlined />
                <span>识别结果</span>
                <Tag color={SENSITIVITY_META[testResult.sensitivity_level]?.color || "default"}>
                  {SENSITIVITY_META[testResult.sensitivity_level]?.label || testResult.sensitivity_level}
                </Tag>
              </Space>
            }
          >
            {testResult.hits.length === 0 ? (
              <Text type="secondary">
                未命中任何敏感规则 → 判定为内部数据（INTERNAL）。
                {testResult.sensitivity_level === "CONFIDENTIAL" ? "（机密规则命中不计字段明细）" : ""}
              </Text>
            ) : (
              <>
                {testResult.hits.map((h) => (
                  <Row key={`${h.column}-${h.rule}`} align="middle" style={{ padding: "6px 0", borderBottom: "1px solid #f0f0f0" }}>
                    <Col flex="auto">
                      <Space>
                        <Tag color={CATEGORY_COLORS[h.category] || "default"}>{h.category_label}</Tag>
                        <Text strong>{h.column}</Text>
                        <Text type="secondary" style={{ fontSize: 12 }}>命中规则 {h.rule}</Text>
                      </Space>
                    </Col>
                    <Col>
                      <Space size="small">
                        <Text type="secondary" style={{ fontSize: 12 }}>{MATCHED_BY_LABEL[h.matched_by] || h.matched_by}</Text>
                        <Tag color={h.confidence >= 0.7 ? "green" : "orange"}>{Math.round(h.confidence * 100)}%</Tag>
                      </Space>
                    </Col>
                  </Row>
                ))}
                <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
                  提示：可在下方新增/编辑规则后重新测试，验证后再上生产。
                </Text>
              </>
            )}
          </Card>
        )}
      </Modal>

      {/* 按新规则重扫 */}
      <Modal
        title="按新规则重扫已采集资产"
        open={rescanOpen}
        onCancel={() => setRescanOpen(false)}
        onOk={() => rescanForm.submit()}
        confirmLoading={rescanLoading}
        okText="开始重扫"
        destroyOnClose
      >
        <Form form={rescanForm} onFinish={handleRescan} layout="vertical">
          <Form.Item name="source_id" label="数据源（可选）">
            <Input placeholder="留空重扫全部已采集资产（上限 1000 张）" />
          </Form.Item>
          <Text type="secondary" style={{ fontSize: 12 }}>
            使用当前生效的规则集（含你刚配置的自定义规则）重新计算敏感级别，仅升不降，并回写字段级 PII 明细。
          </Text>
        </Form>
        {rescanResult && (
          <Card size="small" style={{ marginTop: 12 }}>
            <Space wrap>
              <Tag color="blue">扫描 {String(rescanResult.scanned ?? 0)}</Tag>
              <Tag color="orange">变更 {String(rescanResult.changed ?? 0)}</Tag>
              <Tag color="red">PII {String(rescanResult.pii_found ?? 0)}</Tag>
              <Tag color="default">降级 {String(rescanResult.degraded ?? 0)}</Tag>
            </Space>
          </Card>
        )}
      </Modal>
    </Card>
  );
}
