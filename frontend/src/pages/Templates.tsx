import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, Cascader, message, Space, Descriptions, Popconfirm, Tooltip, Switch, Divider, Segmented, Alert, Collapse } from "antd";
import type { FormInstance } from "antd";
import { PlusOutlined, ArrowLeftOutlined, HeartOutlined, ReadOutlined, EditOutlined, QuestionCircleOutlined } from "@ant-design/icons";
import {
  listTemplates,
  createMetric,
  instantiateTemplate,
  listFavorites,
  addFavorite,
  removeFavorite,
  listUsers,
  updateTemplateOwner,
  setTemplateActive,
  updateMetricTemplate,
  listDomainTree,
  listDictItems,
  getDomainDefaults,
  listMeasureCatalogs,
  listCatalogs,
  UnisenseApiError,
} from "../api";
import type { MetricCreateRequest, MetricTemplate, MetricType, UserBrief, SubjectDomainTreeNode, MeasureCatalog } from "../types";
import RoleOwnerSelect, { type RoleOwnerValue } from "../components/RoleOwnerSelect";
import { useTracking } from "../hooks/useTracking";
import { usePermission } from "../hooks/usePermission";
import { enumLabel, METRIC_TYPE_LABEL, GRANULARITY_LABEL, AGGREGATION_LABEL, TIME_SEMANTICS_LABEL, FRESHNESS_LABEL, DW_LAYER_LABEL, METRIC_TIER_LABEL } from "../utils/enums";
import { validateMetricCode } from "../utils/metricCode";

// 域树 → Cascader 选项（对齐注册指标页：树形选择，避免手输域编码）
function treeToCascaderOptions(nodes: SubjectDomainTreeNode[]): any[] {
  return nodes.map((n) => ({
    value: n.code,
    label: `${n.name} (${n.code})`,
    children: n.children.length > 0 ? treeToCascaderOptions(n.children) : undefined,
  }));
}

// ===== 挂载实体选项框（源表/列）未采集兜底：与注册指标页同构——
// 平台已采集的表/列作为选项，未采集的（模板可能预设了库外表）允许输入后选中，
// 既满足"选项框选择"，又不破坏既有自由输入能力。=====
type TableSelectOption = { value: string; label: string; uncollected?: boolean };
function withUncollectedOption(q: string, options: TableSelectOption[]): TableSelectOption[] {
  const kw = (q ?? "").trim();
  if (!kw) return options;
  if (options.some((o) => o.value === kw)) return options;
  return [{ value: kw, label: kw, uncollected: true }, ...options];
}
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
  return opt?.label;
}

/** 递归查找叶子域编码的完整路径（根→叶，供 Cascader 预填）。
 * 模板 domain 是叶子码（如 "sales_order"），Cascader value 须为完整路径
 * （["sales", "sales_order"]）——只包单元素会显示空（多级域下）。
 */
function findDomainPath(options: any[], leafCode: string): string[] | undefined {
  for (const opt of options) {
    if (opt.value === leafCode) return [opt.value];
    if (opt.children?.length) {
      const sub = findDomainPath(opt.children, leafCode);
      if (sub) return [opt.value, ...sub];
    }
  }
  return undefined;
}

// 字典项 → Select 选项（对齐注册指标页：粒度/单位等从字典下拉选择，避免手输漂移）
function dictToOptions(items: Array<{ code: string; label: string; status: string }>) {
  return items
    .filter((it) => it.status === "active")
    .map((it) => ({ value: it.code, label: `${it.label} (${it.code})` }));
}

// 必填字段可选清单（value=MetricCreateRequest 字段名，后端实例化校验按 merged 查该名；
// label=中文业务名，仅展示。tags 模式仍可自由输入其他字段名）
const REQUIRED_FIELD_OPTIONS = [
  { value: "metric_code", label: "指标编码" },
  { value: "name", label: "指标名称" },
  { value: "domain", label: "业务域" },
  { value: "type", label: "指标类型" },
  { value: "granularity", label: "统计粒度" },
  { value: "measure_id", label: "逻辑度量" },
  { value: "mount", label: "挂载实体" },
  { value: "unit", label: "单位" },
  { value: "currency", label: "币种" },
  { value: "aggregation", label: "聚合方式" },
  { value: "time_semantics", label: "时间语义" },
  { value: "freshness", label: "新鲜度" },
  { value: "dw_layer", label: "数仓层" },
  { value: "metric_tier", label: "指标分级" },
  { value: "serving_mode", label: "服务模式" },
  { value: "additivity", label: "可加性" },
  { value: "definition_json", label: "口径定义" },
  { value: "pii_flag", label: "PII 标记" },
  { value: "product_owner_id", label: "产品需求方" },
  { value: "tech_owner_id", label: "技术方" },
  { value: "dw_developer_id", label: "数仓开发" },
];

// 必填字段 code → 中文名（展示用；未收录字段原样返回）
const requiredFieldLabel = (v: string) =>
  REQUIRED_FIELD_OPTIONS.find((o) => o.value === v)?.label ?? v;

// 模板作用引导样例：说明「模板 = 标准指标样板，一键实例化」，并给出原子/派生/复合三类参考
const TEMPLATE_SAMPLES: Array<{ tag: string; title: string; preset: string; result: string }> = [
  {
    tag: "原子",
    title: "门诊支付金额（日）",
    preset: "类型=原子指标 · 逻辑度量=支付金额(pay_amt) · 聚合=SUM · 单位=元 · 数仓层=DWS · 口径=SQL 模式",
    result: "实例化得到 DRAFT 原子指标，自动继承逻辑度量的格式/单位/小数位，口径与必填字段按模板预填",
  },
  {
    tag: "派生",
    title: "科室维度支付金额（月）",
    preset: "类型=派生指标 · 粒度=月 · 挂载实体=dws_hosp_pay_month（源表/度量列/默认周期） · 口径=表达式 sum(pay_amount)",
    result: "实例化后自动落 metric_mount 挂载行，并生成「指标↔落地表」血缘边",
  },
  {
    tag: "复合",
    title: "门诊支付金额占比",
    preset: "类型=复合指标 · 必填字段=依赖指标 · 口径=表达式 gmv / total_gmv",
    result: "实例化时若未填依赖/表达式会被拦截，保证复合指标口径完整、可计算",
  },
];

/** 口径模式：后端 definition_json 支持 sql / expression 双形态（schemas.py 校验），
 *  JSON 仅为兜底输入形式——三种模式对齐注册指标页「口径定义模式」交互。 */
type DefMode = "sql" | "expression" | "advanced";

const DEF_MODE_OPTIONS = [
  { value: "expression", label: "表达式模式" },
  { value: "sql", label: "SQL 模式" },
  { value: "advanced", label: "高级 JSON" },
];

const DEF_MODE_TAG: Record<DefMode, { label: string; color: string }> = {
  sql: { label: "SQL 模式", color: "geekblue" },
  expression: { label: "表达式模式", color: "cyan" },
  advanced: { label: "高级 JSON", color: "purple" },
};

/** 口径主内容键（切换模式时仅替换这两个键，其余键如 source_tables/dw_definition 保留）。 */
const CALIBER_KEYS = new Set(["sql", "expression"]);

/** 依据口径内容反推展示模式（打开弹窗回填时按 sql / expression 判定，对齐注册页推断回填逻辑）。 */
function detectDefMode(def?: Record<string, unknown> | null): DefMode {
  if (!def) return "expression";
  if (typeof def.sql === "string" && def.sql.trim()) return "sql";
  if (typeof def.expression === "string" && def.expression.trim()) return "expression";
  // 含其他口径键（base_atomic / dw_definition / pseudo_definition 等）→ 高级模式兜底，避免静默丢弃
  return Object.keys(def).length ? "advanced" : "expression";
}

/** 合并口径：清空主口径键后写入当前模式的值（空值不写入，便于彻底清空口径）。 */
function withCaliberBody(def: Record<string, unknown>, body: Record<string, unknown>): Record<string, unknown> {
  const next: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(def)) if (!CALIBER_KEYS.has(k)) next[k] = v;
  for (const [k, v] of Object.entries(body)) if (String(v ?? "").trim()) next[k] = v;
  return next;
}

/** 所选逻辑度量的「统计口径」（stat_caliber）：
 *  OneData 下原子指标的口径由逻辑度量承载，实例化时无需再写一份绑定物理表的 SQL
 *  （另写会绕回「原子绑物理表」）。此处只读预览，供用户确认继承来源。 */
function measureStatCaliber(
  options: Array<{ value: number; measure: MeasureCatalog }>,
  measureId?: number | null,
): string | null {
  if (!measureId) return null;
  return options.find((o) => o.value === measureId)?.measure.stat_caliber ?? null;
}

/** 口径模式编辑器（受控）：SQL 模式写 {sql}，表达式模式写 {expression, source_tables}，
 *  高级 JSON 保留原文本域兜底（覆盖 base_atomic / dw_definition 等特殊键）。
 *  onChange(next, error) —— error 非空表示高级模式 JSON 非法，父组件提交前须拦截。 */
