import { useEffect, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { BarsOutlined, ArrowLeftOutlined, PlusOutlined, MinusCircleOutlined, RobotOutlined } from "@ant-design/icons";
import {
  Alert, AutoComplete, Button, Card, Checkbox, Cascader, Col, Collapse, Divider, Drawer, Form, Input, Modal, Radio, Row, Segmented, Select, Space, Spin, Steps, Switch, Table, Tooltip, Typography, App as AntApp, Tag,
} from "antd";
import {
  createMetric, listCatalogs, autoSuggestMetric, suggestDomain, parseSqlBatch, batchRegisterFromSql, listDomainTree, listDictItems, checkConflict, batchRegisterMetrics, batchSubmitMetrics, listDimensions, listMetrics, getDomainDefaults, listUsers, listMeasureCatalogs, fetchCurrentUser, refineMetricDefinition, UnisenseApiError,
} from "../api";
import type { MetricCreateRequest, MetricBatchRegisterRequest, MetricBatchRegisterResult, MetricType, MetricTier, SubjectDomainTreeNode, ConflictCheckResult, SuggestionField, AutoSuggestResponse, DomainSuggestionCandidate, Dimension, MeasureCatalog, MeasureSuggestion, MetricMountInput, SqlBatchParseResult, SqlBatchCandidate, CurrentUser, ConsumptionGuidePayload } from "../types";
import { CONFLICT_TYPE_LABEL, CONFLICT_SEVERITY_LABEL, enumLabel } from "../utils/enums";
import { MEASURE_FORMAT_LABEL } from "../types";
import { usePermission } from "../hooks/usePermission";
import RoleOwnerSelect, { type RoleOwnerValue } from "../components/RoleOwnerSelect";
import { ListEditor } from "./ConsumptionGuide";

const { Title, Paragraph } = Typography;
const { TextArea } = Input;

// SQL 批量解析草稿持久化（生产就绪加固）：候选存 React state 刷新/离开即丢、
// 无草稿会话——解析结果 + SQL 输入 + 切分模式/规则 + 合成开关写入 localStorage，
// 重新进入页面可一键恢复继续创建（"解析 50 个候选关掉页面回来"不再丢失）。
const SQL_BATCH_DRAFT_KEY = "unisense.sql-batch.draft";
interface SqlBatchDraft {
  sql: string;
  splitMode: "semicolon" | "statement" | "custom";
  customDelimiters: string;
  customMarkers: string;
  synthesize: boolean;
  useLlm: boolean;
  result: SqlBatchParseResult;
  savedAt: number;
}

function loadSqlBatchDraft(): SqlBatchDraft | null {
  try {
    const raw = localStorage.getItem(SQL_BATCH_DRAFT_KEY);
    if (!raw) return null;
    const draft = JSON.parse(raw) as SqlBatchDraft;
    // 仅恢复 24h 内的草稿（避免陈旧草稿长期占用）
    if (!draft?.result || Date.now() - (draft.savedAt ?? 0) > 24 * 3600 * 1000) {
      localStorage.removeItem(SQL_BATCH_DRAFT_KEY);
      return null;
    }
    return draft;
  } catch {
    return null;
  }
}

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

// 域建议回填：将域 code 映射为 Cascader 需要的完整路径数组（父→子），
// 找不到（非叶子/停用域）返回 null——调用方提示手动选择。
function findDomainPath(nodes: SubjectDomainTreeNode[], code: string): string[] | null {
  for (const n of nodes) {
    if (n.code === code) return [n.code];
    const sub = findDomainPath(n.children, code);
    if (sub) return [n.code, ...sub];
  }
  return null;
}

// SQL 批量解析 skipped 原因分类 → 友好文案（对齐后端 _classify_no_measure）。
const SQL_SKIP_REASON_TEXT: Record<string, string> = {
  ddl_only: "建表/删表等非查询语句（无聚合度量）已跳过",
  parse_failed: "含聚合但语法/方言无法识别，已尝试 AI 兜底仍未能提取",
  no_aggregate: "语句未包含 SUM/COUNT 等聚合函数，已跳过",
  llm_infer_failed: "已尝试 AI 兜底解析仍无法识别聚合度量",
  // P1-2（第五轮）：后端批级 LLM 兜底额度（_LLM_BATCH_LIMIT=5）耗尽——降级 skipped
  // 并产 reason=llm_limit；此前前端无此键落到兜底文案，误导用户以为 SQL 有问题
  llm_limit: "已达本批 AI 兜底上限（每批最多 5 次），建议缩减语句数后重试",
  // use_llm 显式模式：LLM 高置信度判定该候选非业务度量，已从候选中剔除
  llm_not_measure: "AI 判定为非业务度量（置信度较高），已剔除；如确为度量请用「解析候选」重试",
};

/** 汇总多条 skipped 成一行可读文案（按原因去重，未知原因用兜底文案）。 */
function sqlSkipSummary(skipped: { index: number; sql: string; reason: string }[]): string {
  const reasons = [...new Set(skipped.map((s) => s.reason))];
  if (reasons.length === 0) return "未解析到可注册的指标候选";
  return reasons
    .map((r) => SQL_SKIP_REASON_TEXT[r] || "部分语句未能解析出聚合度量")
    .join("；");
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
  { dictType: "currency", field: "currency", label: "币种" },
  { dictType: "aggregation", field: "aggregation", label: "聚合" },
  { dictType: "time_semantics", field: "time_semantics", label: "时间语义" },
  { dictType: "freshness", field: "freshness", label: "新鲜度" },
  { dictType: "dw_layer", field: "dw_layer", label: "数仓层" },
  { dictType: "metric_type", field: "type", label: "类型" },
  { dictType: "additivity", field: "additivity", label: "可加性" },
  { dictType: "serving_mode", field: "serving_mode", label: "服务模式" },
  { dictType: "metric_tier", field: "metric_tier", label: "分级" },
];

// SQL 批量解析候选行内可编辑的聚合方式（对齐 MetricCreateRequest 聚合枚举）。
const AGG_OPTIONS = [
  "SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "FIRST_VALUE",
  "MAX", "MIN", "MEDIAN", "PERCENTILE",
].map((v) => ({ value: v, label: v }));

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

// 未采集表/字段手动输入：搜索关键词在当前选项里没有精确匹配时，下拉顶部注入
// 「关键词」选项（下拉里以橙色标注“未采集，手动输入”提示），点选即录入，选中后仍显示
// 干净表名。表/字段尚未被采集进平台时，注册流程仍可手动录入完整表名/列名
//（后端无“必须已采集”校验，落库天然接受）。
interface TableSelectOption {
  value: string;
  label: string;
  uncollected?: boolean;
}

function withUncollectedOption(q: string, options: TableSelectOption[]): TableSelectOption[] {
  const kw = (q ?? "").trim();
  if (!kw) return options;
  if (options.some((o) => o.value === kw)) return options;
  return [{ value: kw, label: kw, uncollected: true }, ...options];
}

// 下拉里未采集项显示「（未采集，手动输入）」提示；选中后的 tag/选中文本仍是干净表名。
// antd 5.22 optionRender 签名：(oriOption: FlattenOptionData, info) => ReactNode，直接返回渲染内容。
function tableOptionRender(oriOption: { data?: TableSelectOption }) {
  const opt = oriOption?.data;
  if (opt?.uncollected) {
    return (
      <span>
        {opt.label}
        <span style={{ color: "#d46b08", marginLeft: 6 }}>（未采集，手动输入）</span>
      </span>
    );
  }
  return opt?.label ?? null;
}

// 推断来源 → 徽标样式（与后端 SuggestionField.source 对齐）
const SOURCE_META: Record<string, { color: string; text: string }> = {
  sql_parse: { color: "geekblue", text: "SQL解析" },
  column_meta: { color: "purple", text: "列元数据" },
  domain_default: { color: "gold", text: "域默认" },
  rule: { color: "cyan", text: "规则" },
  llm: { color: "magenta", text: "AI" },
  fallback: { color: "default", text: "兜底" },
  // 业务域建议来源（FR-010 域建议增强）
  catalog: { color: "green", text: "采集目录" },
  mount: { color: "blue", text: "挂载实体" },
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

// 三类指标生产配置差异引导（对齐 PRD 4.5：原子=绑定物理来源；派生=引用上游+表达式；复合=跨域聚合）。
// 选类型后展示，说明该类型的核心配置，避免统一表单的认知负担。
const TYPE_HINTS: Record<MetricType, string> = {
  atomic: "基于物理表字段直接聚合（如 GMV = SUM(pay_amt)）。核心配置：源表、度量列、聚合方式与统计周期。",
  derived: "引用已发布上游指标 + 计算表达式（如 客单价 = gmv / order_cnt）。核心配置：依赖指标与计算表达式。",
  composite: "跨域 / 带过滤条件汇总多个指标（如 华东区GMV占比）。核心配置：多个依赖指标与聚合表达式。",
};

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
  // OneData 向导：当前步骤（0=业务域 1=指标定义 2=口径确认 3=治理/提交）。
  // 分步一屏 + 底部导航，替代原先"编号打补丁"的平铺卡片流（方案 C 重构）。
  const [currentStep, setCurrentStep] = useState(0);
  // 当前用户角色（挂载时获取）：管理/数仓角色默认展开高级治理，业务角色默认折叠
  const [currentRole, setCurrentRole] = useState<string>("");
  // 当前用户完整信息（跨域权限预检：domain_admin/metric_owner 仅可操作本域指标）
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  useEffect(() => {
    fetchCurrentUser()
      .then((u) => {
        setCurrentUser(u);
        setCurrentRole(u.role);
      })
      .catch(() => {});
  }, []);
  // 消费指南（选填）：创建时透传落库（guide_source=manual）；null=未填写
  const [guideDraft, setGuideDraft] = useState<ConsumptionGuidePayload | null>(null);
  // 指标类型联动：atomic（原子）基于源表直接聚合，不应有上游依赖指标；derived/composite 才有
  // 用 state 而非 Form.useWatch：向导分步卸载 Form.Item 后，useWatch 与 getFieldsValue() 对未挂载字段
  // 均返回 undefined（antd 仅保留 store，默认取值路径排除未挂载字段），导致跨步骤后
  // isAtomic/isDerivedOrComposite 整体失效、提交校验跳过、payload 丢失 type。
  // 通过 Form onValuesChange 同步所有写入路径（Segmented 点击/域默认预填/推断回填）。
  const [metricType, setMetricType] = useState<MetricType>("atomic");
  const isAtomic = metricType === "atomic";
  const isDerivedOrComposite = metricType === "derived" || metricType === "composite";

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

  // 逻辑度量目录选项（OneData 原子层）：原子指标选择逻辑度量——度量格式/默认单位/小数位/
  // 源头系统/同义词从度量目录继承（PRD FR-02-08），注册页不再重复填写基础度量属性。
  const [measureOptions, setMeasureOptions] = useState<Array<{ value: number; label: string; measure: MeasureCatalog }>>([]);
  const [selectedMeasure, setSelectedMeasure] = useState<MeasureCatalog | null>(null);

  const [suggesting, setSuggesting] = useState(false);
  const [suggestedCode, setSuggestedCode] = useState<string | null>(null);

  const [mode, setMode] = useState<"expression" | "sql">("expression");
  const [sqlText, setSqlText] = useState("");
  // 三层口径（产品文档 §2.2）：业务口径（一句话口径定义，四方评审必读）→ definition_json.definition
  const [businessDefinition, setBusinessDefinition] = useState("");
  // 口径双字段（对齐 Step④ 责任方：技术方=系统开发、数仓开发=数仓建模）：
  // - pseudo_definition：系统开发提供的伪代码口径（伪 SQL/自然语言，非完整 SQL）
  // - dw_definition：数仓开发指标的详细口径（完整 SQL/建模口径）
  const [pseudoDefinition, setPseudoDefinition] = useState("");
  const [dwDefinition, setDwDefinition] = useState("");
  // 三层口径 LLM 增强：记录正在推断的口径层（business/pseudo/dw），对应按钮 loading
  const [refiningField, setRefiningField] = useState<"business" | "pseudo" | "dw" | null>(null);
  // OneData 向导：SQL 智能推断是"工具"而非主流程步骤（方案 C）——收敛为右上角抽屉入口
  const [sqlInferOpen, setSqlInferOpen] = useState(false);
  // 派生/复合指标的计算表达式（MEL 语法，如 gmv / order_cnt），自动合入 definition_json.expression。
  // 原子指标不展示此输入（其聚合表达式由 源表+度量列+聚合 生成）。
  const [calcExpression, setCalcExpression] = useState("");
  // R5: 口径定义 JSON 即时校验（输入时实时检测语法，内联显示错误）
  const [definitionError, setDefinitionError] = useState<string | null>(null);
  const [sourceTables, setSourceTables] = useState<string[]>([]);
  // 下游使用表（消费方）：该指标产出的数据被哪些表消费（写入口径定义 downstream_tables，
  // 血缘据此注册 metric → table 下游边）。与 sourceTables（上游依赖）方向相反。
  const [downstreamTables, setDownstreamTables] = useState<string[]>([]);
  const [tableOptions, setTableOptions] = useState<{ value: string; label: string }[]>([]);
  const [tableSearching, setTableSearching] = useState(false);

  // 源表搜索（自动推断区）
  const [srcTableSearchOptions, setSrcTableSearchOptions] = useState<{ value: string; label: string }[]>([]);
  const [srcTableSearchLoading, setSrcTableSearchLoading] = useState(false);
  const [columnOptions, setColumnOptions] = useState<{ value: string; label: string }[]>([]);
  const srcTableSearchTimer = useRef<ReturnType<typeof setTimeout>>();
  // 未采集表/字段手动输入：记录各 Select 最近搜索关键词，用于在选项顶部注入
  // 「关键词（未采集，手动输入）」项——已采集表/列照常点选，未采集的也能手输录入。
  // 独立 state 避免多个 Select 共用同一 onSearch 时关键词互相覆盖。
  const [srcTableKw, setSrcTableKw] = useState("");        // 自动推断区源表名
  const [mountSrcTableKw, setMountSrcTableKw] = useState(""); // 挂载源表
  const [batchSrcTableKw, setBatchSrcTableKw] = useState(""); // 批量注册源表
  const [depTableKw, setDepTableKw] = useState("");        // 依赖表（上游）
  const [downTableKw, setDownTableKw] = useState("");      // 使用表（下游）
  const [columnKw, setColumnKw] = useState("");            // 度量列
  const [mountColumnKw, setMountColumnKw] = useState("");  // 挂载度量列
  // 域默认值预填字段集合（TD §3.8）：选域触发 autoSuggest 时这些字段不被推断覆盖
  // （管理员显式配置的域默认值优先于自动推断），SQL 推断等用户主动操作可正常覆盖。
  const domainPrefillRef = useRef<Set<string>>(new Set());

  const [prechecking, setPrechecking] = useState(false);
  const [precheckResult, setPrecheckResult] = useState<ConflictCheckResult | null>(null);

  // SQL 智能推断入口状态
  const [sqlInferText, setSqlInferText] = useState("");
  const [sqlInferring, setSqlInferring] = useState(false);
  // 当前运行/上次选择的推断模式（false=程序规则，true=LLM 全字段）——用于双按钮 loading 反馈
  const [sqlInferLlm, setSqlInferLlm] = useState(false);
  // 推断结果回填：各字段来源徽标 + 自动生成的口径定义预览
  const [inferred, setInferred] = useState<Record<string, SuggestionField>>({});
  const [inferredDefinition, setInferredDefinition] = useState<{ json: Record<string, unknown> | null; mode: string | null }>({ json: null, mode: null });
  // 推断结果友好摘要（SQL 智能推断成功后展示，让用户明确知道推断出了什么）
  const [inferSummary, setInferSummary] = useState<AutoSuggestResponse | null>(null);
  const [inferSummaryOpen, setInferSummaryOpen] = useState(false);
  // SQL 智能推断模式透传：LLM 推断按钮/程序推断按钮共用 handleSqlInfer 流程（含域建议、
  // 多候选域挑选），用 ref 在候选确认后仍保持用户选择的推断模式
  const sqlInferUseLlmRef = useRef(false);
  // 逻辑度量推荐（信息最大化）：SQL 推断按度量列名匹配已发布逻辑度量目录，
  // 供原子指标一键继承 measure_id（OneData 原子层 = 逻辑度量 + 聚合方式）。
  const [measureSuggestions, setMeasureSuggestions] = useState<MeasureSuggestion[]>([]);

  // 业务域建议（FR-010 域建议增强）：SQL 推断时反向定位/LLM 兜底推断业务域。
  // domainSuggestionStatus：unique/llm=已应用；conflict=建议域与当前所选不同；matched=与所选一致；
  // multiple=多候选待挑；none=无法建议。
  const [domainSuggesting, setDomainSuggesting] = useState(false);
  const [domainSuggestion, setDomainSuggestion] = useState<DomainSuggestionCandidate | null>(null);
  const [domainSuggestionStatus, setDomainSuggestionStatus] = useState<string | null>(null);
  const [candidateCandidates, setCandidateCandidates] = useState<DomainSuggestionCandidate[]>([]);
  const [candidateOpen, setCandidateOpen] = useState(false);
  const [candidateChecked, setCandidateChecked] = useState<string>("");

  // 批量注册指标弹窗状态（POST /metric-definitions/batch-register）
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchForm] = Form.useForm();
  const [batchSubmitting, setBatchSubmitting] = useState(false);
  const [batchResult, setBatchResult] = useState<MetricBatchRegisterResult | null>(null);
  // L2 重试失败项：记录最近一批的失败列（candidates 与列按下标一一对应），
  // 「重试失败项」按钮仅重跑失败列，避免「继续注册」全量重跑把已建 DRAFT 的列再判冲突
  const [batchRetryFailed, setBatchRetryFailed] = useState<string[]>([]);
  // SQL 批量解析（FR-010 批量注册增强，场景A/B：多语句切分 + 多度量拆分 + 复合合成）。
  // 独立 sqlBatch* state 前缀，不动既有 handleSqlInfer/batch 逻辑。
  const [sqlBatchMode, setSqlBatchMode] = useState<"single" | "batch">("single");
  const [sqlBatchParsing, setSqlBatchParsing] = useState(false);
  // LLM 推断按钮独立 loading（与「解析候选」区分，双按钮各自转圈）
  const [sqlBatchLlmParsing, setSqlBatchLlmParsing] = useState(false);
  const [sqlBatchResult, setSqlBatchResult] = useState<SqlBatchParseResult | null>(null);
  // 合成复合指标开关（单语句多度量时组内追加复合候选）
  const [sqlBatchSynthesize, setSqlBatchSynthesize] = useState(false);
  // P2-8：切分模式（semicolon/statement/custom）——后端已实现，前端此前硬编码 statement
  const [sqlBatchSplitMode, setSqlBatchSplitMode] = useState<"semicolon" | "statement" | "custom">(
    "statement"
  );
  // P2-8：custom 模式自定义切分规则（delimiters/start_markers 正则，逗号分隔多规则）
  const [sqlBatchCustomDelimiters, setSqlBatchCustomDelimiters] = useState("");
  const [sqlBatchCustomMarkers, setSqlBatchCustomMarkers] = useState("");
  // 勾选的候选 key 集合（默认全选原子；复合由「合成复合」开关生成）
  const [sqlBatchChecked, setSqlBatchChecked] = useState<Set<string>>(new Set());
  // 勾选联动提示：取消勾选被复合依赖的原子时弹窗
  const [sqlBatchConflictKey, setSqlBatchConflictKey] = useState<string>("");
  // SQL 批量草稿恢复（生产就绪加固）：挂载时若存在 24h 内草稿，恢复解析结果与
  // 输入/切分配置——"解析后刷新/离开再回来"候选不丢，可继续勾选创建
  useEffect(() => {
    const draft = loadSqlBatchDraft();
    if (!draft) return;
    setSqlInferText(draft.sql);
    setSqlBatchMode("batch");
    setSqlBatchSplitMode(draft.splitMode);
    setSqlBatchCustomDelimiters(draft.customDelimiters);
    setSqlBatchCustomMarkers(draft.customMarkers);
    setSqlBatchSynthesize(draft.synthesize);
    setSqlBatchResult(draft.result);
    setSqlBatchChecked(
      new Set(draft.result.candidates.filter((c) => c.type === "atomic").map((c) => c.key)),
    );
    message.info("已恢复上次的 SQL 批量解析草稿，可继续勾选创建");
  }, []);
  const [sqlBatchConflictOpen, setSqlBatchConflictOpen] = useState(false);
  // 批量创建结果（复用 batchResult 分桶展示，但保留复合候选的「需先发布原子」提示）
  const [sqlBatchCreateResult, setSqlBatchCreateResult] = useState<MetricBatchRegisterResult | null>(null);
  const [sqlBatchCreating, setSqlBatchCreating] = useState(false);
  // P1-1：SQL 批量失败候选的 key 集合（仅重跑失败项，避免全量重跑把已建 DRAFT 再判冲突）
  const [sqlBatchRetryFailedKeys, setSqlBatchRetryFailedKeys] = useState<string[]>([]);
  // 批量注册成功 → 「批量提交评审」直达（复用 /batch-submit，复审 D1）
  const [batchSubmitLoading, setBatchSubmitLoading] = useState(false);
  // 批量提交评审指派（复审 P2-10）：默认域评审组，可指定评审用户（对齐单指标提交的 reviewer_type/id）
  const [batchReviewerType, setBatchReviewerType] = useState<"domain" | "user">("domain");
  const [batchReviewerId, setBatchReviewerId] = useState<number | undefined>(undefined);
  const [batchUsers, setBatchUsers] = useState<Array<{ id: number; username: string; display_name?: string | null }>>([]);
  // 口径三方责任（产品需求方/技术方/数仓开发）用户选项：挂载时加载一次，供三个选人 Select
  const [ownerUsers, setOwnerUsers] = useState<Array<{ id: number; username: string; display_name?: string | null }>>([]);
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

  // 可加性默认值不硬编码：字典加载后取第一个 active 值（ADDITIVE 即产品默认，sort_order=0）。
  // 避免「initialValues 硬编码引用已停用字典值 → 后端 DICT_VALUE_INACTIVE 拦截 → 注册 400」。
  // 用户已手动选择（isFieldTouched）或域默认已预填（getFieldValue 有值）时不覆盖。
  const additivityOptions = dictOptions["additivity"] ?? [];
  const defaultAdditivity = additivityOptions[0]?.value ?? "ADDITIVE";
  useEffect(() => {
    const opts = dictOptions["additivity"];
    if (opts?.length && !form.isFieldTouched("additivity") && !form.getFieldValue("additivity")) {
      form.setFieldValue("additivity", opts[0].value);
    }
  }, [dictOptions, form]);

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
    // 口径三方责任用户选项（产品需求方/技术方/数仓开发）
    listUsers()
      .then(setOwnerUsers)
      .catch(() => setOwnerUsers([]));
    // OneData 原子层：加载已发布逻辑度量（度量目录），供原子指标选择继承度量属性
    listMeasureCatalogs({ status: "PUBLISHED", page_size: 200 })
      .then((res) =>
        setMeasureOptions(
          (res.items ?? []).map((m) => ({
            value: m.id,
            label: `${m.name} (${m.measure_code})`,
            measure: m,
          })),
        ),
      )
      .catch(() => setMeasureOptions([]));
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
    // 用户已手输指标编码时不被推断建议覆盖（尊重显式输入；留空才用建议）
    // 用「当前值非空」判断而非 isFieldTouched（后者在 setFieldsValue 后行为不稳定）
    const curCode = String(form.getFieldValue("metric_code") ?? "").trim();
    const shouldSetCode = result.metric_code_suggestion && curCode === "";
    if (shouldSetCode) merged.metric_code = result.metric_code_suggestion;
    if (Object.keys(merged).length > 0) form.setFieldsValue(merged);
    if (shouldSetCode) setSuggestedCode(result.metric_code_suggestion);
    setInferred(fields);
    // 逻辑度量推荐（信息最大化）：候选交给 UI 展示，用户可一键应用 measure_id
    setMeasureSuggestions(result.measure_suggestions ?? []);
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
    // 依赖表（血缘推断）自动回填到「口径定义」：
    // - 上游依赖表（result.source_tables，向后兼容 related_tables）→ 依赖表（上游）
    // - 下游使用表（result.downstream_tables）→ 使用表（下游）
    // 均合并保留用户已选；方向不再混（此前 related_tables 含源表下游邻居被误填为上游）。
    const sugg = result as unknown as {
      related_tables?: string[];
      source_tables?: string[];
      downstream_tables?: string[];
    };
    const upstream = Array.isArray(sugg.source_tables)
      ? sugg.source_tables
      : Array.isArray(sugg.related_tables)
        ? sugg.related_tables
        : [];
    if (Array.isArray(upstream) && upstream.length > 0) {
      setSourceTables((prev) => Array.from(new Set([...(prev || []), ...upstream])));
    }
    if (Array.isArray(sugg.downstream_tables) && sugg.downstream_tables.length > 0) {
      setDownstreamTables((prev) => Array.from(new Set([...(prev || []), ...sugg.downstream_tables!])));
    }
    // 未采集表/列推断值补进 options：依赖表/使用表/度量列在下拉中可显示可再选
    //（LLM 推断出的表/列可能尚未被采集进平台，不补进去下拉会没有对应项）
    const inferredTables = [...upstream, ...(sugg.downstream_tables ?? [])];
    if (inferredTables.length > 0) {
      setTableOptions((prev) => {
        const seen = new Set((prev ?? []).map((o) => o.value));
        const added = inferredTables
          .filter((t): t is string => Boolean(t) && !seen.has(t))
          .map((t) => ({ value: t, label: t }));
        return added.length ? [...(prev ?? []), ...added] : prev;
      });
    }
    const inferredColumn = merged.measure_column;
    if (typeof inferredColumn === "string" && inferredColumn) {
      setColumnOptions((prev) =>
        prev.some((o) => o.value === inferredColumn)
          ? prev
          : [...prev, { value: inferredColumn, label: inferredColumn }]
      );
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

  // 一键应用推荐逻辑度量（信息最大化）：SQL 推断匹配到已发布逻辑度量时，
  // 用户点选即回填 measure_id 并同步 selectedMeasure（继承单位/格式/小数位）。
  // 若该度量不在已加载 options 中（page_size 截断等），补进下拉保证可显示。
  function applyMeasureSuggestion(sugg: MeasureSuggestion) {
    form.setFieldValue("measure_id", sugg.id);
    let found = measureOptions.find((o) => o.value === sugg.id)?.measure ?? null;
    if (!found) {
      found = {
        id: sugg.id,
        measure_code: sugg.measure_code,
        name: sugg.name,
        measure_format: sugg.measure_format,
        default_unit: sugg.default_unit,
        description: null,
        default_decimal_places: null,
        source_system: null,
        synonyms: null,
        category: "OTHER",
        stat_caliber: null,
        domain: "",
        owner_id: 0,
        status: "PUBLISHED",
        created_at: "",
        updated_at: "",
      } as MeasureCatalog;
      setMeasureOptions((prev) => [
        ...prev,
        { value: sugg.id, label: `${sugg.name} (${sugg.measure_code})`, measure: found as MeasureCatalog },
      ]);
    }
    setSelectedMeasure(found);
    message.success(`已应用逻辑度量「${sugg.name} (${sugg.measure_code})」——单位/格式/小数位将继承`);
  }

  // 域默认值预填（TD §3.8 主题域默认值）：选域后将该域配置的默认粒度/单位/聚合等
  // 预填到表单（用户可覆盖），打通「主题域配置 → 注册指标预填」的跨服务闭环。
  // 用 isFieldTouched 区分「用户手动输入」与「initialValues 全局默认」：
  // 域默认值覆盖全局 initialValues（域配置优先），但尊重用户已手动修改的字段。
  // 独立函数供「域建议自动选域」复用（不触发 autoSuggest，调用方自行推断）。
  async function loadDomainDefaults(domainCode: string) {
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
    await loadDomainDefaults(domainCode);
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

  // 应用域建议：将建议域预填到 Step0 域 Cascader + 预填域默认值（不触发 autoSuggest——
  // 调用方 handleSqlInfer 会随后用该域跑 SQL 推断，避免重复网络请求）。
  async function applyDomainSuggestion(dom: DomainSuggestionCandidate) {
    setDomainSuggestion(dom);
    setDomainSuggestionStatus("applied");
    const path = findDomainPath(domainTree, dom.code);
    if (!path) {
      message.warning(`建议的业务域「${dom.name}（${dom.code}）」不在可选域树中，请手动选择`);
      return;
    }
    form.setFieldValue("domain_path", path);
    setSelectedDomain(dom.code);
    await loadDomainDefaults(dom.code);
    message.success(`已按建议选择业务域：${dom.name}（${dom.code}）`);
  }

  // 三层口径 LLM 增强：AI 生成/丰富/优化业务口径、伪代码口径、数仓SQL口径（注册向导）。
  // 空值 → generate（从上下文生成）；有值 → business enrich、pseudo/dw optimize。
  // LLM 只回填文本（不落库），回填后用户可继续编辑再提交。
  async function handleRefineDefinition(field: "business" | "pseudo" | "dw") {
    if (refiningField) return;
    const current =
      field === "business"
        ? businessDefinition
        : field === "pseudo"
          ? pseudoDefinition
          : dwDefinition;
    const action =
      field === "business"
        ? current.trim()
          ? "enrich"
          : "generate"
        : current.trim()
          ? "optimize"
          : "generate";
    const name = String(form.getFieldValue("name") ?? "").trim();
    const code = String(form.getFieldValue("metric_code") ?? "").trim();
    setRefiningField(field);
    try {
      const res = await refineMetricDefinition({
        field,
        action,
        current,
        metric_code: code || undefined,
        metric_name: name || undefined,
        domain: selectedDomain || undefined,
        sql: sqlText.trim() || undefined,
        expression: calcExpression.trim() || undefined,
        business_definition: businessDefinition.trim() || undefined,
        pseudo_definition: pseudoDefinition.trim() || undefined,
        dw_definition: dwDefinition.trim() || undefined,
      });
      const label = field === "business" ? "业务口径" : field === "pseudo" ? "伪代码口径" : "数仓SQL口径";
      if (field === "business") setBusinessDefinition(res.content);
      else if (field === "pseudo") setPseudoDefinition(res.content);
      else setDwDefinition(res.content);
      message.success(`${label}已${action === "generate" ? "生成" : action === "enrich" ? "丰富增强" : "优化"}，可继续编辑后提交`);
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError && err.code === "LLM_INFER_UNAVAILABLE"
          ? "LLM 不可用：请检查 LLM 配置或稍后重试"
          : err instanceof Error
            ? err.message
            : "AI 增强失败",
      );
    } finally {
      setRefiningField(null);
    }
  }

  // 粘贴 SQL 智能推断（独立入口：仅用于推断并回填属性，与最终「口径定义」相互独立）
  // 用指定域跑 SQL 自动推断并回填（域建议后重跑也复用；错误内部消化不阻断）
  // useLlm=true 走 LLM 全字段推断（后端枚举白名单校验兜底），false 走程序规则推断
  async function runSqlInfer(domainCode: string, useLlm = false) {
    setSqlInferring(true);
    setSqlInferLlm(useLlm);
    try {
      const result = await autoSuggestMetric({
        domain_code: domainCode,
        sql: sqlInferText.trim(),
        use_llm: useLlm || undefined,
      });
      applySuggestion(result);
      setInferSummary(result);
      setInferSummaryOpen(true);
      const srcTable = (result.fields?.source_table?.value as string) || null;
      const measure = (result.fields?.measure_column?.value as string) || null;
      const parsed = result.parsed_measures;
      const parsedNote =
        Array.isArray(parsed) && parsed.length > 1
          ? `（识别到 ${parsed.length} 个度量列，已回填首个「${parsed[0].alias ?? parsed[0].column}」，详见结果弹窗）`
          : "";
      const modeNote = useLlm ? "（已用 LLM 全字段推断，枚举字段经校验兜底）" : "";
      if (srcTable || measure) {
        message.success(
          srcTable && measure
            ? `已从 SQL 识别：源表 ${srcTable} · 度量列 ${measure}${parsedNote}${modeNote}`
            : srcTable
              ? `已从 SQL 识别源表：${srcTable}${modeNote}`
              : `已从 SQL 识别度量列：${measure}${modeNote}`
        );
      } else {
        message.success(`已完成 SQL 解析，字段已按规则推断回填${parsedNote}${modeNote}`);
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

  // 粘贴 SQL 智能推断（独立入口：仅用于推断并回填属性，与最终「口径定义」相互独立）。
  // 不再要求先选业务域（FR-010 域建议增强）：未选域时先反向定位/LLM 兜底推断业务域，
  // 预填 Step0 域 Cascader 后继续推断；已选域则交叉校验（不同域提示可切换）。
  // useLlm=true 走 LLM 全字段推断，false 走程序默认规则推断（SQL 智能推断入口双按钮）。
  async function handleSqlInfer(useLlm = false) {
    const sql = sqlInferText.trim();
    if (!sql) { message.warning("请先粘贴指标 SQL"); return; }
    sqlInferUseLlmRef.current = useLlm;
    setSqlInferring(true);
    setDomainSuggesting(true);
    let effectiveDomain = selectedDomain;
    // ① 业务域建议——失败不阻断主推断（域建议只是辅助）
    try {
      const suggestion = await suggestDomain({ sql });
      if (suggestion.status === "unique" || suggestion.status === "llm") {
        const dom = suggestion.domain;
        if (dom) {
          if (!effectiveDomain) {
            await applyDomainSuggestion(dom);
            effectiveDomain = dom.code;
          } else if (dom.code !== effectiveDomain) {
            setDomainSuggestion(dom);
            setDomainSuggestionStatus("conflict");
          } else {
            setDomainSuggestion(dom);
            setDomainSuggestionStatus("matched");
          }
        } else {
          setDomainSuggestion(null);
          setDomainSuggestionStatus("none");
        }
      } else if (suggestion.status === "multiple") {
        setCandidateCandidates(suggestion.candidates || []);
        setCandidateOpen(true);
        setDomainSuggestion(null);
        setDomainSuggestionStatus("multiple");
      } else {
        setDomainSuggestion(null);
        setDomainSuggestionStatus("none");
      }
    } catch {
      setDomainSuggestion(null);
      setDomainSuggestionStatus("none");
    } finally {
      setDomainSuggesting(false);
    }
    // ② 按最终域跑 SQL 自动推断（域可能刚被建议更新；保持用户选择的推断模式）
    await runSqlInfer(effectiveDomain || "", sqlInferUseLlmRef.current);
  }

  // 多候选域挑一个：应用域建议后用该域重跑 SQL 推断（保持用户选择的推断模式）
  async function handleCandidateConfirm(code: string) {
    const dom = candidateCandidates.find((c) => c.code === code);
    setCandidateOpen(false);
    if (!dom) return;
    await applyDomainSuggestion(dom);
    await runSqlInfer(dom.code, sqlInferUseLlmRef.current);
  }

  // ---- SQL 批量解析（FR-010 批量注册增强，场景A/B）----
  // 独立 sqlBatch* state，不动既有 handleSqlInfer/batch 逻辑。

  /** 核心批量解析：调 parse-sql-batch 并默认全选原子候选。
   *  useLlm=true 走显式 LLM 模式（后端对规则候选做一次 LLM 批量补全+规范收敛）。 */
  async function runSqlBatchParse(synthesize: boolean, useLlm = false) {
    const sql = sqlInferText.trim();
    if (!sql) { message.warning("请先粘贴大段指标 SQL"); return; }
    // 生产就绪：超大 SQL 前端友好拦截（后端 schema max_length=65536 会 422，此前
    // 前端透传技术错误不友好）——分拆后分批解析
    if (sql.length > 65536) {
      message.warning(`SQL 过长（${sql.length} 字符，上限 65536），请按指标分拆后分批解析`);
      return;
    }
    if (useLlm) {
      setSqlBatchLlmParsing(true);
    } else {
      setSqlBatchParsing(true);
    }
    try {
      const result = await parseSqlBatch({
        sql,
        split_mode: sqlBatchSplitMode,
        custom_rules:
          sqlBatchSplitMode === "custom"
            ? {
                delimiters: sqlBatchCustomDelimiters
                  ? sqlBatchCustomDelimiters.split(",").map((s) => s.trim()).filter(Boolean)
                  : undefined,
                start_markers: sqlBatchCustomMarkers
                  ? sqlBatchCustomMarkers.split(",").map((s) => s.trim()).filter(Boolean)
                  : undefined,
              }
            : undefined,
        synthesize_composite: synthesize,
        use_llm: useLlm,
      });
      setSqlBatchResult(result);
      setSqlBatchChecked(
        new Set(result.candidates.filter((c) => c.type === "atomic").map((c) => c.key))
      );
      // 域建议：未选域且后端建议唯一/LLM 域 → 自动应用（对齐 handleSqlInfer 流程）
      const dom = result.domain;
      if (!selectedDomain && dom) {
        if (dom.code && (dom.status === "unique" || dom.status === "llm")) {
          await applyDomainSuggestion({
            code: dom.code,
            name: dom.name || dom.code,
            confidence: dom.confidence ?? 0,
            source: dom.status === "llm" ? "llm" : "catalog",
            reason: "SQL 批量解析时自动建议的业务域",
          });
        } else if (dom.status === "multiple" && dom.candidates.length > 0) {
          setCandidateCandidates(dom.candidates);
          setCandidateOpen(true);
        }
      }
      if (result.candidates.length === 0) {
        // 按后端 skipped 原因分类提示（避免一律「请检查 SQL 是否含 SELECT + 聚合函数」）
        message.warning(
          result.skipped.length > 0
            ? `${sqlSkipSummary(result.skipped)}，请调整 SQL 后重试`
            : "未解析到可注册的指标候选（请检查 SQL 是否含 SELECT + 聚合函数）",
        );
      } else {
        // 生产就绪加固：解析成功即存草稿（刷新/离开不丢，重新进入页面可恢复
        // 继续勾选创建——批量候选此前存 React state 刷新即失）
        try {
          localStorage.setItem(
            SQL_BATCH_DRAFT_KEY,
            JSON.stringify({
              sql,
              splitMode: sqlBatchSplitMode,
              customDelimiters: sqlBatchCustomDelimiters,
              customMarkers: sqlBatchCustomMarkers,
              synthesize,
              useLlm,
              result,
              savedAt: Date.now(),
            } satisfies SqlBatchDraft),
          );
        } catch {
          /* localStorage 不可用（隐私模式/配额满）静默跳过，不影响主流程 */
        }
        message.success(
          useLlm
            ? `已用 LLM 全字段推断解析 ${result.candidates.length} 个候选指标，可勾选后批量创建`
            : `已解析 ${result.candidates.length} 个候选指标，可勾选后批量创建`,
        );
      }
    } catch (err) {
      const detail = err instanceof UnisenseApiError ? err.message : "";
      message.error(detail ? `批量解析失败：${detail}` : "批量解析失败，请检查 SQL 语法或稍后重试");
    } finally {
      if (useLlm) {
        setSqlBatchLlmParsing(false);
      } else {
        setSqlBatchParsing(false);
      }
    }
  }

  async function handleParseSqlBatch(useLlm = false) {
    await runSqlBatchParse(sqlBatchSynthesize, useLlm);
  }

  // 合成复合开关：重新解析（开关影响候选生成）
  async function handleSqlBatchSynthesizeChange(v: boolean) {
    setSqlBatchSynthesize(v);
    if (sqlInferText.trim()) {
      await runSqlBatchParse(v);
    }
  }

  /** 编辑候选字段（名称/聚合等，行内微调后提交）。 */
  function handleSqlBatchEdit(key: string, patch: Partial<SqlBatchCandidate>) {
    setSqlBatchResult((prev) => {
      if (!prev) return prev;
      return {
        ...prev,
        candidates: prev.candidates.map((c) => (c.key === key ? { ...c, ...patch } : c)),
      };
    });
  }

  /** 勾选切换：取消原子若被某复合候选依赖 → 弹窗让用户选「跳过复合」或「回滚勾选」。 */
  function handleSqlBatchToggle(key: string, checked: boolean) {
    if (!sqlBatchResult) return;
    const next = new Set(sqlBatchChecked);
    if (checked) {
      next.add(key);
      setSqlBatchChecked(next);
      return;
    }
    const cand = sqlBatchResult.candidates.find((c) => c.key === key);
    if (!cand) return;
    // 仅当「被勾选」的复合候选依赖该原子时才弹窗（未勾选复合则直接取消原子）
    const dependents = sqlBatchResult.candidates.filter(
      (c) =>
        c.type === "composite" &&
        sqlBatchChecked.has(c.key) &&
        (c.dependencies || []).includes(cand.metric_code)
    );
    if (dependents.length > 0) {
      setSqlBatchConflictKey(key);
      setSqlBatchConflictOpen(true);
      return; // 不立即取消，等用户选择
    }
    next.delete(key);
    setSqlBatchChecked(next);
  }

  // 弹窗「跳过复合」：取消该原子 + 同时取消依赖它的复合候选
  function handleSqlBatchSkipComposite() {
    if (!sqlBatchResult) return;
    const next = new Set(sqlBatchChecked);
    const cand = sqlBatchResult.candidates.find((c) => c.key === sqlBatchConflictKey);
    if (cand) {
      next.delete(cand.key);
      sqlBatchResult.candidates
        .filter((c) => c.type === "composite" && (c.dependencies || []).includes(cand.metric_code))
        .forEach((c) => next.delete(c.key));
    }
    setSqlBatchChecked(next);
    setSqlBatchConflictOpen(false);
    setSqlBatchConflictKey("");
  }

  // 弹窗「回滚勾选」：保持原子勾选，仅关闭弹窗
  function handleSqlBatchRollback() {
    setSqlBatchConflictOpen(false);
    setSqlBatchConflictKey("");
  }

  // 批量创建：勾选候选 → batch-register-from-sql（savepoint 逐条隔离，结果分桶展示）
  async function handleSqlBatchCreate() {
    if (!sqlBatchResult) return;
    if (!selectedDomain) {
      message.warning("请先选择业务域（可到第 ① 步选择，或确认上方建议域后重试）");
      return;
    }
    await submitSqlBatch(new Set(sqlBatchChecked));
  }

  // P1-1：SQL 批量创建核心（可被「批量创建」与「重试失败项」复用）——按给定 key 集合
  // 从原始候选过滤出待提交项，调用 batch-register-from-sql；成功后记录失败 key 供重试。
  async function submitSqlBatch(keys: Set<string>) {
    if (!sqlBatchResult) return;
    if (!selectedDomain) {
      message.warning("请先选择业务域（可到第 ① 步选择，或确认上方建议域后重试）");
      return;
    }
    // 跨域权限预检（生产就绪审查 P2）：domain_admin/metric_owner 后端仅可批量注册
    // 本域指标（service 层整批 FORBIDDEN）——前端先拦截，避免用户提交后整批失败零创建
    const restrictedRole = currentUser?.role === "domain_admin" || currentUser?.role === "metric_owner";
    if (restrictedRole && currentUser?.domain && selectedDomain !== currentUser.domain) {
      message.warning(`您仅可批量注册本域指标（${currentUser.domain}），当前选择 ${selectedDomain} 将整批被拒绝`);
      return;
    }
    const checked = sqlBatchResult.candidates.filter((c) => keys.has(c.key));
    if (checked.length === 0) { message.warning("请至少勾选一个候选指标"); return; }
    setSqlBatchCreating(true);
    try {
      // P0-1：候选编码可能为空（域未定时后端不 bake-in）——按最终 selectedDomain 生成
      // 4 段编码（对齐后端 generate_metric_code：域_业务对象_度量_周期），避免空段非法编码。
      const resolveCode = (c: SqlBatchCandidate): string => {
        if (c.metric_code) return c.metric_code;
        const rawTable = c.source_table || "";
        const biz = rawTable
          ? (rawTable.split(".").pop() || "")
              .replace(/^(dwd_|ods_|dws_|ads_|dim_|tmp_)/, "")
              .split("_")[0]
          : "entity";
        const measure = (c.measure_column || "metric").replace(/_/g, "").toLowerCase();
        return [selectedDomain, biz || "entity", measure, c.period || "day"].join("_");
      };
      // 口径溯源（P2）：候选所属语句的整句原始 SQL——候选仅带表达式，原文从语句
      // meta 提取（按 statement_index），批量创建透传落 Metric.raw_sql 可反查口径全文
      const resolveRawSql = (c: SqlBatchCandidate): string | undefined =>
        c.raw_sql ||
        sqlBatchResult.statements.find((s) => s.index === c.statement_index)?.sql ||
        undefined;
      const res = await batchRegisterFromSql({
        domain: selectedDomain,
        candidates: checked.map((c) => ({
          key: c.key,
          metric_code: resolveCode(c),
          name: c.name,
          type: c.type,
          source_table: c.source_table,
          measure_column: c.measure_column,
          aggregation: c.aggregation,
          unit: c.unit,
          period: c.period,
          granularity: c.granularity,
          definition_json: c.definition_json,
          dependencies: c.dependencies,
          // OneData 接线（P2）：候选关联逻辑度量（前端选择器写入，SQL 无法推断）——
          // 批量创建的原子指标得以关联逻辑度量，不再全部游离走"旧式物理来源"路径
          measure_id: c.measure_id ?? null,
          // 口径溯源（P2）：整句原始 SQL 透传落 Metric.raw_sql
          raw_sql: resolveRawSql(c),
          // P0-2：复合候选携带口径三方责任（批量创建补齐 OwnerChain）
          product_owner_id: c.product_owner_id,
          tech_owner_id: c.tech_owner_id,
          dw_developer_id: c.dw_developer_id,
          product_owner_name: c.product_owner_name,
          tech_owner_name: c.tech_owner_name,
          dw_developer_name: c.dw_developer_name,
        })),
      });
      setSqlBatchCreateResult(res);
      // P1-1：记录失败候选的 key（从结果反查原始候选），供「重试失败项」仅重跑失败项
      const failCodes = new Set(
        res.candidates.filter((c) => c.status === "VALIDATION_ERROR").map((c) => c.metric_code),
      );
      setSqlBatchRetryFailedKeys(
        sqlBatchResult.candidates.filter((c) => failCodes.has(c.metric_code)).map((c) => c.key),
      );
    } catch (err) {
      const detail = err instanceof UnisenseApiError ? err.message : "";
      message.error(detail ? `批量创建失败：${detail}` : "批量创建失败，请稍后重试");
    } finally {
      setSqlBatchCreating(false);
    }
  }

  // 批量创建结果确认后：清空解析状态，可再次解析
  function handleSqlBatchCreateDone() {
    setSqlBatchCreateResult(null);
    setSqlBatchResult(null);
    setSqlBatchChecked(new Set());
    // 生产就绪：创建完成后清除草稿（避免下次进入恢复已用完的批次）
    try {
      localStorage.removeItem(SQL_BATCH_DRAFT_KEY);
    } catch {
      /* localStorage 不可用静默跳过 */
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
    // 依赖表（上游，加工出指标的表）→ definition_json.source_tables
    const tables = sourceTables.length ? { source_tables: sourceTables } : {};
    // 使用表（下游，消费指标的表）→ definition_json.downstream_tables（血缘注册下游边）
    const downTables = downstreamTables.length ? { downstream_tables: downstreamTables } : {};
    // 主表单选中的维度 → definition_json.dimensions（血缘注册指标↔维度边）
    const dimsField = selectedDims.length ? { dimensions: selectedDims } : {};
    // 依赖指标 → definition_json.dependencies（血缘注册上游→本指标边，仅 derived/composite）
    const depsField =
      isDerivedOrComposite && selectedDeps.length ? { dependencies: selectedDeps } : {};
    // 原子指标：源表/度量列 → 口径（血缘注册读 definition.source_table 建「指标↔落地表」边）
    const src = String(values.source_table || "").trim();
    const srcField = isAtomic && src ? { source_table: src } : {};
    const measure = String(values.measure_column || "").trim();
    const measureField = isAtomic && measure ? { measure_column: measure } : {};
    // 三层口径 → definition_json：业务口径（一句话）+ 伪代码口径 + 数仓SQL口径
    const businessField = businessDefinition.trim() ? { definition: businessDefinition.trim() } : {};
    const pseudoField = pseudoDefinition.trim() ? { pseudo_definition: pseudoDefinition.trim() } : {};
    const dwField = dwDefinition.trim() ? { dw_definition: dwDefinition.trim() } : {};
    const caliberFields = { ...businessField, ...pseudoField, ...dwField };
    if (mode === "sql") {
      const sql = sqlText.trim();
      if (!sql) { message.error("口径 SQL 模式请输入 SQL 语句"); return null; }
      return { sql, ...tables, ...downTables, ...srcField, ...measureField, ...dimsField, ...depsField, ...caliberFields };
    }
    let def: Record<string, unknown>;
    try { def = values.definition ? JSON.parse(String(values.definition)) : {}; }
    catch { message.error("口径定义需为合法 JSON"); return null; }
    if (isAtomic) {
      // 用户未手写 expression 时自动生成「聚合(度量列)」，保证原子指标有计算主体（对齐后端类型化校验）
      const hasManualExpression =
        typeof def.expression === "string" && String(def.expression).trim();
      const autoExpr =
        measure && !hasManualExpression
          ? { expression: `${String(values.aggregation || "SUM")}(${measure})` }
          : {};
      return { ...def, ...autoExpr, ...srcField, ...measureField, ...tables, ...downTables, ...dimsField, ...caliberFields };
    }
    // derived/composite：计算表达式输入 + 依赖指标 → 口径（不读源表/度量列）
    const expr = calcExpression.trim() ? { expression: calcExpression.trim() } : {};
    return { ...def, ...expr, ...tables, ...downTables, ...dimsField, ...depsField, ...caliberFields };
  }

  // OneData 向导：下一步纯前进（不逐级硬校验——避免打断"先粗填再回头改"的构建式流程；
  // 最终提交由 handleSubmit 的类型化必填校验统一兜底，保证错误在真正创建前被拦截）
  function handleNext() {
    setCurrentStep((s) => Math.min(s + 1, 3));
  }

  // 向导步骤导航：每步内容末尾常驻，形成"填完当前步 → 下一步"的引导流。
  // Step0-2 显示「上一步 + 下一步」，Step3（最后一步）显示「上一步 + 冲突预检 + 创建草稿」。
  const renderStepNav = () => (
    <Form.Item style={{ marginBottom: 0 }}>
      <Space>
        {currentStep > 0 && (
          <Button onClick={() => setCurrentStep(currentStep - 1)}>上一步</Button>
        )}
        {currentStep < 3 ? (
          <Button type="primary" onClick={handleNext}>
            {["下一步：指标定义", "下一步：治理与口径", "下一步：责任方与提交"][currentStep]}
          </Button>
        ) : (
          <>
            <Button onClick={handlePrecheck} loading={prechecking}>冲突预检</Button>
            {canCreate && <Button type="primary" htmlType="submit" loading={loading}>创建草稿</Button>}
          </>
        )}
      </Space>
    </Form.Item>
  );

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

  async function handleSubmit(_values: Record<string, unknown>) {
    // onFinish 的 values 仅含当前挂载字段；向导分步卸载上一步后 type/source_table 等字段不在其中。
    // antd store 已 preserve（getFieldValue 可读），仅 getFieldsValue()/onFinish 默认排除未挂载字段——
    // 用 getFieldsValue(true) 取含保留值的完整字段集，保证类型化校验与提交 payload 拿到全部字段。
    const values = form.getFieldsValue(true) as Record<string, unknown>;
    // 类型化必填校验（对齐后端 definition_json 类型校验 + PRD 4.5）：
    // 派生/复合=须有依赖指标+计算表达式；原子=须有源表度量列或手写口径。
    if (isDerivedOrComposite) {
      if (selectedDeps.length === 0) {
        message.warning("派生/复合指标必须选择至少 1 个依赖指标");
        return;
      }
      if (!calcExpression.trim()) {
        message.warning("请填写计算表达式（如 gmv / order_cnt）");
        return;
      }
    } else if (isAtomic && mode === "expression") {
      const measure = String(values.measure_column || "").trim();
      const hasDefinition = String(values.definition || "").trim().length > 0;
      if (!selectedMeasure && !measure && !hasDefinition) {
        message.warning("原子指标请选择逻辑度量（推荐）或源表与度量列，或填写口径定义");
        return;
      }
    }
    setLoading(true);
    const definitionJson = buildDefinitionJson(values);
    if (!definitionJson) { setLoading(false); return; }
    // OneData 挂载层：派生指标收集挂载配置（源表/列/粒度/周期/域）→ 服务端自动落 metric_mount
    let mount: MetricMountInput | undefined;
    if (metricType === "derived") {
      const ms = String(values.mount_source_table || "").trim();
      const mc = String(values.mount_source_column || "").trim();
      const mg = String(values.mount_granularity || "").trim();
      if (ms && mc && mg) {
        mount = {
          source_table: ms,
          source_column: mc,
          granularity: mg,
          default_period: String(values.mount_default_period || "") || null,
          domain: selectedDomain,
        };
      }
    }
    const req: MetricCreateRequest = {
      metric_code: values.metric_code ? String(values.metric_code) : undefined,
      name: String(values.name),
      domain: selectedDomain,
      type: String(values.type) as MetricType,
      // OneData：粒度下沉挂载——原子不设，派生由 mount 承载（主表冗余回填由服务端处理）
      granularity: isAtomic
        ? undefined
        : values.granularity
          ? String(values.granularity)
          : mount?.granularity ?? undefined,
      // OneData 原子层：原子指标关联逻辑度量（度量格式/单位/小数位继承）
      measure_id: isAtomic ? selectedMeasure?.id ?? undefined : undefined,
      mount,
      // OneData：单位与物理属性——原子由逻辑度量继承/后端默认（不提交）；派生/复合缺省后端兜底
      unit: isAtomic ? undefined : values.unit ? String(values.unit) : undefined,
      currency: isAtomic ? undefined : values.currency ? String(values.currency) : undefined,
      aggregation: String(values.aggregation) as MetricCreateRequest["aggregation"],
      time_semantics: (isAtomic ? undefined : values.time_semantics ? String(values.time_semantics) : undefined) as MetricCreateRequest["time_semantics"],
      freshness: (isAtomic ? undefined : values.freshness ? String(values.freshness) : undefined) as MetricCreateRequest["freshness"],
      dw_layer: (isAtomic ? undefined : values.dw_layer ? String(values.dw_layer) : undefined) as MetricCreateRequest["dw_layer"],
      metric_tier: String(values.metric_tier || "T3") as MetricTier,
      serving_mode: String(values.serving_mode || "BATCH_ONLY") as MetricCreateRequest["serving_mode"],
      additivity: String(values.additivity || defaultAdditivity) as MetricCreateRequest["additivity"],
      definition_json: definitionJson,
      pii_flag: Boolean(values.pii_flag),
      // 消费指南（选填）：创建时透传（guide_source=manual）；未填写则省略由后端自动生成
      consumption_guide: guideDraft
        ? {
            recommended_usage: guideDraft.recommended_usage.filter((s) => s.trim()),
            cautions: guideDraft.cautions.filter((s) => s.trim()),
            related_metrics: guideDraft.related_metrics.filter((s) => s.trim()),
          }
        : undefined,
      // 口径三方责任（可选）：平台用户 id 或外部人员名称兜底（RoleOwnerSelect 组合值拆分）
      product_owner_id: (values.product_owner as RoleOwnerValue | undefined)?.id ?? undefined,
      tech_owner_id: (values.tech_owner as RoleOwnerValue | undefined)?.id ?? undefined,
      dw_developer_id: (values.dw_developer as RoleOwnerValue | undefined)?.id ?? undefined,
      product_owner_name: (values.product_owner as RoleOwnerValue | undefined)?.name ?? undefined,
      tech_owner_name: (values.tech_owner as RoleOwnerValue | undefined)?.name ?? undefined,
      dw_developer_name: (values.dw_developer as RoleOwnerValue | undefined)?.name ?? undefined,
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

  // 批量注册维度列映射：源表列名 → 维度名自动推断（列名命中业务特征即预填，用户可改）。
  // 例如 dept_code/科室 → dept、doctor_id/医生 → doctor、patient_id/患者 → patient。
  // 推断仅作"免手输"提效——预填后仍是 AutoComplete 可搜索平台维度/手输兜底，不强制平台存在
  // （血缘「指标↔维度」边可挂未采集维度节点，与单条注册 dimensions 语义一致）。
  const COLUMN_DIM_HINTS: Array<{ match: RegExp; dim: string }> = [
    { match: /dept|ks_|科室|部门|department/i, dim: "dept" },
    { match: /doctor|physician|ys_|医生|医师/i, dim: "doctor" },
    { match: /patient|br_|患者|病人/i, dim: "patient" },
    { match: /diag|zd_|病种|诊断/i, dim: "diagnosis" },
    { match: /drug|yp_|药品/i, dim: "drug" },
    { match: /presc|cf_|处方/i, dim: "prescription" },
    { match: /pharm|药房/i, dim: "pharmacy" },
    { match: /settle|yb_|医保|结算/i, dim: "yb_settle" },
    { match: /channel|渠道/i, dim: "channel" },
    { match: /store|门店|shop/i, dim: "store" },
    { match: /city|城市|region|区域/i, dim: "region" },
    { match: /date|dt_|time|时间/i, dim: "time" },
  ];
  // 列名 → 维度名：命中第一条业务特征返回对应维度名，未命中返回 null（不预填）
  function inferDimFromColumn(colName: string): string | null {
    const hit = COLUMN_DIM_HINTS.find((h) => h.match.test(colName));
    return hit ? hit.dim : null;
  }

  // 提交批量注册：度量列按行拆分，维度映射为可选 JSON，成功/失败明细展示在结果区
  async function handleBatchSubmit(values: Record<string, unknown>) {
    // tags Select 返回数组；兼容历史手输换行文本
    const rawMeasure = values.measure_columns;
    const splitColumns = (Array.isArray(rawMeasure) ? rawMeasure : String(rawMeasure || "").split("\n"))
      .map((s) => String(s).trim())
      .filter(Boolean);
    // 去重（保留首次出现顺序）：重复列会生成相同 metric_code，第二条被后端判
    // VALIDATION_ERROR 误导、且结果表 rowKey 重复（修复前无去重）
    const measureColumns = [...new Set(splitColumns)];
    if (measureColumns.length === 0) {
      message.warning("请至少填写一个度量列");
      return;
    }
    if (measureColumns.length < splitColumns.length) {
      message.warning(`已去除 ${splitColumns.length - measureColumns.length} 个重复度量列`);
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
      llm_prefill: true,
      dimension_mapping: dimensionMapping,
    };
    setBatchResult(null);
    setBatchSubmitting(true);
    try {
      const result = await batchRegisterMetrics(req);
      setBatchResult(result);
      // L2：按 candidates 与列下标一一对应，记录本批失败列供「重试失败项」按钮使用
      const failedCols = result.candidates
        .map((c, i) => (c.status === "VALIDATION_ERROR" ? measureColumns[i] : null))
        .filter((c): c is string => Boolean(c));
      setBatchRetryFailed(failedCols);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量注册失败");
    } finally {
      setBatchSubmitting(false);
    }
  }

  const dictSelect = (dictType: string, _field: string, placeholder: string) => (
    <Select
      showSearch
      allowClear
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
        <Space>
          <Tooltip title="粘贴指标 SQL 智能推断并回填字段（未选域时可先推断出业务域建议，独立工具，不占注册主流程）">
            <Button icon={<RobotOutlined />} onClick={() => setSqlInferOpen(true)}>
              SQL 智能推断
            </Button>
          </Tooltip>
          <Tooltip title={canBatchRegister ? "批量注册（宽表多度量列 → 批量 DRAFT）" : "仅平台/域管理员与指标 Owner 可批量注册"}>
            <Button type="dashed" icon={<BarsOutlined />} onClick={openBatchModal} disabled={!canBatchRegister}>
              批量注册指标
            </Button>
          </Tooltip>
        </Space>
      </Space>
      <Steps
        current={currentStep}
        onChange={(c) => setCurrentStep(c)}
        style={{ marginBottom: 20 }}
        items={[
          { title: "业务域", description: "选域并继承域默认值" },
          { title: "指标定义", description: "类型 + 度量/依赖来源" },
          { title: "口径确认", description: "自动生成 + 维度/表关联" },
          { title: "治理/提交", description: "高级治理 + 创建草稿" },
        ]}
      />
      <Spin
        spinning={sqlInferring}
        size="large"
        tip="正在智能推断指标定义，请稍候…"
        style={{ minHeight: 320 }}
      >
        <Card>
        <Form form={form} layout="vertical" scrollToFirstError onFinish={handleSubmit}
          onValuesChange={(changed) => {
            // 表单字段变化即旧预检结果失效（避免「无冲突」结果在改口径后仍误导提交）
            if (precheckResult) setPrecheckResult(null);
            // 同步指标类型到 state（覆盖 Segmented 点击/域默认预填/推断回填等所有写入路径；
            // 见 metricType 声明——useWatch 跨步骤卸载后失效，须由 state 持有）
            if ("type" in changed) setMetricType(changed.type as MetricType);
          }}
          initialValues={{
          type: "atomic", granularity: "day", aggregation: "SUM",
          time_semantics: "PERIOD", freshness: "T1", dw_layer: "DWD",
          metric_tier: "T3", serving_mode: "BATCH_ONLY", // additivity 不硬编码——由字典 active 值动态预填（见上）
          pii_flag: false, period: "day",
        }}>
          <Space style={{ width: "100%" }} direction="vertical" size="middle">
            {/* Step 0: 选域（OneData 向导） */}
            {currentStep === 0 && (<>
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
            {renderStepNav()}
            </>)}

            {/* Step 1: 类型 + 来源（OneData 向导） */}
            {currentStep === 1 && (<>
            <Card type="inner" title="② 选择指标类型" size="small">
              <Form.Item
                name="type"
                label="指标类型"
                rules={[{ required: true, message: "请选择指标类型" }]}
                extra={TYPE_HINTS[(metricType ?? "atomic") as MetricType]}
              >
                <Segmented
                  block
                  options={[
                    { value: "atomic", label: "原子指标" },
                    { value: "derived", label: "派生指标" },
                    { value: "composite", label: "复合指标" },
                  ]}
                />
              </Form.Item>
            </Card>

            {/* Step 2: 按类型的来源配置——原子=逻辑度量/源字段；派生/复合=依赖指标（SQL 推断已收敛为工具栏抽屉） */}
            <Card
              type="inner"
              title={isAtomic ? "② 原子来源（逻辑度量 + 聚合方式）" : "② 依赖指标"}
              size="small"
              extra={suggesting && <Spin size="small" />}
            >
              {isAtomic ? (
                <>
                  <Form.Item
                    name="measure_id"
                    label="逻辑度量（原子指标口径库，OneData 原子层）"
                    extra={
                      <>
                        {selectedMeasure
                          ? `继承：${MEASURE_FORMAT_LABEL[selectedMeasure.measure_format] ?? selectedMeasure.measure_format} · 单位 ${selectedMeasure.default_unit || "—"} · 小数位 ${selectedMeasure.default_decimal_places ?? "按需"}${selectedMeasure.source_system?.length ? ` · 源头系统 ${selectedMeasure.source_system.join("/")}` : ""}`
                          : "原子指标 = 逻辑度量 + 聚合方式，不直接绑定物理表；度量格式/单位/小数位由原子指标口径库继承"}
                        {measureSuggestions.length > 0 && !selectedMeasure ? (
                          <div style={{ marginTop: 4 }}>
                            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                              SQL 推断推荐：
                            </Typography.Text>
                            {measureSuggestions.map((s) => (
                              <Tag
                                key={s.id}
                                color="green"
                                style={{ cursor: "pointer", marginInlineEnd: 6 }}
                                onClick={() => applyMeasureSuggestion(s)}
                              >
                                {s.name}
                              </Tag>
                            ))}
                            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                              （点击应用）
                            </Typography.Text>
                          </div>
                        ) : null}
                      </>
                    }
                  >
                    <Select
                      showSearch
                      allowClear
                      placeholder="选择或搜索逻辑度量（如 支付金额 pay_amt）"
                      optionFilterProp="label"
                      onChange={(id: number) =>
                        setSelectedMeasure(measureOptions.find((o) => o.value === id)?.measure ?? null)
                      }
                      options={measureOptions.map((o) => ({ value: o.value, label: o.label }))}
                    />
                  </Form.Item>
                  <Row gutter={16}>
                    <Col span={8}>
                      <Form.Item name="source_table" label="源表名（兼容旧式来源，可选）">
                        <Select
                          showSearch
                          allowClear
                          placeholder="选择或搜索源表（已接入的可选；未采集的可输入完整表名）"
                          onSearch={(q) => {
                            setSrcTableKw(q);
                            handleSrcTableSearch(q);
                          }}
                          onChange={handleSrcTableSelect}
                          onOpenChange={handleSrcTableDropdown}
                          loading={srcTableSearchLoading}
                          notFoundContent={srcTableSearchLoading ? <Spin size="small" /> : "无匹配表，可手动输入完整表名"}
                          options={withUncollectedOption(srcTableKw, srcTableSearchOptions)}
                          optionRender={tableOptionRender}
                          filterOption={false}
                        />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item name="measure_column" label="度量列（兼容旧式来源，可选）">
                        <Select
                          showSearch
                          allowClear
                          placeholder={columnOptions.length > 0 ? "选择度量列，或输入自定义列名" : "选择源表后自动带出列；也可直接输入列名"}
                          onSearch={setColumnKw}
                          onChange={handleColumnSelect}
                          options={withUncollectedOption(columnKw, columnOptions)}
                          optionRender={tableOptionRender}
                          notFoundContent={columnOptions.length === 0 ? "未采集列，可直接输入列名" : "无匹配列，可直接输入"}
                          filterOption={(input, option) =>
                            (option?.label as string)?.toLowerCase().includes(input.toLowerCase())
                          }
                        />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item name="period" label="统计周期（兼容旧式推断，可选）">
                        <Select
                          allowClear
                          placeholder="选择统计周期"
                          onChange={handlePeriodSelect}
                          options={PERIOD_OPTIONS}
                        />
                      </Form.Item>
                    </Col>
                  </Row>
                </>
              ) : (
                <>
                <Form.Item
                  label="依赖指标"
                  required
                  extra={
                    metricType === "composite"
                      ? "复合指标跨域/多指标聚合：选择多个已发布上游指标（可跨域），血缘据此生成依赖边。"
                      : "派生指标基于已发布上游指标计算：选择至少 1 个已发布指标，血缘据此生成依赖边。"
                  }
                >
                  <Select
                    mode="multiple"
                    showSearch
                    filterOption={false}
                    onSearch={handleDepSearch}
                    loading={depSearching}
                    placeholder="搜索并选择依赖指标（仅已发布指标可选）"
                    style={{ width: "100%" }}
                    value={selectedDeps}
                    onChange={setSelectedDeps}
                    options={depOptions}
                    allowClear
                  />
                </Form.Item>
                {metricType === "derived" && (
                  <Form.Item
                    label={<span>挂载实体表（指标的家）<Tag color="blue" style={{ marginLeft: 6 }}>OneData 挂载层</Tag></span>}
                    extra="【通俗理解】这个指标计算出来的结果最终存到哪张物理表？这张表就是指标的“家”（落地/物化表），粒度、统计周期也挂在它身上——不是“原料表”，也不是“消费表”。原子/复合指标不挂载。"
                  >
                    <Row gutter={12}>
                      <Col span={8}>
                        <Form.Item name="mount_source_table" noStyle>
                          <Select
                            showSearch
                            allowClear
                            placeholder="源表（如 dwd.sales_detail；未采集的可输入完整表名）"
                            onSearch={(q) => {
                              setMountSrcTableKw(q);
                              handleSrcTableSearch(q);
                            }}
                            onOpenChange={handleSrcTableDropdown}
                            loading={srcTableSearchLoading}
                            notFoundContent={srcTableSearchLoading ? <Spin size="small" /> : "无匹配表，可手动输入完整表名"}
                            options={withUncollectedOption(mountSrcTableKw, srcTableSearchOptions)}
                            optionRender={tableOptionRender}
                            filterOption={false}
                          />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item name="mount_source_column" noStyle>
                          <Select
                            showSearch
                            allowClear
                            placeholder="度量列（可直接输入列名）"
                            onSearch={setMountColumnKw}
                            options={withUncollectedOption(mountColumnKw, columnOptions)}
                            optionRender={tableOptionRender}
                            notFoundContent={columnOptions.length === 0 ? "未采集列，可直接输入列名" : "无匹配列，可直接输入"}
                            filterOption={(input, option) =>
                              (option?.label as string)?.toLowerCase().includes(input.toLowerCase())
                            }
                          />
                        </Form.Item>
                      </Col>
                      <Col span={5}>
                        <Form.Item name="mount_granularity" noStyle>
                          <Input placeholder="粒度（如 日/月）" maxLength={64} />
                        </Form.Item>
                      </Col>
                      <Col span={5}>
                        <Form.Item name="mount_default_period" noStyle>
                          <Select
                            allowClear
                            placeholder="默认周期"
                            options={PERIOD_OPTIONS}
                          />
                        </Form.Item>
                      </Col>
                    </Row>
                  </Form.Item>
                )}
                </>
              )}
            </Card>
            {renderStepNav()}
            </>
            )}

            {/* Step 2: 治理确认 + 口径定义（OneData 向导） */}
            {currentStep === 2 && (<>
            <Card type="inner" title="③ 确认治理" size="small">
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

              {/* OneData：聚合方式为原子指标核心（始终可见）；其余治理字段收敛为"高级设置"——
                  由域默认值/度量目录/挂载层自动接管；管理/数仓角色默认展开、业务角色默认折叠 */}
              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="aggregation" label={<span>聚合{fieldBadge("aggregation")}</span>} rules={[{ required: true, message: "请选择聚合方式" }]}>
                    {dictSelect("aggregation", "aggregation", "选择聚合方式")}
                  </Form.Item>
                </Col>
              </Row>
              <Collapse
                ghost
                defaultActiveKey={["platform_admin", "domain_admin"].includes(currentRole) ? ["gov"] : []}
                items={[
                  {
                    key: "gov",
                    label: (
                      <span>
                        高级治理设置
                        <Tag style={{ marginLeft: 8 }} color="blue">已由域默认/原子指标口径库自动接管</Tag>
                      </span>
                    ),
                    children: (
                      <>
                        {!isAtomic && (
                          <>
                            <Row gutter={16}>
                              <Col span={8}>
                                <Form.Item name="granularity" label="粒度" extra="缺省取挂载粒度（②挂载配置）">
                                  {dictSelect("granularity", "granularity", "选择粒度")}
                                </Form.Item>
                              </Col>
                              <Col span={8}>
                                <Form.Item name="unit" label="单位" extra="缺省继承依赖指标单位">
                                  {dictSelect("unit", "unit", "选择单位")}
                                </Form.Item>
                              </Col>
                              <Col span={8}>
                                <Form.Item
                                  name="currency"
                                  label="币种（选填）"
                                  extra="ISO 4217 标准币种，仅交易类指标需要"
                                >
                                  {dictSelect("currency", "currency", "选择币种")}
                                </Form.Item>
                              </Col>
                            </Row>
                            <Row gutter={16}>
                              <Col span={8}>
                                <Form.Item name="time_semantics" label="时间语义">
                                  {dictSelect("time_semantics", "time_semantics", "选择时间语义（缺省 PERIOD）")}
                                </Form.Item>
                              </Col>
                              <Col span={8}>
                                <Form.Item name="freshness" label="新鲜度">
                                  {dictSelect("freshness", "freshness", "选择新鲜度（缺省 T1）")}
                                </Form.Item>
                              </Col>
                              <Col span={8}>
                                <Form.Item name="dw_layer" label="数仓层">
                                  {dictSelect("dw_layer", "dw_layer", "选择数仓层（缺省 DWD）")}
                                </Form.Item>
                              </Col>
                            </Row>
                          </>
                        )}
                        <Row gutter={16}>
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
                          <Col span={8}>
                            <Form.Item name="metric_tier" label={<span>分级{fieldBadge("metric_tier")}</span>}>
                              {dictSelect("metric_tier", "metric_tier", "选择分级")}
                            </Form.Item>
                          </Col>
                        </Row>
                        <Row gutter={16}>
                          <Col span={8}>
                            <Form.Item name="pii_flag" label="含 PII" valuePropName="checked">
                              <Checkbox>含 PII</Checkbox>
                            </Form.Item>
                          </Col>
                        </Row>
                      </>
                    ),
                  },
                ]}
              />
              <Collapse
                ghost
                items={[
                  {
                    key: "guide",
                    label: (
                      <span>
                        消费指南（选填）
                        <Tag style={{ marginLeft: 8 }} color={guideDraft ? "green" : "default"}>
                          {guideDraft ? "已填写" : "自动生成"}
                        </Tag>
                      </span>
                    ),
                    children: (
                      <>
                        <Alert
                          type="info"
                          showIcon
                          style={{ marginBottom: 12 }}
                          message="推荐用法/注意事项/关联指标将在指标详情与消费指南页展示；不填写则按指标语义自动生成。"
                        />
                        <ListEditor
                          size="small"
                          label="推荐使用方式"
                          value={guideDraft?.recommended_usage ?? []}
                          onChange={(v) => setGuideDraft((d) => ({ ...(d ?? { cautions: [], related_metrics: [] }), recommended_usage: v }))}
                          placeholder="如：适用 sales 域 daily 粒度分析"
                        />
                        <ListEditor
                          size="small"
                          label="注意事项"
                          value={guideDraft?.cautions ?? []}
                          onChange={(v) => setGuideDraft((d) => ({ ...(d ?? { recommended_usage: [], related_metrics: [] }), cautions: v }))}
                          placeholder="如：该指标包含 PII 数据"
                        />
                        <ListEditor
                          size="small"
                          label="关联指标编码"
                          value={guideDraft?.related_metrics ?? []}
                          onChange={(v) => setGuideDraft((d) => ({ ...(d ?? { recommended_usage: [], cautions: [] }), related_metrics: v }))}
                          placeholder="如：sales_uv_daily"
                        />
                      </>
                    ),
                  },
                ]}
              />
            </Card>

            {/* 关联数据表 */}
            <Card type="inner" title="④ 口径定义" size="small">
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 12 }}
                message="三类表的关系，方向别搞混："
                description={
                  <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.8 }}>
                    <li>
                      <b>依赖表（上游）</b>：这个指标是靠哪些表“加工”出来的？——如
                      <span className="mono" style={{ fontSize: 12 }}> dwd.sales_detail</span>
                      （血缘自动生成 表 → 指标 边）
                    </li>
                    <li>
                      <b>使用表（下游）</b>：哪些表会“消费”这个指标的结果？——如
                      <span className="mono" style={{ fontSize: 12 }}> ads.gmv_report</span>
                      （血缘自动生成 指标 → 表 边）
                    </li>
                    <li>
                      <b>挂载实体表（指标的家）</b>（Step②，仅派生指标）：结果存到哪张物理表？
                      ——区别于上面的“原料”和“客户”。
                    </li>
                  </ul>
                }
              />
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
              <Form.Item
                label="依赖表（上游）"
                extra="加工出这个指标的“原料表”（可多选）——血缘据此生成 表 → 指标 上游边"
              >
                <Select
                  mode="multiple" allowClear showSearch
                  placeholder="展开浏览已接入表，或输入关键词搜索（未采集的可直接录入）"
                  value={sourceTables}
                  onChange={(v: string[]) => setSourceTables(v)}
                  onSearch={(q) => {
                    setDepTableKw(q);
                    searchTables(q);
                  }}
                  onOpenChange={handleTableDropdown}
                  loading={tableSearching}
                  notFoundContent={tableSearching ? <Spin size="small" /> : "无匹配表，可手动输入完整表名"}
                  options={withUncollectedOption(depTableKw, tableOptions)}
                  optionRender={tableOptionRender}
                  filterOption={false}
                />
              </Form.Item>
              <Form.Item
                label="使用表（下游）"
                extra="消费这个指标结果的“客户表”（可多选）——血缘据此生成 指标 → 表 下游边"
              >
                <Select
                  mode="multiple" allowClear showSearch
                  placeholder="展开浏览已接入表，或输入关键词搜索（未采集的可直接录入）"
                  value={downstreamTables}
                  onChange={(v: string[]) => setDownstreamTables(v)}
                  onSearch={(q) => {
                    setDownTableKw(q);
                    searchTables(q);
                  }}
                  onOpenChange={handleTableDropdown}
                  loading={tableSearching}
                  notFoundContent={tableSearching ? <Spin size="small" /> : "无匹配表，可手动输入完整表名"}
                  options={withUncollectedOption(downTableKw, tableOptions)}
                  optionRender={tableOptionRender}
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
                  {isDerivedOrComposite && (
                    <Form.Item
                      label="计算表达式"
                      required
                      extra="引用上方依赖指标编码的计算式（MEL 语法，如 gmv / order_cnt；复合指标如 SUM(region_in_east_gmv) / SUM(total_gmv)）。"
                    >
                      <Input
                        className="mono"
                        placeholder="如 gmv / order_cnt"
                        value={calcExpression}
                        onChange={(e) => setCalcExpression(e.target.value)}
                      />
                    </Form.Item>
                  )}
                  <Form.Item
                    label="关联维度（可选）"
                    extra={
                      isAtomic
                        ? "从平台维度清单选择，将写入口径定义 dimensions；血缘图谱据此生成指标↔维度边。"
                        : "派生/复合指标继承来源指标维度，可在此增补；将写入口径定义 dimensions。"
                    }
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
                    name="definition"
                    label="口径定义 (JSON)"
                    validateStatus={definitionError ? "error" : undefined}
                    help={
                      definitionError ||
                      (isAtomic
                        ? "聚合表达式将基于 源表/度量列/聚合 自动生成；可在此手写 expression 覆盖。"
                        : "结构：expression（计算表达式，已在上方填写）、dependencies（依赖指标）、source_tables（依赖表/上游）、downstream_tables（使用表/下游）、dimensions（维度）。")
                    }
                    extra={
                      <Space size={8}>
                        <Button size="small" onClick={() => {
                          const v = form.getFieldValue("definition");
                          if (!v) { message.info("口径定义为空，无需格式化"); return; }
                          try { form.setFieldValue("definition", JSON.stringify(JSON.parse(String(v)), null, 2)); message.success("已格式化"); }
                          catch { message.error("JSON 格式错误，无法格式化"); }
                        }}>格式化 JSON</Button>
                        <span className="muted" style={{ fontSize: 12 }}>输入时实时校验语法（R5）</span>
                      </Space>
                    }
                  >
                    <TextArea
                      rows={5}
                      placeholder={isAtomic ? '{"expression": "sum(amount)", "source_tables": []}' : '{"expression": "gmv / order_cnt", "dependencies": []}'}
                      className="mono"
                      onChange={(e) => {
                        const v = e.target.value.trim();
                        if (!v) { setDefinitionError(null); return; }
                        try { JSON.parse(v); setDefinitionError(null); }
                        catch { setDefinitionError("口径定义 JSON 语法错误"); }
                      }}
                    />
                  </Form.Item>
                </>
              ) : (
                <Form.Item label="技术口径（源业务库口径）">
                  <TextArea rows={5} value={sqlText} onChange={(e) => setSqlText(e.target.value)} placeholder="SELECT SUM(amount) AS gmv\nFROM catalog.sales.orders" className="mono" />
                  <Paragraph type="secondary" style={{ marginTop: 4, fontSize: 12 }}>后端将用 sqlglot 校验 SQL 语法；不合法将拒绝提交。</Paragraph>
                </Form.Item>
              )}

              <Divider style={{ margin: "8px 0 16px" }} />
              {/* 三层口径（产品文档 §2.2）：业务口径（一句话，四方评审必读）为第一层，
                  独立输入框 → definition_json.definition；与下方伪代码/数仓SQL口径构成完整三层 */}
              <Form.Item
                label="业务口径"
                extra="一句话业务口径（口径定义）——不含表名/物理字段名；四方评审必读字段"
              >
                <Space direction="vertical" style={{ width: "100%" }}>
                  <div style={{ textAlign: "right" }}>
                    <Button
                      size="small"
                      icon={<RobotOutlined />}
                      loading={refiningField === "business"}
                      onClick={() => handleRefineDefinition("business")}
                    >
                      {businessDefinition.trim() ? "AI 丰富增强" : "AI 生成"}
                    </Button>
                  </div>
                  <TextArea
                    rows={2}
                    value={businessDefinition}
                    onChange={(e) => setBusinessDefinition(e.target.value)}
                    placeholder="如：按就诊号去重统计的就诊次数"
                    aria-label="业务口径"
                  />
                </Space>
              </Form.Item>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 12 }}
                message="口径分角色填写（可选）"
                description={
                  <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.8 }}>
                    <li>
                      <b>伪代码口径</b>：由<b>系统开发（技术方）</b>提供——描述“这个指标大致怎么算”，可用伪代码/自然语言，如
                      <span className="mono" style={{ fontSize: 12 }}> SUM(收费金额) WHERE 结算日期 = 当日</span>
                    </li>
                    <li>
                      <b>数仓SQL口径</b>：由<b>数仓开发</b>提供——落地加工的具体 SQL/建模口径，如
                      <span className="mono" style={{ fontSize: 12 }}> SELECT ... FROM dwd.fee_bill_di WHERE ...</span>
                    </li>
                  </ul>
                }
              />
              <Form.Item
                label="伪代码口径（系统开发）"
                extra="技术方（系统开发）提供的口径说明——伪 SQL/自然语言即可，描述“这个指标大致怎么算”"
              >
                <Space direction="vertical" style={{ width: "100%" }}>
                  <div style={{ textAlign: "right" }}>
                    <Button
                      size="small"
                      icon={<RobotOutlined />}
                      loading={refiningField === "pseudo"}
                      onClick={() => handleRefineDefinition("pseudo")}
                    >
                      {pseudoDefinition.trim() ? "AI 优化" : "AI 生成"}
                    </Button>
                  </div>
                  <TextArea
                    rows={3}
                    value={pseudoDefinition}
                    onChange={(e) => setPseudoDefinition(e.target.value)}
                    placeholder="如：SUM(收费金额) 按结算日期，去重就诊，剔除退费"
                    aria-label="伪代码口径"
                    className="mono"
                  />
                </Space>
              </Form.Item>
              <Form.Item
                label="数仓SQL口径"
                extra="数仓开发提供的落地加工口径——具体 SQL 或建模口径（走血缘校验/冲突预检的依据）"
              >
                <Space direction="vertical" style={{ width: "100%" }}>
                  <div style={{ textAlign: "right" }}>
                    <Button
                      size="small"
                      icon={<RobotOutlined />}
                      loading={refiningField === "dw"}
                      onClick={() => handleRefineDefinition("dw")}
                    >
                      {dwDefinition.trim() ? "AI 优化" : "AI 生成"}
                    </Button>
                  </div>
                  <TextArea
                    rows={4}
                    value={dwDefinition}
                    onChange={(e) => setDwDefinition(e.target.value)}
                    placeholder={"如：SELECT visit_date, SUM(real_amount) AS amt\nFROM dwd.fee_bill_di\nWHERE biz_type='outp'\nGROUP BY visit_date"}
                    aria-label="数仓SQL口径"
                    className="mono"
                  />
                </Space>
              </Form.Item>
            </Card>
            {renderStepNav()}
            </>
            )}

            {/* Step 3: 责任方（OneData 向导） */}
            {currentStep === 3 && (<>
            <Card type="inner" title="④ 口径责任方（可选）" size="small">
              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="product_owner" label="产品需求方" extra="口径业务语义提出人">
                    <RoleOwnerSelect users={ownerUsers} placeholder="选择平台用户或输入外部人员" />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="tech_owner" label="技术方" extra="口径 ETL/SQL 实现人">
                    <RoleOwnerSelect users={ownerUsers} placeholder="选择平台用户或输入外部人员" />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="dw_developer" label="数仓开发" extra="数仓建模/血缘维护人">
                    <RoleOwnerSelect users={ownerUsers} placeholder="选择平台用户或输入外部人员" />
                  </Form.Item>
                </Col>
              </Row>
            </Card>
            {renderStepNav()}
            </>)}

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

      {/* OneData 向导：SQL 智能推断收敛为抽屉工具（非主流程步骤，方案 C）；含批量解析模式（FR-010） */}
      <Drawer
        title="SQL 智能推断"
        open={sqlInferOpen}
        onClose={() => setSqlInferOpen(false)}
        width={sqlBatchMode === "batch" ? 760 : 540}
      >
        <Segmented
          block
          size="small"
          value={sqlBatchMode}
          onChange={(v) => setSqlBatchMode(v as "single" | "batch")}
          options={[
            { label: "单条推断", value: "single" },
            { label: "批量解析", value: "batch" },
          ]}
          style={{ marginBottom: 12 }}
        />
        <TextArea
          rows={6}
          value={sqlInferText}
          onChange={(e) => setSqlInferText(e.target.value)}
          placeholder={"单条推断：SELECT SUM(amount) AS gmv FROM dwd.sales_detail GROUP BY dt\n批量解析：粘贴含多个 SELECT 的大段 SQL（支持 ; / CTE / INSERT 切分）"}
          className="mono"
        />
        {sqlBatchMode === "single" ? (
          <>
            <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12 }}>
              面向原子指标：粘贴一段指标定义 SQL（含 SELECT + 聚合 + GROUP BY + 时间过滤），
              系统用 sqlglot 解析并自动推断类型/名称/粒度/单位/聚合/时间语义/新鲜度/数仓层/
              可加性/服务模式/分级，并生成口径定义。推断结果回填到向导各步骤，可确认或覆盖。
              {!selectedDomain && (
                <span style={{ display: "block", marginTop: 6 }}>
                  尚未选择业务域：将先按 SQL 涉及表<b>反向定位业务域</b>（未采集表走 AI 推断），
                  建议域会预填到第 ① 步，可确认或改选。
                </span>
              )}
            </Paragraph>
            {canInferDesc && (
              <Space.Compact block style={{ marginTop: 12 }}>
                <Button
                  type="primary"
                  style={{ flex: 1 }}
                  onClick={() => handleSqlInfer(false)}
                  disabled={!sqlInferText.trim() || sqlInferring}
                  loading={sqlInferring && !sqlInferLlm}
                >
                  智能推断并回填字段
                </Button>
                <Button
                  icon={<RobotOutlined />}
                  style={{ flex: 1 }}
                  onClick={() => handleSqlInfer(true)}
                  disabled={!sqlInferText.trim() || sqlInferring}
                  loading={sqlInferring && sqlInferLlm}
                >
                  LLM 推断并回填字段
                </Button>
              </Space.Compact>
            )}
            {sqlInferLlm && !sqlInferring && (
              <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 6 }}>
                LLM 模式：AI 依据 SQL 语义推断名称/聚合/单位/度量列，枚举字段经系统校验兜底（非法自动回退规则），可到各步骤确认或覆盖。
              </Paragraph>
            )}
            {/* 业务域建议（FR-010 域建议增强）：推断时反向定位/LLM 兜底推断业务域 */}
            {domainSuggesting && (
              <div style={{ marginTop: 12 }}>
                <Spin size="small" /> <Typography.Text type="secondary" style={{ fontSize: 12 }}>正在推断业务域…</Typography.Text>
              </div>
            )}
            {domainSuggestion && !domainSuggesting && (
              <Alert
                type={domainSuggestionStatus === "conflict" ? "warning" : "success"}
                showIcon
                style={{ marginTop: 12 }}
                message={
                  domainSuggestionStatus === "conflict"
                    ? `该 SQL 涉及表主要归属「${domainSuggestion.name}（${domainSuggestion.code}）」域，与当前所选不同`
                    : domainSuggestionStatus === "applied"
                      ? `已按建议选择业务域：${domainSuggestion.name}（${domainSuggestion.code}）`
                      : `SQL 涉及表归属业务域：${domainSuggestion.name}（${domainSuggestion.code}）`
                }
                description={
                  <Space wrap>
                    <span>
                      来源：{SOURCE_META[domainSuggestion.source]?.text ?? domainSuggestion.source} ·
                      置信度 {Math.round((domainSuggestion.confidence || 0) * 100)}%
                    </span>
                    {domainSuggestionStatus === "conflict" && (
                      <Button
                        size="small"
                        type="link"
                        onClick={() => {
                          void applyDomainSuggestion(domainSuggestion);
                        }}
                      >
                        切换为 {domainSuggestion.name}
                      </Button>
                    )}
                  </Space>
                }
              />
            )}
            {domainSuggestionStatus === "none" && !domainSuggesting && (
              <Alert
                type="info"
                showIcon
                style={{ marginTop: 12 }}
                message="未能自动推断业务域（SQL 涉及表未被采集且 AI 不可用），请到第 ① 步手动选择"
              />
            )}
            {inferSummary && (
              <Alert
                type="info"
                showIcon
                style={{ marginTop: 12 }}
                message="已根据 SQL 自动回填字段，可关闭抽屉到各步骤确认或覆盖"
              />
            )}
          </>
        ) : (
          <>
            <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12 }}>
              面向大段 SQL（含多个指标）：按语句切分（支持 ; / CTE/INSERT 语义，规则未生效
              时 AI 兜底），每条语句可拆出多个度量候选；开启「合成复合指标」后多度量语句
              追加一个复合候选（依赖组内原子）。候选可勾选/改名/改聚合后一键批量创建 DRAFT。
            </Paragraph>
            {/* P2-8：切分模式（semicolon/statement/custom）在解析前可配置 */}
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
              <span className="muted" style={{ fontSize: 12 }}>切分模式</span>
              <Select
                size="small"
                style={{ width: 120 }}
                value={sqlBatchSplitMode}
                onChange={(v) => setSqlBatchSplitMode(v)}
                data-testid="sql-batch-split-mode"
                options={[
                  { value: "statement", label: "语义切分" },
                  { value: "semicolon", label: "分号切分" },
                  { value: "custom", label: "自定义规则" },
                ]}
              />
              {sqlBatchSplitMode === "custom" && (
                <>
                  <Input
                    size="small"
                    style={{ width: 170 }}
                    placeholder="分隔符正则(逗号分隔)"
                    value={sqlBatchCustomDelimiters}
                    onChange={(e) => setSqlBatchCustomDelimiters(e.target.value)}
                    allowClear
                  />
                  <Input
                    size="small"
                    style={{ width: 170 }}
                    placeholder="起始标记正则(逗号分隔)"
                    value={sqlBatchCustomMarkers}
                    onChange={(e) => setSqlBatchCustomMarkers(e.target.value)}
                    allowClear
                  />
                </>
              )}
            </div>
            <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
              <Button
                type="primary"
                block
                onClick={() => void handleParseSqlBatch(false)}
                disabled={!sqlInferText.trim() || sqlBatchParsing || sqlBatchLlmParsing}
                loading={sqlBatchParsing}
              >
                解析候选
              </Button>
              <Button
                block
                icon={<RobotOutlined />}
                onClick={() => void handleParseSqlBatch(true)}
                disabled={!sqlInferText.trim() || sqlBatchParsing || sqlBatchLlmParsing}
                loading={sqlBatchLlmParsing}
              >
                LLM 推断并回填字段
              </Button>
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
              LLM 模式：AI 对规则解析出的候选做一次批量补全（中文名/周期/非度量过滤），
              枚举字段系统校验兜底，同一 SQL 每次结果一致
            </div>
            {/* 批量解析结果：域提示 + 合成复合开关 + 候选分组预览 + 批量创建 */}
            {sqlBatchResult && (
              <>
                {!selectedDomain && (
                  <Alert
                    type={domainSuggestionStatus === "none" ? "warning" : "info"}
                    showIcon
                    style={{ marginTop: 12 }}
                    message={
                      domainSuggestionStatus === "none"
                        ? "未能自动推断业务域，请先到第 ① 步选择业务域后再批量创建"
                        : `批量解析域建议：${sqlBatchResult.domain?.name || sqlBatchResult.domain?.code || "已按第 ① 步所选"}（${sqlBatchResult.domain?.status || "user"}）`
                    }
                  />
                )}
                <div
                  style={{
                    marginTop: 12,
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                  }}
                >
                  <span className="muted" style={{ fontSize: 12 }}>
                    共 {sqlBatchResult.candidates.length} 个候选 · 已勾选 {sqlBatchChecked.size} 个
                    {sqlBatchResult.skipped.length > 0 && (
                      <Tooltip
                        title={sqlBatchResult.skipped
                          .map(
                            (s) =>
                              `#${s.index + 1} ${SQL_SKIP_REASON_TEXT[s.reason] || s.reason}`,
                          )
                          .join("\n")}
                      >
                        <span style={{ color: "#faad14", cursor: "help" }}>
                          {" "}
                          · {sqlBatchResult.skipped.length} 条语句跳过
                        </span>
                      </Tooltip>
                    )}
                  </span>
                  <Space size={8}>
                    <span className="muted" style={{ fontSize: 12 }}>合成复合指标</span>
                    <Switch
                      size="small"
                      checked={sqlBatchSynthesize}
                      onChange={(v) => void handleSqlBatchSynthesizeChange(v)}
                    />
                  </Space>
                </div>
                <Collapse
                  style={{ marginTop: 8, maxHeight: 360, overflow: "auto" }}
                  size="small"
                  defaultActiveKey={sqlBatchResult.statements.map((s) => `stmt-${s.index}`)}
                  items={(() => {
                    const byStmt = new Map<number, SqlBatchCandidate[]>();
                    for (const c of sqlBatchResult.candidates) {
                      const arr = byStmt.get(c.statement_index) || [];
                      arr.push(c);
                      byStmt.set(c.statement_index, arr);
                    }
                    return [...byStmt.entries()].map(([idx, cands]) => {
                      const meta = sqlBatchResult.statements.find((s) => s.index === idx);
                      return {
                        key: `stmt-${idx}`,
                        label: `语句 ${idx + 1} · ${meta?.source_tables.join(", ") || "未知源表"} · ${cands.length} 个候选`,
                        children: (
                          <div>
                            {cands.map((c) => (
                              <div
                                key={c.key}
                                style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0", flexWrap: "wrap" }}
                              >
                                <Checkbox
                                  checked={sqlBatchChecked.has(c.key)}
                                  onChange={(e) => handleSqlBatchToggle(c.key, e.target.checked)}
                                  aria-label={`勾选 ${c.name}`}
                                />
                                <Tag color={c.type === "composite" ? "purple" : "blue"}>
                                  {c.type === "composite" ? "复合" : "原子"}
                                </Tag>
                                {/* P2-2：LLM 兜底提取的候选加「AI 推断」标识，与规则层可靠产出
                                    视觉区分——用户可分辨哪些需人工复核（编码/名称/聚合/周期） */}
                                {c.source === "llm" && (
                                  <Tooltip title="该候选由 AI 兜底从 SQL 中推断提取（规则层未能解析出度量），编码/名称/聚合/周期建议人工复核后创建">
                                    <Tag color="gold" style={{ fontSize: 12 }}>AI 推断</Tag>
                                  </Tooltip>
                                )}
                                {/* A-1/2：CASE/窗口/下沉子查询口径候选——expression 保留原始
                                    结构（非简化 SUM(col)），注册后口径不直观，提示人工核对 */}
                                {c.needs_review && (
                                  <Tooltip title="该候选口径含 CASE 条件/窗口函数/子查询下沉，expression 保留原始 SQL 结构——请核对注册后指标口径是否符合预期">
                                    <Tag color="orange" style={{ fontSize: 12 }}>口径需核对</Tag>
                                  </Tooltip>
                                )}
                                {c.type === "atomic" ? (
                                  <>
                                    <Input
                                      size="small"
                                      style={{ width: 150 }}
                                      value={c.name}
                                      onChange={(e) => handleSqlBatchEdit(c.key, { name: e.target.value })}
                                    />
                                    <Select
                                      size="small"
                                      style={{ width: 130 }}
                                      value={c.aggregation || undefined}
                                      onChange={(v) => handleSqlBatchEdit(c.key, { aggregation: v })}
                                      options={AGG_OPTIONS}
                                    />
                                    {/* P2-9：周期可编辑（推断错可行内修正，不必先创建再改） */}
                                    <Select
                                      size="small"
                                      style={{ width: 110 }}
                                      value={c.period || "day"}
                                      onChange={(v) => handleSqlBatchEdit(c.key, { period: v })}
                                      data-testid={`sql-batch-period-${c.key}`}
                                      options={PERIOD_OPTIONS}
                                    />
                                    {/* OneData 接线（P2）：批量候选关联逻辑度量——SQL 无法推断，
                                        前端选择器补全；提交透传 measure_id，批量原子不再游离逻辑
                                        度量体系（对齐单条创建 Step②同款控件） */}
                                    <Select
                                      size="small"
                                      showSearch
                                      allowClear
                                      style={{ width: 160 }}
                                      placeholder="关联逻辑度量"
                                      optionFilterProp="label"
                                      value={c.measure_id ?? undefined}
                                      onChange={(v) => handleSqlBatchEdit(c.key, { measure_id: v ?? null })}
                                      data-testid={`sql-batch-measure-${c.key}`}
                                      options={measureOptions.map((o) => ({ value: o.value, label: o.label }))}
                                    />
                                    {/* P1-2（第六轮）：批量候选行产品需求方——此前批量流全程无法
                                        设置责任方（解析器不产出 owner、候选行无控件），批量产物
                                        OwnerChain 全空；对齐单条 RoleOwnerSelect 的最小形态，仅设
                                        product_owner（tech/dw 随创建人/域默认）。提交透传
                                        product_owner_id，后端 Phase1 落 Metric 三方责任 */}
                                    <Select
                                      size="small"
                                      showSearch
                                      allowClear
                                      style={{ width: 120 }}
                                      placeholder="产品负责"
                                      optionFilterProp="label"
                                      value={c.product_owner_id ?? undefined}
                                      onChange={(v) => handleSqlBatchEdit(c.key, { product_owner_id: v ?? null })}
                                      data-testid={`sql-batch-owner-${c.key}`}
                                      options={ownerUsers.map((u) => ({ value: u.id, label: u.display_name || u.username }))}
                                    />
                                    {/* P2-10：语句级建议域与当前生效域不一致时提示（跨域脚本） */}
                                    {c.suggested_domain_code && c.suggested_domain_code !== selectedDomain && (
                                      <Tooltip title={`该语句表反查建议域为「${c.suggested_domain_code}」，与当前域 ${selectedDomain || "未选"} 不一致；将按当前域创建`}>
                                        <Tag color="orange" style={{ fontSize: 12 }}>建议域 {c.suggested_domain_code}</Tag>
                                      </Tooltip>
                                    )}
                                  </>
                                ) : (
                                  <Typography.Text style={{ fontSize: 12 }}>{c.name}</Typography.Text>
                                )}
                                {/* P0-1：候选编码为空（域未定时后端不 bake-in）→ 提示选域后自动生成 */}
                                <Typography.Text code style={{ fontSize: 12, flex: 1 }}>
                                  {c.metric_code || <span className="muted">（选域后自动生成）</span>}
                                </Typography.Text>
                                {/* 口径溯源（P2）：候选口径表达式创建前即可核对——Tooltip 展示完整
                                    expression（CASE/窗口等原始结构），不必"先创建再改" */}
                                {c.type === "atomic" && c.definition_json?.expression ? (
                                  <Tooltip title={`口径表达式：${String(c.definition_json.expression)}`}>
                                    <Typography.Text
                                      type="secondary"
                                      style={{ fontSize: 12, maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", cursor: "help" }}
                                    >
                                      {String(c.definition_json.expression)}
                                    </Typography.Text>
                                  </Tooltip>
                                ) : null}
                                {c.type === "composite" && (
                                  <Tooltip title="复合指标依赖批内原子（DRAFT）；批量提交评审会被「依赖未发布」拦截，需先发布原子后再提交">
                                    <Tag color="orange">需先发布依赖原子</Tag>
                                  </Tooltip>
                                )}
                              </div>
                            ))}
                          </div>
                        ),
                      };
                    });
                  })()}
                />
                <Button
                  type="primary"
                  block
                  style={{ marginTop: 12 }}
                  loading={sqlBatchCreating}
                  disabled={sqlBatchChecked.size === 0}
                  onClick={() => void handleSqlBatchCreate()}
                >
                  批量创建选中指标（{sqlBatchChecked.size}）
                </Button>
                {/* 批量创建结果（复用 batchResult 分桶展示） */}
                {sqlBatchCreateResult && (
                  <div style={{ marginTop: 16 }}>
                    {(() => {
                      const failed = sqlBatchCreateResult.candidates.filter(
                        (c) => c.status === "VALIDATION_ERROR"
                      ).length;
                      const succeeded = sqlBatchCreateResult.candidates.length - failed;
                      const checkedComposites = sqlBatchResult.candidates.filter(
                        (c) => c.type === "composite" && sqlBatchChecked.has(c.key)
                      );
                      // P1-1：可一键送审的原子 DRAFT（排除复合——依赖原子未发布会被拦截，
                      // 需先逐个发布原子后再对复合发起提交评审）
                      const compositeCodes = new Set(
                        sqlBatchResult.candidates
                          .filter((c) => c.type === "composite")
                          .map((c) => c.metric_code),
                      );
                      const draftAtoms = sqlBatchCreateResult.candidates.filter(
                        (c) => c.status === "DRAFT" && !compositeCodes.has(c.metric_code),
                      );
                      const submitReview = async () => {
                        const codes = draftAtoms.map((c) => c.metric_code);
                        if (codes.length === 0) return;
                        if (batchReviewerType === "user" && batchReviewerId == null) {
                          message.warning("请先选择评审用户，或切换回域评审组");
                          return;
                        }
                        setBatchSubmitLoading(true);
                        try {
                          const res = await batchSubmitMetrics(
                            codes.map((metric_code) => ({
                              code: metric_code,
                              change_reason: "SQL 批量注册后提交评审",
                              reviewer_type: batchReviewerType,
                              reviewer_id: batchReviewerType === "user" ? batchReviewerId : undefined,
                            })),
                          );
                          message.success(`批量提交完成：成功 ${res.ok_count} / 失败 ${res.fail_count}`);
                          handleSqlBatchCreateDone();
                        } catch (err) {
                          message.error(
                            err instanceof UnisenseApiError
                              ? `${err.message}（${err.codeZh}）`
                              : "批量提交失败",
                          );
                        } finally {
                          setBatchSubmitLoading(false);
                        }
                      };
                      return (
                        <>
                          <Alert
                            type={failed > 0 ? "warning" : "success"}
                            showIcon
                            message={`批量创建完成：成功 ${succeeded} / 失败 ${failed}`}
                            description={
                              draftAtoms.length > 0
                                ? `批次号：${sqlBatchCreateResult.batch_id}（${draftAtoms.length} 个原子指标已创建为 DRAFT，可一键提交评审；复合候选需先发布依赖原子）`
                                : `批次号：${sqlBatchCreateResult.batch_id}（成功的指标已创建为 DRAFT 草稿）`
                            }
                          />
                          <Table
                            size="small"
                            rowKey="metric_code"
                            dataSource={sqlBatchCreateResult.candidates}
                            columns={BATCH_RESULT_COLUMNS}
                            pagination={false}
                            scroll={{ y: 280 }}
                            style={{ marginTop: 12 }}
                            locale={{ emptyText: "无创建结果" }}
                          />
                          {checkedComposites.length > 0 && (
                            <Alert
                              type="info"
                              showIcon
                              style={{ marginTop: 8 }}
                              message={`含 ${checkedComposites.length} 个复合候选：依赖的原子指标为 DRAFT，需先逐个发布原子后，再对复合指标发起提交评审`}
                            />
                          )}
                          <Space style={{ marginTop: 12 }} wrap>
                            {/* P1-1：仅重跑失败候选（已成功候选不重复创建，避免「继续注册」
                                全量重跑把已建 DRAFT 再判冲突） */}
                            <Button
                              disabled={sqlBatchRetryFailedKeys.length === 0}
                              loading={sqlBatchCreating}
                              onClick={() => void submitSqlBatch(new Set(sqlBatchRetryFailedKeys))}
                            >
                              重试失败项{sqlBatchRetryFailedKeys.length > 0 ? `（${sqlBatchRetryFailedKeys.length}）` : ""}
                            </Button>
                            {/* P1-1：原子 DRAFT 一键送审（对齐宽表批量弹窗的「批量提交评审」直达，
                                消除「批量注册成功仅提示即结束、需回目录手动勾选提交」的闭环断点） */}
                            <Button
                              type="primary"
                              loading={batchSubmitLoading}
                              disabled={draftAtoms.length === 0}
                              onClick={() => void submitReview()}
                            >
                              批量提交评审
                            </Button>
                            <Button onClick={handleSqlBatchCreateDone}>
                              完成
                            </Button>
                          </Space>
                        </>
                      );
                    })()}
                  </div>
                )}
              </>
            )}
          </>
        )}
      </Drawer>

      {/* 勾选联动提示：取消勾选原子但复合候选仍被勾选时，让用户选择处理方式（FR-010 批量） */}
      <Modal
        title="复合指标依赖该原子"
        open={sqlBatchConflictOpen}
        onCancel={handleSqlBatchRollback}
        onOk={handleSqlBatchSkipComposite}
        okText="跳过复合（同时取消复合勾选）"
        cancelText="回滚勾选（保留该原子）"
      >
        <Paragraph type="secondary" style={{ fontSize: 13 }}>
          该原子指标被一个或多个复合候选依赖。若取消它，复合候选将缺少依赖而无法发布。
          请选择：「跳过复合」同时取消依赖它的复合候选；或「回滚勾选」保留该原子。
        </Paragraph>
      </Modal>

      {/* 业务域多候选选择：跨域共用 DWD 层表时列候选让用户挑（FR-010 域建议增强） */}
      <Modal
        title="选择业务域（SQL 涉及表归属多个域）"
        open={candidateOpen}
        onCancel={() => setCandidateOpen(false)}
        onOk={() => {
          const checked = candidateCandidates.find((c) => c.code === candidateChecked);
          if (checked) void handleCandidateConfirm(checked.code);
        }}
        okText="应用并推断"
        cancelText="取消"
      >
        <Paragraph type="secondary" style={{ fontSize: 12 }}>
          SQL 涉及的表在平台中归属多个业务域（跨域共用 DWD 层表是常态），请选择最贴合的域；
          也可取消后到第 ① 步手动选择。
        </Paragraph>
        <Radio.Group
          style={{ width: "100%" }}
          value={candidateChecked}
          onChange={(e) => setCandidateChecked(e.target.value)}
        >
          <Space direction="vertical" style={{ width: "100%" }}>
            {candidateCandidates.map((c) => (
              <Radio key={c.code} value={c.code}>
                {c.name}（{c.code}）
                <Tag color={SOURCE_META[c.source]?.color} style={{ marginLeft: 6 }}>
                  {SOURCE_META[c.source]?.text ?? c.source} · {Math.round((c.confidence || 0) * 100)}%
                </Tag>
              </Radio>
            ))}
          </Space>
        </Radio.Group>
      </Modal>

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
            {measureSuggestions.length > 0 ? (
              <div style={{ marginBottom: 12 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  推荐逻辑度量（命中已发布原子指标口径库，点击一键应用为原子指标的继承源）：
                </Typography.Text>
                <div style={{ marginTop: 6, display: "flex", flexWrap: "wrap", gap: 8 }}>
                  {measureSuggestions.map((s) => (
                    <Tag
                      key={s.id}
                      color="green"
                      style={{ cursor: "pointer", padding: "4px 10px" }}
                      title={s.reason}
                      onClick={() => applyMeasureSuggestion(s)}
                    >
                      {s.name} ({s.measure_code}) · 匹配度 {Math.round(s.confidence * 100)}%
                    </Tag>
                  ))}
                </div>
              </div>
            ) : null}
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
            {(() => {
              const pm = inferSummary.parsed_measures;
              if (!Array.isArray(pm) || pm.length === 0) return null;
              return (
                <div style={{ marginTop: 12 }}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    SQL 解析出的度量列（{pm.length} 个）——请核对是否真正识别成功：
                  </Typography.Text>
                  <div style={{ marginTop: 6, border: "1px solid #f0f0f0", borderRadius: 6, padding: "4px 10px" }}>
                    {pm.map((m, idx) => (
                      <div
                        key={`${m.alias ?? m.column}-${idx}`}
                        style={{
                          padding: "6px 0",
                          borderBottom: idx < pm.length - 1 ? "1px solid #f0f0f0" : "none",
                        }}
                      >
                        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                          <Typography.Text strong style={{ fontSize: 13 }} className="mono">
                            {m.alias ?? m.column}
                          </Typography.Text>
                          <Tag color="blue">{m.agg}</Tag>
                          {m.column && m.column !== m.alias ? (
                            <Typography.Text type="secondary" style={{ fontSize: 12 }} className="mono">
                              列 {m.column}
                            </Typography.Text>
                          ) : null}
                          {m.table ? (
                            <Typography.Text type="secondary" style={{ fontSize: 12 }} className="mono">
                              表 {m.table}
                            </Typography.Text>
                          ) : null}
                        </div>
                        {m.expression ? (
                          <Typography.Text
                            type="secondary"
                            style={{ fontSize: 12, display: "block", marginTop: 2 }}
                            className="mono"
                          >
                            {m.expression}
                          </Typography.Text>
                        ) : null}
                      </div>
                    ))}
                  </div>
                  {pm.length > 1 ? (
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginTop: 8 }}
                      message={`识别到 ${pm.length} 个度量列：当前回填首个「${pm[0].alias ?? pm[0].column}」为原子指标；如需分别创建多个原子指标，请使用下方「批量解析」模式勾选创建`}
                    />
                  ) : null}
                </div>
              );
            })()}
            {(() => {
              const s = inferSummary as unknown as {
                related_tables?: string[];
                source_tables?: string[];
                downstream_tables?: string[];
              };
              const upstream = Array.isArray(s.source_tables) ? s.source_tables : s.related_tables;
              const downstream = s.downstream_tables;
              if (!upstream?.length && !downstream?.length) return null;
              return (
                <div style={{ marginTop: 12 }}>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    血缘推断关联表（已回填到 Step③ 口径定义）：
                  </Typography.Text>
                  {upstream?.length ? (
                    <div style={{ marginTop: 6 }}>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>依赖表（上游）：</Typography.Text>
                      <div style={{ marginTop: 4 }}>
                        {upstream.map((t) => (
                          <Tag key={t} className="mono" style={{ marginBottom: 4 }}>{t}</Tag>
                        ))}
                      </div>
                    </div>
                  ) : null}
                  {downstream?.length ? (
                    <div style={{ marginTop: 6 }}>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>使用表（下游）：</Typography.Text>
                      <div style={{ marginTop: 4 }}>
                        {downstream.map((t) => (
                          <Tag key={t} className="mono" style={{ marginBottom: 4 }}>{t}</Tag>
                        ))}
                      </div>
                    </div>
                  ) : null}
                </div>
              );
            })()}
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
            {/* 提交评审指派（复审 P2-10）：默认域评审组；选「指定用户」后须选人，否则提交被拦 */}
            <Space style={{ marginTop: 12 }} wrap>
              <span className="muted" style={{ fontSize: 12 }}>提交评审指派</span>
              <Segmented
                size="small"
                value={batchReviewerType}
                onChange={(v) => {
                  setBatchReviewerType(v as "domain" | "user");
                  if (v === "user" && batchUsers.length === 0) {
                    listUsers()
                      .then(setBatchUsers)
                      .catch(() => {});
                  }
                }}
                options={[
                  { label: "域评审组（默认）", value: "domain" },
                  { label: "指定用户", value: "user" },
                ]}
              />
              {batchReviewerType === "user" && (
                <Select
                  size="small"
                  style={{ width: 220 }}
                  placeholder="选择评审用户"
                  showSearch
                  optionFilterProp="label"
                  value={batchReviewerId}
                  onChange={setBatchReviewerId}
                  options={batchUsers.map((u) => ({
                    value: u.id,
                    label: u.display_name ? `${u.display_name}（${u.username}）` : u.username,
                  }))}
                />
              )}
            </Space>
            <Space style={{ marginTop: 16 }}>
              <Button onClick={() => setBatchResult(null)}>继续注册</Button>
              {/* L2：仅重跑失败列（已成功列不重复创建），替代「继续注册」全量重跑——
                  全量重跑会把已建 DRAFT 的列再判为重复冲突变 VALIDATION_ERROR，结果误导 */}
              <Button
                disabled={batchRetryFailed.length === 0}
                onClick={() => {
                  batchForm.setFieldsValue({ measure_columns: batchRetryFailed });
                  void handleBatchSubmit({
                    ...batchForm.getFieldsValue(),
                    measure_columns: batchRetryFailed,
                  });
                }}
              >
                重试失败项{batchRetryFailed.length > 0 ? `（${batchRetryFailed.length}）` : ""}
              </Button>
              {/* 批量提交直达：把本次成功注册的 DRAFT 指标一键送审（复用原子 /batch-submit，
                  消除「批量注册成功仅弹窗即结束、需回目录手动勾选提交」的闭环断点，复审 D1） */}
              <Button
                type="primary"
                loading={batchSubmitLoading}
                disabled={batchResult.candidates.filter((c) => c.status === "DRAFT").length === 0}
                onClick={async () => {
                  const codes = batchResult.candidates
                    .filter((c) => c.status === "DRAFT")
                    .map((c) => c.metric_code);
                  if (codes.length === 0) return;
                  // 指定用户模式未选人：提示并中止（避免 reviewer_type=user 但无 id 被后端拒绝）
                  if (batchReviewerType === "user" && batchReviewerId == null) {
                    message.warning("请先选择评审用户，或切换回域评审组");
                    return;
                  }
                  setBatchSubmitLoading(true);
                  try {
                    const res = await batchSubmitMetrics(
                      codes.map((metric_code) => ({
                        code: metric_code,
                        change_reason: "批量注册后提交评审",
                        reviewer_type: batchReviewerType,
                        reviewer_id: batchReviewerType === "user" ? batchReviewerId : undefined,
                      })),
                    );
                    message.success(`批量提交完成：成功 ${res.ok_count} / 失败 ${res.fail_count}`);
                    setBatchOpen(false);
                  } catch (err) {
                    message.error(
                      err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量提交失败",
                    );
                  } finally {
                    setBatchSubmitLoading(false);
                  }
                }}
              >
                批量提交评审
              </Button>
              <Button onClick={() => setBatchOpen(false)}>
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
                placeholder="选择或搜索源宽表（已接入的可选；未采集的可输入完整表名）"
                onSearch={(q) => {
                  setBatchSrcTableKw(q);
                  handleSrcTableSearch(q);
                }}
                onChange={handleBatchSrcTableChange}
                onOpenChange={handleSrcTableDropdown}
                loading={srcTableSearchLoading}
                notFoundContent={srcTableSearchLoading ? <Spin size="small" /> : "无匹配表，可手动输入完整表名"}
                options={withUncollectedOption(batchSrcTableKw, srcTableSearchOptions)}
                optionRender={tableOptionRender}
                filterOption={false}
              />
            </Form.Item>
            <Form.Item
              name="measure_columns"
              label="度量列"
              rules={[{ required: true, message: "请至少选择一个度量列" }]}
              extra={batchColumnOptions.length > 0 ? "从该表列中选择（可多选），或输入自定义列名" : "可输入自定义列名（选择已采集源表后自动带出该表列）"}
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
              extra="每行一个：维度名 + 该维度在源表对应的列。维度名将写入指标维度（血缘图据此生成指标↔维度边）；选择源表列后系统按列名自动推断维度名，可修改或搜索平台维度。"
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
                onChange={(colVal) => {
                  // 列名自动推断维度名预填（仅当该行维度名为空，不覆盖用户已填值）
                  if (!colVal) return;
                  const inferred = inferDimFromColumn(String(colVal));
                  if (inferred) {
                    const current = batchForm.getFieldValue(["dimension_mapping_list", name, "dim_name"]);
                    if (!current) batchForm.setFieldValue(["dimension_mapping_list", name, "dim_name"], inferred);
                  }
                }}
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
