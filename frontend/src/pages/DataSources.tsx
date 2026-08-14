import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Space, Statistic, Row, Col, Descriptions, Alert, Progress, Collapse, Popconfirm } from "antd";
import { PlusOutlined, ThunderboltOutlined, ScheduleOutlined, ReloadOutlined, ApiOutlined, EditOutlined, DatabaseOutlined, DeleteOutlined } from "@ant-design/icons";
import {
  listDataSources,
  getDataSource,
  createDataSource,
  updateDataSource,
  deleteDataSource,
  collectSourceNow,
  streamCollectionJob,
  getCollectionJob,
  scheduleSource,
  getSourceHealth,
  getSourceWatermark,
  listDataSourceTypes,
  listDomainTree,
  testDataSourceConnection,
  checkDataSourceConnection,
  listDataSourceDatabases,
  listDriftLogs,
  UnisenseApiError,
} from "../api";
import type { DataSource, SourceHealth, Watermark, CollectResult, SourceTypeInfo, TestConnectionResult, SourceType, SubjectDomainTreeNode, DataSourceCreateRequest, DataSourceUpdateRequest, CollectionProgress } from "../types";
import type { DriftLogItem } from "../api";
import { ObjectView } from "../utils/display";
import { COLLECTION_MODE_LABEL, SOURCE_HEALTH_LABEL } from "../utils/enums";

const FALLBACK_TYPES: SourceTypeInfo[] = [
  { source_type: "mysql", label: "MySQL", default_port: 3306, supports_database: true, supports_schema: false, description: "关系型数据库" },
  { source_type: "postgres", label: "PostgreSQL", default_port: 5432, supports_database: true, supports_schema: true, description: "关系型数据库" },
  { source_type: "hive", label: "Hive", default_port: 10000, supports_database: true, supports_schema: false, description: "数据仓库" },
  { source_type: "spark", label: "Spark", default_port: 10000, supports_database: true, supports_schema: false, description: "Spark SQL（Thrift Server）" },
  { source_type: "doris", label: "Doris", default_port: 9030, supports_database: true, supports_schema: false, description: "MPP 分析库" },
  { source_type: "clickhouse", label: "ClickHouse", default_port: 8123, supports_database: true, supports_schema: false, description: "列式分析库" },
  { source_type: "kafka", label: "Kafka", default_port: 9092, supports_database: false, supports_schema: false, description: "消息队列" },
  { source_type: "starrocks", label: "StarRocks", default_port: 9030, supports_database: true, supports_schema: false, description: "MPP 分析库" },
];

function typeInfo(types: SourceTypeInfo[], t: string): SourceTypeInfo | undefined {
  return types.find((x) => x.source_type === t);
}

// 主题域树 → 扁平化选项（保留层级前缀，便于区分同名子域）
function flattenDomains(
  nodes: SubjectDomainTreeNode[],
  depth = 0,
  out: Array<{ value: string; label: string }> = [],
): Array<{ value: string; label: string }> {
  for (const n of nodes) {
    const indent = depth > 0 ? `${"　".repeat(depth)}` : "";
    out.push({ value: n.code, label: `${indent}${n.name}（${n.code}）` });
    if (n.children?.length) flattenDomains(n.children, depth + 1, out);
  }
  return out;
}

// 连接检查 detail 字段名 → 中文
const CONN_DETAIL_LABEL: Record<string, string> = {
  host: "主机",
  port: "端口",
  database: "数据库",
  schema: "Schema",
  user: "账号",
  error: "错误信息",
  stage: "检查阶段",
  latency_ms: "延迟",
};

const DRIFT_CHANGE_LABEL: Record<string, string> = {
  ADD_COLUMN: "新增列",
  DROP_COLUMN: "删除列",
  TYPE_CHANGE: "类型变更",
  SCHEMA_CHANGED: "结构变更",
};

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

function previewSourceId(sourceType: string | undefined, cfg: Record<string, unknown>, domain: string): string {
  const base = String(cfg.database || cfg.schema || domain || "default");
  const norm = base.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase() || "default";
  return `${sourceType || "?"}_${norm}`.slice(0, 64);
}

