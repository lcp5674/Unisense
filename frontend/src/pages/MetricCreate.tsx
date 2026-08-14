import { useEffect, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert, Button, Card, Checkbox, Cascader, Col, Form, Input, Row, Segmented, Select, Space, Spin, Tooltip, Typography, App as AntApp, Tag,
} from "antd";
import {
  createMetric, listCatalogs, autoSuggestMetric, listDomainTree, listDictItems, checkConflict, UnisenseApiError,
} from "../api";
import type { MetricCreateRequest, MetricType, MetricTier, SubjectDomainTreeNode, ConflictCheckResult, DBCatalog, SuggestionField, AutoSuggestResponse } from "../types";
import { CONFLICT_TYPE_LABEL, CONFLICT_SEVERITY_LABEL, enumLabel } from "../utils/enums";

const { Title, Paragraph } = Typography;
const { TextArea } = Input;

function treeToCascaderOptions(nodes: SubjectDomainTreeNode[]): any[] {
  return nodes.map((n) => ({
    value: n.code,
    label: `${n.name} (${n.code})`,
    children: n.children.length > 0 ? treeToCascaderOptions(n.children) : undefined,
  }));
}

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

const PERIOD_OPTIONS = [
  { value: "day", label: "日 (day)" },
  { value: "week", label: "周 (week)" },
  { value: "month", label: "月 (month)" },
  { value: "quarter", label: "季 (quarter)" },
  { value: "year", label: "年 (year)" },
];

interface ColumnInfo {
  name: string;
  type?: string;
  comment?: string;
}

// 推断来源 → 徽标样式（与后端 SuggestionField.source 对齐）
const SOURCE_META: Record<string, { color: string; text: string }> = {
  sql_parse: { color: "geekblue", text: "SQL解析" },
  column_meta: { color: "purple", text: "列元数据" },
  domain_default: { color: "gold", text: "域默认" },
  rule: { color: "cyan", text: "规则" },
  llm: { color: "magenta", text: "AI" },
  fallback: { color: "default", text: "兜底" },
};

function InferBadge({ field }: { field: SuggestionField }) {
  const meta = SOURCE_META[field.source] || { color: "default", text: field.source };
  const pct = Math.round((Number(field.confidence) || 0) * 100);
  return (
    <Tooltip title={field.reason || `${field.source}（置信度 ${pct}%）`}>
      <Tag color={meta.color} style={{ marginLeft: 6 }}>
        {meta.text} · {pct}%
      </Tag>
    </Tooltip>
  );
}

