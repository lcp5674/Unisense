import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, App as AntApp, Button, Card, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag, Tooltip } from "antd";
import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  RedoOutlined,
  ReloadOutlined,
  RobotOutlined,
  StopOutlined,
} from "@ant-design/icons";
import {
  approveMeasureCatalog,
  autoSuggestMeasureCatalog,
  batchApproveMeasures,
  batchDeleteMeasures,
  batchDeprecateMeasures,
  batchReactivateMeasures,
  batchRejectMeasures,
  batchSubmitMeasures,
  createMeasureCatalog,
  deleteMeasureCatalog,
  deprecateMeasureCatalog,
  fetchCurrentUser,
  listDomainTree,
  listMeasureCatalogs,
  reactivateMeasureCatalog,
  rejectMeasureCatalog,
  restoreMeasureCatalog,
  submitMeasureCatalog,
  UnisenseApiError,
  updateMeasureCatalog,
} from "../api";
import type { ReviewSubmitBody } from "../api";
import type { BatchResult, CurrentUser, MeasureCatalog, MeasureCategory, MeasureFormat, MeasureSuggestResult, SubjectDomainTreeNode } from "../types";
import { MEASURE_CATEGORY_LABEL, MEASURE_FORMAT_LABEL } from "../types";
import { formatCnTime } from "../utils/timeCn";
import { usePermission } from "../hooks/usePermission";
import { MasterDataBatch, type BatchActionKey } from "../components/MasterDataBatch";
import {
  MasterDataReviewActions,
  MasterDataReviewModals,
  useMasterDataReview,
} from "../components/MasterDataReview";

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

