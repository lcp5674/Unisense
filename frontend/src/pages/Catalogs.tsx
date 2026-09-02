import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Space, Alert, Tooltip, Drawer, Empty, Descriptions, Dropdown, Checkbox, Collapse, Popconfirm } from "antd";
import { PlusOutlined, ReloadOutlined, DeleteOutlined, EyeOutlined, SyncOutlined, ArrowLeftOutlined, HeartOutlined, SettingOutlined, ExperimentOutlined } from "@ant-design/icons";
import { listCatalogs, registerCatalog, bulkDeprecateCatalogs, listDataSources, listCatalogDatabases, refreshCatalogEntity, inferColumnDescription, inferDescriptions, updateColumnDescription, listFavorites, addFavorite, removeFavorite, sampleCatalogEntity, fetchSamplingCoverage, UnisenseApiError } from "../api";
import type { SamplingCoverage } from "../api";
import type { DBCatalog, DataSource, SchemaColumn } from "../types";
import { enumLabel, ENTITY_TYPE_LABEL } from "../utils/enums";
import { SchemaTable } from "../components/SchemaTable";
import { DescriptionCoveragePanel, type DescriptionCoveragePanelHandle } from "../components/DescriptionCoveragePanel";
import { useResizableColumns } from "../components/ResizableTable";
import { usePermission } from "../hooks/usePermission";
import { usePersistentPageSize } from "../hooks/usePersistentPageSize";
import { formatCnTime } from "../utils/timeCn";

const SENSITIVITY_LABEL: Record<string, string> = {
  PUBLIC: "公开",
  INTERNAL: "内部",
  CONFIDENTIAL: "机密",
  PII: "PII",
  NEEDS_REVIEW: "待复核",
  UNKNOWN: "未知",
};
const SENSITIVITY_COLOR: Record<string, string> = {
  PUBLIC: "default",
  INTERNAL: "blue",
  CONFIDENTIAL: "orange",
  PII: "red",
  NEEDS_REVIEW: "gold",
  UNKNOWN: "default",
};

/** 列显示开关选项（实体/操作列固定展示，不参与开关）。 */
const COLUMN_OPTIONS = [
  { label: "数据源", value: "source" },
  { label: "字段覆盖", value: "fields" },
  { label: "业务域", value: "domain" },
  { label: "敏感度", value: "sensitivity" },
  { label: "责任人", value: "owner" },
  { label: "最近更新", value: "updated_at" },
];
const ALL_COLUMN_VALUES = COLUMN_OPTIONS.map((o) => o.value);

/** 相对时间（资产「最近更新」列）：刚刚/N 分钟前/N 小时前/N 天前/日期。 */
function formatRelative(iso?: string | null): string {
  if (!iso) return "—";
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return "—";
  const diff = Date.now() - ts;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN");
}

/**
 * 模块级推断去重：退出页面再进入时组件内 loading 会丢失，但该 Map 跨组件实例保留，
 * 进行中的推断未完成时拦截重复点击（后端另有 Redis/进程内 409 幂等兜底）。
 */
const inferInflight = new Map<string, Promise<unknown>>();

/** 若 key 对应推断已在途中则返回 null（拦截）；否则执行并登记，完成时清理。 */
function runInflight<T>(key: string, task: () => Promise<T>): Promise<T> | null {
  if (inferInflight.has(key)) return null;
  const p = task().finally(() => inferInflight.delete(key));
  inferInflight.set(key, p);
  return p;
}

/** 后端 409 LLM_INFER_IN_PROGRESS：已有推断进行中（可能是其它会话/进程触发）。 */
function isInferInProgress(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { code?: string }).code === "LLM_INFER_IN_PROGRESS"
  );
}

