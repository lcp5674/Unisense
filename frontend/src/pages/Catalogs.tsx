import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Space, Alert, Tooltip, Drawer, Empty, Statistic, Row, Col, Descriptions } from "antd";
import { PlusOutlined, ReloadOutlined, DeleteOutlined, EyeOutlined, SyncOutlined, ArrowLeftOutlined, HeartOutlined } from "@ant-design/icons";
import { listCatalogs, registerCatalog, bulkDeprecateCatalogs, listDataSources, listCatalogDatabases, refreshCatalogEntity, inferColumnDescription, inferDescriptions, updateColumnDescription, fetchDescriptionCoverage, listFavorites, addFavorite, removeFavorite, UnisenseApiError } from "../api";
import type { DBCatalog, DataSource, SchemaColumn } from "../types";
import type { DescriptionCoverage } from "../api";
import { enumLabel, ENTITY_TYPE_LABEL } from "../utils/enums";
import { SchemaTable } from "../components/SchemaTable";

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
  const [items, setItems] = useState<DBCatalog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [sourceId, setSourceId] = useState(urlSourceId);
  const [sourceStatus, setSourceStatus] = useState<"" | "active" | "deleted">("");
  const [entityType, setEntityType] = useState("");
  const [sensitivity, setSensitivity] = useState(urlSensitivity);
  const [keyword, setKeyword] = useState(urlKw);
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
  // 描述缺失概览统计卡（TD §12.1）
  const [coverage, setCoverage] = useState<DescriptionCoverage | null>(null);
  const [coverageLoading, setCoverageLoading] = useState(false);

  function openFieldDetail(record: DBCatalog) {
    setFieldDrawerCatalog(record);
    setFieldColumns(parseSchemaColumns(record));
    setFieldDrawerOpen(true);
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
  }, [page, pageSize, sourceId, sourceStatus, entityType, sensitivity, keyword, database]);

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

  // 描述缺失统计（字段覆盖率 / 缺失表 / 缺失字段）
  async function loadCoverage() {
    setCoverageLoading(true);
    try {
      setCoverage(await fetchDescriptionCoverage());
    } catch {
      // 统计卡为增强信息，加载失败不阻塞主列表
    } finally {
      setCoverageLoading(false);
    }
  }

  useEffect(() => {
    loadCoverage();
  }, []);

  // 库名选项随数据源联动：切换数据源时刷新库名下拉并重置已选库名
  async function loadDatabases() {
    setDatabasesLoading(true);
    try {
      setDatabases(await listCatalogDatabases(sourceId || undefined));
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
  }, [sourceId]);

  async function loadSources() {
    setSourcesLoading(true);
    try {
      const res = await listDataSources({ page: 1, page_size: 200 });
      setSources(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载数据源失败");
    } finally {
      setSourcesLoading(false);
    }
  }

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
      } else {
        // 源端已无此表（采集对账后被标记废弃）
        setFieldDrawerOpen(false);
        setFieldDrawerCatalog(null);
        setFieldColumns([]);
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

  const columns = [
    {
      title: "数据源",
      dataIndex: "source_id",
      key: "source_id",
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
      title: "实体",
      dataIndex: "entity_name",
      key: "entity_name",
      ellipsis: true,
      render: (v: string, r: DBCatalog) => (
        <Space size={4} wrap={false}>
          <span className="mono">{v}</span>
          <Tag>{enumLabel(ENTITY_TYPE_LABEL, r.entity_type)}</Tag>
          {r.schema_incomplete && <Tag color="warning">schema 缺失</Tag>}
        </Space>
      ),
    },
    {
      title: "敏感度",
      dataIndex: "sensitivity_level",
      key: "sensitivity",
      width: 100,
      render: (v: string) => <Tag color={SENSITIVITY_COLOR[v]}>{SENSITIVITY_LABEL[v] ?? v}</Tag>,
    },
    {
      title: "责任人",
      dataIndex: "owner_id",
      key: "owner",
      width: 100,
      render: (v: number | null, r: DBCatalog) => (
        <Tooltip title={v != null ? `owner_id=${v}` : undefined}>
          <span>{(r.owner_name ?? v) || <Tag>无</Tag>}</span>
        </Tooltip>
      ),
    },
    {
      title: "操作",
      key: "action",
      width: 110,
      render: (_: unknown, record: DBCatalog) => (
        <Space size={2}>
          <Button
            type="link"
            size="small"
            icon={<HeartOutlined style={{ color: favNames.has(record.entity_name) ? "#eb2f96" : undefined }} />}
            onClick={() => toggleFavorite(record)}
          >
            {favNames.has(record.entity_name) ? "已收藏" : "收藏"}
          </Button>
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            onClick={() => openFieldDetail(record)}
          >
            字段详情
          </Button>
        </Space>
      ),
    },
  ];

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
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>登记实体</Button>
      </div>

      {coverage && (
        <Row gutter={12} style={{ marginBottom: 16 }}>
          <Col span={6}>
            <Card size="small">
              <Statistic
                title="字段描述覆盖率"
                value={coverage.total_fields > 0 ? Math.round((coverage.fields_with_desc / coverage.total_fields) * 100) : 0}
                suffix="%"
                valueStyle={{ color: coverage.fields_with_desc >= coverage.fields_missing_desc ? "#3f8600" : "#cf1322" }}
                loading={coverageLoading}
              />
              <div className="muted" style={{ fontSize: 12 }}>
                {coverage.fields_with_desc} / {coverage.total_fields} 字段有描述
              </div>
            </Card>
          </Col>
          <Col span={6}>
            <Card size="small">
              <Statistic
                title="缺失字段数"
                value={coverage.fields_missing_desc}
                valueStyle={{ color: coverage.fields_missing_desc > 0 ? "#cf1322" : "#3f8600" }}
                loading={coverageLoading}
              />
              <div className="muted" style={{ fontSize: 12 }}>可 LLM 推断或人工补全</div>
            </Card>
          </Col>
          <Col span={6}>
            <Card size="small">
              <Statistic
                title="缺表描述"
                value={coverage.tables_missing_desc}
                valueStyle={{ color: coverage.tables_missing_desc > 0 ? "#cf1322" : "#3f8600" }}
                loading={coverageLoading}
              />
              <div className="muted" style={{ fontSize: 12 }}>
                {coverage.tables_with_desc} / {coverage.total_tables} 表已补全
              </div>
            </Card>
          </Col>
          <Col span={6}>
            <Card size="small">
              <Statistic title="目录表总数" value={coverage.total_tables} loading={coverageLoading} />
              <div className="muted" style={{ fontSize: 12 }}>含表 / 视图</div>
            </Card>
          </Col>
        </Row>
      )}

      <Card
        extra={
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
            刷新
          </Button>
        }
      >
        <Space style={{ marginBottom: 12 }} wrap>
          <Input
            placeholder="Source ID"
            className="mono"
            style={{ width: 150 }}
            value={sourceId}
            onChange={(e) => setSourceId(e.target.value)}
            onPressEnter={() => { setPage(1); load(); }}
          />
          <Select
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
          <Select
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
          <Select
            allowClear
            placeholder="全部类型"
            style={{ width: 120 }}
            value={entityType || undefined}
            onChange={(v) => { setEntityType(v || ""); setPage(1); }}
            options={["TABLE", "VIEW", "FIELD"].map((v) => ({ value: v, label: ENTITY_TYPE_LABEL[v] ?? v }))}
          />
          <Select
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
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onSearch={() => { setPage(1); load(); }}
          />
          {selectedRowKeys.length > 0 && (
            <Button danger icon={<DeleteOutlined />} onClick={handleBulkDeprecate}>
              批量废弃（{selectedRowKeys.length}）
            </Button>
          )}
        </Space>

        <Table
          dataSource={items}
          columns={columns}
          rowKey={(r) => `${r.source_id}-${r.entity_name}`}
          loading={loading}
          rowSelection={{ selectedRowKeys, onChange: setSelectedRowKeys }}
          onRow={(r) => ({
            style: focusName && r.entity_name === focusName ? { background: "#fffbe6" } : undefined,
          })}
          pagination={{ current: page, pageSize, total, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100], onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showTotal: (t) => `共 ${t} 条` }}
          scroll={{ x: 900 }}
          locale={{ emptyText: "暂无目录实体" }}
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
            <Select options={["TABLE", "VIEW", "FIELD"].map((v) => ({ value: v, label: ENTITY_TYPE_LABEL[v] ?? v }))} />
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
        onClose={() => { setFieldDrawerOpen(false); setFieldDrawerCatalog(null); setFieldColumns([]); }}
        width={720}
        destroyOnClose={false}
      >
        {fieldDrawerCatalog && (() => {
          const isSchemaIncomplete = fieldDrawerCatalog.schema_incomplete;
          const hasNoSchema = fieldColumns.length === 0;

          return (
            <>
              <Space style={{ marginBottom: 12 }} align="center">
                <Button
                  icon={<SyncOutlined />}
                  loading={refreshing}
                  onClick={handleRefreshEntity}
                  size="small"
                >
                  采集该表
                </Button>
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
              {hasNoSchema ? (
                <Empty description="暂无字段信息，可点击「采集该表」从源端获取" />
              ) : (
                <SchemaTable
                  columns={fieldColumns}
                  editable={true}
                  inferable={true}
                  onEdit={handleEdit}
                  onInfer={handleInfer}
                  onBatchInfer={handleBatchInfer}
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