export function MeasureCatalogs() {
  const { message } = AntApp.useApp();
  const { can } = usePermission();
  const canWrite = can("metric:create") || can("metric:import");
  // 全局搜索跳转定位：?kw= 预填关键词（与维度/术语/模板等列表页一致）
  const [searchParams] = useSearchParams();
  const urlKw = searchParams.get("kw") ?? "";
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [items, setItems] = useState<MeasureCatalog[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [domain, setDomain] = useState<string>();
  const [status, setStatus] = useState<string>();
  const [keyword, setKeyword] = useState<string | undefined>(urlKw || undefined);
  // 回收站视图：deleted=true 时列出已软删度量（仅管理员/原 Owner 可恢复）
  const [deleted, setDeleted] = useState(false);
  const [domainOptions, setDomainOptions] = useState<{ value: string; label: string }[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<MeasureCatalog | null>(null);
  const [saving, setSaving] = useState(false);
  const [suggestLoading, setSuggestLoading] = useState(false);
  const [suggestResult, setSuggestResult] = useState<MeasureSuggestResult | null>(null);
  // 审核流状态（共享 hook：提交审核 Modal / 驳回 Modal / 正在审批的度量）
  const review = useMasterDataReview();
  const [form] = Form.useForm();
  const watchFormat = Form.useWatch("measure_format", form);
  // 批量操作：多选行（rowSelection）+ 共享 MasterDataBatch 组件（对齐指标完整批量模式）
  const [selected, setSelected] = useState<MeasureCatalog[]>([]);
  // 评审角色判断（对齐后端 _REVIEW_ROLES）：平台管理员/域管理员/评审员可审
  const canReview =
    !!currentUser &&
    (currentUser.role === "platform_admin" ||
      currentUser.role === "domain_admin" ||
      currentUser.role === "reviewer");

  async function runBatch(action: BatchActionKey, opts: {
    codes: string[];
    reason?: string;
    changeReason?: string;
    reviewerType?: "user" | "domain" | null;
    reviewerId?: number | null;
    reviewerDomain?: string | null;
  }): Promise<BatchResult> {
    if (action === "submit") {
      return batchSubmitMeasures(
        opts.codes.map((code) => ({
          code,
          change_reason: opts.changeReason ?? "批量提交审核",
          reviewer_id: opts.reviewerType === "user" ? opts.reviewerId : null,
          reviewer_type: opts.reviewerType,
          reviewer_domain: opts.reviewerType === "domain" ? opts.reviewerDomain : null,
        })),
      );
    }
    if (action === "approve") return batchApproveMeasures(opts.codes);
    if (action === "reject") return batchRejectMeasures(opts.codes, opts.reason ?? "");
    if (action === "reactivate") return batchReactivateMeasures(opts.codes);
    if (action === "delete") return batchDeleteMeasures(opts.codes);
    return batchDeprecateMeasures(opts.codes);
  }

  async function load() {
    setLoading(true);
    try {
      const res = await listMeasureCatalogs({ domain, status, keyword, deleted, page, page_size: pageSize });
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
  }, [page, pageSize, domain, status, keyword, deleted]);

  // URL ?kw= 变化时同步关键词（全局搜索跳转定位；避免空值覆盖用户输入）
  useEffect(() => {
    if (urlKw && urlKw !== keyword) {
      setKeyword(urlKw);
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw]);

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
  async function handleSubmitReview(values: ReviewSubmitBody) {
    if (!review.submitTarget) return;
    review.setSubmitBusy(true);
    try {
      await submitMeasureCatalog(review.submitTarget.code, values);
      message.success(`「${review.submitTarget.name}」已提交审核，待评审通过后发布`);
      review.setSubmitTarget(null);
      await load();
    } catch (e) {
      message.error(errMsg(e, "提交审核失败"));
    } finally {
      review.setSubmitBusy(false);
    }
  }

  // 审核通过（REVIEW → PUBLISHED）
  async function handleApprove(row: { code: string; name: string }) {
    review.setBusyCode(row.code);
    try {
      await approveMeasureCatalog(row.code, { comment: null });
      message.success(`「${row.name}」审核通过，已发布`);
      await load();
    } catch (e) {
      message.error(errMsg(e, "审核通过失败"));
    } finally {
      review.setBusyCode(null);
    }
  }

  // 审核驳回（REVIEW → DRAFT，驳回原因必填）
  async function handleReject(reason: string) {
    if (!review.rejectTarget) return;
    review.setRejectBusy(true);
    try {
      await rejectMeasureCatalog(review.rejectTarget.code, { reason });
      message.success(`「${review.rejectTarget.name}」已驳回，可修改后重新提交`);
      review.setRejectTarget(null);
      await load();
    } catch (e) {
      message.error(errMsg(e, "驳回失败"));
    } finally {
      review.setRejectBusy(false);
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

  // 重新启用（DEPRECATED → DRAFT，可编辑后重新走审核）
  async function handleReactivate(row: MeasureCatalog) {
    try {
      await reactivateMeasureCatalog(row.measure_code);
      message.success(`「${row.name}」已重新启用，回到草稿，请提交审核后发布`);
      await load();
    } catch (e) {
      message.error(errMsg(e, "重新启用失败"));
    }
  }

  // 软删除（仅 DRAFT/DEPRECATED 可删；管理员或原 Owner）
  async function handleDelete(row: MeasureCatalog) {
    try {
      await deleteMeasureCatalog(row.measure_code);
      message.success(`「${row.name}」已删除，可在回收站恢复`);
      await load();
    } catch (e) {
      message.error(errMsg(e, "删除失败"));
    }
  }

  // 回收站恢复
  async function handleRestore(row: MeasureCatalog) {
    try {
      await restoreMeasureCatalog(row.measure_code);
      message.success(`「${row.name}」已恢复`);
      await load();
    } catch (e) {
      message.error(errMsg(e, "恢复失败"));
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
        <Select
          value={deleted ? "trash" : undefined}
          placeholder="回收站"
          allowClear
          style={{ width: 120 }}
          onChange={(v) => {
            setPage(1);
            setDeleted(v === "trash");
            setSelected([]);
          }}
          options={[{ value: "trash", label: "回收站" }]}
        />
        <MasterDataBatch
          selected={selected}
          codeKey="measure_code"
          entityLabel="逻辑度量"
          actions={[
            { key: "submit", label: "批量提交审核（草稿）" },
            { key: "approve", label: "批量通过（审核中）" },
            { key: "reject", label: "批量驳回（审核中）" },
            { key: "reactivate", label: "批量重新启用（已废弃）" },
            { key: "deprecate", label: "批量废弃（已发布）", danger: true },
            { key: "delete", label: "批量删除（草稿/废弃）", danger: true },
          ]}
          canRun={(a) => (a === "approve" || a === "reject" ? !!canReview : canWrite)}
          onRun={runBatch}
          onDone={() => {
            setSelected([]);
            void load();
          }}
          reviewerDomainOptions={domainOptions}
          user={currentUser}
        />
      </Space>

      <Table<MeasureCatalog>
        rowKey="id"
        loading={loading}
        dataSource={items}
        rowSelection={{
          selectedRowKeys: selected.map((s) => s.id),
          onChange: (_keys, rows) => setSelected(rows),
        }}
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
            width: 300,
            render: (_, row) => {
              if (deleted) {
                return (
                  <Space size={4}>
                    <Popconfirm
                      title="确认恢复该逻辑度量？"
                      description="恢复后回到原状态（草稿/废弃），可重新走审核流"
                      onConfirm={() => handleRestore(row)}
                    >
                      <Button size="small" type="primary" icon={<ReloadOutlined />}>恢复</Button>
                    </Popconfirm>
                  </Space>
                );
              }
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
                  <MasterDataReviewActions
                    row={{
                      code: row.measure_code,
                      name: row.name,
                      status: row.status,
                      reviewer_type: row.reviewer_type,
                      reviewer_id: row.reviewer_id,
                      reviewer_domain: row.reviewer_domain,
                    }}
                    user={currentUser}
                    busyCode={review.busyCode}
                    onApprove={handleApprove}
                    onOpenSubmit={(r) => review.setSubmitTarget({ code: r.code, name: r.name })}
                    onOpenReject={(r) => review.setRejectTarget({ code: r.code, name: r.name })}
                  />
                  {row.status === "PUBLISHED" && (
                    <Popconfirm
                      title="确认废弃该逻辑度量？"
                      description="被指标引用的度量无法废弃"
                      onConfirm={() => handleDeprecate(row)}
                    >
                      <Button size="small" danger icon={<StopOutlined />} />
                    </Popconfirm>
                  )}
                  {row.status === "DEPRECATED" && (
                    <>
                      <Popconfirm
                        title="确认重新启用该逻辑度量？"
                        description="回到草稿状态，需重新提交审核后才能发布"
                        onConfirm={() => handleReactivate(row)}
                      >
                        <Button size="small" icon={<RedoOutlined />}>重新启用</Button>
                      </Popconfirm>
                      <Popconfirm
                        title="确认删除该逻辑度量？"
                        description="删除后进入回收站，可恢复；被指标引用的度量无法删除"
                        onConfirm={() => handleDelete(row)}
                      >
                        <Button size="small" danger icon={<DeleteOutlined />} aria-label="删除" />
                      </Popconfirm>
                    </>
                  )}
                  {row.status === "DRAFT" && (
                    <Popconfirm
                      title="确认删除该逻辑度量？"
                      description="删除后进入回收站，可恢复"
                      onConfirm={() => handleDelete(row)}
                    >
                      <Button size="small" danger icon={<DeleteOutlined />} aria-label="删除" />
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

      {/* 提交审核 + 驳回审核 Modal（共享组件）：度量发布前须评审通过 */}
      <MasterDataReviewModals
        entityLabel="逻辑度量"
        submitDescription="度量是原子指标的权威定义（单位/格式/小数位/口径直接传播到下游指标）。提交后由评审人审核通过才可发布；审核期间度量锁定不可编辑，驳回后可修改重提。"
        reviewerDomainOptions={domainOptions}
        user={currentUser}
        submitTarget={review.submitTarget}
        submitBusy={review.submitBusy}
        onCancelSubmit={() => review.setSubmitTarget(null)}
        onConfirmSubmit={handleSubmitReview}
        rejectTarget={review.rejectTarget}
        rejectBusy={review.rejectBusy}
        onCancelReject={() => review.setRejectTarget(null)}
        onConfirmReject={handleReject}
      />
    </Card>
  );
}
