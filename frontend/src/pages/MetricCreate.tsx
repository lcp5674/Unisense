import { useEffect, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { BarsOutlined, ArrowLeftOutlined, PlusOutlined, MinusCircleOutlined } from "@ant-design/icons";
import {
  Alert, AutoComplete, Button, Card, Checkbox, Cascader, Col, Form, Input, Modal, Row, Segmented, Select, Space, Spin, Switch, Table, Tooltip, Typography, App as AntApp, Tag,
} from "antd";
import {
  createMetric, listCatalogs, autoSuggestMetric, listDomainTree, listDictItems, checkConflict, batchRegisterMetrics, listDimensions, listMetrics, getDomainDefaults, UnisenseApiError,
} from "../api";
import type { MetricCreateRequest, MetricBatchRegisterRequest, MetricBatchRegisterResult, MetricType, MetricTier, SubjectDomainTreeNode, ConflictCheckResult, DBCatalog, SuggestionField, AutoSuggestResponse, Dimension } from "../types";
import { CONFLICT_TYPE_LABEL, CONFLICT_SEVERITY_LABEL, enumLabel } from "../utils/enums";
import { usePermission } from "../hooks/usePermission";

const { Title, Paragraph } = Typography;
const { TextArea } = Input;

function treeToCascaderOptions(nodes: SubjectDomainTreeNode[]): any[] {
  return nodes.map((n) => ({
    value: n.code,
    label: `${n.name} (${n.code})`,
    children: n.children.length > 0 ? treeToCascaderOptions(n.children) : undefined,
  }));
}

// 批量注册弹窗：域树扁平化（仅 active 域可选，域编码作为请求体 domain）。
// 与主表单 Cascader 语义对齐——停用/非 active 域不提供选择，避免选到后端拒绝的域。
function flattenDomainOptions(nodes: SubjectDomainTreeNode[]): Array<{ value: string; label: string }> {
  return nodes.flatMap((n) => [
    ...(n.status === "active"
      ? [{ value: n.code, label: `${n.name} (${n.code})` }]
      : []),
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

// 统计周期选项：与粒度字典（granularity dict）对齐，避免同一"周期"概念两套数据源漂移。
// 字典种子含 minute/hour/day/week/month/quarter/year/realtime（见 seed_domains_dicts.py）。
const PERIOD_OPTIONS = [
  { value: "realtime", label: "实时 (realtime)" },
  { value: "minute", label: "分钟 (minute)" },
  { value: "hour", label: "小时 (hour)" },
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
  // 批量注册按钮级权限：批量注册权限点（metric:import），后端接口强制为最终边界
  const { can } = usePermission();
  const canBatchRegister = can("metric:import");
  const canCreate = can("metric:create");
  const canInferDesc = can("metric:infer-description");
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();
  // 指标类型联动：atomic（原子）基于源表直接聚合，不应有上游依赖指标；derived/composite 才有
  const metricType = Form.useWatch("type", form);
  const isAtomic = metricType === "atomic";

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
  // 平台维度清单（维度映射下拉）：来自维度管理模块，支持搜索 + 手动输入兜底
  const [dimensionOptions, setDimensionOptions] = useState<Array<{ value: string; label: string }>>([]);
  // 主表单口径定义区选中的维度（合入 definition_json.dimensions，避免手写 JSON 编码错误）
  const [selectedDims, setSelectedDims] = useState<string[]>([]);
  // 依赖指标（dependencies）：派生/复合指标的上游，从已发布指标搜索选择
  const [depOptions, setDepOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [depSearching, setDepSearching] = useState(false);
  const depSearchTimer = useRef<ReturnType<typeof setTimeout>>();
  const [selectedDeps, setSelectedDeps] = useState<string[]>([]);

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
  // 域默认值预填字段集合（TD §3.8）：选域触发 autoSuggest 时这些字段不被推断覆盖
  // （管理员显式配置的域默认值优先于自动推断），SQL 推断等用户主动操作可正常覆盖。
  const domainPrefillRef = useRef<Set<string>>(new Set());

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
  // 批量弹窗：当前源表对应的列选项（选源表后加载，供度量列点选）
  const [batchColumnOptions, setBatchColumnOptions] = useState<{ value: string; label: string }[]>([]);
  const [batchColLoading, setBatchColLoading] = useState(false);

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

  // 加载平台活跃维度（维度映射下拉的数据源，来自维度管理模块）
  useEffect(() => {
    listDimensions({ status: "active", page_size: 200 })
      .then((res) =>
        setDimensionOptions(
          (res.items ?? []).map((d: Dimension) => ({
            value: d.dim_code,
            label: `${d.name} (${d.dim_code})`,
          })),
        ),
      )
      .catch(() => setDimensionOptions([]));
    // 初始加载已发布指标作为依赖选项（派生/复合指标上游）
    setDepSearching(true);
    listMetrics({ status: "PUBLISHED", page_size: 50 })
      .then((res) => setDepOptions((res.items ?? []).map((m) => ({ value: m.metric_code, label: `${m.name} (${m.metric_code})` }))))
      .catch(() => setDepOptions([]))
      .finally(() => setDepSearching(false));
  }, []);

  // 口径定义区：关联数据表搜索（与源表名一致的惰性交互——空关键词加载平台已采集的表，可关键词搜索）
  async function searchTables(q: string) {
    setTableSearching(true);
    try {
      const res = await listCatalogs({
        entity_type: "TABLE",
        keyword: q.trim() || undefined,
        page_size: 20,
        source_status: "active",
      });
      setTableOptions(
        res.items.map((it) => ({
          value: it.entity_name,
          label: it.source_name ? `${it.entity_name}（${it.source_name}）` : it.entity_name,
        }))
      );
    } catch { setTableOptions([]); }
    finally { setTableSearching(false); }
  }

  // 关联数据表下拉展开/聚焦时若无选项，先加载平台已采集的表供选择（惰性设计：不让用户凭空输入）
  function handleTableDropdown(open: boolean) {
    if (open && tableOptions.length === 0 && !tableSearching) {
      void searchTables("");
    }
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

  // 取某表的列选项（name + type + comment），供主表单与批量注册弹窗复用
  async function fetchColumnsForTable(entityName: string): Promise<{ value: string; label: string }[]> {
    try {
      const res = await listCatalogs({ entity_type: "TABLE", keyword: entityName, page_size: 5, source_status: "active" });
      const catalog = res.items.find((it) => it.entity_name === entityName);
      if (!catalog) return [];
      const cols: ColumnInfo[] = (catalog as any).schema_def?.columns || (catalog as any).schema_json?.columns || [];
      return cols.map((col) => ({
        value: col.name,
        label: col.type ? `${col.name} (${col.type})${col.comment ? " — " + col.comment : ""}` : col.name,
      }));
    } catch {
      return [];
    }
  }

  // 选源表后加载该表列信息（供主表单度量列选择）
  async function loadColumnsForTable(entityName: string) {
    const opts = await fetchColumnsForTable(entityName);
    setColumnOptions(opts);
    if (opts.length === 0) message.info("该表无列信息（schema 未采集完整）");
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
  // respectPrefill=true（选域触发）：跳过已被域默认值预填的字段，域配置优先于自动推断
  function applySuggestion(result: AutoSuggestResponse, respectPrefill = false) {
    const fields = result.fields || {};
    const merged: Record<string, unknown> = {};
    const protectedFields = respectPrefill ? domainPrefillRef.current : null;
    for (const [key, sf] of Object.entries(fields)) {
      if (key === "definition_json" || key === "definition_mode") continue;
      if (protectedFields && protectedFields.has(key)) continue;
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
    // 切换域先清空上一域的推断建议（编码/字段徽标/口径定义预览），
    // 避免推断失败时残留旧域建议导致提交用错域的自动生成编码
    setSuggestedCode(null);
    setInferred({});
    setInferredDefinition({ json: null, mode: null });
    // 域默认值预填（TD §3.8 主题域默认值）：选域后将该域配置的默认粒度/单位/聚合等
    // 预填到表单（用户可覆盖），打通「主题域配置 → 注册指标预填」的跨服务闭环。
    // 用 isFieldTouched 区分「用户手动输入」与「initialValues 全局默认」：
    // 域默认值覆盖全局 initialValues（域配置优先），但尊重用户已手动修改的字段。
    try {
      const defaults = await getDomainDefaults(domainCode);
      const prefill: Record<string, unknown> = {};
      const DEFAULT_FIELDS = [
        "granularity", "unit", "aggregation", "time_semantics", "freshness",
        "dw_layer", "metric_tier", "serving_mode", "additivity", "type",
      ] as const;
      for (const f of DEFAULT_FIELDS) {
        const v = defaults[f];
        if (v !== undefined && v !== null && v !== "" && !form.isFieldTouched(f)) {
          prefill[f] = v;
        }
      }
      if (Object.keys(prefill).length) {
        domainPrefillRef.current = new Set(Object.keys(prefill));
        form.setFieldsValue(prefill);
      } else {
        domainPrefillRef.current = new Set();
      }
    } catch {
      // 域默认值拉取失败不阻断选域流程（推断仍进行）
      domainPrefillRef.current = new Set();
    }
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
      applySuggestion(result, true);
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

  // 依赖指标远程搜索（防抖）：listMetrics 按关键词搜已发布指标
  function handleDepSearch(q: string) {
    if (depSearchTimer.current) clearTimeout(depSearchTimer.current);
    depSearchTimer.current = setTimeout(() => {
      setDepSearching(true);
      listMetrics({ status: "PUBLISHED", keyword: q.trim() || undefined, page_size: 50 })
        .then((res) => setDepOptions((res.items ?? []).map((m) => ({ value: m.metric_code, label: `${m.name} (${m.metric_code})` }))))
        .catch(() => {})
        .finally(() => setDepSearching(false));
    }, 300);
  }

  function buildDefinitionJson(values: Record<string, unknown>): Record<string, unknown> | null {
    const tables = sourceTables.length ? { source_tables: sourceTables } : {};
    // ②自动推断区选定的源表/度量列 → 口径定义（血缘注册读 definition.source_table 建「指标↔落地表」边）
    const src = String(values.source_table || "").trim();
    const srcField = src ? { source_table: src } : {};
    const measure = String(values.measure_column || "").trim();
    const measureField = measure ? { measure_column: measure } : {};
    // 主表单选中的维度 → definition_json.dimensions（血缘注册指标↔维度边）
    const dimsField = selectedDims.length ? { dimensions: selectedDims } : {};
    // 依赖指标 → definition_json.dependencies（血缘注册原子→衍生指标边）
    // 原子指标基于源表直接聚合，不应携带上游依赖（后端血缘对 atomic 跳过）
    const depsField =
      !isAtomic && selectedDeps.length ? { dependencies: selectedDeps } : {};
    if (mode === "sql") {
      const sql = sqlText.trim();
      if (!sql) { message.error("口径 SQL 模式请输入 SQL 语句"); return null; }
      return { sql, ...tables, ...srcField, ...measureField, ...dimsField, ...depsField };
    }
    let def: Record<string, unknown>;
    try { def = values.definition ? JSON.parse(String(values.definition)) : {}; }
    catch { message.error("口径定义需为合法 JSON"); return null; }
    return { ...def, ...tables, ...srcField, ...measureField, ...dimsField, ...depsField };
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
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "冲突预检失败");
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
      currency: values.currency ? String(values.currency) : undefined,
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
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    } finally {
      setLoading(false);
    }
  }

  // 打开批量注册弹窗：清空上次结果，主表单已选域时预填，减少重复选择
  function openBatchModal() {
    batchForm.resetFields();
    setBatchResult(null);
    setBatchColumnOptions([]);
    if (selectedDomain) batchForm.setFieldValue("domain", selectedDomain);
    setBatchOpen(true);
  }

  // 批量弹窗：选源表后加载该表列（供度量列点选，不要求用户手输列名）
  async function handleBatchSrcTableChange(entityName: string) {
    if (!entityName) {
      setBatchColumnOptions([]);
      return;
    }
    setBatchColLoading(true);
    try {
      const opts = await fetchColumnsForTable(entityName);
      setBatchColumnOptions(opts);
      if (opts.length === 0) message.info("该表无列信息（schema 未采集完整），可手动输入列名");
    } finally {
      setBatchColLoading(false);
    }
  }

  // 提交批量注册：度量列按行拆分，维度映射为可选 JSON，成功/失败明细展示在结果区
  async function handleBatchSubmit(values: Record<string, unknown>) {
    // tags Select 返回数组；兼容历史手输换行文本
    const rawMeasure = values.measure_columns;
    const measureColumns = (Array.isArray(rawMeasure) ? rawMeasure : String(rawMeasure || "").split("\n"))
      .map((s) => String(s).trim())
      .filter(Boolean);
    if (measureColumns.length === 0) {
      message.warning("请至少填写一个度量列");
      return;
    }
    let dimensionMapping: Record<string, string> | undefined;
    const mappingRows = (Array.isArray(values.dimension_mapping_list) ? values.dimension_mapping_list : []) as Array<{
      dim_name?: string;
      col_name?: string;
    }>;
    const validRows = mappingRows.filter(
      (r) => r && String(r.dim_name || "").trim() && String(r.col_name || "").trim(),
    );
    if (validRows.length > 0) {
      dimensionMapping = Object.fromEntries(
        validRows.map((r) => [String(r.dim_name).trim(), String(r.col_name).trim()]),
      );
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
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量注册失败");
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
        <Tooltip title={canBatchRegister ? "批量注册（宽表多度量列 → 批量 DRAFT）" : "仅平台/域管理员与指标 Owner 可批量注册"}>
          <Button type="dashed" icon={<BarsOutlined />} onClick={openBatchModal} disabled={!canBatchRegister}>
            批量注册指标
          </Button>
        </Tooltip>
      </Space>
      <Spin
        spinning={sqlInferring}
        size="large"
        tip="正在智能推断指标定义，请稍候…"
        style={{ minHeight: 320 }}
      >
        <Card>
        <Form form={form} layout="vertical" scrollToFirstError onFinish={handleSubmit} initialValues={{
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
            <Card type="inner" title="①⑤ 粘贴 SQL 智能推断" size="small" extra={sqlInferring && <Spin size="small" />}>
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
              {canInferDesc && (
                <Button
                  type="dashed"
                  block
                  onClick={handleSqlInfer}
                  disabled={!selectedDomain || !sqlInferText.trim()}
                >
                  智能推断并回填字段
                </Button>
              )}
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
                  <Form.Item
                    name="metric_code"
                    label="指标编码"
                    extra={
                      suggestedCode ? (
                        <span style={{ marginTop: 4 }}>
                          <Tag color="blue">系统建议: {suggestedCode}</Tag>
                          <Button type="link" size="small" style={{ padding: 0 }} onClick={() => form.setFieldValue("metric_code", suggestedCode)}>
                            一键采纳
                          </Button>
                        </span>
                      ) : (
                        <span className="mono" style={{ color: "#0E7C86" }}>留空则由系统自动生成</span>
                      )
                    }
                  >
                    <Input placeholder={suggestedCode ? `点击右侧「一键采纳」使用 ${suggestedCode}` : "4段式: 域_业务对象_度量_周期（留空自动生成）"} maxLength={64} showCount />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="name" label={<span>名称{fieldBadge("name")}</span>} rules={[{ required: true }, { max: 128, message: "名称最长 128 字符" }]}>
                    <Input placeholder="指标显示名称" maxLength={128} showCount />
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
                  <Form.Item name="granularity" label={<span>粒度{fieldBadge("granularity")}</span>} rules={[{ required: true, message: "请选择粒度" }]}>
                    {dictSelect("granularity", "granularity", "选择粒度")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="unit" label={<span>单位{fieldBadge("unit")}</span>} rules={[{ required: true, message: "请选择单位" }]}>
                    {dictSelect("unit", "unit", "选择单位")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="currency" label="币种（选填）" extra="如 CNY（人民币）/ USD（美元），仅交易类指标需要">
                    <Input placeholder="CNY" maxLength={16} allowClear />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="aggregation" label={<span>聚合{fieldBadge("aggregation")}</span>} rules={[{ required: true, message: "请选择聚合方式" }]}>
                    {dictSelect("aggregation", "aggregation", "选择聚合方式")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="time_semantics" label={<span>时间语义{fieldBadge("time_semantics")}</span>} rules={[{ required: true, message: "请选择时间语义" }]}>
                    {dictSelect("time_semantics", "time_semantics", "选择时间语义")}
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="freshness" label={<span>新鲜度{fieldBadge("freshness")}</span>} rules={[{ required: true, message: "请选择新鲜度" }]}>
                    {dictSelect("freshness", "freshness", "选择新鲜度")}
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="dw_layer" label={<span>数仓层{fieldBadge("dw_layer")}</span>} rules={[{ required: true, message: "请选择数仓层" }]}>
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
                  placeholder="展开浏览已接入表，或输入关键词搜索（支持多选）"
                  value={sourceTables}
                  onChange={(v: string[]) => setSourceTables(v)}
                  onSearch={searchTables}
                  onOpenChange={handleTableDropdown}
                  loading={tableSearching}
                  notFoundContent={tableSearching ? <Spin size="small" /> : "无匹配表"}
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
                <>
                  <Form.Item
                    label="关联维度（可选）"
                    extra="从平台维度清单选择，将写入口径定义 dimensions；血缘图谱据此生成指标↔维度边。"
                  >
                    <Select
                      mode="multiple"
                      placeholder="选择平台维度（可搜索）"
                      style={{ width: "100%" }}
                      value={selectedDims}
                      onChange={setSelectedDims}
                      options={dimensionOptions}
                      allowClear
                    />
                  </Form.Item>
                  <Form.Item
                    label="依赖指标（可选）"
                    extra={
                      isAtomic
                        ? "原子指标基于源表直接聚合，无需依赖上游指标。请先在上方将类型改为「衍生/复合」以配置依赖。"
                        : "选择该指标基于的上游指标（原子→衍生/复合血缘）；可输入关键词搜索已发布指标。"
                    }
                  >
                    <Select
                      mode="multiple"
                      showSearch
                      filterOption={false}
                      onSearch={handleDepSearch}
                      loading={depSearching}
                      placeholder={isAtomic ? "原子指标无需依赖指标" : "搜索并选择依赖指标"}
                      style={{ width: "100%" }}
                      value={isAtomic ? [] : selectedDeps}
                      onChange={isAtomic ? undefined : setSelectedDeps}
                      options={depOptions}
                      disabled={isAtomic}
                      allowClear={!isAtomic}
                    />
                  </Form.Item>
                  <Form.Item
                    name="definition"
                    label="口径定义 (JSON)"
                    extra="结构：expression（聚合表达式）、dependencies（已在上方选择）、source_tables（来源表）、dimensions（已在上方选择）。"
                  >
                    <TextArea rows={5} placeholder='{"expression": "sum(amount)", "dependencies": [], "source_tables": []}' className="mono" />
                  </Form.Item>
                </>
              ) : (
                <Form.Item label="口径 SQL">
                  <TextArea rows={5} value={sqlText} onChange={(e) => setSqlText(e.target.value)} placeholder="SELECT SUM(amount) AS gmv\nFROM catalog.sales.orders" className="mono" />
                  <Paragraph type="secondary" style={{ marginTop: 4, fontSize: 12 }}>后端将用 sqlglot 校验 SQL 语法；不合法将拒绝提交。</Paragraph>
                </Form.Item>
              )}
            </Card>

            <Form.Item>
              <Space>
                {canCreate && <Button type="primary" htmlType="submit" loading={loading} size="large">创建草稿</Button>}
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
          <Form form={batchForm} layout="vertical" scrollToFirstError onFinish={handleBatchSubmit}>
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
                onChange={handleBatchSrcTableChange}
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
              rules={[{ required: true, message: "请至少选择一个度量列" }]}
              extra={batchColumnOptions.length > 0 ? "从该表列中选择（可多选），或输入自定义列名" : "请先选择源表，可自动带出该表列"}
            >
              <Select
                mode="tags"
                tokenSeparators={[",", "\n"]}
                placeholder={batchColumnOptions.length > 0 ? "选择该表列（可多选，也可输入）" : "请先选择源表后自动带出该表列"}
                options={batchColumnOptions}
                loading={batchColLoading}
                notFoundContent={batchColLoading ? <Spin size="small" /> : "无匹配列，可直接输入"}
                filterOption={(input, option) =>
                  (option?.label as string)?.toLowerCase().includes(input.toLowerCase())
                }
              />
            </Form.Item>
            <Form.Item
              label="维度列映射（可选）"
              extra="每行一个：维度名 + 该维度在源表对应的列（列名从源表列选择）"
            >
              <Form.List name="dimension_mapping_list">
                {(fields, { add, remove }) => (
                  <>
                    {fields.map(({ key, name, ...restField }) => (
                      <Space key={key} style={{ display: "flex", marginBottom: 6 }} align="baseline">
                        <Form.Item
                          {...restField}
                          name={[name, "dim_name"]}
                          rules={[{ required: true, message: "维度名" }]}
                          style={{ marginBottom: 0 }}
                        >
                          <AutoComplete
                            data-testid="dim-name-auto"
                            placeholder="维度名（可搜索平台维度或手输）"
                            style={{ width: 200 }}
                            options={dimensionOptions}
                            filterOption={(input, option) =>
                              String(option?.value ?? "")
                                .toLowerCase()
                                .includes(input.toLowerCase())
                            }
                          />
                        </Form.Item>
                        <Form.Item
                          {...restField}
                          name={[name, "col_name"]}
                          rules={[{ required: true, message: "列名" }]}
                          style={{ marginBottom: 0 }}
                        >
                          <Select
                            showSearch
                            allowClear
                            placeholder={batchColumnOptions.length > 0 ? "选择源表列" : "先选源表后可选列"}
                            options={batchColumnOptions}
                            loading={batchColLoading}
                            style={{ width: 220 }}
                          />
                        </Form.Item>
                        <Button type="text" danger icon={<MinusCircleOutlined />} aria-label="删除该维度映射行" onClick={() => remove(name)} />
                      </Space>
                    ))}
                    <Button type="dashed" onClick={() => add()} block icon={<PlusOutlined />}>
                      添加维度映射
                    </Button>
                  </>
                )}
              </Form.List>
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
