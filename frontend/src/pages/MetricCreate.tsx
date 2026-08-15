import { useEffect, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { BarsOutlined, ArrowLeftOutlined } from "@ant-design/icons";
import {
  Alert, Button, Card, Checkbox, Cascader, Col, Form, Input, Modal, Row, Segmented, Select, Space, Spin, Switch, Table, Tooltip, Typography, App as AntApp, Tag,
} from "antd";
import {
  createMetric, listCatalogs, autoSuggestMetric, listDomainTree, listDictItems, checkConflict, batchRegisterMetrics, UnisenseApiError,
} from "../api";
import type { MetricCreateRequest, MetricBatchRegisterRequest, MetricBatchRegisterResult, MetricType, MetricTier, SubjectDomainTreeNode, ConflictCheckResult, DBCatalog, SuggestionField, AutoSuggestResponse } from "../types";
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

// 批量注册弹窗：域树扁平化（父子域均可选，域编码作为请求体 domain）
function flattenDomainOptions(nodes: SubjectDomainTreeNode[]): Array<{ value: string; label: string }> {
  return nodes.flatMap((n) => [
    { value: n.code, label: `${n.name} (${n.code})` },
    ...flattenDomainOptions(n.children),
  ]);
}

// 批量注册结果明细列：成功=DRAFT（草稿），失败=VALIDATION_ERROR（含原因）
const BATCH_RESULT_COLUMNS = [
  { title: "指标编码", dataIndex: "metric_code", key: "metric_code" },
  {
    title: "状态",
    dataIndex: "status",
    key: "status",
    render: (s: string) =>
      s === "DRAFT" ? <Tag color="success">已创建草稿</Tag> : <Tag color="error">校验失败</Tag>,
  },
  {
    title: "失败原因",
    dataIndex: "validation_errors",
    key: "validation_errors",
    render: (v: string | null) => v || <span className="muted">—</span>,
  },
];

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

  // 统一返回上一入口：优先回退浏览器历史（总览快捷入口等），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

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
  // 推断结果友好摘要（SQL 智能推断成功后展示，让用户明确知道推断出了什么）
  const [inferSummary, setInferSummary] = useState<AutoSuggestResponse | null>(null);
  const [inferSummaryOpen, setInferSummaryOpen] = useState(false);

  // 批量注册指标弹窗状态（POST /metric-definitions/batch-register）
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchForm] = Form.useForm();
  const [batchSubmitting, setBatchSubmitting] = useState(false);
  const [batchResult, setBatchResult] = useState<MetricBatchRegisterResult | null>(null);

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

  // 自动推断区：源表名模糊搜索（防抖 300ms）；空关键词时加载平台已采集的默认表列表
  function handleSrcTableSearch(q: string) {
    if (srcTableSearchTimer.current) clearTimeout(srcTableSearchTimer.current);
    srcTableSearchTimer.current = setTimeout(async () => {
      setSrcTableSearchLoading(true);
      try {
        const res = await listCatalogs({
          entity_type: "TABLE",
          keyword: q.trim() || undefined,
          page_size: 20,
          source_status: "active",
        });
        setSrcTableSearchOptions(
          res.items.map((it) => ({
            value: it.entity_name,
            label: it.source_name ? `${it.entity_name}（${it.source_name}）` : it.entity_name,
          }))
        );
      } catch { setSrcTableSearchOptions([]); }
      finally { setSrcTableSearchLoading(false); }
    }, q.trim() ? 300 : 0);
  }

  // 下拉展开/聚焦时若无选项，先加载平台已采集的表供选择（惰性设计：不让用户凭空输入）
  function handleSrcTableDropdown(open: boolean) {
    if (open && srcTableSearchOptions.length === 0 && !srcTableSearchLoading) {
      handleSrcTableSearch("");
    }
  }

  // 选源表后加载该表列信息（供度量列选择）
  async function loadColumnsForTable(entityName: string) {
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
          return;
        }
        setColumnOptions([]);
        message.info("该表无列信息（schema 未采集完整）");
        return;
      }
      setColumnOptions([]);
    } catch {
      setColumnOptions([]);
    }
  }

  // 选了源表后：1) 加载该表列信息  2) 触发自动推断（含依赖表）
  async function handleSrcTableSelect(entityName: string) {
    if (!entityName) {
      setSelectedTableCatalog(null);
      setColumnOptions([]);
      handleAutoSuggest();
      return;
    }
    await loadColumnsForTable(entityName);
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

    // 源表被推断出来后联动加载该表列（供度量列选择，避免用户手动输入）
    const srcTable = merged.source_table;
    if (typeof srcTable === "string" && srcTable) {
      void loadColumnsForTable(srcTable);
      // 保证 Select 能显示回填的源表（options 无该表时下拉会显示空白）
      setSrcTableSearchOptions((prev) =>
        prev.some((o) => o.value === srcTable) ? prev : [{ value: srcTable, label: srcTable }, ...prev]
      );
    }
    // 依赖表（血缘推断的关联表）自动填充到「口径定义 → 关联数据表」，并合并保留用户已选
    const related = (result as unknown as { related_tables?: string[] }).related_tables;
    if (Array.isArray(related) && related.length > 0) {
      setSourceTables((prev) => Array.from(new Set([...(prev || []), ...related])));
    }
    // 推断口径定义回填到实际表单（expression → definition JSON；sql → sqlText）
    const defJson = (defField?.value as Record<string, unknown>) ?? null;
    const defMode = (modeField?.value as string) ?? null;
    if (defJson) {
      if (defMode === "sql") {
        setMode("sql");
        setSqlText(String(defJson.sql ?? JSON.stringify(defJson, null, 2)));
      } else {
        setMode("expression");
        form.setFieldValue("definition", JSON.stringify(defJson, null, 2));
      }
    }
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
    if (!selectedDomain) { message.warning("请先选择业务域"); return; }
    if (!sqlInferText.trim()) { message.warning("请先粘贴指标 SQL"); return; }
    setSqlInferring(true);
    try {
      const result = await autoSuggestMetric({
        domain_code: selectedDomain,
        sql: sqlInferText.trim(),
      });
      applySuggestion(result);
      setInferSummary(result);
      setInferSummaryOpen(true);
      const srcTable = (result.fields?.source_table?.value as string) || null;
      const measure = (result.fields?.measure_column?.value as string) || null;
      if (srcTable || measure) {
        message.success(
          srcTable && measure
            ? `已从 SQL 识别：源表 ${srcTable} · 度量列 ${measure}`
            : srcTable
              ? `已从 SQL 识别源表：${srcTable}`
              : `已从 SQL 识别度量列：${measure}`
        );
      } else {
        message.success("已完成 SQL 解析，字段已按规则推断回填");
      }
    } catch (err) {
      const detail = err instanceof UnisenseApiError ? err.message : "";
      message.error(
        detail ? `SQL 推断失败：${detail}` : "SQL 推断失败，请检查 SQL 语法（须含 SELECT + 聚合 + FROM）或稍后重试"
      );
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

  // 打开批量注册弹窗：清空上次结果，主表单已选域时预填，减少重复选择
  function openBatchModal() {
    batchForm.resetFields();
    setBatchResult(null);
    if (selectedDomain) batchForm.setFieldValue("domain", selectedDomain);
    setBatchOpen(true);
  }

  // 提交批量注册：度量列按行拆分，维度映射为可选 JSON，成功/失败明细展示在结果区
  async function handleBatchSubmit(values: Record<string, unknown>) {
    const measureColumns = String(values.measure_columns || "")
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    if (measureColumns.length === 0) {
      message.warning("请至少填写一个度量列");
      return;
    }
    let dimensionMapping: Record<string, string> | undefined;
    const mappingText = String(values.dimension_mapping || "").trim();
    if (mappingText) {
      try {
        const parsed: unknown = JSON.parse(mappingText);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          dimensionMapping = parsed as Record<string, string>;
        } else {
          message.warning("维度映射需为 JSON 对象，已忽略");
        }
      } catch {
        message.warning("维度映射不是合法 JSON，已忽略");
      }
    }
    const req: MetricBatchRegisterRequest = {
      source_table: String(values.source_table).trim(),
      measure_columns: measureColumns,
      domain: String(values.domain),
      llm_prefill: Boolean(values.llm_prefill),
      dimension_mapping: dimensionMapping,
    };
    setBatchSubmitting(true);
    try {
      const result = await batchRegisterMetrics(req);
      setBatchResult(result);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.code}）` : "批量注册失败");
    } finally {
      setBatchSubmitting(false);
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
      <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 8 }}>
        返回
      </Button>
      <Space style={{ width: "100%", justifyContent: "space-between", marginBottom: 16 }} align="center">
        <Title level={3} style={{ margin: 0 }}>注册指标（草稿）</Title>
        <Button type="dashed" icon={<BarsOutlined />} onClick={openBatchModal}>
          批量注册指标
        </Button>
      </Space>
      <Spin
        spinning={sqlInferring}
        size="large"
        tip="正在智能推断指标定义，请稍候…"
        style={{ minHeight: 320 }}
      >
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
                      placeholder="选择或搜索源表（已接入的表可直接选，如 dwd.sales_detail）"
                      onSearch={handleSrcTableSearch}
                      onChange={handleSrcTableSelect}
                      onOpenChange={handleSrcTableDropdown}
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
      </Spin>

      {/* 推断结果摘要：SQL 智能推断成功后展示，让用户明确知道识别出了什么（惰性设计：给反馈而非只默默回填） */}
      <Modal
        title="SQL 智能推断结果"
        open={inferSummaryOpen}
        onCancel={() => setInferSummaryOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setInferSummaryOpen(false)}>知道了</Button>
          </Space>
        }
        width={640}
      >
        {inferSummary && (
          <div>
            <Alert
              type="success"
              showIcon
              style={{ marginBottom: 12 }}
              message="已根据 SQL 自动回填以下字段，可到②③④步确认或覆盖"
            />
            <Row gutter={[8, 4]}>
              {[
                ["source_table", "源表"],
                ["measure_column", "度量列"],
                ["name", "名称"],
                ["type", "类型"],
                ["granularity", "粒度"],
                ["unit", "单位"],
                ["aggregation", "聚合"],
                ["time_semantics", "时间语义"],
                ["freshness", "新鲜度"],
                ["dw_layer", "数仓层"],
                ["additivity", "可加性"],
                ["serving_mode", "服务模式"],
                ["metric_tier", "分级"],
              ].map(([key, label]) => {
                const sf = inferSummary.fields?.[key];
                if (!sf || sf.value === null || sf.value === undefined) return null;
                return (
                  <Col span={12} key={key}>
                    <div style={{ display: "flex", alignItems: "baseline", gap: 6, padding: "2px 0" }}>
                      <Typography.Text type="secondary" style={{ flex: "0 0 56px", fontSize: 12 }}>
                        {label}
                      </Typography.Text>
                      <Typography.Text strong style={{ fontSize: 13 }} className="mono">
                        {typeof sf.value === "object" ? JSON.stringify(sf.value) : String(sf.value)}
                      </Typography.Text>
                      <InferBadge field={sf} />
                    </div>
                  </Col>
                );
              })}
            </Row>
            {(inferSummary as unknown as { related_tables?: string[] }).related_tables?.length ? (
              <div style={{ marginTop: 12 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>关联数据表（来自血缘推断）：</Typography.Text>
                <div style={{ marginTop: 6 }}>
                  {(inferSummary as unknown as { related_tables: string[] }).related_tables.map((t) => (
                    <Tag key={t} className="mono" style={{ marginBottom: 4 }}>{t}</Tag>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        )}
      </Modal>

      {/* 批量注册指标弹窗：源宽表 + 多个度量列 → 批量创建 DRAFT（对齐 FR-030） */}
      <Modal
        title="批量注册指标"
        open={batchOpen}
        onCancel={() => setBatchOpen(false)}
        footer={null}
        width={720}
        destroyOnClose
      >
        {batchResult ? (
          <div>
            {(() => {
              const failed = batchResult.candidates.filter((c) => c.status === "VALIDATION_ERROR").length;
              const succeeded = batchResult.candidates.length - failed;
              return (
                <Alert
                  type={failed > 0 ? "warning" : "success"}
                  showIcon
                  message={`批量注册完成：成功 ${succeeded} / 失败 ${failed}`}
                  description={`批次号：${batchResult.batch_id}（成功的指标已创建为 DRAFT 草稿）`}
                />
              );
            })()}
            <Table
              size="small"
              rowKey="metric_code"
              dataSource={batchResult.candidates}
              columns={BATCH_RESULT_COLUMNS}
              pagination={false}
              style={{ marginTop: 16 }}
              locale={{ emptyText: "无注册结果" }}
            />
            <Space style={{ marginTop: 16 }}>
              <Button onClick={() => setBatchResult(null)}>继续注册</Button>
              <Button type="primary" onClick={() => setBatchOpen(false)}>
                关闭
              </Button>
            </Space>
          </div>
        ) : (
          <Form form={batchForm} layout="vertical" onFinish={handleBatchSubmit}>
            <Form.Item
              name="domain"
              label="业务域"
              rules={[{ required: true, message: "请选择业务域" }]}
            >
              <Select
                showSearch
                placeholder="选择所属业务域（须为 active 域）"
                optionFilterProp="label"
                options={flattenDomainOptions(domainTree)}
                loading={domainLoading}
              />
            </Form.Item>
            <Form.Item
              name="source_table"
              label="源表名"
              rules={[{ required: true, message: "请输入源宽表名" }]}
            >
              <Select
                showSearch
                allowClear
                placeholder="选择或搜索源宽表（已接入的表可直接选，如 dwd.sales_detail）"
                onSearch={handleSrcTableSearch}
                onOpenChange={handleSrcTableDropdown}
                loading={srcTableSearchLoading}
                notFoundContent={srcTableSearchLoading ? <Spin size="small" /> : "无匹配表"}
                options={srcTableSearchOptions}
                filterOption={false}
              />
            </Form.Item>
            <Form.Item
              name="measure_columns"
              label="度量列"
              rules={[{ required: true, message: "请至少填写一个度量列" }]}
              extra="每行一个度量列，系统将逐个创建指标草稿（DRAFT）"
            >
              <TextArea
                rows={5}
                placeholder={"gmv\norder_cnt\nrefund_amt"}
                className="mono"
              />
            </Form.Item>
            <Form.Item
              name="dimension_mapping"
              label="维度列映射（可选）"
              extra='JSON 对象，如 {"date": "dt", "shop": "shop_id"}；解析失败将被忽略'
            >
              <TextArea rows={2} placeholder='{"date": "dt"}' className="mono" />
            </Form.Item>
            <Form.Item name="llm_prefill" label="LLM 预填" valuePropName="checked" initialValue={true}>
              <Switch checkedChildren="开启" unCheckedChildren="手动" />
            </Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={batchSubmitting}>
                提交批量注册
              </Button>
              <Button onClick={() => setBatchOpen(false)}>取消</Button>
            </Space>
          </Form>
        )}
      </Modal>
    </div>
  );
}