function DefinitionModeEditor({
  mode,
  onModeChange,
  value,
  onChange,
}: {
  mode: DefMode;
  onModeChange: (m: DefMode) => void;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>, error?: string | null) => void;
}) {
  const [jsonText, setJsonText] = useState(() => JSON.stringify(value ?? {}, null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);
  // 外部值变化（切换弹窗/清空）时同步高级模式的文本域，避免展示旧内容
  useEffect(() => {
    if (mode === "advanced") return;
    setJsonText(JSON.stringify(value ?? {}, null, 2));
    setJsonError(null);
  }, [mode, value]);

  const str = (k: string) => (typeof value[k] === "string" ? (value[k] as string) : "");
  const tables = (k: string) => (Array.isArray(value[k]) ? (value[k] as string[]) : []);

  const applyJson = (text: string) => {
    setJsonText(text);
    if (!text.trim()) {
      setJsonError(null);
      onChange({}, null);
      return;
    }
    try {
      const parsed = JSON.parse(text);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        const msg = "口径需为 JSON 对象（如 {\"expression\": \"sum(amount)\"}）";
        setJsonError(msg);
        // 错误须回传父组件：否则提交时 editDefError/instDefError 仍为空，非法内容会被静默保存
        onChange(value, msg);
        return;
      }
      setJsonError(null);
      onChange(parsed as Record<string, unknown>, null);
    } catch (e) {
      const msg = `JSON 格式错误：${e instanceof Error ? e.message : String(e)}`;
      setJsonError(msg);
      onChange(value, msg);
    }
  };

  return (
    <div style={{ width: "100%" }}>
      <Segmented
        block
        value={mode}
        onChange={(v) => onModeChange(v as DefMode)}
        options={DEF_MODE_OPTIONS}
      />
      <div style={{ marginTop: 10 }}>
        {mode === "sql" ? (
          <>
            <Input.TextArea
              rows={4}
              className="mono"
              value={str("sql")}
              maxLength={16384}
              placeholder={"select\n  sum(pay_amount) as pay_amt\nfrom dwd_order_di\nwhere dt = '${bizdate}'\ngroup by hosp_code"}
              onChange={(e) => onChange(withCaliberBody(value, { sql: e.target.value }))}
            />
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              保存时服务端用 sqlglot 校验语法（非法 SQL 会被拒绝），长度上限 16384 字符
            </div>
          </>
        ) : mode === "expression" ? (
          <Space direction="vertical" style={{ width: "100%" }} size={8}>
            <Input
              className="mono"
              value={str("expression")}
              placeholder="聚合/计算表达式，如 sum(pay_amount) 或 gmv / order_cnt"
              onChange={(e) => onChange(withCaliberBody(value, { expression: e.target.value }))}
            />
            <Select
              mode="tags"
              style={{ width: "100%" }}
              value={tables("source_tables")}
              placeholder="源表（可选）：输入表名后回车，如 dwd_order_di"
              onChange={(v: string[]) => {
                if (!v?.length) {
                  const { source_tables: _drop, ...rest } = value;
                  onChange(rest);
                  return;
                }
                onChange({ ...value, source_tables: v });
              }}
            />
          </Space>
        ) : (
          <>
            <Input.TextArea
              rows={4}
              className="mono"
              value={jsonText}
              placeholder='{"expression": "sum(pay_amount)", "source_tables": ["dwd_order_di"], "base_atomic": "active_doctor_daily"}'
              onChange={(e) => applyJson(e.target.value)}
            />
            <Space size={8} style={{ marginTop: 4 }}>
              <Button
                size="small"
                onClick={() => {
                  try {
                    setJsonText(JSON.stringify(JSON.parse(jsonText), null, 2));
                    setJsonError(null);
                  } catch {
                    message.error("JSON 格式错误，无法格式化");
                  }
                }}
              >
                格式化 JSON
              </Button>
              {jsonError ? <span style={{ color: "#cf1322", fontSize: 12 }}>{jsonError}</span> : null}
            </Space>
          </>
        )}
      </div>
    </div>
  );
}

