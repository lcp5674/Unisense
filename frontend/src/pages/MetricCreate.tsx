import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button, Card, Checkbox, Cascader, Col, Form, Input, Row, Segmented, Select, Space, Spin, Typography, App as AntApp, Tag,
} from "antd";
import {
  createMetric, fetchAssetSearch, autoSuggestMetric, listDomainTree, listDictItems, UnisenseApiError,
} from "../api";
import type { MetricCreateRequest, MetricType, MetricTier, SubjectDomainTreeNode, AutoSuggestResponse } from "../types";

const { Title, Paragraph } = Typography;
const { TextArea } = Input;

// 域树→Cascader options
function treeToCascaderOptions(nodes: SubjectDomainTreeNode[]): any[] {
  return nodes.map((n) => ({
    value: n.code,
    label: `${n.name} (${n.code})`,
    children: n.children.length > 0 ? treeToCascaderOptions(n.children) : undefined,
  }));
}

// 字典字段配置：dict_type → 表单字段名
const DICT_FIELD_MAP: Array<{ dictType: string; field: string; label: string }> = [
  { dictType: "granularity", field: "granularity", label: "粒度" },
  { dictType: "unit", field: "unit", label: "单位" },
  { dictType: "aggregation", field: "aggregation", label: "聚合" },
  { dictType: "time_semantics", field: "time_semantics", label: "时间语义" },
  { dictType: "freshness", field: "freshness", label: "新鲜度" },
  { dictType: "dw_layer", field: "dw_layer", label: "数仓层" },
  { dictType: "metric_type", field: "type", label: "类型" },
  { dictType: "additivity", field: "additivity", label: "可加性" },
  { dictType: "serving_mode", field: "serving_mode", label: "服务模式" },
  { dictType: "metric_tier", field: "metric_tier", label: "分级" },
];