function SourceDetailModal({
  source,
  types,
  onClose,
  onEdit,
  onDelete,
  deleting,
}: {
  source: DataSource;
  types: SourceTypeInfo[];
  onClose: () => void;
  onEdit: (source: DataSource) => void;
  onDelete: (source: DataSource) => void;
  deleting: boolean;
}) {
  const navigate = useNavigate();
  const [health, setHealth] = useState<SourceHealth | null>(null);
  const [watermark, setWatermark] = useState<Watermark | null>(null);
  const [collecting, setCollecting] = useState(false);
  const [collectResult, setCollectResult] = useState<CollectResult | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [progress, setProgress] = useState<CollectionProgress | null>(null);
  const [progressMessages, setProgressMessages] = useState<string[]>([]);
  const abortRef = useRef<(() => void) | null>(null);
  const [cron, setCron] = useState("0 3 * * *");
  const [scheduleMode, setScheduleMode] = useState("FULL");
  const [checking, setChecking] = useState(false);
  const [checkResult, setCheckResult] = useState<TestConnectionResult | null>(null);
  const [driftLogs, setDriftLogs] = useState<DriftLogItem[]>([]);

  useEffect(() => {
    getSourceHealth(source.source_id).then(setHealth).catch(() => {});
    getSourceWatermark(source.source_id).then(setWatermark).catch(() => {});
    listDriftLogs(source.source_id, { page: 1, page_size: 10 })
      .then((res) => setDriftLogs(res.items))
      .catch(() => setDriftLogs([]));
  }, [source.source_id]);

  // 组件卸载时取消进行中的 SSE 订阅，避免内存泄漏
  useEffect(() => () => abortRef.current?.(), []);

  // 进度百分比：有实体总数时按 index/total；扫描阶段给 10% 占位
  const progressPct =
    progress?.index && progress?.total
      ? Math.min(100, Math.round((progress.index / progress.total) * 100))
      : progress?.phase === "scanning"
        ? 10
        : 0;

  /** 统一消费终态任务详情 → 更新结果/健康/水位（SSE onDone 与轮询兜底共用）。 */
  function applyDone(status: { status: string; detail?: Record<string, unknown> | null }) {
    abortRef.current = null;
    setCollecting(false);
    const detail = status.detail ?? {};
    const result: CollectResult = {
      source_id: source.source_id,
      scanned: Number(detail.scanned ?? 0),
      registered: Number(detail.registered ?? 0),
      pii_registered: Number(detail.pii_registered ?? 0),
      failed_count: Number(detail.failed_count ?? 0),
      failed_specs: (detail.failed_specs as CollectResult["failed_specs"]) ?? [],
      coverage: Number(detail.coverage ?? 0),
      mode: String(detail.mode ?? "FULL"),
      drift_count: Number(detail.drift_count ?? 0),
      drift_events: (detail.drift_events as CollectResult["drift_events"]) ?? [],
      deprecated_count: Number(detail.deprecated_count ?? 0),
      entities: (detail.entities as CollectResult["entities"]) ?? [],
    };
    setCollectResult(result);
    // 进度兜底拉满：SSE 为 1s 快照，终态到达时最后一帧 RUNNING 进度可能停在中间值
    // （如 25%），此处以结果 scanned 作为 index=total 把进度条推进到 100%。
    setProgress({ phase: "done", index: result.scanned, total: result.scanned });
    if (status.status === "FAILED") {
      const errMsg = typeof detail.error === "string" ? detail.error : "采集失败";
      message.error(`采集失败：${errMsg}`);
    } else {
      message.success(
        `采集完成：扫描 ${result.scanned} · 注册 ${result.registered} · PII ${result.pii_registered}`,
      );
    }
    getSourceHealth(source.source_id).then(setHealth).catch(() => {});
    getSourceWatermark(source.source_id).then(setWatermark).catch(() => {});
  }

  async function handleCollect() {
    if (collecting) return;
    setCollecting(true);
    setCollectResult(null);
    setProgress(null);
    setProgressMessages([]);
    setJobId(null);
    try {
      const { job_id } = await collectSourceNow(source.source_id, "FULL");
      setJobId(job_id);
      // 订阅 SSE 实时进度；终态事件含完整结果（entities 明细）
      abortRef.current = streamCollectionJob(job_id, {
        onProgress: (_status, p) => {
          if (!p) return;
          setProgress(p);
          if (p.messages?.length) setProgressMessages(p.messages);
        },
        onDone: (status) => applyDone(status),
        onError: (msg) => {
          abortRef.current = null;
          setCollecting(false);
          // SSE 中断兜底：任务可能仍在后台完成，轮询一次终态
          getCollectionJob(job_id)
            .then((st) => {
              if (st && (st.status === "COMPLETED" || st.status === "FAILED")) {
                applyDone(st as { status: string; detail?: Record<string, unknown> | null });
                return;
              }
              message.error(msg);
            })
            .catch(() => message.error(msg));
        },
      });
    } catch (err) {
      setCollecting(false);
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "采集失败");
    }
  }

  async function handleCheck() {
    setChecking(true);
    setCheckResult(null);
    try {
      const res = await checkDataSourceConnection(source.source_id);
      setCheckResult(res);
      if (res.ok) {
        message.success(`连接正常（${res.latency_ms}ms）`);
      } else {
        message.error(`连接失败：${res.error ?? "未知错误"}`);
      }
      // 刷新健康状态展示
      getSourceHealth(source.source_id).then(setHealth).catch(() => {});
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "检查失败");
    } finally {
      setChecking(false);
    }
  }

  async function handleSchedule() {
    try {
      const res = await scheduleSource(source.source_id, cron, scheduleMode);
      if (res?.scheduled) {
        message.success(`已保存定时调度：${res.cron}（${res.mode}）`);
      } else {
        message.success("调度配置已保存");
      }
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "调度失败");
    }
  }

  return (
    <Modal open onCancel={onClose} footer={null} width={720} title={`数据源：${source.name}（${source.source_id}）`}>
      <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
        <Descriptions.Item label="类型">
          <Tag>{typeInfo(types, source.source_type)?.label ?? source.source_type}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="域">{source.domain}</Descriptions.Item>
        <Descriptions.Item label="覆盖度">{Math.round(source.coverage * 100)}%</Descriptions.Item>
        <Descriptions.Item label="采集模式">{COLLECTION_MODE_LABEL[source.collection_mode] ?? source.collection_mode}</Descriptions.Item>
        <Descriptions.Item label="健康状态">
          <Tag color={source.health_status === "healthy" ? "success" : source.health_status === "unhealthy" ? "error" : "default"}>
            {SOURCE_HEALTH_LABEL[source.health_status] ?? source.health_status}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label="连接配置">{(source.connection_config_present ? "已配置" : "未配置")}</Descriptions.Item>
      </Descriptions>

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <Statistic title="最近采集" value={watermark?.last_collected_at ?? "从未"} valueStyle={{ fontSize: 16 }} />
        </Col>
        <Col span={8}>
          <Statistic title="累计扫描" value={watermark?.scanned_count ?? 0} />
        </Col>
        <Col span={8}>
          <Statistic title="采集失败" value={watermark?.failed_count ?? 0} valueStyle={{ color: (watermark?.failed_count ?? 0) > 0 ? "var(--danger)" : undefined }} />
        </Col>
      </Row>

      {checkResult && (
        <Alert
          type={checkResult.ok ? "success" : "error"}
          showIcon
          style={{ marginBottom: 12 }}
          message={checkResult.ok ? `连接正常（${checkResult.latency_ms}ms）` : `连接失败：${checkResult.error}`}
          description={checkResult.detail ? <ObjectView data={checkResult.detail} labels={CONN_DETAIL_LABEL} /> : undefined}
        />
      )}
      {health?.last_error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={`最近错误：${health.last_error}`} />}
      {(collecting || progress) && (
        <Card size="small" title={collecting ? `正在采集（job: ${jobId ?? "…"}）` : "采集进度"} style={{ marginBottom: 12 }}>
          <Progress
            percent={progressPct}
            status={collecting ? "active" : "success"}
            format={(p) => (progress?.entity_name ? `${progress.entity_name} · ${p}%` : `${p}%`)}
          />
          {progressMessages.length > 0 && (
            <div style={{ maxHeight: 120, overflow: "auto", marginTop: 8 }}>
              {progressMessages.slice(-12).map((m, i) => (
                <div key={i} className="mono" style={{ fontSize: 12, lineHeight: "18px" }}>{m}</div>
              ))}
            </div>
          )}
        </Card>
      )}
      {collectResult && (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 12 }}
          message={`采集结果：注册 ${collectResult.registered} · PII ${collectResult.pii_registered} · 漂移 ${collectResult.drift_count}`}
          description={
            <div>
              <div style={{ marginBottom: 4 }}>
                {collectResult.drift_events?.length
                  ? collectResult.drift_events.slice(0, 5).map((d) => `${d.entity_name} (${DRIFT_CHANGE_LABEL[d.change_type] ?? d.change_type})`).join("、")
                  : "无 schema 漂移"}
              </div>
              <Button type="link" size="small" style={{ padding: 0 }} onClick={() => navigate(`/catalogs?source_id=${encodeURIComponent(source.source_id)}`)}>
                在采集目录中查看 →
              </Button>
            </div>
          }
        />
      )}
      {collectResult?.entities && collectResult.entities.length > 0 && (
        <Collapse
          size="small"
          style={{ marginBottom: 12 }}
          items={[
            {
              key: "entities",
              label: `本次采集到的表（${collectResult.entities.length}）`,
              children: (
                <Table
                  size="small"
                  rowKey="entity_name"
                  pagination={false}
                  dataSource={collectResult.entities}
                  scroll={{ y: 240 }}
                  columns={[
                    { title: "表名", dataIndex: "entity_name", ellipsis: true, render: (v: string) => <span className="mono">{v}</span> },
                    {
                      title: "敏感度",
                      dataIndex: "sensitivity_level",
                      width: 120,
                      render: (v: string) => <Tag color={SENSITIVITY_COLOR[v]}>{SENSITIVITY_LABEL[v] ?? v}</Tag>,
                    },
                    {
                      title: "漂移",
                      dataIndex: "drifted",
                      width: 100,
                      render: (v: boolean, r: { change_type?: string | null }) =>
                        v ? <Tag color="warning">{DRIFT_CHANGE_LABEL[r.change_type ?? ""] ?? "已漂移"}</Tag> : <Tag>无</Tag>,
                    },
                  ]}
                />
              ),
            },
          ]}
        />
      )}

      {driftLogs.length > 0 && (
        <Card
          size="small"
          title={`变更审计（${driftLogs.length}）`}
          style={{ marginBottom: 12 }}
          extra={<span className="muted">Schema Drift · GB/T 36073 §6.4</span>}
        >
          <Table
            size="small"
            rowKey="entity_name"
            pagination={false}
            dataSource={driftLogs}
            columns={[
              {
                title: "实体",
                dataIndex: "entity_name",
                ellipsis: true,
                render: (v: string) => <span className="mono">{v}</span>,
              },
              {
                title: "变更类型",
                dataIndex: "change_type",
                width: 130,
                render: (v: string) => (
                  <Tag color={v === "DROP_COLUMN" ? "error" : v === "TYPE_CHANGE" ? "warning" : "processing"}>
                    {DRIFT_CHANGE_LABEL[v] ?? v}
                  </Tag>
                ),
              },
              {
                title: "检测时间",
                dataIndex: "detected_at",
                width: 170,
                render: (v: string | null) => (v ? new Date(v).toLocaleString() : "—"),
              },
            ]}
          />
        </Card>
      )}

      <Space wrap>
        <Button type="primary" icon={<EditOutlined />} onClick={() => onEdit(source)}>
          编辑
        </Button>
        <Button type="primary" icon={<ThunderboltOutlined />} loading={collecting} onClick={handleCollect}>
          立即采集
        </Button>
        <Button icon={<ApiOutlined />} loading={checking} onClick={handleCheck}>
          测试连接
        </Button>
        <Input
          className="mono"
          value={cron}
          onChange={(e) => setCron(e.target.value)}
          style={{ width: 150 }}
          placeholder="cron"
        />
        <Select value={scheduleMode} onChange={setScheduleMode} style={{ width: 130 }} options={[{ value: "FULL", label: "全量" }, { value: "INCREMENTAL", label: "增量" }]} />
        <Button icon={<ScheduleOutlined />} onClick={handleSchedule}>设置调度</Button>
        <Popconfirm
          title="删除数据源"
          description={`确定删除「${source.name}」？其采集目录、水位、漂移日志将一并清理，删除后原 ID 可重建同名数据源。`}
          okText="确认删除"
          okButtonProps={{ danger: true }}
          cancelText="取消"
          onConfirm={() => onDelete(source)}
        >
          <Button danger icon={<DeleteOutlined />} loading={deleting}>删除</Button>
        </Popconfirm>
      </Space>
    </Modal>
  );
}