export function Catalogs() {
  const navigate = useNavigate();
  // 按钮级权限点：无对应权限时隐藏/禁用按钮（后端强制兜底）
  const { can } = usePermission();
  const canInferCatalog = can("catalog:infer-description");
  const canEditCatalog = can("catalog:edit-description");
  const canDeprecateCatalog = can("catalog:deprecate");
  const canCollectCatalog = can("data-source:collect");
  // 采样是采集能力的子集（补采样本而非重跑采集），复用同一写权限，后端 _WRITE_DEPS 兜底
  const canSampleCatalog = can("data-source:collect");
  const [searchParams] = useSearchParams();
  // URL 直达参数（?kw= / ?source_id=）作为初始筛选，避免「先查全量再过滤」的竞态覆盖
  const urlKw = searchParams.get("kw") ?? "";
  const urlSourceId = searchParams.get("source_id") ?? "";
  // 来源感知返回（?from=变更追踪 / ?from=资产地图）：从资产地图跳入查看采集记录时，
  // 返回按钮精确回到来源 Tab，而非笼统回退浏览器历史（历史回退会丢 AssetMap 内部 Tabs 状态）。
  const urlFrom = searchParams.get("from") ?? "";
  // 目标行高亮（?focus=实体名）：从资产地图「在采集目录中查看」跳入时，定位并高亮该表
  const urlFocus = searchParams.get("focus") ?? "";
  // 敏感级别下钻（?sensitivity=，总览仪表「数据表」资产卡片）作为初始筛选
  const urlSensitivity = searchParams.get("sensitivity") ?? "";
  // 责任人（Owner）下钻（?owner_id=，总览仪表 Owner 责任分布）
  const urlOwnerId = searchParams.get("owner_id");
  const [items, setItems] = useState<DBCatalog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  // F-1（第十一轮）：每页条数持久化（对齐 MetricCatalog/Dimensions 模式）
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.catalogs.pageSize", 20);
  const setPageSize = (ps: number) => onShowSizeChange(0, ps);
  // 列显示开关（DataHub 式：默认全开，实体/操作列固定不可关）
  const [visibleCols, setVisibleCols] = useState<string[]>(ALL_COLUMN_VALUES);
  const [sourceId, setSourceId] = useState(urlSourceId);
  // 源状态筛选默认「活跃源」：已删除源的采集目录默认不展示（设计为追溯保留），
  // 需查看历史采集记录时显式切换「已删除源」。
  const [sourceStatus, setSourceStatus] = useState<"" | "active" | "deleted">("active");
  // 治理面板命令式句柄（方案 D：主列表「刷新」按钮共享刷新治理面板）
  const panelRef = useRef<DescriptionCoveragePanelHandle>(null);
  const [entityType, setEntityType] = useState("");
  const [sensitivity, setSensitivity] = useState(urlSensitivity);
  const [ownerId, setOwnerId] = useState<number | undefined>(
    urlOwnerId && /^\d+$/.test(urlOwnerId) ? Number(urlOwnerId) : undefined,
  );
  const [keyword, setKeyword] = useState(urlKw);
  // C1 搜索防抖：keyword 每击键触发 load effect（含分页查询）——
  // 输入即时更新（inputValue），查询值延迟 350ms 提交并重置页码。
  const [keywordInput, setKeywordInput] = useState(urlKw);
  const keywordRef = useRef(urlKw);
  const searchTimer = useRef<number | null>(null);
  const commitSearchFilters = () => {
    setKeyword(keywordRef.current);
    setPage(1);
  };
  const scheduleSearch = (value: string) => {
    setKeywordInput(value);
    keywordRef.current = value;
    if (searchTimer.current !== null) window.clearTimeout(searchTimer.current);
    searchTimer.current = window.setTimeout(commitSearchFilters, 350);
  };
  // 卸载清理防抖定时器
  useEffect(() => () => {
    if (searchTimer.current !== null) window.clearTimeout(searchTimer.current);
  }, []);
  // 目标行高亮（?focus=）：数据就绪后定位并短暂高亮，定位成功 3 秒后自动清除
  const [focusName, setFocusName] = useState(urlFocus);
  const focusDoneRef = useRef(false);
  useEffect(() => {
    if (!focusName || focusDoneRef.current) return;
    // 列表尚未加载完成：等 items 就绪后再尝试定位（避免首次空列表时锁死，加载慢场景滚动定位失效）
    if (items.length === 0) return;
    const el = document.querySelector<HTMLElement>(`tr[data-row-key*="${focusName}"]`);
    if (el) {
      // 只有真正找到目标行才停止重试
      focusDoneRef.current = true;
      try {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
      } catch {
        // jsdom 等环境未实现 scrollIntoView：滚动降级为 no-op，不影响行高亮
      }
    }
    // 数据就绪后统一启动 3 秒高亮倒计时（找不到也清除，避免 focusName 永驻）
    const clearTimer = window.setTimeout(() => setFocusName(""), 3000);
    return () => window.clearTimeout(clearTimer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusName, items]);
  // 库名筛选（随数据源联动）
  const [database, setDatabase] = useState("");
  const [databases, setDatabases] = useState<string[]>([]);
  const [databasesLoading, setDatabasesLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  // 数据表收藏（C 层多资产收藏：TABLE，以 entity_name 为业务编码）
  const [favNames, setFavNames] = useState<Set<string>>(new Set());
  const [form] = Form.useForm();
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果
  const loadSeq = useRef(0);
  // 数据源选项（登记实体时选择归属数据源，source_id 由系统自动填充，无需手填）
  const [sources, setSources] = useState<DataSource[]>([]);
  const [sourcesLoading, setSourcesLoading] = useState(false);
  // 字段详情抽屉
  const [fieldDrawerOpen, setFieldDrawerOpen] = useState(false);
  const [fieldDrawerCatalog, setFieldDrawerCatalog] = useState<DBCatalog | null>(null);
  const [fieldColumns, setFieldColumns] = useState<SchemaColumn[]>([]);
  const [inferLoading, setInferLoading] = useState(false);
  const [batchInferLoading, setBatchInferLoading] = useState(false);
  // 单表采集刷新
  const [refreshing, setRefreshing] = useState(false);
  // 单表立即采样 loading（不重跑全量采集，只补采脱敏样本并重算 PII）
  const [sampling, setSampling] = useState(false);
  // 采样覆盖率（当前筛选数据源的已采样表/列占比，全部源时为全局）——PII 识别精度可观测性
  const [coverage, setCoverage] = useState<SamplingCoverage | null>(null);
  const [coverageLoading, setCoverageLoading] = useState(false);
  // 行对齐样本视图（一行 = 源库一条真实记录，脱敏值）
  const [sampleRows, setSampleRows] = useState<Record<string, string>[]>([]);

  function openFieldDetail(record: DBCatalog) {
    setFieldDrawerCatalog(record);
    setFieldColumns(parseSchemaColumns(record));
    setSampleRows(parseSampleRows(record));
    setFieldDrawerOpen(true);
  }

  /** 解析 catalog 的 schema_def/schema_json.sample_rows 为行视图样本 */
  function parseSampleRows(catalog: DBCatalog): Record<string, string>[] {
    const schemaDef = (catalog.schema_def ?? (catalog as unknown as { schema_json?: unknown }).schema_json) as
      | Record<string, unknown>
      | undefined;
    const rows = schemaDef?.sample_rows;
    if (!Array.isArray(rows)) return [];
    return rows.filter(
      (r): r is Record<string, string> => typeof r === "object" && r !== null && !Array.isArray(r),
    ) as Record<string, string>[];
  }

  /** 解析 catalog 的 schema_def/schema_json.columns 为 SchemaColumn[] */
  function parseSchemaColumns(catalog: DBCatalog): SchemaColumn[] {
    // 兼容两种字段名：规范契约是 schema_def，但部分端点/历史数据返回 schema_json
    // （FastAPI by_alias 曾把 alias 当输出键）；双读兜底避免字段详情抽屉读不到列。
    const schemaDef = (catalog.schema_def ?? (catalog as unknown as { schema_json?: unknown }).schema_json) as
      | Record<string, unknown>
      | undefined;
    if (!schemaDef || typeof schemaDef !== "object") return [];
    const columns = schemaDef.columns || schemaDef.fields;
    if (!Array.isArray(columns)) return [];
    return columns.map((col: unknown) => {
      if (typeof col === "object" && col !== null) {
        const c = col as Record<string, unknown>;
        return {
          name: String(c.name || c.column || ""),
          type: c.type ? String(c.type) : c.data_type ? String(c.data_type) : undefined,
          comment: c.comment ? String(c.comment) : undefined,
          // 后端已把 column_descriptions 合并进 schema_def.columns[]（推断/人工编辑的描述）
          description: c.description ? String(c.description) : undefined,
          description_source: c.description_source
            ? (c.description_source as SchemaColumn["description_source"])
            : undefined,
          nullable: c.nullable != null ? Boolean(c.nullable) : undefined,
          default: c.default != null ? String(c.default) : undefined,
          // 脱敏样本值（多值列表）+ 采样命中的敏感类别（立即采样/采集采样后才有）；
          // 兼容存量单值字符串：统一转为列表供字段清单多值展示
          sample:
            c.sample != null
              ? Array.isArray(c.sample)
                ? c.sample.map((s) => String(s))
                : [String(c.sample)]
              : undefined,
          sample_rule: c.sample_rule != null ? String(c.sample_rule) : undefined,
        } as SchemaColumn;
      }
      return { name: String(col) } as SchemaColumn;
    });
  }

  /** 刷新字段列表（推断/编辑后更新某字段的描述） */
  function updateFieldDescription(columnName: string, description: string, source: string) {
    setFieldColumns((prev) =>
      prev.map((col) =>
        col.name === columnName
          ? { ...col, description, description_source: source as "manual" | "llm" | "schema" }
          : col,
      ),
    );
  }

  /** 单字段推断 */
  async function handleInfer(col: SchemaColumn) {
    if (!fieldDrawerCatalog) return;
    setInferLoading(true);
    try {
      const catalogId = (fieldDrawerCatalog as DBCatalog & { id?: number }).id ?? 0;
      if (!catalogId) {
        message.warning("该目录实体缺少 ID，无法推断");
        return;
      }
      const key = `column:${catalogId}:${col.name}`;
      const p = runInflight(key, () =>
        inferColumnDescription(catalogId, col.name, {
          entity_name: fieldDrawerCatalog.entity_name,
          column_type: col.type,
        }).then((result) => {
          updateFieldDescription(col.name, result.description, result.source);
          message.success(`字段「${col.name}」推断成功`);
        }),
      );
      if (!p) {
        message.info("该字段的 LLM 推断正在进行中，请稍候");
        return;
      }
      await p;
    } catch (err) {
      if (isInferInProgress(err)) {
        message.info("该字段的 LLM 推断正在进行中，请稍候");
      } else {
        message.error(
          err instanceof UnisenseApiError
            ? `${err.message}（${err.codeZh}）`
            : "LLM 推断暂时不可用，请稍后重试",
        );
      }
    } finally {
      setInferLoading(false);
    }
  }

  /** 批量推断 */
  async function handleBatchInfer() {
    if (!fieldDrawerCatalog) return;
    setBatchInferLoading(true);
    try {
      const catalogId = (fieldDrawerCatalog as DBCatalog & { id?: number }).id ?? 0;
      if (!catalogId) {
        message.warning("该目录实体缺少 ID，无法推断");
        return;
      }
      const key = `batch:${catalogId}`;
      const p = runInflight(key, () =>
        inferDescriptions(catalogId).then((result) => {
          // 更新推断成功的字段
          for (const item of result.inferred) {
            updateFieldDescription(item.column_name, item.description, item.source);
          }
          message.success(
            `推断完成：成功 ${result.inferred.length}，跳过 ${result.skipped.length}，失败 ${result.failed.length}`,
          );
        }),
      );
      if (!p) {
        message.info("该表的批量推断正在进行中，请稍候");
        return;
      }
      await p;
    } catch (err) {
      if (isInferInProgress(err)) {
        message.info("该表的批量推断正在进行中，请稍候");
      } else {
        message.error(
          err instanceof UnisenseApiError
            ? `${err.message}（${err.codeZh}）`
            : "批量推断暂时不可用，请稍后重试",
        );
      }
    } finally {
      setBatchInferLoading(false);
    }
  }

  /** 人工编辑描述 */
  async function handleEdit(col: SchemaColumn, newDesc: string) {
    if (!fieldDrawerCatalog) return;
    const catalogId = (fieldDrawerCatalog as DBCatalog & { id?: number }).id ?? 0;
    if (!catalogId) {
      message.warning("该目录实体缺少 ID，无法编辑");
      return;
    }
    await updateColumnDescription(catalogId, col.name, newDesc);
    updateFieldDescription(col.name, newDesc, "manual");
    message.success(`字段「${col.name}」描述已更新`);
  }

  // 支持从全局搜索栏 / 数据源详情经 ?kw= 或 ?source_id= 直达定位；初始值已由 useState 承接，
  // 此处仅同步「URL 出现新筛选值」的场景，并保留用户手动清空筛选的能力。
  useEffect(() => {
    if (urlKw && urlKw !== keyword) setKeyword(urlKw);
    if (urlSourceId && urlSourceId !== sourceId) setSourceId(urlSourceId);
    if (urlKw || urlSourceId) setPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw, urlSourceId]);

  // 响应 URL 敏感级别参数变化（总览仪表「数据表」资产卡片二次下钻）；sensitivity 在 load 依赖中自动重查
  useEffect(() => {
    if (urlSensitivity && urlSensitivity !== sensitivity) {
      setSensitivity(urlSensitivity);
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlSensitivity]);

  // 响应 URL 责任人参数变化（Owner 责任分布二次下钻）；ownerId 在 load 依赖中自动重查
  useEffect(() => {
    if (urlOwnerId && /^\d+$/.test(urlOwnerId) && Number(urlOwnerId) !== ownerId) {
      setOwnerId(Number(urlOwnerId));
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlOwnerId]);

  async function load() {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listCatalogs({
        source_id: sourceId || undefined,
        entity_type: entityType || undefined,
        sensitivity_level: sensitivity || undefined,
        database: database || undefined,
        keyword: keyword || undefined,
        source_status: sourceStatus || undefined,
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
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  /** 主列表「刷新」同时刷新治理面板（方案 D：共享刷新，去掉面板内重复刷新按钮）。 */
  function handleRefreshAll() {
    load();
    panelRef.current?.reload();
  }

  /** 采样覆盖率：跟随当前数据源筛选（无筛选=全局），采样/采集后调用刷新 */
  async function loadCoverage() {
    setCoverageLoading(true);
    try {
      const cov = await fetchSamplingCoverage(sourceId || undefined);
      setCoverage(cov);
    } catch {
      setCoverage(null);
    } finally {
      setCoverageLoading(false);
    }
  }

  // 来源感知返回：从资产地图（变更追踪/资产地图 Tab）跳入时精确回来源 Tab；
  // 其他入口（总览资产卡片/血缘视图/数据源详情等）回退浏览器历史，无上一页兜底总览仪表
  function handleBack() {
    if (urlFrom === "变更追踪") {
      navigate("/assetmap?tab=changes");
      return;
    }
    if (urlFrom === "资产地图") {
      navigate("/assetmap");
      return;
    }
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }
  const backLabel = urlFrom === "变更追踪" ? "返回变更追踪" : urlFrom === "资产地图" ? "返回资产地图" : "返回";

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, sourceId, sourceStatus, entityType, sensitivity, keyword, database, ownerId]);

  // 采样覆盖率跟随数据源筛选变化（无筛选=全局），首次进入也加载
  useEffect(() => {
    loadCoverage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceId]);

  // 当前用户数据表收藏（TABLE）供行内收藏按钮判断
  useEffect(() => {
    listFavorites()
      .then((favs) =>
        setFavNames(
          new Set(favs.filter((f) => f.asset_type === "TABLE").map((f) => f.asset_id)),
        ),
      )
      .catch(() => {});
  }, []);

  // 数据表收藏切换（行内心形，以 entity_name 为业务编码）
  async function toggleFavorite(record: DBCatalog) {
    const fav = favNames.has(record.entity_name);
    try {
      if (fav) {
        await removeFavorite("TABLE", record.entity_name);
        setFavNames((prev) => {
          const next = new Set(prev);
          next.delete(record.entity_name);
          return next;
        });
        message.success("已取消收藏");
      } else {
        await addFavorite("TABLE", record.entity_name);
        setFavNames((prev) => new Set(prev).add(record.entity_name));
        message.success("已收藏");
      }
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "收藏操作失败",
      );
    }
  }

  // 库名选项随数据源联动：切换数据源/源状态时刷新库名下拉并重置已选库名
  // （source_status 透传后端，避免已删源的库名出现在「活跃源」下拉中，与列表默认筛选对齐）
  async function loadDatabases() {
    setDatabasesLoading(true);
    try {
      setDatabases(await listCatalogDatabases(sourceId || undefined, sourceStatus || undefined));
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载库名失败");
      setDatabases([]);
    } finally {
      setDatabasesLoading(false);
    }
  }

  useEffect(() => {
    loadDatabases();
    setDatabase("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceId, sourceStatus]);

  // 数据源下拉选项（登记实体/筛选共用）：随源状态联动——active 列活跃源、
  // deleted 列已软删源（采集目录追溯保留场景，筛选「已删除源」时也能按名选源）。
  async function loadSources() {
    setSourcesLoading(true);
    try {
      const res = await listDataSources({
        page: 1,
        page_size: 200,
        source_status: sourceStatus || undefined,
      });
      setSources(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载数据源失败");
    } finally {
      setSourcesLoading(false);
    }
  }

  useEffect(() => {
    loadSources();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceStatus]);

  async function handleRegister(values: Record<string, unknown>) {
    try {
      // source_id 由选中的数据源自动填充，系统不再要求手填
      const sid = String(values.source_id);
      await registerCatalog(sid, {
        entity_name: String(values.entity_name),
        entity_type: String(values.entity_type ?? "TABLE"),
        schema_def: {},
        etl_sql: values.etl_sql ? String(values.etl_sql) : null,
      });
      message.success("目录实体已登记");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "登记失败");
    }
  }

  async function handleBulkDeprecate() {
    if (selectedRowKeys.length === 0) return;
    const selected = items.filter((i) => selectedRowKeys.includes(`${i.source_id}-${i.entity_name}`));
    try {
      const res = await bulkDeprecateCatalogs(selected.map((i) => ({ source_id: i.source_id, entity_name: i.entity_name })));
      message.success(`批量废弃成功 ${res.succeeded.length} 项${res.failed.length ? `，失败 ${res.failed.length}` : ""}`);
      // P2（审查修复）：失败项展示逐条明细，不再只给一个失败计数
      if (res.failed.length > 0) {
        Modal.warning({
          title: `批量废弃 ${res.failed.length} 项失败`,
          width: 560,
          content: (
            <div style={{ maxHeight: 320, overflow: "auto" }}>
              {res.failed.map((f, i) => {
                const row = f as Record<string, unknown>;
                const key = row.entity_name ?? row.source_id ?? JSON.stringify(row);
                const reason = typeof row.error === "string" ? row.error : row.reason ?? "";
                return (
                  <div key={i} style={{ padding: "6px 0", borderBottom: "1px dashed var(--line)", fontSize: 13 }}>
                    <div style={{ fontWeight: 500 }}>{String(key)}</div>
                    {reason ? <div style={{ color: "var(--text-tertiary)" }}>{String(reason)}</div> : null}
                  </div>
                );
              })}
            </div>
          ),
        });
      }
      setSelectedRowKeys([]);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量废弃失败");
    }
  }

  /** 只采集当前实体（单表/单实体刷新），成功后重拉最新 schema 回填抽屉 */
  async function handleRefreshEntity() {
    if (!fieldDrawerCatalog) return;
    setRefreshing(true);
    try {
      const res = await refreshCatalogEntity(fieldDrawerCatalog.source_id, fieldDrawerCatalog.entity_name);
      message.success(
        `「${fieldDrawerCatalog.entity_name}」采集完成：${res.columns} 个字段${res.drifted ? "，检测到 Schema 漂移" : ""}`,
      );
      // 重新拉取该实体的最新目录记录（refresh 只返回列数，不回完整 schema）
      const freshList = await listCatalogs({
        source_id: fieldDrawerCatalog.source_id,
        keyword: fieldDrawerCatalog.entity_name,
        page: 1,
        page_size: 100,
      });
      const fresh = freshList.items.find((i) => i.entity_name === fieldDrawerCatalog?.entity_name);
      if (fresh) {
        setFieldDrawerCatalog(fresh);
        setFieldColumns(parseSchemaColumns(fresh));
        setSampleRows(parseSampleRows(fresh));
      } else {
        // 源端已无此表（采集对账后被标记废弃）
        setFieldDrawerOpen(false);
        setFieldDrawerCatalog(null);
        setFieldColumns([]);
        setSampleRows([]);
        message.warning("该实体在源端已不存在，可能已被标记废弃");
      }
      load();
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError
          ? `${err.message}（${err.codeZh}）`
          : "该表采集失败，请确认数据源连接正常后重试",
      );
    } finally {
      setRefreshing(false);
    }
  }

  /** 单表立即采样：不重跑全量采集，只补采脱敏样本并重算字段级 PII 命中 */
  async function handleSampleEntity() {
    if (!fieldDrawerCatalog) return;
    setSampling(true);
    try {
      const res = await sampleCatalogEntity(fieldDrawerCatalog.source_id, fieldDrawerCatalog.entity_name);
      const detail = [
        `采样 ${res.sampled}/${res.columns} 列`,
        res.pii_hits > 0 ? `新增 ${res.pii_hits} 处 PII 命中` : "",
        res.cleared_pii_columns.length > 0 ? `清除误判 ${res.cleared_pii_columns.length} 列` : "",
      ]
        .filter(Boolean)
        .join("，");
      message.success(`「${fieldDrawerCatalog.entity_name}」${detail || "完成"}`);
      // 源端编码乱码告警：GBK→UTF-8 替换残留、信息已在源头丢失，需在 Hive 侧修复后重采
      if (res.mojibake_fields?.length) {
        message.warning(
          `「${fieldDrawerCatalog.entity_name}」检测到源端编码乱码字段：${res.mojibake_fields.join(
            "、"
          )}（GBK→UTF-8 替换，信息已在源头丢失，请在 Hive 侧修复数据/注释后重新采集）`,
          6,
        );
      }
      // 采样会更新 schema_json（sample/sample_rule）与字段级 PII，重拉最新目录记录回填抽屉
      const freshList = await listCatalogs({
        source_id: fieldDrawerCatalog.source_id,
        keyword: fieldDrawerCatalog.entity_name,
        page: 1,
        page_size: 100,
      });
      const fresh = freshList.items.find((i) => i.entity_name === fieldDrawerCatalog?.entity_name);
      if (fresh) {
        setFieldDrawerCatalog(fresh);
        setFieldColumns(parseSchemaColumns(fresh));
        setSampleRows(parseSampleRows(fresh));
      }
      loadCoverage();
      load();
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError
          ? `${err.message}（${err.codeZh}）`
          : "采样失败：请确认数据源已开启采样（配额设置采样行数）且连接正常",
      );
    } finally {
      setSampling(false);
    }
  }

  const columns = [
    {
      title: "实体",
      dataIndex: "entity_name",
      key: "entity_name",
      // 不设 width：tableLayout=fixed 下该列吸收表格剩余宽度，宽屏一屏放下、列间距均匀
      minWidth: 260,
      ellipsis: true,
      render: (v: string, r: DBCatalog) => (
        <div>
          <Space size={4} wrap={false}>
            <span className="mono" style={{ fontWeight: 500 }}>{v}</span>
            <Tag>{enumLabel(ENTITY_TYPE_LABEL, r.entity_type)}</Tag>
            {r.schema_incomplete && <Tag color="warning">schema 缺失</Tag>}
          </Space>
          <div
            className="muted"
            style={{ fontSize: 12, lineHeight: "18px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", width: "100%" }}
          >
            {r.description || (r.domain ? `域：${r.domain}` : "（无表描述，可在字段详情中补全）")}
          </div>
        </div>
      ),
    },
    {
      title: "数据源",
      dataIndex: "source_id",
      key: "source",
      width: 190,
      ellipsis: true,
      render: (v: string, r: DBCatalog) => (
        <Tooltip title={r.source_name && r.source_name !== v ? `${r.source_name}（${v}）` : v}>
          <span className="mono" style={{ fontSize: 12 }}>
            {r.source_name ?? v}
            {r.source_deleted && <Tag color="default" style={{ marginLeft: 6 }}>源已删除</Tag>}
          </span>
        </Tooltip>
      ),
    },
    {
      title: "字段",
      key: "fields",
      width: 130,
      ellipsis: true,
      render: (_: unknown, r: DBCatalog) => {
        if (r.schema_incomplete) return <span className="muted">—</span>;
        const cols = parseSchemaColumns(r);
        if (!cols.length) return <span className="muted">—</span>;
        const described = cols.filter((c) => c.description || c.comment).length;
        const pct = Math.round((described / cols.length) * 100);
        return (
          <span style={{ fontSize: 12, color: pct >= 80 ? "#3f8600" : pct > 0 ? "#d48806" : "#cf1322" }}>
            {cols.length} 字段 · {described} 已描述（{pct}%）
          </span>
        );
      },
    },
    {
      title: "业务域",
      dataIndex: "domain",
      key: "domain",
      width: 110,
      ellipsis: true,
      render: (v: string | null | undefined) =>
        v ? <Tag color="geekblue">{v}</Tag> : <span className="muted">—</span>,
    },
    {
      title: "敏感度",
      dataIndex: "sensitivity_level",
      key: "sensitivity",
      width: 96,
      ellipsis: true,
      render: (v: string) => <Tag color={SENSITIVITY_COLOR[v]}>{SENSITIVITY_LABEL[v] ?? v}</Tag>,
    },
    {
      title: "责任人",
      dataIndex: "owner_id",
      key: "owner",
      width: 100,
      ellipsis: true,
      render: (v: number | null, r: DBCatalog) => (
        <Tooltip title={v != null ? `owner_id=${v}` : undefined}>
          <span>{(r.owner_name ?? v) || <Tag>无</Tag>}</span>
        </Tooltip>
      ),
    },
    {
      title: "最近更新",
      dataIndex: "updated_at",
      key: "updated_at",
      width: 130,
      ellipsis: true,
      render: (v: string | null | undefined) =>
        v ? (
          <Tooltip title={formatCnTime(v)}>
            <span className="muted" style={{ fontSize: 12 }}>{formatRelative(v)}</span>
          </Tooltip>
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      title: "操作",
      key: "action",
      width: 100,
      render: (_: unknown, record: DBCatalog) => (
        <Space size={0}>
          <Tooltip title={favNames.has(record.entity_name) ? "取消收藏" : "收藏"}>
            <Button
              type="text"
              size="small"
              icon={<HeartOutlined style={{ color: favNames.has(record.entity_name) ? "#eb2f96" : undefined }} />}
              onClick={(e) => { e.stopPropagation(); toggleFavorite(record); }}
              aria-label={favNames.has(record.entity_name) ? "取消收藏" : "收藏"}
            />
          </Tooltip>
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            onClick={(e) => { e.stopPropagation(); openFieldDetail(record); }}
          >
            字段详情
          </Button>
        </Space>
      ),
    },
  ];
  // 列显示开关过滤：实体/操作列固定展示，其余按用户开关
  const visibleColumns = columns.filter(
    (c) => c.key === "entity_name" || c.key === "action" || visibleCols.includes(c.key),
  );

  const { columns: resizableColumns, components: resizableComponents } = useResizableColumns<DBCatalog>(
    visibleColumns,
    "unisense:catalogs-col-widths",
  );

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            {backLabel}
          </Button>
          <div className="page-kicker">Collection / Catalog</div>
          <h2>采集目录</h2>
          <p>采集器登记的元数据目录——含敏感度分级与 schema 完整性标记。</p>
        </div>
        {canEditCatalog && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>登记实体</Button>
        )}
      </div>

      {/* 描述缺失治理工作台（TD §12.1）：统计卡可下钻 + 按表列缺失字段数治理表格
          + 治理抽屉（表级/字段级编辑与 LLM 推断）。与资产地图 summary 总览共享同一组件。
          Collapse 可折叠：默认展开，收起后给下方采集目录列表让位（方案 B）。
          治理动作集中在主列表「刷新」按钮共享刷新（方案 D）。 */}
      <Collapse
        defaultActiveKey={["descCoverage"]}
        style={{ marginBottom: 16 }}
        items={[
          {
            key: "descCoverage",
            label: (
              <Space size={8}>
                <span style={{ fontWeight: 500 }}>描述缺失治理</span>
                <span className="muted" style={{ fontSize: 12, fontWeight: 400 }}>
                  治理视角：按表列缺失字段数优先补全表/字段描述（支持 LLM 推断）；
                  与下方采集目录列表数据同源、职责互补
                </span>
              </Space>
            ),
            children: <DescriptionCoveragePanel ref={panelRef} variant="full" />,
          },
        ]}
      />

      <Card
        title={
          <Space size={8}>
            <span>采集目录</span>
            <span className="muted" style={{ fontSize: 12, fontWeight: 400 }}>
              运维视角：浏览 / 检索 / 管理全部采集实体
            </span>
          </Space>
        }
        extra={
          <Button icon={<ReloadOutlined />} onClick={handleRefreshAll} loading={loading}>
            刷新
          </Button>
        }
      >
        <Space style={{ marginBottom: 12 }} wrap>
          <Select
            allowClear
            showSearch
            placeholder="全部数据源"
            style={{ width: 220 }}
            loading={sourcesLoading}
            value={sourceId || undefined}
            onChange={(v) => { setSourceId(v || ""); setPage(1); }}
            optionFilterProp="label"
            options={sources.map((s) => ({ value: s.source_id, label: `${s.name}（${s.source_id}）` }))}
            notFoundContent={sourcesLoading ? <span>加载中…</span> : <span>无可用数据源</span>}
          />
          <Select showSearch
            allowClear
            placeholder="全部源状态"
            style={{ width: 130 }}
            value={sourceStatus || undefined}
            onChange={(v) => { setSourceStatus((v as "" | "active" | "deleted") || ""); setPage(1); }}
            options={[
              { value: "active", label: "活跃源" },
              { value: "deleted", label: "已删除源" },
            ]}
          />
          <Select showSearch
            allowClear
            placeholder="全部库名"
            style={{ width: 160 }}
            loading={databasesLoading}
            disabled={!sourceId && databases.length === 0}
            value={database || undefined}
            onChange={(v) => { setDatabase(v || ""); setPage(1); }}
            options={databases.map((d) => ({ value: d, label: d }))}
            notFoundContent={databasesLoading ? <span>加载中…</span> : <span>无可用库名</span>}
          />
          <Select showSearch
            allowClear
            placeholder="全部类型"
            style={{ width: 120 }}
            value={entityType || undefined}
            onChange={(v) => { setEntityType(v || ""); setPage(1); }}
            options={["TABLE", "VIEW", "FIELD"].map((v) => ({ value: v, label: enumLabel(ENTITY_TYPE_LABEL, v) }))}
          />
          <Select showSearch
            allowClear
            placeholder="全部敏感度"
            style={{ width: 140 }}
            value={sensitivity || undefined}
            onChange={(v) => { setSensitivity(v || ""); setPage(1); }}
            options={Object.entries(SENSITIVITY_LABEL).map(([v, l]) => ({ value: v, label: l }))}
          />
          <Input.Search
            placeholder="搜索实体"
            style={{ width: 200 }}
            value={keywordInput}
            onChange={(e) => scheduleSearch(e.target.value)}
            onSearch={() => {
              if (searchTimer.current !== null) window.clearTimeout(searchTimer.current);
              commitSearchFilters();
            }}
          />
          {canDeprecateCatalog && selectedRowKeys.length > 0 && (
            <Popconfirm
              title={`确认批量废弃选中的 ${selectedRowKeys.length} 个目录实体？`}
              description="废弃后实体在治理视图中不再作为活跃资产展示，可重新启用；该操作不可撤销。"
              okText="确认废弃"
              cancelText="取消"
              onConfirm={handleBulkDeprecate}
            >
              <Button danger icon={<DeleteOutlined />}>批量废弃（{selectedRowKeys.length}）</Button>
            </Popconfirm>
          )}
          <Dropdown
            trigger={["click"]}
            dropdownRender={() => (
              <Card size="small" style={{ boxShadow: "0 2px 8px rgba(0,0,0,.15)", minWidth: 140 }}>
                <Checkbox.Group
                  style={{ display: "flex", flexDirection: "column", gap: 6 }}
                  value={visibleCols}
                  onChange={(vals) => setVisibleCols(vals as string[])}
                  options={COLUMN_OPTIONS}
                />
              </Card>
            )}
          >
            <Button icon={<SettingOutlined />}>列设置</Button>
          </Dropdown>
        </Space>

        {/* 采样覆盖率（PII 识别精度可观测性）：跟随数据源筛选，展示已采样表/列占比与双重验证列数 */}
        {coverage && (
          <div
            style={{
              marginBottom: 12,
              display: "flex",
              alignItems: "center",
              gap: 16,
              flexWrap: "wrap",
              fontSize: 12,
              padding: "6px 12px",
              background: "rgba(22, 119, 255, 0.04)",
              borderRadius: 6,
            }}
          >
            <span style={{ fontWeight: 500 }}>采样覆盖率</span>
            <span>
              表{" "}
              <b>
                {coverage.sampled_entities}/{coverage.total_entities}
              </b>{" "}
              （{(coverage.entity_coverage * 100).toFixed(1)}%）
            </span>
            <span>
              列{" "}
              <b>
                {coverage.sampled_columns}/{coverage.total_columns}
              </b>{" "}
              （{(coverage.column_coverage * 100).toFixed(1)}%）
            </span>
            {coverage.verified_columns > 0 && (
              <span style={{ color: "#52c41a" }}>
                ✓ {coverage.verified_columns} 列经 name+sample 双重验证
              </span>
            )}
            {coverageLoading && <span className="muted">加载中…</span>}
          </div>
        )}

        <Table
          dataSource={items}
          columns={resizableColumns}
          components={resizableComponents}
          rowKey={(r) => `${r.source_id}-${r.entity_name}`}
          loading={loading}
          // fixed 布局：未设宽度的「实体」列吸收剩余空间，宽屏一屏放下、列间距均匀
          tableLayout="fixed"
          rowSelection={{ selectedRowKeys, onChange: setSelectedRowKeys }}
          onRow={(r) => ({
            // 行点击打开字段详情（对齐描述缺失治理面板交互）：点选择列复选框/操作按钮不触发
            onClick: (e) => {
              const target = e.target as HTMLElement;
              if (target.closest(".ant-table-selection-column")) return;
              if (target.closest("button, a")) return;
              openFieldDetail(r);
            },
            style: {
              cursor: "pointer",
              ...(focusName && r.entity_name === focusName ? { background: "#fffbe6" } : undefined),
            },
          })}
          pagination={{ current: page, pageSize, total, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100], onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showTotal: (t) => `共 ${t} 条` }}
          scroll={{ x: "max" }}
          locale={{
            emptyText: (
              <Empty description="暂无目录实体，采集链路尚未产出元数据">
                <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate("/data-sources")}>
                  前往数据源管理
                </Button>
              </Empty>
            ),
          }}
        />
      </Card>

      <Modal
        title="登记目录实体"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        okText="登记"
        afterOpenChange={(open) => {
          if (open) loadSources();
        }}
      >
        <Form form={form} layout="vertical" onFinish={handleRegister} style={{ marginTop: 8 }}>
          <Form.Item name="source_id" label="数据源" rules={[{ required: true, message: "请选择归属的数据源" }]}>
            <Select
              showSearch
              loading={sourcesLoading}
              placeholder="选择数据源（source_id 自动填充）"
              optionFilterProp="label"
              options={sources.map((s) => ({ value: s.source_id, label: `${s.name}（${s.source_id}）` }))}
            />
          </Form.Item>
          <Form.Item name="entity_name" label="实体名" rules={[{ required: true }]}>
            <Input className="mono" placeholder="如 dwd_finance_order" />
          </Form.Item>
          <Form.Item name="entity_type" label="类型" initialValue="TABLE">
            <Select showSearch options={["TABLE", "VIEW", "FIELD"].map((v) => ({ value: v, label: enumLabel(ENTITY_TYPE_LABEL, v) }))} />
          </Form.Item>
          <Form.Item name="etl_sql" label="ETL SQL（可选）">
            <Input.TextArea rows={3} className="mono" />
          </Form.Item>
          <Alert type="info" showIcon message="source_id 由所选数据源自动确定，无需手填；schema_def 可在采集时自动填充。" />
        </Form>
      </Modal>

      <Drawer
        title={fieldDrawerCatalog ? `字段详情：${fieldDrawerCatalog.entity_name}` : "字段详情"}
        open={fieldDrawerOpen}
        onClose={() => { setFieldDrawerOpen(false); setFieldDrawerCatalog(null); setFieldColumns([]); setSampleRows([]); }}
        width={720}
        destroyOnClose={false}
      >
        {fieldDrawerCatalog && (() => {
          const isSchemaIncomplete = fieldDrawerCatalog.schema_incomplete;
          const hasNoSchema = fieldColumns.length === 0;
          // 源端编码乱码标记（schema_json.mojibake：采集/采样时检测到 GBK→UTF-8 替换残留）
          const mojibakeFields = (() => {
            const sd = (fieldDrawerCatalog?.schema_def ??
              (fieldDrawerCatalog as unknown as { schema_json?: unknown }).schema_json) as
              | Record<string, unknown>
              | undefined;
            const m = sd?.mojibake as
              | { sample_fields?: string[]; comment_fields?: string[] }
              | undefined;
            if (!m) return [];
            return Array.from(
              new Set([...(m.sample_fields ?? []), ...(m.comment_fields ?? [])]),
            );
          })();

          return (
            <>
              <Space style={{ marginBottom: 12 }} align="center">
                {canCollectCatalog && (
                  <Button
                    icon={<SyncOutlined />}
                    loading={refreshing}
                    onClick={handleRefreshEntity}
                    size="small"
                  >
                    采集该表
                  </Button>
                )}
                {canSampleCatalog && !hasNoSchema && (
                  <Tooltip title="不重跑全量采集，只对已有字段补采脱敏样本——样本可让 PII 识别从「仅靠字段名推断」升级为「字段名+实际值」双重验证，并发现字段名无语义但实际存敏感值的隐藏 PII">
                    <Button
                      icon={<ExperimentOutlined />}
                      loading={sampling}
                      onClick={handleSampleEntity}
                      size="small"
                    >
                      立即采样
                    </Button>
                  </Tooltip>
                )}
                <span className="muted" style={{ fontSize: 12 }}>
                  {hasNoSchema ? "从源端采集字段元数据" : "重新采集，同步源端最新字段"}
                </span>
              </Space>
              {/* 表级信息（从列表列收敛到此，避免表格横向滚动）：描述/域/责任人/ETL/签名 */}
              <Descriptions
                size="small"
                column={2}
                bordered
                style={{ marginBottom: 16 }}
                items={[
                  {
                    key: "desc",
                    label: "表描述",
                    span: 2,
                    children: fieldDrawerCatalog.description ? (
                      <>
                        {fieldDrawerCatalog.description}
                        {fieldDrawerCatalog.description_source && (
                          <Tag
                            style={{ marginLeft: 8 }}
                            color={fieldDrawerCatalog.description_source === "manual" ? "blue" : "purple"}
                          >
                            {fieldDrawerCatalog.description_source === "manual" ? "人工" : "LLM"}
                          </Tag>
                        )}
                      </>
                    ) : (
                      <span className="muted">—</span>
                    ),
                  },
                  {
                    key: "domain",
                    label: "业务域",
                    children: fieldDrawerCatalog.domain || <span className="muted">—</span>,
                  },
                  {
                    key: "owner",
                    label: "责任人",
                    children:
                      fieldDrawerCatalog.owner_name ??
                      (fieldDrawerCatalog.owner_id ?? <span className="muted">—</span>),
                  },
                  {
                    key: "etl",
                    label: "ETL SQL",
                    span: 2,
                    children: fieldDrawerCatalog.etl_sql ? (
                      <Tooltip title={fieldDrawerCatalog.etl_sql}>
                        <span className="mono" style={{ fontSize: 12 }}>
                          {fieldDrawerCatalog.etl_sql.slice(0, 120)}
                          {fieldDrawerCatalog.etl_sql.length > 120 ? "…" : ""}
                        </span>
                      </Tooltip>
                    ) : (
                      <span className="muted">—</span>
                    ),
                  },
                  {
                    key: "upstream",
                    label: "上游签名",
                    children: fieldDrawerCatalog.upstream_signature ? (
                      <Tooltip title={fieldDrawerCatalog.upstream_signature}>
                        <span className="mono" style={{ fontSize: 12 }}>
                          {fieldDrawerCatalog.upstream_signature.slice(0, 20)}…
                        </span>
                      </Tooltip>
                    ) : (
                      <span className="muted">—</span>
                    ),
                  },
                  {
                    key: "content",
                    label: "内容签名",
                    children: fieldDrawerCatalog.content_signature ? (
                      <Tooltip title={fieldDrawerCatalog.content_signature}>
                        <span className="mono" style={{ fontSize: 12 }}>
                          {fieldDrawerCatalog.content_signature.slice(0, 20)}…
                        </span>
                      </Tooltip>
                    ) : (
                      <span className="muted">—</span>
                    ),
                  },
                ]}
              />
              {isSchemaIncomplete && !hasNoSchema && (
                <Alert
                  type="warning"
                  showIcon
                  message="Schema 不完整，部分字段信息缺失"
                  style={{ marginBottom: 12 }}
                />
              )}
              {mojibakeFields.length > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  message="检测到源端编码乱码"
                  description={`字段「${mojibakeFields.join(
                    "、",
                  )}」含编码替换符（GBK→UTF-8 转换残留，信息已在源头丢失）。请在 Hive 侧修复数据/注释后重新采集。`}
                  style={{ marginBottom: 12 }}
                />
              )}
              {hasNoSchema ? (
                <Empty description="暂无字段信息，可点击「采集该表」从源端获取" />
              ) : (
                <SchemaTable
                  columns={fieldColumns}
                  editable={canEditCatalog}
                  inferable={true}
                  canInfer={canInferCatalog}
                  onEdit={handleEdit}
                  onInfer={handleInfer}
                  onBatchInfer={handleBatchInfer}
                  sampleRows={sampleRows}
                  loading={inferLoading || batchInferLoading}
                />
              )}
            </>
          );
        })()}
      </Drawer>
    </div>
  );
}
