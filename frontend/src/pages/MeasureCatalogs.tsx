import { useEffect, useState } from "react";
import { Alert, App as AntApp, Button, Card, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag, Tooltip } from "antd";
import {
  CheckOutlined,
  CloseOutlined,
  EditOutlined,
  PlusOutlined,
  RobotOutlined,
  SendOutlined,
  StopOutlined,
} from "@ant-design/icons";
import {
  approveMeasureCatalog,
  autoSuggestMeasureCatalog,
  createMeasureCatalog,
  deprecateMeasureCatalog,
  fetchCurrentUser,
  listDomainTree,
  listMeasureCatalogs,
  rejectMeasureCatalog,
  submitMeasureCatalog,
  UnisenseApiError,
  updateMeasureCatalog,
} from "../api";
import type { CurrentUser, MeasureCatalog, MeasureCategory, MeasureFormat, MeasureSuggestResult, SubjectDomainTreeNode } from "../types";
import { MEASURE_CATEGORY_LABEL, MEASURE_FORMAT_LABEL } from "../types";
import { formatCnTime } from "../utils/timeCn";
import { usePermission } from "../hooks/usePermission";

const STATUS_COLOR: Record<string, string> = {
  DRAFT: "default",
  REVIEW: "processing",
  PUBLISHED: "success",
  DEPRECATED: "error",
};
const STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  REVIEW: "审核中",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
};

// 度量格式 → 默认单位/默认小数位（PRD FR-02-08 联动；前端与后端 schema 保持同规则）
const FORMAT_DEFAULTS: Record<MeasureFormat, { unit: string; decimal: number | null }> = {
  AMOUNT: { unit: "元", decimal: 2 },
  RATIO: { unit: "小数", decimal: 4 },
  NUMERIC: { unit: "", decimal: null },
};

const FORMAT_OPTIONS = [
  { value: "AMOUNT", label: "金额 (AMOUNT)" },
  { value: "RATIO", label: "比率 (RATIO)" },
  { value: "NUMERIC", label: "数值 (NUMERIC)" },
];

const CATEGORY_OPTIONS = (Object.keys(MEASURE_CATEGORY_LABEL) as MeasureCategory[]).map((v) => ({
  value: v,
  label: MEASURE_CATEGORY_LABEL[v],
}));

function flattenDomainNames(nodes: SubjectDomainTreeNode[], acc: Map<string, string>) {
  for (const n of nodes) {
    acc.set(n.code, n.name);
    if (n.children?.length) flattenDomainNames(n.children, acc);
  }
}

function errMsg(e: unknown, fallback: string): string {
  return e instanceof UnisenseApiError ? `${e.message}（${e.codeZh}）` : fallback;
}

/** 审核权判断（对齐 MetricReview canReview）：指派评审人/域评审组/未指派域管理员兜底 */
function canReviewMeasure(row: MeasureCatalog, user: CurrentUser | null): boolean {
  if (!user) return false;
  if (user.role === "platform_admin") return true;
  if (row.reviewer_type === "user" && row.reviewer_id != null) {
    return user.id === row.reviewer_id;
  }
  if (row.reviewer_type === "domain" && row.reviewer_domain) {
    return (
      (user.role === "domain_admin" || user.role === "reviewer") &&
      user.domain === row.reviewer_domain
    );
  }
  return user.role === "domain_admin";
}

