import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert, App as AntApp, Button, Card, Col, Form, Input, Modal, Progress,
  Radio, Row, Select, Slider, Space, Table, Tag, Typography,
} from "antd";
import {
  ApiOutlined, BookOutlined, CheckCircleOutlined, DeleteOutlined,
  EditOutlined, ExperimentOutlined, PlusOutlined, ReloadOutlined,
  SafetyCertificateOutlined, SearchOutlined, StopOutlined,
} from "@ant-design/icons";
import {
  createSensitiveRule, deleteSensitiveRule, listSensitiveRuleCategories,
  listSensitiveRules, setSensitiveRuleStatus, testSensitiveRule,
  updateSensitiveRule, validateSensitiveRegex, classificationRescan,
  batchSetSensitiveRuleStatus, batchSetSensitiveRuleConfidence,
  listDataSources, fetchAssetTables, fetchAssetEntityDetail,
  activateDictItem, createDictItem, deactivateDictItem,
  deleteDictItem, listAllDictItems, updateDictItem,
} from "../api";
import type {
  SensitiveRuleCategory, SensitiveRuleItem, SensitiveRuleTestResponse,
  DataSource, SystemDictItem, DictItemCreateRequest,
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

//: PII 上下文词表元信息（pii_vocab 字典项 → 中文说明/类型/提示）
const VOCAB_META: Record<string, { label: string; kind: "regex" | "words"; hint: string }> = {
  person_name_re: { label: "人名前缀词表", kind: "regex", hint: "带此前缀的 *_name 列是个人姓名（patient_name/用户姓名）→ 判 PII" },
  entity_name_re: { label: "机构/地点前缀词表", kind: "regex", hint: "带此前缀的 *_name 是机构/地点名（village_name/表名）→ 不判 PII" },
  person_entity_re: { label: "人员语义表名词表", kind: "regex", hint: "裸 name 列所在表含人员语义（patient/用户表）→ 视为姓名" },
  entity_entity_re: { label: "机构语义表名词表", kind: "regex", hint: "裸 name 列所在表含机构语义（village/部门表）→ 视为机构名" },
  health_org_re: { label: "健康机构字段词表", kind: "regex", hint: "注释含健康词但字段是机构/位置（org_name/病区）→ 降级不判 PII" },
  health_keep_re: { label: "明确健康字段词表", kind: "regex", hint: "字段名含健康词（disease_name/血压）→ 保留 PII" },
  aggregate_re: { label: "聚合统计量词词表", kind: "regex", hint: "字段名含量词（_cnt/_rate/_avg）→ 群体统计不判 PII（heart_rate 豁免）" },
  value_exempt_prefix: { label: "值型豁免前缀", kind: "words", hint: "即使带量词仍是个人测量值（heart_rate/心率）→ 保留 PII" },
  exempt_field: { label: "误报豁免字段（精确）", kind: "words", hint: "精确字段名命中则跳过——误报反馈按钮一键写入" },
  exempt_prefix: { label: "误报豁免前缀", kind: "words", hint: "字段名前缀命中则跳过（灵活豁免）" },
};

//: 内置词表默认值（展示「内置」来源与恢复默认用）
const VOCAB_DEFAULTS: Record<string, string> = {
  value_exempt_prefix: "heart_rate,heartrate,心率",
  exempt_field: "",
  exempt_prefix: "",
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
  const [vocabOpen, setVocabOpen] = useState(false);

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
  const [dataSources, setDataSources] = useState<DataSource[]>([]);

  // 规则搜索 + 批量选择
  const [search, setSearch] = useState("");
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchConfOpen, setBatchConfOpen] = useState(false);
  const [batchConfValue, setBatchConfValue] = useState(0.85);
  const [batchSubmitting, setBatchSubmitting] = useState(false);

  // 测试台：表/字段联动下拉
  const [testTables, setTestTables] = useState<{ value: number; label: string }[]>([]);
  const [testColumns, setTestColumns] = useState<{ name: string; comment?: string | null }[]>([]);
  const [testLoadingTables, setTestLoadingTables] = useState(false);

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
    listDataSources({ page_size: 200 })
      .then((res) => setDataSources(res.items))
      .catch(() => {});
  }, [loadRules]);

  // 搜索过滤：规则名 / 标识 / 类别标签 / 正则
  const filteredRules = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return rules;
    return rules.filter(
      (r) =>
        r.label.toLowerCase().includes(q) ||
        r.rule_id.toLowerCase().includes(q) ||
        r.category_label.toLowerCase().includes(q) ||
        r.name_re.toLowerCase().includes(q),
    );
  }, [rules, search]);

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
      // Select 的 value 是表 id，反查表名传给后端（后端按表名/字段名匹配规则）
      const table = testTables.find((t) => t.value === values.entity_name);
      const result = await testSensitiveRule({
        entity_name: table ? table.label.split("（")[0] : "",
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

  // ---- 测试台：选表 → 加载字段 ----
  const openTester = useCallback(() => {
    setTestResult(null);
    setTestColumns([]);
    setTestTables([]);
    testForm.resetFields();
    setTestOpen(true);
    setTestLoadingTables(true);
    fetchAssetTables({ limit: 200 })
      .then((res) =>
        setTestTables(
          res.items
            .filter((t) => t.id != null)
            .map((t) => ({ value: t.id as number, label: `${t.entity_name}（${t.source_name ?? t.source_id}）` })),
        ),
      )
      .catch(() => message.error("加载表清单失败"))
      .finally(() => setTestLoadingTables(false));
  }, [message, testForm]);

  async function handleTestTableChange(entityId?: number) {
    testForm.setFieldValue("column_name", undefined);
    setTestColumns([]);
    if (!entityId) return;
    try {
      const detail = await fetchAssetEntityDetail(entityId);
      const cols = Array.isArray(detail.schema_summary) ? detail.schema_summary : [];
      setTestColumns(cols.map((c) => ({ name: c.name, comment: c.comment ?? null })));
    } catch {
      message.error("加载字段列表失败");
    }
  }

  // ---- 批量操作 ----
  async function handleBatchToggle(action: "activate" | "deactivate") {
    if (selectedRowKeys.length === 0) return;
    try {
      const res = await batchSetSensitiveRuleStatus(selectedRowKeys.map(String), action);
      if (res.failed.length > 0) {
        message.warning(`已${action === "activate" ? "启用" : "停用"} ${res.succeeded.length} 条，${res.failed.length} 条失败`);
      } else {
        message.success(`已批量${action === "activate" ? "启用" : "停用"} ${res.succeeded.length} 条规则`);
      }
      setSelectedRowKeys([]);
      loadRules();
    } catch (err: any) {
      message.error(err?.message || "批量操作失败");
    }
  }

  function openBatchConfidence() {
    if (selectedRowKeys.length === 0) return;
    setBatchConfValue(0.85);
    setBatchConfOpen(true);
  }

  async function submitBatchConfidence() {
    setBatchSubmitting(true);
    try {
      const res = await batchSetSensitiveRuleConfidence(
        selectedRowKeys.map(String),
        batchConfValue,
      );
      if (res.failed.length > 0) {
        message.warning(`已更新 ${res.succeeded.length} 条，${res.failed.length} 条失败`);
      } else {
        message.success(`已批量更新 ${res.succeeded.length} 条规则的置信度`);
      }
      setSelectedRowKeys([]);
      setBatchConfOpen(false);
      loadRules();
    } catch (err: any) {
      message.error(err?.message || "批量操作失败");
    } finally {
      setBatchSubmitting(false);
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
        source_ids: values.source_ids?.length ? values.source_ids : null,
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
          <Button icon={<ExperimentOutlined />} onClick={openTester}>
            规则测试台
          </Button>
          <Button icon={<BookOutlined />} onClick={() => setVocabOpen(true)}>
            PII 词表
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

      {/* 搜索 + 批量操作栏 */}
      <Row gutter={12} style={{ marginBottom: 12 }}>
        <Col flex="auto">
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索规则名 / 标识 / 类别 / 正则"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ maxWidth: 360 }}
          />
        </Col>
        {canEdit && (
          <Col>
            <Space>
              <Text type="secondary">
                {selectedRowKeys.length > 0 ? `已选 ${selectedRowKeys.length} 条` : "勾选行可批量操作"}
              </Text>
              <Button
                size="small"
                icon={<CheckCircleOutlined />}
                disabled={selectedRowKeys.length === 0}
                onClick={() => handleBatchToggle("activate")}
              >
                批量启用
              </Button>
              <Button
                size="small"
                icon={<StopOutlined />}
                disabled={selectedRowKeys.length === 0}
                onClick={() => handleBatchToggle("deactivate")}
              >
                批量停用
              </Button>
              <Button
                size="small"
                icon={<SafetyCertificateOutlined />}
                disabled={selectedRowKeys.length === 0}
                onClick={openBatchConfidence}
              >
                批量置信度
              </Button>
            </Space>
          </Col>
        )}
      </Row>

      <Table
        columns={columns}
        dataSource={filteredRules}
        rowKey="rule_id"
        loading={loading}
        size="small"
        rowSelection={canEdit ? {
          selectedRowKeys,
          onChange: setSelectedRowKeys,
          preserveSelectedRowKeys: false,
        } : undefined}
        pagination={{ pageSize: 20, showSizeChanger: false }}
      />

      {/* 批量设置置信度 */}
      <Modal
        title={`批量设置置信度（${selectedRowKeys.length} 条）`}
        open={batchConfOpen}
        onCancel={() => setBatchConfOpen(false)}
        onOk={submitBatchConfidence}
        confirmLoading={batchSubmitting}
        okText="应用"
        cancelText="取消"
        width={440}
        destroyOnClose
      >
        <div style={{ padding: "8px 0 4px" }}>
          <Space style={{ justifyContent: "space-between" }}>
            <Text type="secondary">置信度</Text>
            <Tag color={batchConfValue >= 0.85 ? "green" : batchConfValue >= 0.7 ? "orange" : "default"}>
              {Math.round(batchConfValue * 100)}%
            </Tag>
          </Space>
          <Slider
            min={0.5}
            max={1}
            step={0.01}
            value={batchConfValue}
            onChange={setBatchConfValue}
            marks={{ 0.5: "50%", 0.7: "70%", 0.85: "85%", 1: "100%" }}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            批量将所选规则的置信度统一设为该值，规则其余字段保持不变；建议 ≥70% 判定敏感，{`<70%`} 可能标记待复核。
          </Text>
        </div>
      </Modal>

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
                <Select showSearch
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
              <Form.Item
                name="entity_name"
                label="表 / 视图"
                rules={[{ required: true, message: "请选择表/视图" }]}
              >
                <Select
                  showSearch
                  allowClear
                  placeholder="选择表 / 视图"
                  loading={testLoadingTables}
                  options={testTables}
                  optionFilterProp="label"
                  onChange={(v?: number) => handleTestTableChange(v)}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="column_name"
                label="字段名"
                rules={[{ required: true, message: "请选择字段名" }]}
              >
                <Select
                  showSearch
                  allowClear
                  placeholder={testColumns.length === 0 ? "先选表，再选择字段" : "选择字段"}
                  options={testColumns.map((c) => ({
                    value: c.name,
                    label: c.comment ? `${c.name}（${c.comment}）` : c.name,
                  }))}
                  optionFilterProp="label"
                  disabled={testColumns.length === 0}
                />
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
          <Form.Item name="source_ids" label="数据源（可多选，留空重扫全部）">
            <Select
              mode="multiple"
              allowClear
              showSearch
              placeholder="选择数据源，留空重扫全部已采集资产（上限 1000 张）"
              options={dataSources.map((d) => ({
                value: d.source_id,
                label: `${d.name}（${d.source_type ?? "未知类型"}）`,
              }))}
              optionFilterProp="label"
              loading={dataSources.length === 0}
            />
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

      <PiiVocabModal
        open={vocabOpen}
        onClose={() => setVocabOpen(false)}
        canEdit={canEdit}
      />
    </Card>
  );
}

/** PII 上下文词表管理（pii_vocab 字典）：并入敏感规则配置台。
 *
 * 展示「DB 配置 + 内置默认」合并视图：无 DB 项的展示内置默认（来源=内置），
 * 有 DB 项的展示自定义内容（来源=自定义）；新增/编辑/删除/启用/停用，
 * 删除后回退内置默认。修改下次采集/重扫生效（service 每次启动加载一次）。
 */
function PiiVocabModal({ open, onClose, canEdit }: { open: boolean; onClose: () => void; canEdit: boolean }) {
  const { message, modal } = AntApp.useApp();
  const [items, setItems] = useState<SystemDictItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<SystemDictItem | null>(null);
  const [form] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await listAllDictItems("pii_vocab"));
    } catch {
      message.error("加载 PII 词表失败");
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  // 合并展示：DB 项 + 内置默认
  const rows = useMemo(() => {
    const dbByCode = new Map(items.map((i) => [i.code, i]));
    return Object.keys(VOCAB_META).map((code) => {
      const meta = VOCAB_META[code];
      const db = dbByCode.get(code);
      const content =
        db?.description ?? VOCAB_DEFAULTS[code] ?? "（内置默认正则，未在 DB 配置）";
      return { code, meta, db, content };
    });
  }, [items]);

  function openCreate() {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ status: "active" });
    setEditorOpen(true);
  }

  function openEdit(item: SystemDictItem) {
    setEditing(item);
    form.setFieldsValue({
      code: item.code,
      description: item.description ?? "",
      status: item.status ?? "active",
    });
    setEditorOpen(true);
  }

  async function handleToggle(item: SystemDictItem) {
    try {
      if (item.status === "active") {
        await deactivateDictItem("pii_vocab", item.code);
      } else {
        await activateDictItem("pii_vocab", item.code);
      }
      message.success("词表项状态已更新");
      load();
    } catch {
      message.error("词表项状态更新失败");
    }
  }

  function handleDelete(item: SystemDictItem) {
    modal.confirm({
      title: "确认删除该词表项？",
      content: `删除 "${item.code}" 的自定义配置后，将回退到内置默认词表。`,
      onOk: async () => {
        await deleteDictItem("pii_vocab", item.code);
        message.success("词表项已删除（回退内置默认）");
        load();
      },
    });
  }

  async function submit() {
    const values = await form.validateFields();
    const code = String(values.code ?? "").trim();
    const meta = VOCAB_META[code];
    if (!meta) {
      message.error("请选择有效的词表标识");
      return;
    }
    const payload: DictItemCreateRequest = {
      code,
      label: meta.label,
      description: String(values.description ?? "").trim(),
    };
    if (editing) {
      await updateDictItem("pii_vocab", editing.code, { description: payload.description });
    } else {
      await createDictItem("pii_vocab", payload);
    }
    message.success(editing ? "词表项已更新，下次采集/重扫生效" : "词表项已创建，下次采集/重扫生效");
    setEditorOpen(false);
    load();
  }

  const columns = [
    { title: "标识", dataIndex: "code", width: 170, render: (v: string) => <Text code>{v}</Text> },
    { title: "说明", dataIndex: "label", width: 170, render: (_: unknown, r: { meta: { label: string } }) => r.meta.label },
    { title: "类型", dataIndex: "kind", width: 70, render: (_: unknown, r: { meta: { kind: string } }) => (r.meta.kind === "regex" ? <Tag>正则</Tag> : <Tag color="blue">词条</Tag>) },
    { title: "内容", dataIndex: "content", ellipsis: true, render: (v: string) => <Text style={{ fontFamily: "monospace", fontSize: 12 }}>{v}</Text> },
    {
      title: "来源", dataIndex: "source", width: 80,
      render: (_: unknown, r: { db: SystemDictItem | undefined }) =>
        r.db ? <Tag color="orange">自定义</Tag> : <Tag>内置</Tag>,
    },
    {
      title: "状态", dataIndex: "status", width: 80,
      render: (_: unknown, r: { db: SystemDictItem | undefined }) =>
        r.db ? (
          r.db.status === "active" ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>
        ) : (
          <Tag>内置</Tag>
        ),
    },
    ...(canEdit
      ? [{
          title: "操作", dataIndex: "action", width: 150,
          render: (_: unknown, r: { db: SystemDictItem | undefined }) =>
            r.db ? (
              <Space size={4}>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r.db!)} />
                <Button size="small" icon={r.db.status === "active" ? <StopOutlined /> : <CheckCircleOutlined />} onClick={() => handleToggle(r.db!)} />
                <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDelete(r.db!)} />
              </Space>
            ) : (
              <Button size="small" icon={<PlusOutlined />} onClick={openCreate}>配置</Button>
            ),
        }]
      : []),
  ];

  return (
    <Modal
      title="PII 上下文词表（pii_vocab）"
      open={open}
      onCancel={onClose}
      footer={
        <Space>
          {canEdit && (
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              新增词表项
            </Button>
          )}
          <Button onClick={onClose}>关闭</Button>
        </Space>
      }
      width={900}
      destroyOnClose
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="词表定义「上下文判定」：人名/机构前缀、表语义、健康降级、聚合量词、豁免。与规则（pii_rule）分离——调整词表可豁免误报字段、补充人员/机构词，无需改代码发版。修改下次采集/重扫生效。"
      />
      <Table
        columns={columns}
        dataSource={rows}
        rowKey="code"
        loading={loading}
        size="small"
        pagination={false}
      />
      <Modal
        title={editing ? `编辑词表项：${editing.code}` : "新增词表项"}
        open={editorOpen}
        onCancel={() => setEditorOpen(false)}
        onOk={submit}
        okText="保存"
        cancelText="取消"
        width={640}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="code"
            label="词表标识"
            rules={[{ required: true }]}
            tooltip={editing ? "不可修改" : "选择要配置/覆盖的词表键"}
          >
            <Select showSearch
              disabled={!!editing}
              placeholder="选择词表键"
              options={Object.keys(VOCAB_META).map((code) => ({
                value: code,
                label: `${code}（${VOCAB_META[code].label}）`,
              }))}
            />
          </Form.Item>
          <Form.Item name="description" label="词表内容" rules={[{ required: true }]}>
            <Input.TextArea
              rows={5}
              placeholder={editing ? "正则整体（正则类）或逗号分隔词条（词条类）" : "正则整体（正则类）或逗号分隔词条（词条类）；留空回退内置"}
            />
          </Form.Item>
          {editing && VOCAB_META[editing.code] && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {VOCAB_META[editing.code].hint}
            </Text>
          )}
        </Form>
      </Modal>
    </Modal>
  );
}