export function MetricCreate() {
  const navigate = useNavigate();
  const { message } = AntApp.useApp();
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();

  const [domainTree, setDomainTree] = useState<SubjectDomainTreeNode[]>([]);
  const [domainLoading, setDomainLoading] = useState(false);
  const [selectedDomain, setSelectedDomain] = useState<string>("");

  const [dictOptions, setDictOptions] = useState<Record<string, Array<{ value: string; label: string }>>>({});
  const [dictLoading, setDictLoading] = useState(false);

  const [suggesting, setSuggesting] = useState(false);
  const [suggestedCode, setSuggestedCode] = useState<string | null>(null);

  const [mode, setMode] = useState<"expression" | "sql">("expression");
  const [sqlText, setSqlText] = useState("");
  const [sourceTables, setSourceTables] = useState<string[]>([]);
  const [tableOptions, setTableOptions] = useState<{ value: string; label: string }[]>([]);
  const [tableSearching, setTableSearching] = useState(false);

  // 源表搜索（自动推断区）
  const [srcTableSearchOptions, setSrcTableSearchOptions] = useState<{ value: string; label: string }[]>([]);
  const [srcTableSearchLoading, setSrcTableSearchLoading] = useState(false);
  const [_selectedTableCatalog, setSelectedTableCatalog] = useState<DBCatalog | null>(null);
  const [columnOptions, setColumnOptions] = useState<{ value: string; label: string }[]>([]);
  const srcTableSearchTimer = useRef<ReturnType<typeof setTimeout>>();

  const [prechecking, setPrechecking] = useState(false);
  const [precheckResult, setPrecheckResult] = useState<ConflictCheckResult | null>(null);

  // SQL 智能推断入口状态
  const [sqlInferText, setSqlInferText] = useState("");
  const [sqlInferring, setSqlInferring] = useState(false);
  // 推断结果回填：各字段来源徽标 + 自动生成的口径定义预览
  const [inferred, setInferred] = useState<Record<string, SuggestionField>>({});
  const [inferredDefinition, setInferredDefinition] = useState<{ json: Record<string, unknown> | null; mode: string | null }>({ json: null, mode: null });

  useEffect(() => {
    setDomainLoading(true);
    listDomainTree("active")
      .then(setDomainTree)
      .catch(() => message.error("加载域树失败"))
      .finally(() => setDomainLoading(false));
  }, []);

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

  // 口径定义区：关联数据表搜索
  async function searchTables(q: string) {
    if (!q.trim()) return;
    setTableSearching(true);
    try {
      const res = await listCatalogs({ entity_type: "TABLE", keyword: q.trim(), page_size: 20 });
      setTableOptions(res.items.map((it) => ({ value: it.entity_name, label: it.entity_name })));
    } catch { setTableOptions([]); }
    finally { setTableSearching(false); }
  }

  // 自动推断区：源表名模糊搜索（防抖 300ms）
  function handleSrcTableSearch(q: string) {
    if (srcTableSearchTimer.current) clearTimeout(srcTableSearchTimer.current);
    if (!q.trim()) { setSrcTableSearchOptions([]); return; }
    srcTableSearchTimer.current = setTimeout(async () => {
      setSrcTableSearchLoading(true);
      try {
        const res = await listCatalogs({ entity_type: "TABLE", keyword: q.trim(), page_size: 20, source_status: "active" });
        setSrcTableSearchOptions(
          res.items.map((it) => ({
            value: it.entity_name,
            label: it.source_name ? `${it.entity_name}（${it.source_name}）` : it.entity_name,
          }))
        );
      } catch { setSrcTableSearchOptions([]); }
      finally { setSrcTableSearchLoading(false); }
    }, 300);
  }

  // 选了源表后：1) 加载该表列信息  2) 触发自动推断
  async function handleSrcTableSelect(entityName: string) {
    if (!entityName) {
      setSelectedTableCatalog(null);
      setColumnOptions([]);
      handleAutoSuggest();
      return;
    }
    try {
      const res = await listCatalogs({ entity_type: "TABLE", keyword: entityName, page_size: 5, source_status: "active" });
      const catalog = res.items.find((it) => it.entity_name === entityName);
      if (catalog) {
        setSelectedTableCatalog(catalog);
        const cols: ColumnInfo[] = (catalog as any).schema_def?.columns || (catalog as any).schema_json?.columns || [];
        if (cols.length > 0) {
          setColumnOptions(
            cols.map((col) => ({
              value: col.name,
              label: col.type ? `${col.name} (${col.type})${col.comment ? " — " + col.comment : ""}` : col.name,
            }))
          );
        } else {
          setColumnOptions([]);
          message.info("该表无列信息（schema 未采集完整）");
        }
      } else {
        setColumnOptions([]);
      }
    } catch {
      setColumnOptions([]);
    }
    form.setFieldValue("source_table", entityName);
    handleAutoSuggest();
  }

  // 选了度量列后触发自动推断
  function handleColumnSelect(value: string) {
    form.setFieldValue("measure_column", value);
    handleAutoSuggest();
  }

  // 选了统计周期后触发自动推断
  function handlePeriodSelect(value: string) {
    form.setFieldValue("period", value);
    handleAutoSuggest();
  }

  // 将后端推断结果回填到表单：属性字段 + 指标编码，并保存来源徽标与口径定义预览
  function applySuggestion(result: AutoSuggestResponse) {
    const fields = result.fields || {};
    const merged: Record<string, unknown> = {};
    for (const [key, sf] of Object.entries(fields)) {
      if (key === "definition_json" || key === "definition_mode") continue;
      if (sf && sf.value !== null && sf.value !== undefined) merged[key] = sf.value;
    }
    if (result.metric_code_suggestion) merged.metric_code = result.metric_code_suggestion;
    if (Object.keys(merged).length > 0) form.setFieldsValue(merged);
    if (result.metric_code_suggestion) setSuggestedCode(result.metric_code_suggestion);
    setInferred(fields);
    const defField = fields.definition_json;
    const modeField = fields.definition_mode;
    setInferredDefinition({
      json: (defField?.value as Record<string, unknown>) ?? null,
      mode: (modeField?.value as string) ?? null,
    });
  }

  async function handleDomainChange(value: string[], _selectedOptions: any) {
    const domainCode = value[value.length - 1];
    setSelectedDomain(domainCode);
    if (!domainCode) return;
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
      applySuggestion(result);
    } catch {
      // 推断失败不阻断
    } finally {
      setSuggesting(false);
    }
  }

  // 粘贴 SQL 智能推断（独立入口：仅用于推断并回填属性，与最终「口径定义」相互独立）
  async function handleSqlInfer() {
    if (!selectedDomain || !sqlInferText.trim()) return;
    setSqlInferring(true);
    try {
      const result = await autoSuggestMetric({
        domain_code: selectedDomain,
        sql: sqlInferText.trim(),
      });
      applySuggestion(result);
      message.success("已从 SQL 推断并回填字段");
    } catch {
      message.error("SQL 推断失败，请检查语法或稍后重试");
    } finally {
      setSqlInferring(false);
    }
  }

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
      applySuggestion(result);
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

  async function handlePrecheck() {
    const values = form.getFieldsValue();
    if (!selectedDomain) { message.warning("请先选择业务域"); return; }
    setPrechecking(true);
    setPrecheckResult(null);
    try {
      const result = await checkConflict({
        candidate: {
          metric_code: values.metric_code ? String(values.metric_code) : suggestedCode || "",
          domain: selectedDomain,
          definition: mode === "sql" ? sqlText.trim() : String(values.definition || ""),
          source_tables: sourceTables,
          has_pii: Boolean(values.pii_flag),
        },
      });
      setPrecheckResult(result);
      if (result.detections.length === 0) {
        message.success("未检测到口径冲突，可安全创建");
      } else if (result.detections.some((d) => d.block_publish)) {
        message.warning(`检测到 ${result.detections.length} 项冲突（含阻断级），建议先处理再创建`);
      } else {
        message.warning(`检测到 ${result.detections.length} 项潜在冲突`);
      }
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.code}）` : "冲突预检失败");
    } finally {
      setPrechecking(false);
    }
  }

  async function handleSubmit(values: Record<string, unknown>) {
    setLoading(true);
    const definitionJson = buildDefinitionJson(values);
    if (!definitionJson) { setLoading(false); return; }
    const req: MetricCreateRequest = {
      metric_code: values.metric_code ? String(values.metric_code) : undefined,
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

  const dictSelect = (dictType: string, _field: string, placeholder: string) => (
    <Select
      showSearch
      placeholder={placeholder}
      options={dictOptions[dictType] || []}
      optionFilterProp="label"
      disabled={dictLoading}
    />
  );

  // 推断来源徽标：展示某字段是否被自动推断、来自何处、置信度
  const fieldBadge = (name: string) => {
    const sf = inferred[name];
    return sf ? <InferBadge field={sf} /> : null;
  };

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

            {/* Step 1.5: 粘贴 SQL 智能推断（独立入口） */}
            <Card type="inner" title="①½ 粘贴 SQL 智能推断" size="small" extra={sqlInferring && <Spin size="small" />}>
              <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
                粘贴一段指标定义 SQL（含 SELECT + 聚合 + GROUP BY + 时间过滤），系统用 sqlglot 解析并自动推断类型、名称、粒度、单位、聚合、时间语义、新鲜度、数仓层、可加性、服务模式与分级，并生成口径定义。
                该 SQL 仅用于推断，与下方「口径定义」相互独立，最终口径可另行编写。
              </Paragraph>
              <Form.Item label="指标 SQL">
                <TextArea
                  rows={4}
                  value={sqlInferText}
                  onChange={(e) => setSqlInferText(e.target.value)}
                  placeholder={"SELECT SUM(amount) AS gmv\nFROM dwd.sales_detail\nGROUP BY dt, shop_id"}
                  className="mono"
                />
              </Form.Item>
              <Button
                type="dashed"
                block
                onClick={handleSqlInfer}
                disabled={!selectedDomain || !sqlInferText.trim()}
              >
                智能推断并回填字段
              </Button>
            </Card>

            {/* Step 2: 自动推断 */}
            <Card type="inner" title="② 自动推断" size="small" extra={suggesting && <Spin size="small" />}>
              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="source_table" label="源表名">
                    <Select
                      showSearch
                      allowClear
                      placeholder="搜索并选择源表（如 sales_detail）"
                      onSearch={handleSrcTableSearch}
                      onChange={handleSrcTableSelect}
                      loading={srcTableSearchLoading}
                      notFoundContent={srcTableSearchLoading ? <Spin size="small" /> : "无匹配表"}
                      options={srcTableSearchOptions}
                      filterOption={false}
                    />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="measure_column" label="度量列">
                    <Select
                      showSearch
                      allowClear
                      placeholder={columnOptions.length > 0 ? "选择度量列" : "请先选择源表"}
                      onChange={handleColumnSelect}
                      options={columnOptions}
                      disabled={columnOptions.length === 0}
                      filterOption={(input, option) =>
                        (option?.label as string)?.toLowerCase().includes(input.toLowerCase())
                      }
                    />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="period" label="统计周期">
                    <Select
                      allowClear
                      placeholder="选择统计周期"
                      onChange={handlePeriodSelect}
                      options={PERIOD_OPTIONS}
                    />
                  </Form.Item>
                </Col>
              </Row>
            </Card>

            {/* Step 3: 确认/覆盖 */}
            <Card type="inner" title="③ 确认/覆盖字段" size="small">
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="metric_code" label="指标编码" extra={<span>{suggestedCode ? <Tag color="blue" style={{ marginTop: 4 }}>系统建议: {suggestedCode}</Tag> : <span className="mono" style={{ color: "#0E7C86" }}>留空则由系统自动生成</span>}</span>}>
                    <Input placeholder="4段式: 域_业务对象_度量_周期（留空自动生成）" />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="name" label={<span>名称{fieldBadge("name")}</span>} rules={[{ required: true }]}>
                    <Input placeholder="指标显示名称" />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="type" label={<span>类型{fieldBadge("type")}</span>}>
                    {dictSelect("metric_type", "type", "选择类型")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="granularity" label={<span>粒度{fieldBadge("granularity")}</span>}>
                    {dictSelect("granularity", "granularity", "选择粒度")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="unit" label={<span>单位{fieldBadge("unit")}</span>} rules={[{ required: true, message: "请选择单位" }]}>
                    {dictSelect("unit", "unit", "选择单位")}
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="aggregation" label={<span>聚合{fieldBadge("aggregation")}</span>}>
                    {dictSelect("aggregation", "aggregation", "选择聚合方式")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="time_semantics" label={<span>时间语义{fieldBadge("time_semantics")}</span>}>
                    {dictSelect("time_semantics", "time_semantics", "选择时间语义")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="freshness" label={<span>新鲜度{fieldBadge("freshness")}</span>}>
                    {dictSelect("freshness", "freshness", "选择新鲜度")}
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="dw_layer" label={<span>数仓层{fieldBadge("dw_layer")}</span>}>
                    {dictSelect("dw_layer", "dw_layer", "选择数仓层")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="additivity" label={<span>可加性{fieldBadge("additivity")}</span>}>
                    {dictSelect("additivity", "additivity", "选择可加性")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="serving_mode" label={<span>服务模式{fieldBadge("serving_mode")}</span>}>
                    {dictSelect("serving_mode", "serving_mode", "选择服务模式")}
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="metric_tier" label={<span>分级{fieldBadge("metric_tier")}</span>}>
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
              {inferredDefinition.json && (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message={
                    <span>
                      推断口径定义（定义模式：
                      <Tag color={inferredDefinition.mode === "sql" ? "geekblue" : "cyan"}>
                        {inferredDefinition.mode === "sql" ? "SQL 模式" : "表达式模式"}
                      </Tag>
                      ，可据此调整下方口径）
                    </span>
                  }
                  description={
                    <pre className="mono" style={{ margin: 0, maxHeight: 200, overflow: "auto", fontSize: 12 }}>
                      {JSON.stringify(inferredDefinition.json, null, 2)}
                    </pre>
                  }
                />
              )}
              <Form.Item label="关联数据表">
                <Select
                  mode="multiple" allowClear showSearch
                  placeholder="搜索并选择口径引用的数据表"
                  value={sourceTables}
                  onChange={(v: string[]) => setSourceTables(v)}
                  onSearch={searchTables}
                  loading={tableSearching}
                  notFoundContent={tableSearching ? <Spin size="small" /> : null}
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
              <Space>
                <Button type="primary" htmlType="submit" loading={loading} size="large">创建草稿</Button>
                <Button onClick={handlePrecheck} loading={prechecking} size="large">冲突预检</Button>
              </Space>
            </Form.Item>

            {precheckResult && precheckResult.detections.length > 0 && (
              <Alert
                type={precheckResult.detections.some((d) => d.block_publish) ? "error" : "warning"}
                showIcon
                message="冲突预检结果"
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {precheckResult.detections.map((d, idx) => (
                      <li key={idx}>
                        {enumLabel(CONFLICT_TYPE_LABEL, d.conflict_type)}：与
                        <Tag style={{ margin: "0 4px" }}>{d.existing_code}</Tag>
                        冲突（严重度 {enumLabel(CONFLICT_SEVERITY_LABEL, d.severity)}
                        {d.block_publish ? " · 阻断发布" : ""}）
                        {d.reason ? ` — ${d.reason}` : ""}
                      </li>
                    ))}
                  </ul>
                }
              />
            )}
          </Space>
        </Form>
      </Card>
    </div>
  );
}
