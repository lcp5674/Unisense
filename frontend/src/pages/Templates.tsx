import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Space } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { listTemplates, createMetric, instantiateTemplate, UnisenseApiError } from "../api";
import type { MetricCreateRequest, MetricTemplate, MetricType } from "../types";
import { useTracking } from "../hooks/useTracking";
import { enumLabel, METRIC_TYPE_LABEL, GRANULARITY_LABEL, AGGREGATION_LABEL, TIME_SEMANTICS_LABEL, FRESHNESS_LABEL, DW_LAYER_LABEL, METRIC_TIER_LABEL } from "../utils/enums";

export function Templates() {
  const [searchParams] = useSearchParams();
  // URL 直达参数（?kw=）作为初始筛选，避免「先查全量再过滤」的竞态覆盖
  const urlKw = searchParams.get("kw") ?? "";
  // 启用状态下钻（?is_active=，总览仪表「指标模板」资产卡片）作为初始筛选；
  // 默认仅展示启用模板（与原有行为一致），inactive 下钻展示停用模板
  const urlIsActive = searchParams.get("is_active") ?? "";
  const [items, setItems] = useState<MetricTemplate[]>([]);
  const [keyword, setKeyword] = useState(urlKw);
  const [isActive, setIsActive] = useState<string>(urlIsActive === "inactive" ? "inactive" : "active");
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [instantiateTarget, setInstantiateTarget] = useState<MetricTemplate | null>(null);
  const [form] = Form.useForm();
  const navigate = useNavigate();
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果
  const loadSeq = useRef(0);
  const { track } = useTracking();

  // 支持从全局搜索栏经 ?kw= 直达定位；初始值已由 useState 承接，
  // 此处仅同步「URL 出现新筛选值」的场景，并保留用户手动清空筛选的能力。
  useEffect(() => {
    if (urlKw && urlKw !== keyword) setKeyword(urlKw);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw]);

  // 响应 URL 启用状态参数变化（总览仪表「指标模板」资产卡片二次下钻）
  useEffect(() => {
    const next = urlIsActive === "inactive" ? "inactive" : "active";
    if (next !== isActive) setIsActive(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlIsActive]);

  async function load() {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      // 默认仅展示启用模板；inactive 时展示停用模板（总览仪表下钻）
      const res = await listTemplates({ is_active: isActive !== "inactive", keyword: keyword || undefined });
      // 已有更新的请求发起，丢弃本次过时响应（防竞态覆盖）
      if (seq !== loadSeq.current) return;
      setItems(res);
    } catch (err) {
      if (seq !== loadSeq.current) return;
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载模板失败");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyword, isActive]);

  async function handleCreate(values: Record<string, unknown>) {
    setLoading(true);
    try {
      // 组装指标基础信息（模板实例化时后端会把模板默认口径与用户覆盖合并）
      const payload: MetricCreateRequest = {
        metric_code: values.metric_code ? String(values.metric_code) : undefined,
        name: String(values.name),
        domain: String(values.domain),
        type: (String(values.type) as MetricType) ?? "atomic",
        granularity: String(values.granularity || "daily"),
        unit: String(values.unit || ""),
        aggregation: (String(values.aggregation) as MetricCreateRequest["aggregation"]) ?? "SUM",
        time_semantics: (String(values.time_semantics) as MetricCreateRequest["time_semantics"]) ?? "PERIOD",
        freshness: (String(values.freshness) as MetricCreateRequest["freshness"]) ?? "T1",
        dw_layer: (String(values.dw_layer) as MetricCreateRequest["dw_layer"]) ?? "DWS",
        // 模板默认口径优先保留（defaults_json.definition_json）；缺省时补空对象满足后端必填
        definition_json:
          (instantiateTarget?.defaults_json?.definition_json as Record<string, unknown>) ?? {},
      };
      // 从模板实例化：调用专用接口（后端合并模板默认字段）；无模板上下文时退回普通创建指标
      const created = instantiateTarget
        ? await instantiateTemplate(instantiateTarget.id, payload)
        : await createMetric(payload);
      message.success(instantiateTarget ? `已从模板实例化：${created.metric_code}` : `已创建指标：${created.metric_code}`);
      track("template_instantiate", created.metric_code, "template");
      setModalOpen(false);
      navigate(`/detail/${created.metric_code}`);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "实例化失败");
    } finally {
      setLoading(false);
    }
  }

  function openInstantiate(tpl: MetricTemplate) {
    setInstantiateTarget(tpl);
    form.resetFields();
    form.setFieldsValue({
      metric_code: tpl.code,
      name: tpl.name,
      domain: tpl.domain,
      type: tpl.type ?? "atomic",
      granularity: tpl.granularity ?? "daily",
      unit: tpl.unit ?? "",
      aggregation: tpl.aggregation ?? "SUM",
      time_semantics: tpl.time_semantics ?? "PERIOD",
      freshness: tpl.freshness ?? "T1",
      dw_layer: tpl.dw_layer ?? "DWS",
    });
    setModalOpen(true);
  }

  const columns = [
    { title: "模板编码", dataIndex: "code", key: "code", render: (v: string) => <span className="mono">{v}</span> },
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "域", dataIndex: "domain", key: "domain", width: 140 },
    { title: "类型", dataIndex: "type", key: "type", width: 100, render: (v: string) => enumLabel(METRIC_TYPE_LABEL, v) },
    { title: "粒度", dataIndex: "granularity", key: "granularity", width: 100, render: (v: string) => enumLabel(GRANULARITY_LABEL, v) },
    { title: "聚合", dataIndex: "aggregation", key: "aggregation", width: 120, render: (v: string) => enumLabel(AGGREGATION_LABEL, v) },
    { title: "时间语义", dataIndex: "time_semantics", key: "time_semantics", width: 110, render: (v: string) => enumLabel(TIME_SEMANTICS_LABEL, v) },
    { title: "新鲜度", dataIndex: "freshness", key: "freshness", width: 90, render: (v: string) => enumLabel(FRESHNESS_LABEL, v) },
    { title: "数仓层", dataIndex: "dw_layer", key: "dw_layer", width: 90, render: (v: string) => enumLabel(DW_LAYER_LABEL, v) },
    { title: "分级", dataIndex: "metric_tier", key: "metric_tier", width: 90, render: (v: string) => <Tag>{enumLabel(METRIC_TIER_LABEL, v)}</Tag> },
    { title: "必填字段", dataIndex: "required_fields", key: "required_fields", render: (v: string[] | null) => (v?.length ? v.join("、") : <span className="muted">—</span>) },
    {
      title: "操作",
      key: "actions",
      width: 140,
      render: (_: unknown, t: MetricTemplate) => (
        <Button type="link" onClick={() => openInstantiate(t)}>实例化指标</Button>
      ),
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Assets / Templates</div>
          <h2>指标模板</h2>
          <p>标准化的指标创建模板——一键实例化，默认口径自动合并。</p>
        </div>
        <Button icon={<PlusOutlined />} onClick={load} loading={loading}>刷新</Button>
      </div>

      <Card>
        <div style={{ display: "flex", gap: 12, marginBottom: 12 }}>
          <Input.Search
            placeholder="搜索模板编码 / 名称 / 描述"
            allowClear
            style={{ width: 280 }}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onSearch={() => load()}
          />
          <Select
            style={{ width: 130 }}
            value={isActive}
            onChange={(v?: string) => setIsActive(v ?? "active")}
            options={[
              { value: "active", label: "启用" },
              { value: "inactive", label: "停用" },
            ]}
          />
        </div>
        <Table
          dataSource={items}
          columns={columns}
          rowKey="id"
          loading={loading}
          pagination={false}
          locale={{ emptyText: "暂无模板" }}
        />
      </Card>

      <Modal
        title={instantiateTarget ? `从模板实例化：${instantiateTarget.name}` : "创建指标"}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        okText="实例化创建"
        confirmLoading={loading}
        width={560}
      >
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Space style={{ width: "100%" }} wrap>
            <Form.Item name="metric_code" label="指标编码" extra={<span className="mono" style={{ color: "#0E7C86" }}>留空则由系统自动生成</span>} style={{ width: 240 }}>
              <Input className="mono" placeholder="留空自动生成" />
            </Form.Item>
            <Form.Item name="name" label="名称" rules={[{ required: true }]} style={{ width: 260 }}>
              <Input />
            </Form.Item>
            <Form.Item name="domain" label="业务域" rules={[{ required: true }]} style={{ width: 240 }}>
              <Input />
            </Form.Item>
            <Form.Item name="type" label="类型" style={{ width: 240 }}>
              <Select options={["atomic", "derived", "composite"].map((v) => ({ value: v, label: METRIC_TYPE_LABEL[v] ?? v }))} />
            </Form.Item>
            <Form.Item name="granularity" label="粒度" style={{ width: 240 }}>
              <Input />
            </Form.Item>
            <Form.Item name="unit" label="单位" style={{ width: 240 }}>
              <Input />
            </Form.Item>
            <Form.Item name="aggregation" label="聚合" style={{ width: 240 }}>
              <Select options={["SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE"].map((v) => ({ value: v, label: AGGREGATION_LABEL[v] ?? v }))} />
            </Form.Item>
            <Form.Item name="time_semantics" label="时间语义" style={{ width: 240 }}>
              <Select options={["PERIOD", "YTD", "TTM", "AVG"].map((v) => ({ value: v, label: TIME_SEMANTICS_LABEL[v] ?? v }))} />
            </Form.Item>
            <Form.Item name="freshness" label="新鲜度" style={{ width: 240 }}>
              <Select options={["REALTIME", "T1", "HOURLY"].map((v) => ({ value: v, label: FRESHNESS_LABEL[v] ?? v }))} />
            </Form.Item>
            <Form.Item name="dw_layer" label="数仓层" style={{ width: 240 }}>
              <Select options={["ODS", "DWD", "DWS", "ADS", "DM"].map((v) => ({ value: v, label: DW_LAYER_LABEL[v] ?? v }))} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </div>
  );
}