export function MetricCreate() {
  const navigate = useNavigate();
  const { message } = AntApp.useApp();
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();

  // 域树数据
  const [domainTree, setDomainTree] = useState<SubjectDomainTreeNode[]>([]);
  const [domainLoading, setDomainLoading] = useState(false);
  const [selectedDomain, setSelectedDomain] = useState<string>("");

  // 字典选项
  const [dictOptions, setDictOptions] = useState<Record<string, Array<{ value: string; label: string }>>>({});
  const [dictLoading, setDictLoading] = useState(false);

  // 自动推断
  const [suggesting, setSuggesting] = useState(false);
  const [suggestedCode, setSuggestedCode] = useState<string | null>(null);

  // 口径录入模式
  const [mode, setMode] = useState<"expression" | "sql">("expression");
  const [sqlText, setSqlText] = useState("");
  const [sourceTables, setSourceTables] = useState<string[]>([]);
  const [tableOptions, setTableOptions] = useState<{ value: string; label: string }[]>([]);
  const [tableSearching, setTableSearching] = useState(false);

  // 加载域树
  useEffect(() => {
    setDomainLoading(true);
    listDomainTree("active")
      .then(setDomainTree)
      .catch(() => message.error("加载域树失败"))
      .finally(() => setDomainLoading(false));
  }, []);

  // 加载所有字典选项
  useEffect(() => {
    setDictLoading(true);
    Promise.all(
      DICT_FIELD_MAP.map(async ({ dictType }) => {
        try {
          const items = await listDictItems(dictType);
          return { dictType, options: items.map((i) => ({ value: i.code, label: `${i.label} (${i.code})` })) };
        } catch {
          return { dictType, options: [] };
        }
      }),
    )
      .then((results) => {
        const map: Record<string, Array<{ value: string; label: string }>> = {};
        for (const r of results) map[r.dictType] = r.options;
        setDictOptions(map);
      })
      .finally(() => setDictLoading(false));
  }, []);

  async function searchTables(q: string) {
    if (!q.trim()) return;
    setTableSearching(true);
    try {
      const res = await fetchAssetSearch({ q: q.trim(), type: "table", limit: 20 });
      setTableOptions(res.items.map((it) => ({ value: it.name, label: it.name })));
    } catch { setTableOptions([]); }
    finally { setTableSearching(false); }
  }

  // 选域后自动推断
  async function handleDomainChange(value: string[], _selectedOptions: any) {
    const domainCode = value[value.length - 1];
    setSelectedDomain(domainCode);
    setSuggesting(true);
    try {
      const sourceTable = form.getFieldValue("source_table");
      const measureColumn = form.getFieldValue("measure_column");
      const period = form.getFieldValue("period") || "day";

      const result = await autoSuggestMetric({
        domain_code: domainCode,
        source_table: sourceTable || undefined,
        measure_column: measureColumn || undefined,
        period,
      });

      // 填入编码建议
      if (result.metric_code_suggestion) {
        setSuggestedCode(result.metric_code_suggestion);
        form.setFieldValue("metric_code", result.metric_code_suggestion);
      }

      // 填入默认值
      const defaults = result.defaults || {};
      for (const { dictType, field } of DICT_FIELD_MAP) {
        const defaultVal = defaults[dictType] || defaults[field];
        if (defaultVal) form.setFieldValue(field, defaultVal);
      }

      // 填入推断的 period → granularity
      if (defaults.granularity) form.setFieldValue("granularity", defaults.granularity);
    } catch {
      // 推断失败不阻断
    } finally {
      setSuggesting(false);
    }
  }

  // 输入源表/度量列后重新推断编码
  async function handleAutoSuggest() {
    if (!selectedDomain) return;
    setSuggesting(true);
    try {
      const sourceTable = form.getFieldValue("source_table");
      const measureColumn = form.getFieldValue("measure_column");
      const period = form.getFieldValue("period") || "day";
      const result = await autoSuggestMetric({
        domain_code: selectedDomain,
        source_table: sourceTable || undefined,
        measure_column: measureColumn || undefined,
        period,
      });
      if (result.metric_code_suggestion) {
        setSuggestedCode(result.metric_code_suggestion);
        form.setFieldValue("metric_code", result.metric_code_suggestion);
      }
    } catch { /* 忽略 */ }
    finally { setSuggesting(false); }
  }

  function buildDefinitionJson(values: Record<string, unknown>): Record<string, unknown> | null {
    const tables = sourceTables.length ? { source_tables: sourceTables } : {};
    if (mode === "sql") {
      const sql = sqlText.trim();
      if (!sql) { message.error("口径 SQL 模式请输入 SQL 语句"); return null; }
      return { sql, ...tables };
    }
    let def: Record<string, unknown>;
    try { def = values.definition ? JSON.parse(String(values.definition)) : {}; }
    catch { message.error("口径定义需为合法 JSON"); return null; }
    return { ...def, ...tables };
  }

  async function handleSubmit(values: Record<string, unknown>) {
    setLoading(true);
    const definitionJson = buildDefinitionJson(values);
    if (!definitionJson) { setLoading(false); return; }
    const req: MetricCreateRequest = {
      metric_code: String(values.metric_code),
      name: String(values.name),
      domain: selectedDomain,
      type: String(values.type) as MetricType,
      granularity: String(values.granularity),
      unit: String(values.unit),
      aggregation: String(values.aggregation) as MetricCreateRequest["aggregation"],
      time_semantics: String(values.time_semantics) as MetricCreateRequest["time_semantics"],
      freshness: String(values.freshness) as MetricCreateRequest["freshness"],
      dw_layer: String(values.dw_layer) as MetricCreateRequest["dw_layer"],
      metric_tier: String(values.metric_tier || "T3") as MetricTier,
      serving_mode: String(values.serving_mode || "BATCH_ONLY") as MetricCreateRequest["serving_mode"],
      additivity: String(values.additivity || "ADDITIVE") as MetricCreateRequest["additivity"],
      definition_json: definitionJson,
      pii_flag: Boolean(values.pii_flag),
      source_table: values.source_table ? String(values.source_table) : undefined,
      measure_column: values.measure_column ? String(values.measure_column) : undefined,
      period: values.period ? String(values.period) : undefined,
    };
    try {
      const created = await createMetric(req);
      message.success(`创建草稿成功：${created.metric_code}`);
      navigate(`/detail/${created.metric_code}`);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.code}）` : "创建失败");
    } finally {
      setLoading(false);
    }
  }

  const dictSelect = (dictType: string, field: string, placeholder: string) => (
    <Select
      showSearch
      placeholder={placeholder}
      options={dictOptions[dictType] || []}
      optionFilterProp="label"
      disabled={dictLoading}
    />
  );

  return (
    <div>
      <Title level={3}>注册指标（草稿）</Title>
      <Card>
        <Form form={form} layout="vertical" onFinish={handleSubmit} initialValues={{
          type: "atomic", granularity: "day", aggregation: "SUM",
          time_semantics: "PERIOD", freshness: "T1", dw_layer: "DWD",
          metric_tier: "T3", serving_mode: "BATCH_ONLY", additivity: "ADDITIVE",
          pii_flag: false, period: "day",
        }}>
          <Space style={{ width: "100%" }} direction="vertical" size="middle">
            {/* Step 1: 选域 */}
            <Card type="inner" title="① 选择业务域" size="small">
              <Form.Item name="domain_path" label="业务域" rules={[{ required: true, message: "请选择业务域" }]}>
                <Cascader
                  options={treeToCascaderOptions(domainTree)}
                  placeholder="选择业务域（如 销售 > 销售订单）"
                  onChange={handleDomainChange}
                  changeOnSelect
                  loading={domainLoading}
                  showSearch
                />
              </Form.Item>
            </Card>

            {/* Step 2: 自动推断 */}
            <Card type="inner" title="② 自动推断" size="small" extra={suggesting && <Spin size="small" />}>
              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="source_table" label="源表名" onBlur={handleAutoSuggest}>
                    <Input placeholder="如 dwd.sales_detail" />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="measure_column" label="度量列" onBlur={handleAutoSuggest}>
                    <Input placeholder="如 amount" />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="period" label="统计周期" onBlur={handleAutoSuggest}>
                    <Input placeholder="如 day" />
                  </Form.Item>
                </Col>
              </Row>
            </Card>

            {/* Step 3: 确认/覆盖 */}
            <Card type="inner" title="③ 确认/覆盖字段" size="small">
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="metric_code" label="指标编码" rules={[{ required: true }]} extra={suggestedCode && <Tag color="blue" style={{ marginTop: 4 }}>系统建议: {suggestedCode}</Tag>}>
                    <Input placeholder="4段式: 域_业务对象_度量_周期" />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="name" label="名称" rules={[{ required: true }]}>
                    <Input placeholder="指标显示名称" />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="type" label="类型">
                    {dictSelect("metric_type", "type", "选择类型")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="granularity" label="粒度">
                    {dictSelect("granularity", "granularity", "选择粒度")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="unit" label="单位">
                    {dictSelect("unit", "unit", "选择单位")}
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="aggregation" label="聚合">
                    {dictSelect("aggregation", "aggregation", "选择聚合方式")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="time_semantics" label="时间语义">
                    {dictSelect("time_semantics", "time_semantics", "选择时间语义")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="freshness" label="新鲜度">
                    {dictSelect("freshness", "freshness", "选择新鲜度")}
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="dw_layer" label="数仓层">
                    {dictSelect("dw_layer", "dw_layer", "选择数仓层")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="additivity" label="可加性">
                    {dictSelect("additivity", "additivity", "选择可加性")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="serving_mode" label="服务模式">
                    {dictSelect("serving_mode", "serving_mode", "选择服务模式")}
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="metric_tier" label="分级">
                    {dictSelect("metric_tier", "metric_tier", "选择分级")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="pii_flag" label="含 PII" valuePropName="checked">
                    <Checkbox>含 PII</Checkbox>
                  </Form.Item>
                </Col>
              </Row>
            </Card>

            {/* 关联数据表 */}
            <Card type="inner" title="④ 口径定义" size="small">
              <Form.Item label="关联数据表">
                <Select
                  mode="multiple" allowClear showSearch
                  placeholder="搜索并选择口径引用的数据表"
                  value={sourceTables}
                  onChange={(v: string[]) => setSourceTables(v)}
                  onSearch={searchTables}
                  loading={tableSearching}
                  notFoundContent={null}
                  options={tableOptions}
                  filterOption={false}
                />
              </Form.Item>
              <Form.Item label="口径定义模式">
                <Segmented
                  block value={mode}
                  onChange={(v) => setMode(v as "expression" | "sql")}
                  options={[
                    { value: "expression", label: "表达式（结构化）" },
                    { value: "sql", label: "SQL 模式" },
                  ]}
                />
              </Form.Item>
              {mode === "expression" ? (
                <Form.Item name="definition" label="口径定义 (JSON)">
                  <TextArea rows={5} placeholder='{"expr": "sum(amount)", "filters": []}' />
                </Form.Item>
              ) : (
                <Form.Item label="口径 SQL">
                  <TextArea rows={5} value={sqlText} onChange={(e) => setSqlText(e.target.value)} placeholder="SELECT SUM(amount) AS gmv\nFROM catalog.sales.orders" className="mono" />
                  <Paragraph type="secondary" style={{ marginTop: 4, fontSize: 12 }}>后端将用 sqlglot 校验 SQL 语法；不合法将拒绝提交。</Paragraph>
                </Form.Item>
              )}
            </Card>

            <Form.Item>
              <Button type="primary" htmlType="submit" loading={loading} size="large">创建草稿</Button>
            </Form.Item>
          </Space>
        </Form>
      </Card>
    </div>
  );
}