export function DataSources() {
  const [items, setItems] = useState<DataSource[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [keyword, setKeyword] = useState("");
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<DataSource | null>(null);
  const [detail, setDetail] = useState<DataSource | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [types, setTypes] = useState<SourceTypeInfo[]>(FALLBACK_TYPES);
  const [domainOptions, setDomainOptions] = useState<Array<{ value: string; label: string }>>([]);
  // 数据库枚举（测试连接通过后自动列出，供选择目标库）
  const [dbOptions, setDbOptions] = useState<string[]>([]);
  const [dbLoading, setDbLoading] = useState(false);
  const [dbEnumerated, setDbEnumerated] = useState(false);
  const [form] = Form.useForm();
  // 编辑回显时的连接配置明文快照（用于判断用户是否实际修改了连接字段）
  const editConfigRef = useRef<Record<string, unknown> | null>(null);
  // 编辑保存且连接配置变更后，引导"立即重新采集"的目标数据源
  const [recollectSource, setRecollectSource] = useState<string | null>(null);
  const [searchParams] = useSearchParams();
  const sourceType = Form.useWatch("source_type", form);
  const watchedDatabase = Form.useWatch("database", form);
  const watchedSchema = Form.useWatch("schema", form);
  const domainWatch = Form.useWatch("domain", form) ?? "";

  // 支持从全局搜索栏经 ?kw= 直达定位
  useEffect(() => {
    const kw = searchParams.get("kw");
    if (kw) {
      setKeyword(kw);
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  async function load(nextPage = page, nextPageSize = pageSize) {
    setLoading(true);
    try {
      // P1-1: 服务端分页（后端返回 {items, total, page, page_size}）
      const resp = await listDataSources({ keyword: keyword || undefined, page: nextPage, page_size: nextPageSize });
      setItems(resp.items);
      setTotal(resp.total);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    listDataSourceTypes()
      .then((t) => setTypes(t.length ? t : FALLBACK_TYPES))
      .catch(() => setTypes(FALLBACK_TYPES));
    // 业务域下拉选项：仅展示启用中的主题域
    listDomainTree("active")
      .then((tree) => setDomainOptions(flattenDomains(tree)))
      .catch(() => setDomainOptions([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyword]);

  // 类型切换时自动带出默认端口，并清空已枚举的数据库列表
  function handleTypeChange(t: string) {
    const info = typeInfo(types, t);
    if (info?.default_port) {
      form.setFieldValue("port", info.default_port);
    }
    setDbOptions([]);
    setDbEnumerated(false);
  }

  /** 枚举实例下可采集的非系统数据库（需 host/类型已填）。 */
  async function loadDatabases() {
    const values = form.getFieldsValue();
    if (!values.source_type || !values.host) {
      message.warning("请先填写类型与 Host，再枚举数据库");
      return;
    }
    if (!typeInfo(types, String(values.source_type))?.supports_database) {
      setDbOptions([]);
      setDbEnumerated(false);
      message.info("该类型不支持枚举数据库（可手填）");
      return;
    }
    const cfg = buildConnectionConfig(values);
    setDbLoading(true);
    try {
      const res = await listDataSourceDatabases({
        source_type: String(values.source_type) as SourceType,
        connection_config: cfg,
      });
      setDbOptions(res.databases);
      setDbEnumerated(true);
      if (res.databases.length === 0) {
        message.info("未发现可采集的数据库（可留空采集全部库）");
      } else {
        message.success(`已枚举到 ${res.databases.length} 个数据库`);
      }
    } catch (err) {
      setDbOptions([]);
      setDbEnumerated(false);
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "枚举数据库失败");
    } finally {
      setDbLoading(false);
    }
  }

  function buildConnectionConfig(values: Record<string, unknown>): Record<string, unknown> {
    const cfg: Record<string, unknown> = { host: String(values.host || "") };
    if (values.port) cfg.port = Number(values.port);
    if (values.database) cfg.database = String(values.database);
    if (values.schema) cfg.schema = String(values.schema);
    if (values.user) cfg.user = String(values.user);
    if (values.password) cfg.password = String(values.password);
    return cfg;
  }

  async function handleTest() {
    try {
      await form.validateFields(["host", "source_type"]);
    } catch {
      message.warning("请先填写类型与 Host");
      return;
    }
    const values = form.getFieldsValue();
    const cfg = buildConnectionConfig(values);
    try {
      const res = await testDataSourceConnection({
        source_type: String(values.source_type) as SourceType,
        connection_config: cfg,
      });
      if (res.ok) {
        message.success(`连接成功（${res.latency_ms}ms）`);
        // 连接通过后自动枚举目标数据库，供用户选择
        loadDatabases();
      } else {
        message.error(`连接失败：${res.error ?? "未知错误"}`);
        setDbOptions([]);
        setDbEnumerated(false);
      }
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "测试失败");
    }
  }

  function openCreate() {
    setEditTarget(null);
    form.resetFields();
    form.setFieldsValue({ source_type: undefined, port: 3306 });
    setDbOptions([]);
    setDbEnumerated(false);
    setModalOpen(true);
  }

  async function handleDeleteSource(source: DataSource) {
    setDeleting(true);
    try {
      await deleteDataSource(source.source_id);
      message.success(`数据源「${source.name}」已删除`);
      setDetail(null);
      // 当前页删空后回退到上一页，避免停在空页
      if (items.length === 1 && page > 1) {
        setPage(page - 1);
        load(page - 1, pageSize);
      } else {
        load();
      }
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败");
    } finally {
      setDeleting(false);
    }
  }

  function openEdit(source: DataSource) {
    setEditTarget(source);
    form.resetFields();
    // 连接配置明文快照：编辑保存时对比表单值，未修改则不覆盖（避免纯改名重置健康状态）
    editConfigRef.current = null;
    // 先填基础字段；连接配置明文由详情接口异步拉取后回显（见下方 getDataSource）。
    form.setFieldsValue({
      name: source.name,
      source_type: source.source_type,
      domain: source.domain,
      cluster_id: source.cluster_id ?? undefined,
      port: typeInfo(types, source.source_type)?.default_port ?? undefined,
    });
    setDbOptions([]);
    setDbEnumerated(false);
    setModalOpen(true);
    // 编辑回显：拉取详情中的明文连接配置并预填连接字段（未修改的连接字段保持原配置）。
    getDataSource(source.source_id)
      .then((d) => {
        const cfg = d.connection_config ?? null;
        if (cfg && typeof cfg === "object") {
          editConfigRef.current = cfg as Record<string, unknown>;
          form.setFieldsValue({
            host: cfg.host != null ? String(cfg.host) : undefined,
            port: cfg.port != null ? String(cfg.port) : undefined,
            database: cfg.database != null ? String(cfg.database) : undefined,
            schema: cfg.schema != null ? String(cfg.schema) : undefined,
            user: cfg.user != null ? String(cfg.user) : undefined,
            password: cfg.password != null ? String(cfg.password) : undefined,
          });
          setDbEnumerated(Boolean(cfg.database));
        }
      })
      .catch(() => {
        // 拉取失败（如密钥漂移）：保持连接字段留空，沿用"留空=保持原配置"兜底
      });
  }

  async function handleSubmit(values: Record<string, unknown>) {
    setLoading(true);
    // 编辑保存时连接配置是否发生变更（决定是否引导重新采集）
    let configChanged = false;
    try {
      const cfg = buildConnectionConfig(values);
      if (editTarget) {
        const payload: DataSourceUpdateRequest = {
          name: String(values.name),
          source_type: String(values.source_type) as SourceType,
          domain: String(values.domain),
        };
        if (values.cluster_id != null) payload.cluster_id = String(values.cluster_id);
        // 连接配置已回显明文：仅当表单值与原始配置快照不同（即用户实际修改了连接字段）
        // 才提交覆盖，避免纯改名/改域时误覆盖配置并重置健康状态。
        const prev = editConfigRef.current;
        if (prev == null) {
          // 未成功回显（拉取失败/无配置）：用 antd touched 兜底判断用户是否填写
          configChanged = ["host", "port", "database", "schema", "user", "password"].some((f) =>
            form.isFieldTouched(f),
          );
        } else {
          configChanged = JSON.stringify(cfg) !== JSON.stringify(prev);
        }
        if (configChanged) payload.connection_config = cfg;
        await updateDataSource(editTarget.source_id, payload);
        message.success(`数据源已更新：${editTarget.source_id}`);
      } else {
        const payload: DataSourceCreateRequest = {
          name: String(values.name),
          source_type: String(values.source_type) as SourceType,
          domain: String(values.domain),
          cluster_id: values.cluster_id ? String(values.cluster_id) : null,
          connection_config: cfg,
        };
        // source_id 不传 → 后端按 类型_库|域 自动生成
        await createDataSource(payload);
        message.success(`数据源已创建（${previewSourceId(String(values.source_type), cfg, String(values.domain))}）`);
      }
      const updatedSourceId = editTarget?.source_id ?? null;
      setModalOpen(false);
      setEditTarget(null);
      form.resetFields();
      load();
      // 编辑保存且连接配置变更 → 引导重新采集：
      // 新配置下的元数据需重新采集；旧采集表会在下次全量采集后自动标记 DEPRECATED（不删除）。
      if (updatedSourceId && configChanged) {
        setRecollectSource(updatedSourceId);
      }
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : editTarget ? "更新失败" : "创建失败");
    } finally {
      setLoading(false);
    }
  }

  const selType = typeInfo(types, sourceType);
  const generated = previewSourceId(sourceType, { database: watchedDatabase, schema: watchedSchema }, domainWatch);

  const columns = [
    { title: "Source ID", dataIndex: "source_id", key: "source_id", render: (v: string) => <span className="mono">{v}</span> },
    { title: "名称", dataIndex: "name", key: "name" },
    {
      title: "类型",
      dataIndex: "source_type",
      key: "type",
      width: 130,
      render: (v: string) => <Tag>{typeInfo(types, v)?.label ?? v}</Tag>,
    },
    { title: "域", dataIndex: "domain", key: "domain", width: 130 },
    {
      title: "健康",
      dataIndex: "health_status",
      key: "health",
      width: 100,
      render: (v: string) => <Tag color={v === "healthy" ? "success" : v === "unhealthy" ? "error" : "default"}>{SOURCE_HEALTH_LABEL[v] ?? v}</Tag>,
    },
    { title: "覆盖度", dataIndex: "coverage", key: "coverage", width: 90, render: (v: number) => `${Math.round(v * 100)}%` },
    { title: "调度", dataIndex: "schedule_cron", key: "schedule", width: 110, render: (v: string | null) => (v ? <span className="mono">{v}</span> : <span className="muted">—</span>) },
    {
      title: "操作",
      key: "actions",
      width: 90,
      render: (_: unknown, s: DataSource) => (
        <Button type="link" onClick={() => setDetail(s)}>管理</Button>
      ),
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Collection / Data Sources</div>
          <h2>数据源管理</h2>
          <p>接入数据源、测试连接、采集元数据并持续发现 schema 漂移。Database 留空时采集该实例下全部非系统库。</p>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建数据源</Button>
      </div>

      <Card extra={<Button icon={<ReloadOutlined />} onClick={() => load()} loading={loading}>刷新</Button>}>
        <Space style={{ marginBottom: 12 }}>
          <Input.Search
            placeholder="搜索数据源名称 / ID"
            allowClear
            style={{ width: 260 }}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onSearch={() => { setPage(1); load(1, pageSize); }}
          />
        </Space>
        <Table
          dataSource={items}
          columns={columns}
          rowKey="source_id"
          loading={loading}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: (t: number) => `共 ${t} 个数据源`,
            onChange: (p: number, ps: number) => {
              setPage(p);
              setPageSize(ps);
              load(p, ps);
            },
          }}
          locale={{ emptyText: "暂无数据源" }}
        />
      </Card>

      <Modal
        title={editTarget ? `编辑数据源：${editTarget.source_id}` : "新建数据源"}
        open={modalOpen}
        onCancel={() => { setModalOpen(false); setEditTarget(null); }}
        onOk={() => form.submit()}
        confirmLoading={loading}
        okText={editTarget ? "保存" : "创建"}
        width={620}
      >
        <Form form={form} layout="vertical" onFinish={handleSubmit} style={{ marginTop: 8 }}>
          <Space size={16} style={{ width: "100%" }}>
            <Form.Item name="name" label="名称" rules={[{ required: true }]} style={{ width: "100%" }}>
              <Input placeholder="如 财务 MySQL" />
            </Form.Item>
            <Form.Item name="domain" label="业务域" rules={[{ required: true }]} style={{ width: "100%" }}>
              <Select
                placeholder="从主题域选择"
                showSearch
                optionFilterProp="label"
                options={domainOptions}
                notFoundContent="暂无启用中的主题域"
              />
            </Form.Item>
          </Space>
          <Space size={16} style={{ width: "100%" }}>
            <Form.Item name="source_type" label="类型" rules={[{ required: true }]} style={{ width: "100%" }}>
              <Select
                placeholder="选择数据源类型"
                onChange={handleTypeChange}
                listHeight={400}
                options={types.map((t) => ({ value: t.source_type, label: `${t.label}（${t.source_type}）` }))}
                optionRender={(opt) => {
                  const t = typeInfo(types, String(opt.value));
                  return (
                    <div>
                      <div>{opt.label}</div>
                      {t?.description && <div style={{ fontSize: 12, color: "rgba(0,0,0,0.45)" }}>{t.description}</div>}
                    </div>
                  );
                }}
              />
            </Form.Item>
            <Form.Item name="cluster_id" label="集群 ID（可选）" style={{ width: "100%" }}>
              <Input className="mono" />
            </Form.Item>
          </Space>
          {editTarget && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message="已回显当前连接配置，可直接修改"
              description="未修改的连接字段将保持原配置；修改连接配置后将重置健康状态并重新探活。"
            />
          )}
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={`Source ID 将由系统自动生成：${generated}`}
            description="不填 Database 时采集该实例下全部非系统库；填了则只采集指定库。"
          />
          <Space size={16} style={{ width: "100%" }} align="start">
            <Form.Item
              name="host"
              label="Host"
              // 编辑模式连接配置留空=保持原配置，故 Host 非必填；新建时才必填
              rules={editTarget ? [] : [{ required: true }]}
              style={{ width: "100%" }}
            >
              <Input className="mono" placeholder="127.0.0.1" />
            </Form.Item>
            <Form.Item name="port" label="Port" initialValue={3306} style={{ width: 130 }}>
              <Input type="number" className="mono" />
            </Form.Item>
          </Space>
          <Space size={16} style={{ width: "100%" }} align="start">
            <Form.Item
              name="database"
              label={dbEnumerated && dbOptions.length ? "Database（选择目标库）" : "Database（留空=采集全部库）"}
              style={{ width: "100%" }}
              tooltip="测试连接通过后可枚举实例下的非系统库；选择指定库则只采集该库，留空则采集全部"
            >
              {dbOptions.length ? (
                <Select
                  showSearch
                  allowClear
                  placeholder="全部库（默认）"
                  optionFilterProp="label"
                  loading={dbLoading}
                  options={dbOptions.map((d) => ({ value: d, label: d }))}
                />
              ) : (
                <Input className="mono" placeholder="留空则采集全部库" />
              )}
            </Form.Item>
            <Form.Item label=" " style={{ width: 120 }}>
              <Button icon={<DatabaseOutlined />} loading={dbLoading} onClick={loadDatabases} block>
                枚举库
              </Button>
            </Form.Item>
            {selType?.supports_schema && (
              <Form.Item name="schema" label="Schema" style={{ width: "100%" }} tooltip="PostgreSQL 库内 schema，默认 public">
                <Input className="mono" placeholder="public" />
              </Form.Item>
            )}
          </Space>
          <Space size={16} style={{ width: "100%" }} align="start">
            <Form.Item name="user" label="User" style={{ width: "100%" }}>
              <Input className="mono" placeholder="连接账号" />
            </Form.Item>
            <Form.Item name="password" label="Password" style={{ width: "100%" }}>
              <Input.Password className="mono" placeholder="连接密码" />
            </Form.Item>
          </Space>
          <Space style={{ marginBottom: 8 }}>
            <Button icon={<ApiOutlined />} onClick={handleTest}>
              测试连接
            </Button>
            <span className="muted" style={{ fontSize: 12 }}>创建前验证 Host / 端口 / 凭据可达性</span>
          </Space>
        </Form>
      </Modal>

      {detail && (
        <SourceDetailModal
          source={detail}
          types={types}
          deleting={deleting}
          onClose={() => setDetail(null)}
          onEdit={(s) => { setDetail(null); openEdit(s); }}
          onDelete={handleDeleteSource}
        />
      )}

      {/* 编辑保存且连接配置变更 → 引导重新采集 */}
      <Modal
        title="连接配置已变更"
        open={recollectSource !== null}
        onCancel={() => setRecollectSource(null)}
        onOk={async () => {
          const sid = recollectSource;
          setRecollectSource(null);
          if (!sid) return;
          try {
            await collectSourceNow(sid, "FULL");
            message.success("已触发重新采集，可到「采集任务中心」查看进度");
          } catch (err) {
            message.error(
              err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "触发采集失败",
            );
          }
        }}
        okText="立即重新采集"
        cancelText="稍后再说"
      >
        <p>
          数据源已更新。是否立即重新采集，以刷新该数据源下的元数据？旧配置采集的表会在下次全量采集后自动标记为「已废弃」。
        </p>
      </Modal>
    </div>
  );
}
