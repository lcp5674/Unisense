import { useEffect, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, Space, Tooltip, App as AntApp, Popconfirm } from "antd";
import { PlusOutlined, SendOutlined, StopOutlined, EditOutlined } from "@ant-design/icons";
import {
  listMeasureCatalogs,
  createMeasureCatalog,
  updateMeasureCatalog,
  publishMeasureCatalog,
  deprecateMeasureCatalog,
  listDomainTree,
  UnisenseApiError,
} from "../api";
import type { MeasureCatalog, MeasureCategory, MeasureFormat, SubjectDomainTreeNode } from "../types";
import { MEASURE_CATEGORY_LABEL, MEASURE_FORMAT_LABEL } from "../types";
import { formatCnTime } from "../utils/timeCn";
import { usePermission } from "../hooks/usePermission";

const STATUS_COLOR: Record<string, string> = { DRAFT: "default", PUBLISHED: "success", DEPRECATED: "error" };
const STATUS_LABEL: Record<string, string> = { DRAFT: "草稿", PUBLISHED: "已发布", DEPRECATED: "已废弃" };

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
  const [form] = Form.useForm();
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
    form.resetFields();
    form.setFieldsValue({ measure_format: "AMOUNT", category: "OTHER" });
    setModalOpen(true);
  }

  function openEdit(row: MeasureCatalog) {
    setEditing(row);
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

  async function handlePublish(row: MeasureCatalog) {
    try {
      await publishMeasureCatalog(row.measure_code);
      message.success(`「${row.name}」已发布`);
      await load();
    } catch (e) {
      message.error(errMsg(e, "发布失败"));
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
            width: 220,
            render: (_, row) => (
              <Space size={4}>
                <Tooltip title="编辑（DRAFT 可改编码）">
                  <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(row)} />
                </Tooltip>
                {row.status === "DRAFT" && (
                  <Tooltip title="发布">
                    <Button size="small" type="primary" icon={<SendOutlined />} onClick={() => handlePublish(row)} />
                  </Tooltip>
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
            ),
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
            <Input placeholder="如 支付金额" maxLength={128} />
          </Form.Item>
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
    </Card>
  );
}