export function MeasureCatalogs() {
  const { message } = AntApp.useApp();
  const { can } = usePermission();
  const canWrite = can("metric:create") || can("metric:import");
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [items, setItems] = useState<MeasureCatalog[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [domain, setDomain] = useState<string>();
  const [status, setStatus] = useState<string>();
  const [keyword, setKeyword] = useState<string>();
  const [domainOptions, setDomainOptions] = useState<{ value: string; label: string }[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<MeasureCatalog | null>(null);
  const [saving, setSaving] = useState(false);
  const [suggestLoading, setSuggestLoading] = useState(false);
  const [suggestResult, setSuggestResult] = useState<MeasureSuggestResult | null>(null);
  // 审核流状态：提交审核 Modal / 驳回 Modal / 正在审批的度量
  const [submitTarget, setSubmitTarget] = useState<MeasureCatalog | null>(null);
  const [submitBusy, setSubmitBusy] = useState(false);
  const [rejectTarget, setRejectTarget] = useState<MeasureCatalog | null>(null);
  const [rejectBusy, setRejectBusy] = useState(false);
  const [busyCode, setBusyCode] = useState<string | null>(null);
  const [form] = Form.useForm();
  const [reviewForm] = Form.useForm();
  const watchFormat = Form.useWatch("measure_format", form);

  async function load() {
    setLoading(true);
    try {
      const res = await listMeasureCatalogs({ domain, status, keyword, page, page_size: pageSize });
      setItems(res.items);
      setTotal(res.total);
    } catch (e) {
      message.error(errMsg(e, "加载度量目录失败"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, domain, status, keyword]);

  useEffect(() => {
    fetchCurrentUser().then(setCurrentUser).catch(() => {});
  }, []);

  useEffect(() => {
    listDomainTree()
      .then((tree) => {
        const map = new Map<string, string>();
        flattenDomainNames(tree, map);
        setDomainOptions([...map.entries()].map(([value, name]) => ({ value, label: `${name} (${value})` })));
      })
      .catch(() => undefined);
  }, []);

  function openCreate() {
    setEditing(null);
    setSuggestResult(null);
    form.resetFields();
    form.setFieldsValue({ measure_format: "AMOUNT", category: "OTHER" });
    setModalOpen(true);
  }

  function openEdit(row: MeasureCatalog) {
    setEditing(row);
    setSuggestResult(null);
    form.setFieldsValue({
      measure_code: row.measure_code,
      name: row.name,
      domain: row.domain,
      description: row.description,
      measure_format: row.measure_format,
      default_unit: row.default_unit,
      default_decimal_places: row.default_decimal_places,
      source_system: row.source_system ?? [],
      synonyms: row.synonyms ?? [],
      category: row.category ?? "OTHER",
      stat_caliber: row.stat_caliber ?? undefined,
    });
    setModalOpen(true);
  }

  // 度量格式联动：切换格式时若未手改单位/小数位，则带出新格式默认（对齐后端 schema）
  function onFormatChange(fmt: MeasureFormat) {
    const def = FORMAT_DEFAULTS[fmt];
    const curUnit = form.getFieldValue("default_unit");
    const curDecimal = form.getFieldValue("default_decimal_places");
    const fields: Record<string, unknown> = {};
    if (!curUnit || Object.values(FORMAT_DEFAULTS).some((d) => d.unit === curUnit)) {
      fields.default_unit = def.unit;
    }
    if (curDecimal == null || [2, 4].includes(Number(curDecimal))) {
      fields.default_decimal_places = def.decimal ?? null;
    }
    form.setFieldsValue(fields);
  }

  // AI 推断：名称/描述 → 逐字段回填（编码/格式/单位/小数位/分类/口径/同义词/域），
  // 用户可改后再提交。后端规则兜底 + LLM 增强，LLM 不可用自动降级规则。
  async function handleSuggest() {
    const name = form.getFieldValue("name");
    if (!name || !String(name).trim()) {
      message.warning("请先输入度量中文名，再点「AI 推断」");
      return;
    }
    const values = form.getFieldsValue();
    setSuggestLoading(true);
    try {
      const res = await autoSuggestMeasureCatalog({
        name: String(name).trim(),
        description: values.description ?? null,
        domain: values.domain ?? null,
      });
      const f = res.fields;
      const llmCount = Object.values(f).filter((x) => x.source === "llm").length;
      form.setFieldsValue({
        measure_code: f.measure_code?.value || undefined,
        measure_format: f.measure_format?.value || "AMOUNT",
        default_unit: f.default_unit?.value || undefined,
        default_decimal_places: f.default_decimal_places?.value ?? undefined,
        source_system: (f.source_system?.value as string[]) || [],
        synonyms: (f.synonyms?.value as string[]) || [],
        category: f.category?.value || "OTHER",
        stat_caliber: f.stat_caliber?.value || undefined,
        description: f.description?.value || values.description,
        domain: f.domain?.value || values.domain,
      });
      setSuggestResult(res);
      message.success(`AI 推断完成：${llmCount} 项 AI 生成，其余规则兜底，可修改后提交`);
    } catch (e) {
      message.error(errMsg(e, "AI 推断失败"));
    } finally {
      setSuggestLoading(false);
    }
  }

  async function handleSubmit() {
    const values = await form.validateFields();
    setSaving(true);
    try {
      if (editing) {
        await updateMeasureCatalog(editing.measure_code, {
          name: values.name,
          domain: values.domain,
          description: values.description ?? null,
          measure_format: values.measure_format,
          default_unit: values.default_unit ?? null,
          default_decimal_places: values.default_decimal_places ?? null,
          source_system: values.source_system ?? null,
          synonyms: values.synonyms ?? null,
          category: values.category ?? "OTHER",
          stat_caliber: values.stat_caliber ?? null,
        });
        message.success("度量已更新");
      } else {
        await createMeasureCatalog({
          measure_code: values.measure_code || undefined,
          name: values.name,
          domain: values.domain,
          description: values.description ?? null,
          measure_format: values.measure_format,
          default_unit: values.default_unit ?? null,
          default_decimal_places: values.default_decimal_places ?? null,
          source_system: values.source_system ?? null,
          synonyms: values.synonyms ?? null,
          category: values.category ?? "OTHER",
          stat_caliber: values.stat_caliber ?? null,
        });
        message.success("逻辑度量已创建（草稿）");
      }
      setModalOpen(false);
      await load();
    } catch (e) {
      message.error(errMsg(e, "保存失败"));
    } finally {
      setSaving(false);
    }
  }

  // 提交审核（DRAFT → REVIEW）：度量是原子指标继承源，发布须先审
  function openSubmitReview(row: MeasureCatalog) {
    setSubmitTarget(row);
    reviewForm.resetFields();
  }

  async function handleSubmitReview() {
    if (!submitTarget) return;
    const values = await reviewForm.validateFields();
    setSubmitBusy(true);
    try {
      await submitMeasureCatalog(submitTarget.measure_code, {
        change_reason: values.change_reason,
        reviewer_type: values.reviewer_type ?? null,
        reviewer_id: values.reviewer_id ?? null,
        reviewer_domain: values.reviewer_domain ?? null,
      });
      message.success(`「${submitTarget.name}」已提交审核，待评审通过后发布`);
      setSubmitTarget(null);
      await load();
    } catch (e) {
      message.error(errMsg(e, "提交审核失败"));
    } finally {
      setSubmitBusy(false);
    }
  }

  // 审核通过（REVIEW → PUBLISHED）
  async function handleApprove(row: MeasureCatalog) {
    setBusyCode(row.measure_code);
    try {
      await approveMeasureCatalog(row.measure_code, { comment: null });
      message.success(`「${row.name}」审核通过，已发布`);
      await load();
    } catch (e) {
      message.error(errMsg(e, "审核通过失败"));
    } finally {
      setBusyCode(null);
    }
  }

  // 审核驳回（REVIEW → DRAFT，驳回原因必填）
  function openReject(row: MeasureCatalog) {
    setRejectTarget(row);
    reviewForm.resetFields();
  }

  async function handleReject() {
    if (!rejectTarget) return;
    const values = await reviewForm.validateFields();
    setRejectBusy(true);
    try {
      await rejectMeasureCatalog(rejectTarget.measure_code, { reason: values.reason });
      message.success(`「${rejectTarget.name}」已驳回，可修改后重新提交`);
      setRejectTarget(null);
      await load();
    } catch (e) {
      message.error(errMsg(e, "驳回失败"));
    } finally {
      setRejectBusy(false);
    }
  }

  async function handleDeprecate(row: MeasureCatalog) {
    try {
      await deprecateMeasureCatalog(row.measure_code);
      message.success(`「${row.name}」已废弃`);
      await load();
    } catch (e) {
      message.error(errMsg(e, "废弃失败"));
    }
  }

  return (
    <Card
      title="逻辑度量目录"
      extra={
        canWrite ? (
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新建逻辑度量
          </Button>
        ) : null
      }
    >
      <Space style={{ marginBottom: 16 }} wrap>
        <Input.Search
          placeholder="编码/名称/描述模糊搜索"
          allowClear
          style={{ width: 260 }}
          onSearch={(v) => {
            setPage(1);
            setKeyword(v || undefined);
          }}
        />
        <Select
          placeholder="业务域"
          allowClear
          style={{ width: 200 }}
          options={domainOptions}
          onChange={(v) => {
            setPage(1);
            setDomain(v);
          }}
        />
        <Select
          placeholder="状态"
          allowClear
          style={{ width: 140 }}
          options={Object.keys(STATUS_LABEL).map((v) => ({ value: v, label: STATUS_LABEL[v] }))}
          onChange={(v) => {
            setPage(1);
            setStatus(v);
          }}
        />
      </Space>

      <Table<MeasureCatalog>
        rowKey="id"
        loading={loading}
        dataSource={items}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          onChange: (p, ps) => {
            setPage(p);
            setPageSize(ps);
          },
        }}
        columns={[
          { title: "编码", dataIndex: "measure_code", width: 180, render: (v) => <code>{v}</code> },
          { title: "名称", dataIndex: "name", width: 160 },
          {
            title: "度量格式",
            dataIndex: "measure_format",
            width: 100,
            render: (v: MeasureFormat) => (
              <Tag color={v === "AMOUNT" ? "gold" : v === "RATIO" ? "cyan" : "default"}>
                {MEASURE_FORMAT_LABEL[v] ?? v}
              </Tag>
            ),
          },
          { title: "默认单位", dataIndex: "default_unit", width: 100, render: (v) => v || "—" },
          {
            title: "默认小数位",
            dataIndex: "default_decimal_places",
            width: 100,
            render: (v) => (v == null ? "按需" : v),
          },
          {
            title: "源头系统",
            dataIndex: "source_system",
            width: 160,
            render: (v: string[] | null) =>
              v?.length ? v.map((s) => <Tag key={s}>{s}</Tag>) : <span className="muted">—</span>,
          },
          {
            title: "同义词",
            dataIndex: "synonyms",
            width: 160,
            render: (v: string[] | null) =>
              v?.length ? v.join("、") : <span className="muted">—</span>,
          },
          {
            title: "度量分类",
            dataIndex: "category",
            width: 110,
            render: (v: MeasureCategory) => (
              <Tag color={v === "FEE" ? "gold" : v === "DRUG" ? "geekblue" : v === "MEDICAL_INSURANCE" ? "purple" : "default"}>
                {MEASURE_CATEGORY_LABEL[v] ?? v}
              </Tag>
            ),
          },
          { title: "业务域", dataIndex: "domain", width: 120 },
          {
            title: "状态",
            dataIndex: "status",
            width: 90,
            render: (v: string) => <Tag color={STATUS_COLOR[v]}>{STATUS_LABEL[v] ?? v}</Tag>,
          },
          {
            title: "更新时间",
            dataIndex: "updated_at",
            width: 160,
            render: (v) => formatCnTime(v),
          },
          {
            title: "操作",
            key: "actions",
            width: 280,
            render: (_, row) => {
              const canReview = canReviewMeasure(row, currentUser);
              return (
                <Space size={4}>
                  {row.status === "REVIEW" ? (
                    <Tooltip title="审核中，锁定不可编辑；驳回后即可修改">
                      <Button size="small" icon={<EditOutlined />} disabled />
                    </Tooltip>
                  ) : (
                    <Tooltip title="编辑（DRAFT 可改编码）">
                      <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(row)} />
                    </Tooltip>
                  )}
                  {row.status === "DRAFT" && (
                    <Tooltip title="提交审核（度量发布前须评审通过）">
                      <Button
                        size="small"
                        type="primary"
                        icon={<SendOutlined />}
                        onClick={() => openSubmitReview(row)}
                      >
                        提交审核
                      </Button>
                    </Tooltip>
                  )}
                  {row.status === "REVIEW" && canReview && (
                    <>
                      <Tooltip title="审核通过并发布">
                        <Button
                          size="small"
                          type="primary"
                          icon={<CheckOutlined />}
                          aria-label="审核通过并发布"
                          loading={busyCode === row.measure_code}
                          onClick={() => handleApprove(row)}
                        />
                      </Tooltip>
                      <Tooltip title="驳回（须填原因）">
                        <Button
                          size="small"
                          danger
                          icon={<CloseOutlined />}
                          aria-label="驳回该度量"
                          onClick={() => openReject(row)}
                        />
                      </Tooltip>
                    </>
                  )}
                  {row.status === "PUBLISHED" && (
                    <Popconfirm
                      title="确认废弃该逻辑度量？"
                      description="被指标引用的度量无法废弃"
                      onConfirm={() => handleDeprecate(row)}
                    >
                      <Button size="small" danger icon={<StopOutlined />} />
                    </Popconfirm>
                  )}
                </Space>
              );
            },
          },
        ]}
      />

      <Modal
        title={editing ? `编辑逻辑度量 · ${editing.measure_code}` : "新建逻辑度量"}
        open={modalOpen}
        onOk={handleSubmit}
        onCancel={() => setModalOpen(false)}
        confirmLoading={saving}
        width={640}
      >
        <Form form={form} layout="vertical">
          {!editing && (
            <Form.Item
              name="measure_code"
              label="逻辑度量编码（英文，缺省自动生成）"
              rules={[{ pattern: /^[a-z][a-z0-9_]*$/, message: "小写字母开头，仅小写字母/数字/下划线" }]}
            >
              <Input placeholder="如 pay_amt" maxLength={64} />
            </Form.Item>
          )}
          <Form.Item name="name" label="度量中文名" rules={[{ required: true, message: "请输入度量中文名" }]}>
            <Input placeholder="如 门诊收费金额" maxLength={128} />
          </Form.Item>
          <Space style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
            <Button
              icon={<RobotOutlined />}
              onClick={handleSuggest}
              loading={suggestLoading}
              disabled={!!editing}
            >
              AI 推断（自动回填全部字段，可修改）
            </Button>
            <span className="muted" style={{ fontSize: 12 }}>
              仅新建时可用；推断结果可修改后再提交
            </span>
          </Space>
          {suggestResult && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message="AI 推断结果（已回填表单，可修改）"
              description={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {[
                    ["measure_code", "编码"],
                    ["measure_format", "格式"],
                    ["category", "分类"],
                    ["default_unit", "单位"],
                    ["default_decimal_places", "小数位"],
                    ["stat_caliber", "统计口径"],
                    ["source_system", "源头系统"],
                    ["synonyms", "同义词"],
                    ["description", "描述"],
                  ].map(([key, label]) => {
                    const sf = suggestResult.fields[key];
                    if (!sf) return null;
                    const v = Array.isArray(sf.value) ? sf.value.join("、") : sf.value ?? "—";
                    return (
                      <li key={key}>
                        <b>{label}：</b>
                        {String(v)}（{sf.source === "llm" ? "AI" : "规则"} · 置信度 {(sf.confidence * 100).toFixed(0)}%）
                      </li>
                    );
                  })}
                </ul>
              }
            />
          )}
          <Form.Item name="domain" label="业务域" rules={[{ required: true, message: "请选择业务域" }]}>
            <Select options={domainOptions} showSearch optionFilterProp="label" placeholder="选择业务域" />
          </Form.Item>
          <Form.Item
            name="category"
            label="度量分类"
            rules={[{ required: true, message: "请选择度量分类" }]}
            extra="按业务视角组织度量目录：流量/费用/药品/医保/效率/质量"
          >
            <Select options={CATEGORY_OPTIONS} placeholder="选择度量分类" />
          </Form.Item>
          <Form.Item
            name="stat_caliber"
            label="统计口径"
            extra="业务侧如何计算该度量，如「收费明细按结算日期去重后求和」"
          >
            <Input.TextArea rows={2} maxLength={1000} placeholder="描述统计口径（可选）" />
          </Form.Item>
          <Form.Item
            name="measure_format"
            label="度量格式"
            rules={[{ required: true }]}
            extra="决定默认单位与小数位（金额:元/2位，比率:小数/4位，数值:自定义）"
          >
            <Select options={FORMAT_OPTIONS} onChange={onFormatChange} />
          </Form.Item>
          <Space size={16} style={{ display: "flex" }}>
            <Form.Item name="default_unit" label="默认单位" style={{ width: 200 }}>
              <Input placeholder="金额默认 元" maxLength={32} />
            </Form.Item>
            <Form.Item name="default_decimal_places" label="默认小数位" style={{ width: 160 }}>
              <Select
                allowClear
                placeholder="按需"
                options={[
                  { value: 0, label: "0" },
                  { value: 1, label: "1" },
                  { value: 2, label: "2" },
                  { value: 4, label: "4" },
                ]}
              />
            </Form.Item>
            {watchFormat === "AMOUNT" && (
              <Form.Item name="currency_hint" label=" " style={{ width: 200 }}>
                <Input placeholder="币种见指标注册页" disabled />
              </Form.Item>
            )}
          </Space>
          <Form.Item name="source_system" label="源头系统（业务系统术语，多选）">
            <Select mode="tags" placeholder="输入后回车添加" tokenSeparators={[","]} />
          </Form.Item>
          <Form.Item name="synonyms" label="同义词（统一查询/查重匹配）">
            <Select mode="tags" placeholder="输入后回车添加" tokenSeparators={[","]} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} maxLength={500} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 提交审核 Modal（DRAFT → REVIEW）：度量发布前须评审通过 */}
      <Modal
        title={submitTarget ? `提交审核 · ${submitTarget.name}` : "提交审核"}
        open={!!submitTarget}
        onOk={handleSubmitReview}
        onCancel={() => setSubmitTarget(null)}
        confirmLoading={submitBusy}
        width={560}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="度量发布前须先评审通过"
          description="度量是原子指标的权威定义（单位/格式/小数位/口径直接传播到下游指标）。提交后由评审人审核通过才可发布；审核期间度量锁定不可编辑，驳回后可修改重提。"
        />
        <Form form={reviewForm} layout="vertical">
          <Form.Item
            name="change_reason"
            label="提交说明"
            rules={[{ required: true, min: 4, message: "请填写提交说明（至少 4 字），说明为何发布该度量" }]}
          >
            <Input.TextArea
              rows={2}
              maxLength={200}
              placeholder="如：门诊收费金额度量已与业务对齐口径，申请发布"
            />
          </Form.Item>
          <Form.Item
            name="reviewer_type"
            label="评审指派（可选）"
            extra="不指定则由域管理员兜底评审"
          >
            <Select
              allowClear
              placeholder="不指派（域管理员兜底）"
              options={[
                { value: "user", label: "指定用户" },
                { value: "domain", label: "指定域评审组" },
              ]}
            />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(prev, cur) => prev.reviewer_type !== cur.reviewer_type}>
            {({ getFieldValue }) =>
              getFieldValue("reviewer_type") === "user" ? (
                <Form.Item
                  name="reviewer_id"
                  label="评审用户 ID"
                  rules={[{ required: true, message: "请填写评审用户 ID" }]}
                >
                  <Input type="number" placeholder="如 5" />
                </Form.Item>
              ) : getFieldValue("reviewer_type") === "domain" ? (
                <Form.Item
                  name="reviewer_domain"
                  label="评审域"
                  rules={[{ required: true, message: "请填写评审域 code" }]}
                >
                  <Input placeholder="如 outpatient" />
                </Form.Item>
              ) : null
            }
          </Form.Item>
        </Form>
      </Modal>

      {/* 驳回审核 Modal（REVIEW → DRAFT）：驳回原因必填，通知提交人修改 */}
      <Modal
        title={rejectTarget ? `驳回审核 · ${rejectTarget.name}` : "驳回审核"}
        open={!!rejectTarget}
        onOk={handleReject}
        onCancel={() => setRejectTarget(null)}
        confirmLoading={rejectBusy}
        width={520}
      >
        <Form form={reviewForm} layout="vertical">
          <Form.Item
            name="reason"
            label="驳回原因"
            rules={[{ required: true, min: 4, message: "请填写驳回原因（至少 4 字），通知提交人修改" }]}
          >
            <Input.TextArea
              rows={3}
              maxLength={500}
              placeholder="如：统计口径与业务实际不符，请补充计算依据后重新提交"
            />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}