export function Templates() {
  const [searchParams] = useSearchParams();
  const { can } = usePermission();
  // URL 直达参数（?kw=）作为初始筛选，避免「先查全量再过滤」的竞态覆盖
  const urlKw = searchParams.get("kw") ?? "";
  // 启用状态下钻（?is_active=，总览仪表「指标模板」资产卡片）作为初始筛选；
  // 默认仅展示启用模板（与原有行为一致），inactive 下钻展示停用模板
  const urlIsActive = searchParams.get("is_active") ?? "";
  // 责任人（Owner）下钻（?owner_id=，总览仪表 Owner 责任分布）
  const urlOwnerId = searchParams.get("owner_id");
  const [items, setItems] = useState<MetricTemplate[]>([]);
  const [keyword, setKeyword] = useState(urlKw);
  // 搜索输入框即时显示值：与过滤值 keyword 分离——输入不打断浏览/不发请求，回车确认才过滤
  const [inputValue, setInputValue] = useState(urlKw);
  const [isActive, setIsActive] = useState<string>(urlIsActive === "inactive" ? "inactive" : "active");
  const [ownerId, setOwnerId] = useState<number | undefined>(
    urlOwnerId && /^\d+$/.test(urlOwnerId) ? Number(urlOwnerId) : undefined,
  );
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [modalOpen, setModalOpen] = useState(false);
  const [instantiateTarget, setInstantiateTarget] = useState<MetricTemplate | null>(null);
  // 实例化弹窗口径（按模式拆分编辑：sql / expression / advanced JSON，避免手搓 JSON 字符串）
  const [instDef, setInstDef] = useState<Record<string, unknown>>({});
  const [instDefMode, setInstDefMode] = useState<DefMode>("expression");
  const [instDefError, setInstDefError] = useState<string | null>(null);
  // 模板收藏（C 层多资产收藏：TEMPLATE）
  const [favCodes, setFavCodes] = useState<Set<string>>(new Set());
  // 责任人人选（模板「负责人」指派下拉）
  const [users, setUsers] = useState<UserBrief[]>([]);
  // 责任方展示：外部人员 name 优先，其次平台用户 id→姓名解析（users 列表），兜底 #id
  const ownerLabel = (id?: number | null, name?: string | null) => {
    if (name) return name;
    if (!id) return null;
    const u = users.find((x) => x.id === id);
    return u ? u.display_name || u.username || `用户 #${id}` : `用户 #${id}`;
  };
  const [form] = Form.useForm();
  // P2-13 模板编辑闭环：编辑弹窗 state + 独立表单（不复用实例化 form，语义分离）
  const [editTpl, setEditTpl] = useState<MetricTemplate | null>(null);
  const [editSaving, setEditSaving] = useState(false);
  const [editForm] = Form.useForm();
  // 编辑弹窗默认口径（与实例化弹窗同构：模式切换 + 结构化编辑）
  const [editDef, setEditDef] = useState<Record<string, unknown>>({});
  const [editDefMode, setEditDefMode] = useState<DefMode>("expression");
  const [editDefError, setEditDefError] = useState<string | null>(null);
  const navigate = useNavigate();
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果
  const loadSeq = useRef(0);
  const { track } = useTracking();
  // 实例化弹窗选项：域树 + 粒度/单位字典（对齐注册指标页惰性选择，避免手输漂移）
  const [domainOptions, setDomainOptions] = useState<any[]>([]);
  const [granularityOptions, setGranularityOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [unitOptions, setUnitOptions] = useState<Array<{ value: string; label: string }>>([]);
  // 挂载实体选项框（源表/列/粒度，实例化+编辑两弹窗共用）：源表从采集目录惰性加载，
  // 选表后带出该表列；粒度用粒度管理字典。未采集值经 withUncollectedOption 兜底可选中。
  const [mountTableOptions, setMountTableOptions] = useState<TableSelectOption[]>([]);
  const [mountColumnOptions, setMountColumnOptions] = useState<TableSelectOption[]>([]);
  const [mountTableSearching, setMountTableSearching] = useState(false);
  const [mountColumnLoading, setMountColumnLoading] = useState(false);
  const [mountTableKw, setMountTableKw] = useState("");
  const [mountColumnKw, setMountColumnKw] = useState("");
  const [mountGranularityKw, setMountGranularityKw] = useState("");
  // OneData 原子层：已发布逻辑度量（度量目录），供原子模板预设/实例化选择（仅 PUBLISHED 可选）
  const [measureOptions, setMeasureOptions] = useState<Array<{ value: number; label: string; measure: MeasureCatalog }>>([]);
  // 模板详情弹窗（默认口径 / 必填字段 / 描述）
  const [detailTpl, setDetailTpl] = useState<MetricTemplate | null>(null);
  // 域 code → 中文名映射（列表「域」列显示中文名，与指标目录一致）
  const [domainMap, setDomainMap] = useState<Record<string, string>>({});

  // 加载域树与字典项，供实例化弹窗选项（惰性选择原则）
  useEffect(() => {
    listDomainTree()
      .then((tree) => {
        setDomainOptions(treeToCascaderOptions(tree));
        const m: Record<string, string> = {};
        const walk = (nodes: SubjectDomainTreeNode[]) => {
          for (const n of nodes) {
            m[n.code] = n.name;
            if (n.children?.length) walk(n.children);
          }
        };
        walk(tree);
        setDomainMap(m);
      })
      .catch(() => {});
    listDictItems("granularity")
      .then((items) => setGranularityOptions(dictToOptions(items)))
      .catch(() => {});
    listDictItems("unit")
      .then((items) => setUnitOptions(dictToOptions(items)))
      .catch(() => {});
    // OneData 原子层：仅已发布逻辑度量可选（度量格式/单位/小数位实例化时继承）
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

  // 支持从全局搜索栏经 ?kw= 直达定位；初始值已由 useState 承接，
  // 此处仅同步「URL 出现新筛选值」的场景，并保留用户手动清空筛选的能力。
  useEffect(() => {
    if (urlKw && urlKw !== keyword) {
      setKeyword(urlKw);
      setInputValue(urlKw);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw]);

  // 响应 URL 启用状态参数变化（总览仪表「指标模板」资产卡片二次下钻）
  useEffect(() => {
    const next = urlIsActive === "inactive" ? "inactive" : "active";
    if (next !== isActive) setIsActive(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlIsActive]);

  // 响应 URL 责任人参数变化（Owner 责任分布二次下钻）；ownerId 在 load 依赖中自动重查
  useEffect(() => {
    if (urlOwnerId && /^\d+$/.test(urlOwnerId) && Number(urlOwnerId) !== ownerId) {
      setOwnerId(Number(urlOwnerId));
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlOwnerId]);

  async function load(overrideKeyword?: string) {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      // 默认仅展示启用模板；inactive 时展示停用模板（总览仪表下钻）
      const res = await listTemplates({
        is_active: isActive !== "inactive",
        keyword: (overrideKeyword ?? keyword) || undefined,
        owner_id: ownerId,
        page,
        page_size: pageSize,
      });
      // 已有更新的请求发起，丢弃本次过时响应（防竞态覆盖）
      if (seq !== loadSeq.current) return;
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      if (seq !== loadSeq.current) return;
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载模板失败");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  // 统一返回上一入口：优先回退浏览器历史（总览资产卡片/全局搜索等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  // 当前用户模板收藏（TEMPLATE）供行内收藏按钮判断
  useEffect(() => {
    listFavorites()
      .then((favs) =>
        setFavCodes(
          new Set(favs.filter((f) => f.asset_type === "TEMPLATE").map((f) => f.asset_id)),
        ),
      )
      .catch(() => {});
  }, []);

  // 模板收藏切换（行内心形）
  // 责任人人选：模板「负责人」指派下拉数据源
  useEffect(() => {
    listUsers()
      .then((u) => setUsers(u))
      .catch(() => {});
  }, []);

  // 指派/解除模板责任人（总览仪表 Owner 责任分布跨资产统计的数据来源）
  async function assignOwner(t: MetricTemplate, ownerId: number | null) {
    try {
      const updated = await updateTemplateOwner(t.id, ownerId);
      setItems((prev) => prev.map((it) => (it.id === updated.id ? updated : it)));
      message.success(ownerId ? "已指派责任人" : "已解除责任人");
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "指派失败",
      );
    }
  }

  // 启用/停用模板（is_active=false 停止新实例化，保留存量模板与列表展示）
  const [activeBusyId, setActiveBusyId] = useState<number | null>(null);
  async function handleToggleActive(t: MetricTemplate) {
    setActiveBusyId(t.id);
    try {
      const next = !t.is_active;
      const updated = await setTemplateActive(t.id, next);
      setItems((prev) => prev.map((it) => (it.id === updated.id ? updated : it)));
      message.success(next ? `已启用模板「${t.name || t.code}」` : `已停用模板「${t.name || t.code}」`);
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败",
      );
    } finally {
      setActiveBusyId(null);
    }
  }

  async function toggleFavorite(t: MetricTemplate) {
    const fav = favCodes.has(t.code);
    try {
      if (fav) {
        await removeFavorite("TEMPLATE", t.code);
        setFavCodes((prev) => {
          const next = new Set(prev);
          next.delete(t.code);
          return next;
        });
        message.success("已取消收藏");
      } else {
        await addFavorite("TEMPLATE", t.code);
        setFavCodes((prev) => new Set(prev).add(t.code));
        message.success("已收藏");
      }
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "收藏操作失败",
      );
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyword, isActive, ownerId, page, pageSize]);

  // 实例化选域后预填域默认值（对齐注册指标页 R8）：模板默认口径优先，仅补空字段；
  // 域默认值是可选项，用户可随时覆盖（惰性设计）
  async function handleInstantiateDomainChange(value: (string | number)[]) {
    const code = value?.length ? String(value[value.length - 1]) : "";
    if (!code) return;
    try {
      const defaults = await getDomainDefaults(code);
      if (!defaults || typeof defaults !== "object") return;
      const prefill: Record<string, string> = {};
      for (const [k, v] of Object.entries(defaults)) {
        if (typeof v !== "string" || !v) continue;
        // 仅补当前为空/未填的字典字段（模板默认值优先，不覆盖）
        const current = form.getFieldValue(k);
        if (current === undefined || current === null || current === "") prefill[k] = v;
      }
      if (Object.keys(prefill).length) form.setFieldsValue(prefill);
    } catch {
      /* 域默认值加载失败不影响实例化（模板默认兜底） */
    }
  }

  async function handleCreate(values: Record<string, unknown>) {
    setLoading(true);
    try {
      // 组装指标基础信息（模板实例化时后端会把模板默认口径与用户覆盖合并）
      const metricType = (String(values.type) as MetricType) ?? "atomic";
      const isAtomic = metricType === "atomic";
      const isDerived = metricType === "derived";
      const domain = Array.isArray(values.domain)
        ? String(values.domain[values.domain.length - 1])
        : String(values.domain);
      const payload: MetricCreateRequest = {
        metric_code: values.metric_code ? String(values.metric_code) : undefined,
        name: String(values.name),
        // 域取自 Cascader 路径叶子（如 ["sales","order"] → "order"），对齐注册指标页 selectedDomain 语义
        domain,
        type: metricType,
        // S7（三轮审查）：原子恒为日粒度——原子 = 逻辑度量 + 基础统计粒度（日），
        // 忽略模板/表单可能预设的非日粒度（原子粒度编辑框已隐藏），防「原子 + 非日粒度」
        granularity: isAtomic ? "day" : String(values.granularity || "day"),
        unit: String(values.unit || ""),
        aggregation: (String(values.aggregation) as MetricCreateRequest["aggregation"]) ?? "SUM",
        time_semantics: (String(values.time_semantics) as MetricCreateRequest["time_semantics"]) ?? "PERIOD",
        freshness: (String(values.freshness) as MetricCreateRequest["freshness"]) ?? "T1",
        dw_layer: (String(values.dw_layer) as MetricCreateRequest["dw_layer"]) ?? "DWS",
        serving_mode: (String(values.serving_mode) as MetricCreateRequest["serving_mode"]) ?? "BATCH_ONLY",
        additivity: (String(values.additivity) as MetricCreateRequest["additivity"]) ?? "ADDITIVE",
        definition_json: {},
      };
      // OneData 原子层（方案A）：原子指标关联逻辑度量（度量格式/单位/小数位实例化时继承）
      if (isAtomic && values.measure_id) {
        payload.measure_id = Number(values.measure_id);
      }
      // OneData 挂载层（方案A）：派生指标挂载实体（源表/列/粒度/周期/域，服务端落 metric_mount）
      if (isDerived) {
        const ms = String(values.mount_source_table ?? "").trim();
        const mc = String(values.mount_source_column ?? "").trim();
        const mg = String(values.mount_granularity ?? "").trim();
        if (ms && mc && mg) {
          payload.mount = {
            source_table: ms,
            source_column: mc,
            granularity: mg,
            default_period: String(values.mount_default_period ?? "") || null,
            domain,
          };
        }
      }
      // 口径三方责任（可选）：平台用户 id 或外部人员名称兜底（RoleOwnerSelect 组合值拆分）
      payload.product_owner_id = (values.product_owner as RoleOwnerValue | undefined)?.id ?? undefined;
      payload.tech_owner_id = (values.tech_owner as RoleOwnerValue | undefined)?.id ?? undefined;
      payload.dw_developer_id = (values.dw_developer as RoleOwnerValue | undefined)?.id ?? undefined;
      payload.product_owner_name = (values.product_owner as RoleOwnerValue | undefined)?.name ?? undefined;
      payload.tech_owner_name = (values.tech_owner as RoleOwnerValue | undefined)?.name ?? undefined;
      payload.dw_developer_name = (values.dw_developer as RoleOwnerValue | undefined)?.name ?? undefined;
      // 口径：弹窗按模式结构化编辑（SQL / 表达式 / 高级 JSON）；
      // 高级模式 JSON 非法已由编辑器拦截提示，留空则回退模板默认口径，避免创建"空心"指标
      if (instDefError) {
        setLoading(false);
        message.error("口径定义格式错误，请修正后再提交");
        return;
      }
      payload.definition_json = Object.keys(instDef).length
        ? instDef
        : ((instantiateTarget?.defaults_json?.definition_json as Record<string, unknown>) ?? {});
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

  // ===== 挂载实体选项框（源表/列/粒度，对齐注册指标页惰性选择 + 未采集兜底）=====
  async function searchMountTables(q: string) {
    setMountTableSearching(true);
    try {
      const res = await listCatalogs({
        entity_type: "TABLE",
        keyword: q.trim() || undefined,
        page_size: 20,
        source_status: "active",
      });
      setMountTableOptions(
        res.items.map((it) => ({
          value: it.entity_name,
          label: it.source_name ? `${it.entity_name}（${it.source_name}）` : it.entity_name,
        }))
      );
    } catch {
      setMountTableOptions([]);
    } finally {
      setMountTableSearching(false);
    }
  }
  function handleMountTableDropdown(open: boolean) {
    if (open && mountTableOptions.length === 0 && !mountTableSearching) {
      void searchMountTables("");
    }
  }
  // 取某表列选项（name + type + comment）；未采集/无 schema 返回空，由 withUncollectedOption 兜底。
  // clearColumn 仅在用户切换源表时置 true（清空旧列），模板回填时置 false（不破坏已回填列）。
  async function loadMountColumns(table: string, targetForm: FormInstance, clearColumn = false) {
    setMountColumnLoading(true);
    setMountColumnKw("");
    try {
      const res = await listCatalogs({ entity_type: "TABLE", keyword: table, page_size: 5, source_status: "active" });
      const catalog = res.items.find((it) => it.entity_name === table);
      const cols: Array<{ name: string; type?: string; comment?: string }> =
        (catalog as any)?.schema_def?.columns || (catalog as any)?.schema_json?.columns || [];
      setMountColumnOptions(
        cols.map((col) => ({
          value: col.name,
          label: col.type ? `${col.name} (${col.type})${col.comment ? " — " + col.comment : ""}` : col.name,
        }))
      );
    } catch {
      setMountColumnOptions([]);
    } finally {
      setMountColumnLoading(false);
      if (clearColumn) targetForm.setFieldValue("mount_source_column", undefined);
    }
  }
  // 用户切换源表：清空已选列并加载该表列（targetForm 区分实例化/编辑弹窗）
  function handleMountSrcTableChange(value: string, targetForm: FormInstance) {
    if (!value) {
      setMountColumnOptions([]);
      setMountColumnKw("");
      targetForm.setFieldValue("mount_source_column", undefined);
      return;
    }
    void loadMountColumns(value, targetForm, true);
  }

  function openInstantiate(tpl: MetricTemplate) {
    setInstantiateTarget(tpl);
    form.resetFields();
    form.setFieldsValue({
      // 模板 code 是模板标识（如 tpl_gmv_daily，3 段），不是指标编码——仅当其为合法
      // 4 段指标编码时才预填，否则留空由系统自动生成（后端 MetricCreateRequest 严格
      // 校验 4 段式，模板 code 直填会 422「当前仅 N 段」）
      metric_code: validateMetricCode(tpl.code) ? undefined : tpl.code,
      name: tpl.name,
      // Cascader 值须为根→叶完整路径数组；模板域为叶子码，须解析为完整路径
      // （多级域下包单元素 [leafCode] 会显示空——findDomainPath 递归补全路径）
      domain: tpl.domain ? findDomainPath(domainOptions, tpl.domain) ?? [tpl.domain] : undefined,
      type: tpl.type ?? "atomic",
      granularity: tpl.granularity ?? "day",
      unit: tpl.unit ?? "",
      aggregation: tpl.aggregation ?? "SUM",
      time_semantics: tpl.time_semantics ?? "PERIOD",
      freshness: tpl.freshness ?? "T1",
      dw_layer: tpl.dw_layer ?? "DWS",
      // OneData 预设（方案A）：原子→逻辑度量；派生→挂载实体；治理与服务模式默认值
      measure_id: tpl.measure_id ?? undefined,
      mount_source_table: tpl.mount?.source_table ?? "",
      mount_source_column: tpl.mount?.source_column ?? "",
      mount_granularity: tpl.mount?.granularity ?? "",
      mount_default_period: tpl.mount?.default_period ?? undefined,
      serving_mode: tpl.serving_mode ?? "BATCH_ONLY",
      additivity: tpl.additivity ?? "ADDITIVE",
      // 口径三方责任预设（模板作者预设的默认责任方，实例化时可改）
      product_owner: tpl.product_owner_id || tpl.product_owner_name
        ? { id: tpl.product_owner_id, name: tpl.product_owner_name }
        : undefined,
      tech_owner: tpl.tech_owner_id || tpl.tech_owner_name
        ? { id: tpl.tech_owner_id, name: tpl.tech_owner_name }
        : undefined,
      dw_developer: tpl.dw_developer_id || tpl.dw_developer_name
        ? { id: tpl.dw_developer_id, name: tpl.dw_developer_name }
        : undefined,
    });
    // 口径预填模板默认（若模板未定义口径则为空，用户可在弹窗内补充——避免实例化出"空心"指标无血缘）；
    // 按内容反推展示模式（SQL / 表达式 / 高级 JSON），对齐注册指标页口径回填
    const def = (tpl.defaults_json?.definition_json as Record<string, unknown>) ?? {};
    setInstDef(def);
    setInstDefMode(detectDefMode(def));
    setInstDefError(null);
    // 模板预设了源表：预加载该表列（不清空回填列），列下拉即可点选/回填可见
    if (tpl.mount?.source_table) {
      void loadMountColumns(tpl.mount.source_table, form, false);
    }
    setModalOpen(true);
  }

  // P2-13 模板编辑闭环：打开编辑弹窗并回填当前值（code 不可改——消费端稳定引用）
  function openEditTpl(tpl: MetricTemplate) {
    setEditTpl(tpl);
    editForm.resetFields();
    editForm.setFieldsValue({
      name: tpl.name,
      domain: tpl.domain ? findDomainPath(domainOptions, tpl.domain) ?? [tpl.domain] : undefined,
      description: tpl.description ?? "",
      type: tpl.type,
      granularity: tpl.granularity,
      unit: tpl.unit,
      aggregation: tpl.aggregation,
      time_semantics: tpl.time_semantics,
      freshness: tpl.freshness,
      dw_layer: tpl.dw_layer,
      serving_mode: tpl.serving_mode,
      additivity: tpl.additivity,
      metric_tier: tpl.metric_tier,
      // OneData 预设（方案A）：逻辑度量/挂载/三方责任回填
      measure_id: tpl.measure_id ?? undefined,
      mount_source_table: tpl.mount?.source_table ?? "",
      mount_source_column: tpl.mount?.source_column ?? "",
      mount_granularity: tpl.mount?.granularity ?? "",
      mount_default_period: tpl.mount?.default_period ?? undefined,
      mount_domain: tpl.mount?.domain ?? tpl.domain,
      product_owner: tpl.product_owner_id || tpl.product_owner_name
        ? { id: tpl.product_owner_id, name: tpl.product_owner_name }
        : undefined,
      tech_owner: tpl.tech_owner_id || tpl.tech_owner_name
        ? { id: tpl.tech_owner_id, name: tpl.tech_owner_name }
        : undefined,
      dw_developer: tpl.dw_developer_id || tpl.dw_developer_name
        ? { id: tpl.dw_developer_id, name: tpl.dw_developer_name }
        : undefined,
      required_fields: tpl.required_fields ?? [],
      owner_id: tpl.owner_id,
      is_active: tpl.is_active,
    });
    // 默认口径：按内容反推模式回填（SQL / 表达式 / 高级 JSON）
    const def = (tpl.defaults_json?.definition_json as Record<string, unknown>) ?? {};
    setEditDef(def);
    setEditDefMode(detectDefMode(def));
    setEditDefError(null);
    // 模板预设了源表：预加载该表列（不清空回填列），列下拉即可点选/回填可见
    if (tpl.mount?.source_table) {
      void loadMountColumns(tpl.mount.source_table, editForm, false);
    }
  }

  // P2-13 提交模板编辑：仅发送实际变更字段（PATCH 语义），成功刷新列表
  async function handleUpdateTpl(values: Record<string, unknown>) {
    if (!editTpl) return;
    setEditSaving(true);
    try {
      const payload: Record<string, unknown> = {};
      // 域 Cascader 值（路径数组）→ 叶子码
      if (values.domain) {
        const path = Array.isArray(values.domain) ? values.domain : [values.domain];
        payload.domain = path[path.length - 1];
      }
      for (const key of [
        "name", "description", "type", "granularity", "unit", "aggregation",
        "time_semantics", "freshness", "dw_layer", "serving_mode", "additivity",
        "metric_tier", "owner_id", "is_active",
      ]) {
        if (values[key] !== undefined && values[key] !== null) payload[key] = values[key];
      }
      // OneData 预设（方案A）：逻辑度量预设局部更新（传 null 清除）
      if (values.measure_id !== undefined) {
        payload.measure_id = values.measure_id ? Number(values.measure_id) : null;
      }
      // OneData 挂载预设：仅当挂载字段被编辑时提交——派生且三项必填齐全 → 落 mount；
      // 否则（清空/切非派生）→ 传 null 清除预设
      if (
        values.mount_source_table !== undefined ||
        values.mount_source_column !== undefined ||
        values.mount_granularity !== undefined ||
        values.mount_default_period !== undefined
      ) {
        const ms = String(values.mount_source_table ?? "").trim();
        const mc = String(values.mount_source_column ?? "").trim();
        const mg = String(values.mount_granularity ?? "").trim();
        if (String(values.type) === "derived" && ms && mc && mg) {
          payload.mount = {
            source_table: ms,
            source_column: mc,
            granularity: mg,
            default_period: String(values.mount_default_period ?? "") || null,
            domain: String(values.mount_domain || editTpl.domain || ""),
          };
        } else {
          payload.mount = null;
        }
      }
      // 口径三方责任预设（RoleOwnerSelect 组合值拆分：id 平台用户 / name 外部人员兜底）
      const ownerKeys: Array<{ formKey: string; idKey: string; nameKey: string }> = [
        { formKey: "product_owner", idKey: "product_owner_id", nameKey: "product_owner_name" },
        { formKey: "tech_owner", idKey: "tech_owner_id", nameKey: "tech_owner_name" },
        { formKey: "dw_developer", idKey: "dw_developer_id", nameKey: "dw_developer_name" },
      ];
      for (const { formKey, idKey, nameKey } of ownerKeys) {
        const v = values[formKey] as RoleOwnerValue | undefined;
        payload[idKey] = v?.id ?? null;
        payload[nameKey] = v?.name ?? null;
      }
      // required_fields（多选 tag）：空数组合法（清空必填约束）
      if (values.required_fields !== undefined) {
        payload.required_fields = Array.isArray(values.required_fields) ? values.required_fields : [];
      }
      // 默认口径：模式化编辑结果写入 defaults_json（保留 defaults_json 其他键）；
      // 清空口径时移除 definition_json 键（而非写空对象）
      if (editDefError) {
        message.error("默认口径格式错误，请检查后重试");
        return;
      }
      if (Object.keys(editDef).length) {
        payload.defaults_json = { ...editTpl.defaults_json, definition_json: editDef };
      } else {
        const { definition_json: _drop, ...rest } = editTpl.defaults_json ?? {};
        payload.defaults_json = rest;
      }
      const updated = await updateMetricTemplate(editTpl.id, payload);
      message.success(`模板「${updated.name}」已更新（v${updated.version}）`);
      track("template_edit", updated.code, "template");
      setEditTpl(null);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "模板更新失败");
    } finally {
      setEditSaving(false);
    }
  }

  // 详情弹窗：默认口径按模式展示——SQL/表达式模式只看该模式内容（不再暴露完整 JSON），
  // 高级模式（含 base_atomic / 数仓口径等特殊键）才展示完整 JSON 原文
  const detailDef = detailTpl?.defaults_json?.definition_json as Record<string, unknown> | undefined;
  const detailDefMode: DefMode = detectDefMode(detailDef);
  const detailDefBody = !detailDef
    ? ""
    : detailDefMode === "sql"
      ? String(detailDef.sql ?? "")
      : detailDefMode === "expression"
        ? String(detailDef.expression ?? "")
        : JSON.stringify(detailDef, null, 2);
  const detailDefTables = Array.isArray(detailDef?.source_tables)
    ? (detailDef?.source_tables as string[])
    : [];

  const columns = [
    { title: "模板编码", dataIndex: "code", key: "code", render: (v: string) => <span className="mono">{v}</span> },
    { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
    {
      title: "状态",
      dataIndex: "is_active",
      key: "is_active",
      width: 120,
      render: (v: boolean, t: MetricTemplate) =>
        can("template:assign-owner") ? (
          <Popconfirm
            title={v ? "停用此模板？" : "启用此模板？"}
            description={v ? "停用后不可再实例化新指标，存量模板保留。" : "启用后可正常实例化。"}
            okText={v ? "停用" : "启用"}
            okButtonProps={{ danger: v, loading: activeBusyId === t.id }}
            onConfirm={() => handleToggleActive(t)}
          >
            {v ? <Tag color="green" style={{ cursor: "pointer" }}>启用</Tag> : <Tag style={{ cursor: "pointer" }}>停用</Tag>}
          </Popconfirm>
        ) : v ? (
          <Tag color="green">启用</Tag>
        ) : (
          <Tag>停用</Tag>
        ),
    },
    { title: "域", dataIndex: "domain", key: "domain", width: 140, render: (v: string) => domainMap[v] ?? v },
    {
      title: "负责人",
      dataIndex: "owner_id",
      key: "owner_id",
      width: 150,
      render: (_: number | null, t: MetricTemplate) => (
        <Select
          size="small"
          style={{ width: 132 }}
          placeholder="未指派"
          value={t.owner_id ?? undefined}
          allowClear
          disabled={!can("template:assign-owner")}
          options={users
            .filter((u) => u.status !== "DISABLED")
            .map((u) => ({ value: u.id, label: u.display_name }))}
          onChange={(next?: number) => assignOwner(t, next ?? null)}
        />
      ),
    },
    { title: "类型", dataIndex: "type", key: "type", width: 100, render: (v: string) => enumLabel(METRIC_TYPE_LABEL, v) },
    { title: "粒度", dataIndex: "granularity", key: "granularity", width: 100, render: (v: string) => enumLabel(GRANULARITY_LABEL, v) },
    { title: "聚合", dataIndex: "aggregation", key: "aggregation", width: 120, render: (v: string) => enumLabel(AGGREGATION_LABEL, v) },
    { title: "时间语义", dataIndex: "time_semantics", key: "time_semantics", width: 110, render: (v: string) => enumLabel(TIME_SEMANTICS_LABEL, v) },
    { title: "新鲜度", dataIndex: "freshness", key: "freshness", width: 90, render: (v: string) => enumLabel(FRESHNESS_LABEL, v) },
    { title: "数仓层", dataIndex: "dw_layer", key: "dw_layer", width: 90, render: (v: string) => enumLabel(DW_LAYER_LABEL, v) },
    { title: "分级", dataIndex: "metric_tier", key: "metric_tier", width: 90, render: (v: string) => <Tag>{enumLabel(METRIC_TIER_LABEL, v)}</Tag> },
    { title: "必填字段", dataIndex: "required_fields", key: "required_fields", render: (v: string[] | null) => (v?.length ? v.map(requiredFieldLabel).join("、") : <span className="muted">—</span>) },
    {
      title: "操作",
      key: "actions",
      width: 140,
      render: (_: unknown, t: MetricTemplate) => (
        <Space size={4} wrap>
          <Button
            type="link"
            icon={<HeartOutlined style={{ color: favCodes.has(t.code) ? "#eb2f96" : undefined }} />}
            onClick={() => toggleFavorite(t)}
          >
            {favCodes.has(t.code) ? "已收藏" : "收藏"}
          </Button>
          <Button type="link" icon={<ReadOutlined />} onClick={() => setDetailTpl(t)}>详情</Button>
          {can("template:assign-owner") && (
            <Button type="link" icon={<EditOutlined />} onClick={() => openEditTpl(t)}>编辑</Button>
          )}
          {can("template:instantiate") && (
            <Tooltip title={t.is_active ? undefined : "模板已停用，暂不可实例化"}>
              <Button type="link" disabled={!t.is_active} onClick={() => openInstantiate(t)}>实例化指标</Button>
            </Tooltip>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">指标资产 / 指标模板</div>
          <h2>指标模板</h2>
          <p>标准化的指标创建模板——一键实例化，默认口径自动合并。</p>
        </div>
        <Button icon={<PlusOutlined />} onClick={() => load()} loading={loading}>刷新</Button>
      </div>

      {/* 模板作用引导 + 参考样例（默认收起，不干扰列表浏览） */}
      <Collapse
        ghost
        style={{ marginBottom: 4 }}
        items={[
          {
            key: "guide",
            label: (
              <span>
                <QuestionCircleOutlined style={{ marginRight: 6 }} />
                指标模板是什么？点击了解作用与参考样例
              </span>
            ),
            children: (
              <Space direction="vertical" size={10} style={{ width: "100%" }}>
                <Alert
                  type="info"
                  showIcon
                  message="模板 = 指标的「样板」，实例化 = 一键克隆出规范指标"
                  description="模板把域内的标准做法沉淀成一套预置值（指标类型 / 粒度 / 聚合 / 单位 / 数仓层 / 分级，外加默认口径与必填字段）。点「实例化指标」会按模板预填好表单，你微调后即可创建出符合规范的 DRAFT 指标，再走正常的评审 → 发布流程。适合把「同域指标口径统一」这件事固化下来复用，也方便新人照着样板快速完成首次注册。"
                />
                <div>
                  <div style={{ fontWeight: 600, marginBottom: 6 }}>参考样例</div>
                  {TEMPLATE_SAMPLES.map((s) => (
                    <Card key={s.title} size="small" style={{ marginBottom: 8 }}>
                      <Space direction="vertical" size={2} style={{ width: "100%" }}>
                        <Space size={8}>
                          <Tag color={s.tag === "原子" ? "blue" : s.tag === "派生" ? "cyan" : "purple"}>
                            {s.tag}指标模板
                          </Tag>
                          <span style={{ fontWeight: 600 }}>{s.title}</span>
                        </Space>
                        <div className="muted" style={{ fontSize: 12 }}>模板预设：{s.preset}</div>
                        <div className="muted" style={{ fontSize: 12 }}>实例化后：{s.result}</div>
                      </Space>
                    </Card>
                  ))}
                </div>
              </Space>
            ),
          },
        ]}
      />

      <Card>
        <div style={{ display: "flex", gap: 12, marginBottom: 12 }}>
          <Input.Search
            placeholder="搜索模板编码 / 名称 / 描述"
            allowClear
            style={{ width: 280 }}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onSearch={() => {
              setKeyword(inputValue);
              setPage(1);
            }}
            onClear={() => {
              setInputValue("");
              setKeyword("");
              setPage(1);
            }}
          />
          <Select
            style={{ width: 130 }}
            value={isActive}
            onChange={(v?: string) => {
              setIsActive(v ?? "active");
              setPage(1);
            }}
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
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50, 100],
            showTotal: (t: number) => `共 ${t} 条`,
            onChange: (p: number, ps: number) => {
              setPage(p);
              setPageSize(ps);
            },
          }}
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
        <Form form={form} layout="vertical" scrollToFirstError onFinish={handleCreate} style={{ marginTop: 8 }}>
          {instantiateTarget?.required_fields?.length ? (
            <div style={{ marginBottom: 12 }}>
              <Tag color="orange">本模板必填字段</Tag>
              <span className="muted">{instantiateTarget.required_fields.map(requiredFieldLabel).join("、")}</span>
            </div>
          ) : null}
          <Space style={{ width: "100%" }} wrap>
            <Form.Item
              name="metric_code"
              label="指标编码"
              rules={[
                {
                  validator: (_r, v) => {
                    const err = validateMetricCode(v);
                    return err ? Promise.reject(new Error(err)) : Promise.resolve();
                  },
                },
              ]}
              extra={
                instantiateTarget ? (
                  validateMetricCode(instantiateTarget.code) ? (
                    <span className="muted" style={{ fontSize: 12 }}>
                      模板编码 {instantiateTarget.code} 非 4 段指标编码，已留空由系统自动生成；如需指定可自行填写
                    </span>
                  ) : (
                    <span className="muted" style={{ fontSize: 12 }}>
                      已预填模板编码，若与现有指标重复请修改（如加业务后缀）
                    </span>
                  )
                ) : (
                  <span className="mono" style={{ color: "#0E7C86" }}>留空则由系统自动生成</span>
                )
              }
              style={{ width: 240 }}
            >
              <Input className="mono" placeholder="留空自动生成" maxLength={64} showCount />
            </Form.Item>
            <Form.Item name="name" label="名称" rules={[{ required: true }, { max: 128, message: "名称最长 128 字符" }]} style={{ width: 260 }}>
              <Input maxLength={128} showCount />
            </Form.Item>
            <Form.Item name="domain" label="业务域" rules={[{ required: true }]} style={{ width: 240 }}>
              <Cascader
                options={domainOptions}
                placeholder="选择业务域（树形）"
                showSearch
                loading={!domainOptions.length}
                allowClear
                onChange={(v) => void handleInstantiateDomainChange(v ?? [])}
              />
            </Form.Item>
            <Form.Item name="type" label="类型" style={{ width: 240 }}>
              <Select options={["atomic", "derived", "composite"].map((v) => ({ value: v, label: METRIC_TYPE_LABEL[v] ?? v }))} />
            </Form.Item>
            {/* OneData 预设（方案A）：按类型条件渲染——原子→逻辑度量；派生→挂载实体 */}
            <Form.Item noStyle shouldUpdate={(prev, cur) => prev.type !== cur.type}>
              {({ getFieldValue }) =>
                getFieldValue("type") === "atomic" ? (
                  <Form.Item
                    name="measure_id"
                    label="逻辑度量（原子指标口径）"
                    extra={
                      <span className="muted" style={{ fontSize: 12 }}>
                        原子指标 = 逻辑度量（原子指标口径）+ 基础统计粒度（日），不绑定业务限定与时间周期；度量格式/单位/小数位实例化时继承
                      </span>
                    }
                    style={{ width: "100%", marginBottom: 8 }}
                  >
                    <Select
                      showSearch
                      allowClear
                      placeholder="选择或搜索逻辑度量（原子指标口径，仅已发布可选，如 支付金额 pay_amt）"
                      optionFilterProp="label"
                      options={measureOptions.map((o) => ({ value: o.value, label: o.label }))}
                    />
                  </Form.Item>
                ) : getFieldValue("type") === "derived" ? (
                  <Form.Item
                    label="挂载实体（指标的家，OneData 挂载层）"
                    style={{ width: "100%", marginBottom: 8 }}
                    extra={
                      <span className="muted" style={{ fontSize: 12 }}>
                        派生指标计算结果的落地表/粒度——服务端自动落 metric_mount 并回填粒度
                      </span>
                    }
                  >
                    <Space wrap>
                      <Form.Item name="mount_source_table" noStyle>
                        <Select
                          showSearch
                          allowClear
                          loading={mountTableSearching}
                          placeholder="选择或搜索源表（已采集表，未采集可输入后选中）"
                          style={{ width: 230 }}
                          onSearch={setMountTableKw}
                          onOpenChange={handleMountTableDropdown}
                          onChange={(v) => handleMountSrcTableChange(v, form)}
                          optionFilterProp="label"
                          options={withUncollectedOption(mountTableKw, mountTableOptions)}
                          optionRender={tableOptionRender}
                          notFoundContent={mountTableSearching ? "搜索中…" : "无匹配表，可直接输入后选中"}
                        />
                      </Form.Item>
                      <Form.Item name="mount_source_column" noStyle>
                        <Select
                          showSearch
                          allowClear
                          loading={mountColumnLoading}
                          placeholder="选择度量列（选源表后自动带出）"
                          style={{ width: 170 }}
                          onSearch={setMountColumnKw}
                          optionFilterProp="label"
                          options={withUncollectedOption(mountColumnKw, mountColumnOptions)}
                          optionRender={tableOptionRender}
                          notFoundContent={mountColumnOptions.length === 0 ? "未采集列，可直接输入列名" : "无匹配列，可直接输入"}
                        />
                      </Form.Item>
                      <Form.Item name="mount_granularity" noStyle>
                        <Select
                          showSearch
                          allowClear
                          placeholder="选择粒度（粒度管理）"
                          style={{ width: 130 }}
                          onSearch={setMountGranularityKw}
                          optionFilterProp="label"
                          options={withUncollectedOption(mountGranularityKw, granularityOptions)}
                          notFoundContent={granularityOptions.length === 0 ? "粒度字典未加载，可直接输入" : "无匹配粒度，可直接输入"}
                        />
                      </Form.Item>
                      <Form.Item name="mount_default_period" noStyle>
                        <Select
                          allowClear
                          placeholder="默认周期"
                          style={{ width: 110 }}
                          options={["day", "week", "month", "quarter", "year"].map((v) => ({ value: v, label: v }))}
                        />
                      </Form.Item>
                    </Space>
                  </Form.Item>
                ) : null
              }
            </Form.Item>
            {/* S7（三轮审查）：原子不渲染粒度——原子 = 逻辑度量 + 基础统计粒度（日），
                粒度/周期归派生与挂载实体层（对齐创建页/编辑弹窗原子不设粒度）。派生/复合
                仍必选粒度（模板预设可继承）。 */}
            <Form.Item
              noStyle
              shouldUpdate={(prev, cur) => prev.type !== cur.type || prev.granularity !== cur.granularity}
            >
              {({ getFieldValue }) =>
                getFieldValue("type") === "atomic" ? null : (
                  <Form.Item name="granularity" label="粒度" rules={[{ required: true, message: "请选择粒度" }]} style={{ width: 240 }}>
                    <Select
                      options={granularityOptions.length ? granularityOptions : undefined}
                      showSearch
                      placeholder={granularityOptions.length ? "选择粒度" : "输入粒度（字典未加载）"}
                      allowClear
                    />
                  </Form.Item>
                )
              }
            </Form.Item>
            <Form.Item name="unit" label="单位" rules={[{ required: true, message: "请选择单位" }]} style={{ width: 240 }}>
              <Select
                options={unitOptions.length ? unitOptions : undefined}
                showSearch
                placeholder={unitOptions.length ? "选择单位" : "输入单位（字典未加载）"}
                allowClear
              />
            </Form.Item>
            <Form.Item name="aggregation" label="聚合" rules={[{ required: true, message: "请选择聚合方式" }]} style={{ width: 240 }}>
              <Select options={["SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "MAX", "MIN", "MEDIAN", "PERCENTILE"].map((v) => ({ value: v, label: AGGREGATION_LABEL[v] ?? v }))} />
            </Form.Item>
            <Form.Item name="time_semantics" label="时间语义" rules={[{ required: true, message: "请选择时间语义" }]} style={{ width: 240 }}>
              <Select options={["PERIOD", "YTD", "TTM", "AVG", "MOM", "YOY"].map((v) => ({ value: v, label: TIME_SEMANTICS_LABEL[v] ?? v }))} />
            </Form.Item>
            <Form.Item name="freshness" label="新鲜度" rules={[{ required: true, message: "请选择新鲜度" }]} style={{ width: 240 }}>
              <Select options={["REALTIME", "T0", "T1", "HOURLY"].map((v) => ({ value: v, label: FRESHNESS_LABEL[v] ?? v }))} />
            </Form.Item>
            <Form.Item name="dw_layer" label="数仓层" rules={[{ required: true, message: "请选择数仓层" }]} style={{ width: 240 }}>
              <Select options={["ODS", "DWD", "DWS", "ADS", "DM"].map((v) => ({ value: v, label: DW_LAYER_LABEL[v] ?? v }))} />
            </Form.Item>
            <Form.Item name="serving_mode" label="服务模式" style={{ width: 240 }}>
              <Select options={["BATCH_ONLY", "REALTIME_ONLY", "BATCH_REALTIME_DUAL"].map((v) => ({ value: v }))} />
            </Form.Item>
            <Form.Item name="additivity" label="可加性" style={{ width: 240 }}>
              <Select options={["ADDITIVE", "SEMI_ADDITIVE", "NON_ADDITIVE"].map((v) => ({ value: v }))} />
            </Form.Item>
            {/* 口径定义：OneData 下原子指标的口径由所选逻辑度量的 stat_caliber 承载，
                再写一份绑定物理表的 SQL 会绕回「原子绑物理表」——原子类型下折叠为高级项
                （默认收起）并只读预览继承来的统计口径，避免与「口径」一词混淆。 */}
            <Form.Item
              noStyle
              shouldUpdate={(prev, cur) => prev.type !== cur.type || prev.measure_id !== cur.measure_id}
            >
              {({ getFieldValue }) => {
                const isAtomicType = getFieldValue("type") === "atomic";
                const caliber = measureStatCaliber(measureOptions, getFieldValue("measure_id"));
                const editor = (
                  <DefinitionModeEditor
                    mode={instDefMode}
                    onModeChange={setInstDefMode}
                    value={instDef}
                    onChange={(next, err) => {
                      setInstDef(next);
                      setInstDefError(err ?? null);
                    }}
                  />
                );
                return (
                  <Form.Item
                    label="口径定义（可留空用模板默认）"
                    style={{ width: "100%", marginBottom: 8 }}
                    extra={
                      isAtomicType ? (
                        <span className="muted" style={{ fontSize: 12 }}>
                          原子指标的口径由所选「逻辑度量（原子指标口径）」继承，<b>一般留空</b>；仅存量旧式指标需补充物理口径
                        </span>
                      ) : undefined
                    }
                  >
                    {isAtomicType ? (
                      <Space direction="vertical" size={8} style={{ width: "100%" }}>
                        {caliber ? (
                          <Alert
                            type="info"
                            showIcon
                            message="已选逻辑度量的统计口径（实例化后自动继承，无需重复填写）"
                            description={<span style={{ whiteSpace: "pre-wrap" }}>{caliber}</span>}
                          />
                        ) : null}
                        <Collapse
                          ghost
                          items={[
                            {
                              key: "def",
                              label: <span className="muted" style={{ fontSize: 12 }}>高级：补充物理口径定义（一般留空）</span>,
                              children: editor,
                            },
                          ]}
                        />
                      </Space>
                    ) : (
                      editor
                    )}
                  </Form.Item>
                );
              }}
            </Form.Item>
            {/* 口径三方责任（可选）：模板预设的默认责任方，实例化时可改 */}
            <Divider plain style={{ margin: "8px 0" }}>口径三方责任（可选，默认沿用模板预设）</Divider>
            <Space wrap>
              <Form.Item name="product_owner" label="产品需求方" extra="口径业务语义提出人" style={{ width: 240, marginBottom: 8 }}>
                <RoleOwnerSelect users={users} placeholder="选择平台用户或输入外部人员" />
              </Form.Item>
              <Form.Item name="tech_owner" label="技术方" extra="口径 ETL/SQL 实现人" style={{ width: 240, marginBottom: 8 }}>
                <RoleOwnerSelect users={users} placeholder="选择平台用户或输入外部人员" />
              </Form.Item>
              <Form.Item name="dw_developer" label="数仓开发" extra="数仓建模/血缘维护人" style={{ width: 240, marginBottom: 8 }}>
                <RoleOwnerSelect users={users} placeholder="选择平台用户或输入外部人员" />
              </Form.Item>
            </Space>
          </Space>
        </Form>
      </Modal>

      {/* 模板详情：描述 / 必填字段 / 默认口径 / 默认属性（数据行已含，无需额外接口） */}
      <Modal
        title={detailTpl ? `模板详情：${detailTpl.name}` : "模板详情"}
        open={!!detailTpl}
        onCancel={() => setDetailTpl(null)}
        footer={<Button onClick={() => setDetailTpl(null)}>关闭</Button>}
        width={620}
        destroyOnHidden
      >
        {detailTpl ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label="模板编码"><span className="mono">{detailTpl.code}</span></Descriptions.Item>
              <Descriptions.Item label="业务域"><span className="mono">{detailTpl.domain}</span></Descriptions.Item>
              <Descriptions.Item label="类型">{enumLabel(METRIC_TYPE_LABEL, detailTpl.type) ?? detailTpl.type}</Descriptions.Item>
              <Descriptions.Item label="粒度">{enumLabel(GRANULARITY_LABEL, detailTpl.granularity) ?? detailTpl.granularity}</Descriptions.Item>
              <Descriptions.Item label="聚合">{enumLabel(AGGREGATION_LABEL, detailTpl.aggregation) ?? detailTpl.aggregation}</Descriptions.Item>
              <Descriptions.Item label="数仓层">{enumLabel(DW_LAYER_LABEL, detailTpl.dw_layer) ?? detailTpl.dw_layer}</Descriptions.Item>
              <Descriptions.Item label="新鲜度">{enumLabel(FRESHNESS_LABEL, detailTpl.freshness) ?? detailTpl.freshness}</Descriptions.Item>
              <Descriptions.Item label="状态">{detailTpl.is_active ? <Tag color="green">启用</Tag> : <Tag>停用</Tag>}</Descriptions.Item>
              <Descriptions.Item label="必填字段" span={2}>
                {detailTpl.required_fields?.length ? detailTpl.required_fields.map(requiredFieldLabel).join("、") : <span className="muted">—</span>}
              </Descriptions.Item>
              {/* OneData 预设（方案A）：逻辑度量 / 挂载实体 / 三方责任 */}
              <Descriptions.Item label="逻辑度量（原子指标口径）预设">
                {detailTpl.measure_id
                  ? measureOptions.find((o) => o.value === detailTpl.measure_id)?.measure.name
                    ? `${measureOptions.find((o) => o.value === detailTpl.measure_id)?.measure.name} (${measureOptions.find((o) => o.value === detailTpl.measure_id)?.measure.measure_code})`
                    : `#${detailTpl.measure_id}`
                  : <span className="muted">—</span>}
              </Descriptions.Item>
              <Descriptions.Item label="挂载实体预设">
                {detailTpl.mount ? `${detailTpl.mount.source_table} / ${detailTpl.mount.source_column} / ${detailTpl.mount.granularity}` : <span className="muted">—</span>}
              </Descriptions.Item>
              <Descriptions.Item label="产品需求方" span={2}>
                {ownerLabel(detailTpl.product_owner_id, detailTpl.product_owner_name) ?? <span className="muted">—</span>}
              </Descriptions.Item>
              <Descriptions.Item label="技术方" span={2}>
                {ownerLabel(detailTpl.tech_owner_id, detailTpl.tech_owner_name) ?? <span className="muted">—</span>}
              </Descriptions.Item>
              <Descriptions.Item label="数仓开发" span={2}>
                {ownerLabel(detailTpl.dw_developer_id, detailTpl.dw_developer_name) ?? <span className="muted">—</span>}
              </Descriptions.Item>
              <Descriptions.Item label="描述" span={2}>
                {detailTpl.description || <span className="muted">—</span>}
              </Descriptions.Item>
            </Descriptions>
            {detailDef && Object.keys(detailDef).length ? (
              <div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>
                  默认口径（实例化时自动合并）
                  <Tag color={DEF_MODE_TAG[detailDefMode].color} style={{ marginLeft: 6 }}>
                    {DEF_MODE_TAG[detailDefMode].label}
                  </Tag>
                </div>
                {detailDefTables.length ? (
                  <div style={{ marginBottom: 4 }}>
                    {detailDefTables.map((t) => (
                      <Tag key={t}>{t}</Tag>
                    ))}
                  </div>
                ) : null}
                <pre className="mono" style={{ fontSize: 12, maxHeight: 200, overflow: "auto", background: "#fafafa", padding: 8, borderRadius: 6, margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word", maxWidth: "100%", boxSizing: "border-box" }}>
                  {detailDefBody}
                </pre>
              </div>
            ) : null}
          </Space>
        ) : null}
      </Modal>

      {/* P2-13 模板编辑：全字段局部更新（code 只读展示——消费端稳定引用，不可改） */}
      <Modal
        title={editTpl ? `编辑模板：${editTpl.code}` : "编辑模板"}
        open={!!editTpl}
        onCancel={() => setEditTpl(null)}
        onOk={() => editForm.submit()}
        okText="保存修改"
        okButtonProps={{ loading: editSaving }}
        confirmLoading={editSaving}
        width={640}
        destroyOnHidden
      >
        <Form form={editForm} layout="vertical" scrollToFirstError onFinish={handleUpdateTpl} style={{ marginTop: 8 }}>
          <Space style={{ width: "100%" }} wrap align="start">
            <Form.Item name="name" label="模板名称" rules={[{ required: true, message: "请填写名称" }, { max: 128, message: "最长 128 字符" }]} style={{ width: 280 }}>
              <Input maxLength={128} showCount />
            </Form.Item>
            <Form.Item name="domain" label="业务域" rules={[{ required: true, message: "请选择业务域" }]} style={{ width: 280 }}>
              <Cascader options={domainOptions} placeholder="选择业务域（树形）" showSearch loading={!domainOptions.length} allowClear />
            </Form.Item>
            <Form.Item name="description" label="模板说明" style={{ width: "100%" }}>
              <Input.TextArea rows={2} maxLength={500} showCount placeholder="模板用途、适用场景说明" />
            </Form.Item>
            <Form.Item name="required_fields" label="必填字段（实例化时强制填写）" style={{ width: "100%" }}>
              <Select
                mode="tags"
                tokenSeparators={[",", "，"]}
                placeholder="选择或输入字段名后回车，如 metric_code、granularity"
                maxTagCount={8}
                options={REQUIRED_FIELD_OPTIONS}
              />
            </Form.Item>
            <Form.Item name="type" label="指标类型预设" style={{ width: 196 }}>
              <Select allowClear options={["atomic", "derived", "composite"].map((v) => ({ value: v, label: METRIC_TYPE_LABEL[v] ?? v }))} placeholder="（不预设）" />
            </Form.Item>
            {/* OneData 预设（方案A）：按类型条件渲染——原子→逻辑度量；派生→挂载实体 */}
            <Form.Item noStyle shouldUpdate={(prev, cur) => prev.type !== cur.type}>
              {({ getFieldValue }) =>
                getFieldValue("type") === "atomic" ? (
                  <Form.Item
                    name="measure_id"
                    label="逻辑度量（原子指标口径）预设"
                    extra={
                      <span className="muted" style={{ fontSize: 12 }}>
                        仅已发布度量可选；实例化时继承度量格式/单位/小数位与统计口径（stat_caliber）
                      </span>
                    }
                    style={{ width: "100%", marginBottom: 8 }}
                  >
                    <Select
                      showSearch
                      allowClear
                      optionFilterProp="label"
                      placeholder="选择逻辑度量（原子指标口径，仅已发布可选）"
                      options={measureOptions.map((o) => ({ value: o.value, label: o.label }))}
                    />
                  </Form.Item>
                ) : getFieldValue("type") === "derived" ? (
                  <Form.Item label="挂载实体预设（OneData 挂载层）" style={{ width: "100%", marginBottom: 8 }}>
                    <Space wrap>
                      <Form.Item name="mount_source_table" noStyle>
                        <Select
                          showSearch
                          allowClear
                          loading={mountTableSearching}
                          placeholder="选择或搜索源表（已采集表，未采集可输入后选中）"
                          style={{ width: 230 }}
                          onSearch={setMountTableKw}
                          onOpenChange={handleMountTableDropdown}
                          onChange={(v) => handleMountSrcTableChange(v, editForm)}
                          optionFilterProp="label"
                          options={withUncollectedOption(mountTableKw, mountTableOptions)}
                          optionRender={tableOptionRender}
                          notFoundContent={mountTableSearching ? "搜索中…" : "无匹配表，可直接输入后选中"}
                        />
                      </Form.Item>
                      <Form.Item name="mount_source_column" noStyle>
                        <Select
                          showSearch
                          allowClear
                          loading={mountColumnLoading}
                          placeholder="选择度量列（选源表后自动带出）"
                          style={{ width: 170 }}
                          onSearch={setMountColumnKw}
                          optionFilterProp="label"
                          options={withUncollectedOption(mountColumnKw, mountColumnOptions)}
                          optionRender={tableOptionRender}
                          notFoundContent={mountColumnOptions.length === 0 ? "未采集列，可直接输入列名" : "无匹配列，可直接输入"}
                        />
                      </Form.Item>
                      <Form.Item name="mount_granularity" noStyle>
                        <Select
                          showSearch
                          allowClear
                          placeholder="选择粒度（粒度管理）"
                          style={{ width: 130 }}
                          onSearch={setMountGranularityKw}
                          optionFilterProp="label"
                          options={withUncollectedOption(mountGranularityKw, granularityOptions)}
                          notFoundContent={granularityOptions.length === 0 ? "粒度字典未加载，可直接输入" : "无匹配粒度，可直接输入"}
                        />
                      </Form.Item>
                      <Form.Item name="mount_default_period" noStyle>
                        <Select
                          allowClear
                          placeholder="默认周期"
                          style={{ width: 110 }}
                          options={["day", "week", "month", "quarter", "year"].map((v) => ({ value: v, label: v }))}
                        />
                      </Form.Item>
                      <Form.Item name="mount_domain" noStyle>
                        <Input placeholder="挂载域（缺省用模板域）" style={{ width: 160 }} maxLength={64} />
                      </Form.Item>
                    </Space>
                  </Form.Item>
                ) : null
              }
            </Form.Item>
            <Form.Item name="granularity" label="粒度预设" style={{ width: 196 }}>
              <Select allowClear options={granularityOptions} showSearch placeholder="（不预设）" />
            </Form.Item>
            <Form.Item name="unit" label="单位预设" style={{ width: 196 }}>
              <Select allowClear options={unitOptions} showSearch placeholder="（不预设）" />
            </Form.Item>
            <Form.Item name="aggregation" label="聚合方式预设" style={{ width: 196 }}>
              <Select allowClear options={["SUM", "AVG", "COUNT", "COUNT_DISTINCT", "LAST_VALUE", "MAX", "MIN", "MEDIAN", "PERCENTILE"].map((v) => ({ value: v, label: AGGREGATION_LABEL[v] ?? v }))} placeholder="（不预设）" />
            </Form.Item>
            <Form.Item name="time_semantics" label="时间语义预设" style={{ width: 196 }}>
              <Select allowClear options={["PERIOD", "YTD", "TTM", "AVG", "MOM", "YOY"].map((v) => ({ value: v, label: TIME_SEMANTICS_LABEL[v] ?? v }))} placeholder="（不预设）" />
            </Form.Item>
            <Form.Item name="freshness" label="新鲜度预设" style={{ width: 196 }}>
              <Select allowClear options={["REALTIME", "T0", "T1", "HOURLY"].map((v) => ({ value: v, label: FRESHNESS_LABEL[v] ?? v }))} placeholder="（不预设）" />
            </Form.Item>
            <Form.Item name="dw_layer" label="数仓层预设" style={{ width: 196 }}>
              <Select allowClear options={["ODS", "DWD", "DWS", "ADS", "DM"].map((v) => ({ value: v, label: DW_LAYER_LABEL[v] ?? v }))} placeholder="（不预设）" />
            </Form.Item>
            <Form.Item name="serving_mode" label="服务模式预设" style={{ width: 196 }}>
              <Select allowClear options={["BATCH_ONLY", "REALTIME_ONLY", "BATCH_REALTIME_DUAL"].map((v) => ({ value: v }))} placeholder="（不预设）" />
            </Form.Item>
            <Form.Item name="additivity" label="可加性预设" style={{ width: 196 }}>
              <Select allowClear options={["ADDITIVE", "NON_ADDITIVE", "SEMI_ADDITIVE"].map((v) => ({ value: v }))} placeholder="（不预设）" />
            </Form.Item>
            <Form.Item name="metric_tier" label="分级预设" style={{ width: 196 }}>
              <Select allowClear options={["T1", "T2", "T3"].map((v) => ({ value: v, label: METRIC_TIER_LABEL[v] ?? v }))} placeholder="（不预设）" />
            </Form.Item>
            <Form.Item name="owner_id" label="负责人" style={{ width: 196 }}>
              <Select
                allowClear
                showSearch
                optionFilterProp="label"
                placeholder="选择负责人"
                options={users.map((u) => ({ value: u.id, label: `${u.display_name ?? u.username}（${u.role}）` }))}
              />
            </Form.Item>
            <Form.Item name="is_active" label="状态" valuePropName="checked" style={{ width: 196 }}>
              <Switch checkedChildren="启用" unCheckedChildren="停用" />
            </Form.Item>
            {/* 默认口径：与实例化弹窗同策略——原子类型下口径由预设逻辑度量的 stat_caliber
                承载，折叠为高级项（默认收起）并只读预览，避免模板再写一份物理口径。 */}
            <Form.Item
              noStyle
              shouldUpdate={(prev, cur) => prev.type !== cur.type || prev.measure_id !== cur.measure_id}
            >
              {({ getFieldValue }) => {
                const isAtomicType = getFieldValue("type") === "atomic";
                const caliber = measureStatCaliber(measureOptions, getFieldValue("measure_id"));
                const editor = (
                  <DefinitionModeEditor
                    mode={editDefMode}
                    onModeChange={setEditDefMode}
                    value={editDef}
                    onChange={(next, err) => {
                      setEditDef(next);
                      setEditDefError(err ?? null);
                    }}
                  />
                );
                return (
                  <Form.Item
                    label="默认口径（实例化时自动合并）"
                    extra={
                      isAtomicType
                        ? "原子模板的口径由预设「逻辑度量（原子指标口径）」继承，一般留空；需定义 base_atomic 等特殊键时展开下方高级项"
                        : "SQL 模式/表达式模式结构化填写；需定义 base_atomic、数仓口径等特殊键时切「高级 JSON」"
                    }
                    style={{ width: "100%" }}
                  >
                    {isAtomicType ? (
                      <Space direction="vertical" size={8} style={{ width: "100%" }}>
                        {caliber ? (
                          <Alert
                            type="info"
                            showIcon
                            message="预设逻辑度量的统计口径（实例化时自动继承）"
                            description={<span style={{ whiteSpace: "pre-wrap" }}>{caliber}</span>}
                          />
                        ) : null}
                        <Collapse
                          ghost
                          items={[
                            {
                              key: "def",
                              label: <span className="muted" style={{ fontSize: 12 }}>高级：补充物理口径定义（一般留空）</span>,
                              children: editor,
                            },
                          ]}
                        />
                      </Space>
                    ) : (
                      editor
                    )}
                  </Form.Item>
                );
              }}
            </Form.Item>
            {/* 口径三方责任预设（可选）：实例化时作为指标默认责任方 */}
            <Divider plain style={{ margin: "8px 0" }}>口径三方责任预设（可选）</Divider>
            <Space wrap>
              <Form.Item name="product_owner" label="产品需求方" extra="口径业务语义提出人" style={{ width: 280, marginBottom: 8 }}>
                <RoleOwnerSelect users={users} placeholder="选择平台用户或输入外部人员" />
              </Form.Item>
              <Form.Item name="tech_owner" label="技术方" extra="口径 ETL/SQL 实现人" style={{ width: 280, marginBottom: 8 }}>
                <RoleOwnerSelect users={users} placeholder="选择平台用户或输入外部人员" />
              </Form.Item>
              <Form.Item name="dw_developer" label="数仓开发" extra="数仓建模/血缘维护人" style={{ width: 280, marginBottom: 8 }}>
                <RoleOwnerSelect users={users} placeholder="选择平台用户或输入外部人员" />
              </Form.Item>
            </Space>
          </Space>
        </Form>
      </Modal>
    </div>
  );
}
