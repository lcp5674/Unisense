import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, InputNumber, Select, message, Space, Statistic, Row, Col, Descriptions, Alert, Progress, Collapse, Popconfirm, Switch, Divider } from "antd";
import { PlusOutlined, ThunderboltOutlined, ScheduleOutlined, ReloadOutlined, ApiOutlined, EditOutlined, DatabaseOutlined, DeleteOutlined, StopOutlined, PlayCircleOutlined, ArrowLeftOutlined } from "@ant-design/icons";
import {
  listDataSources,
  getDataSource,
  createDataSource,
  updateDataSource,
  deleteDataSource,
  batchToggleDataSources,
  batchDeleteDataSources,
  batchTestDataSources,
  batchScheduleDataSources,
  collectSourceNow,
  streamCollectionJob,
  getCollectionJob,
  scheduleSource,
  getSourceHealth,
  getSourceOverview,
  getSourceWatermark,
  listCollectionRuns,
  listAudit,
  listDataSourceTypes,
  listDomainTree,
  testDataSourceConnection,
  checkDataSourceConnection,
  listDataSourceDatabases,
  listDriftLogs,
  listUsers,
  UnisenseApiError,
} from "../api";
import type { DataSource, SourceHealth, SourceOverview, Watermark, CollectResult, SourceTypeInfo, TestConnectionResult, SourceType, SubjectDomainTreeNode, DataSourceCreateRequest, DataSourceUpdateRequest, CollectionProgress, BatchSourceResult, CollectionRun, UserBrief, AuditEntry } from "../types";
import type { DriftLogItem } from "../api";
import { ObjectView } from "../utils/display";
import { COLLECTION_MODE_LABEL, SOURCE_HEALTH_LABEL } from "../utils/enums";
import { formatCnTime } from "../utils/timeCn";
import { useResizableColumns } from "../components/ResizableTable";

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
  onToggleEnabled,
  deleting,
  toggling,
}: {
  source: DataSource;
  types: SourceTypeInfo[];
  onClose: () => void;
  onEdit: (source: DataSource) => void;
  onDelete: (source: DataSource) => void;
  onToggleEnabled: (source: DataSource) => void;
  deleting: boolean;
  toggling: boolean;
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
  const [overview, setOverview] = useState<SourceOverview | null>(null);
  const [runs, setRuns] = useState<CollectionRun[]>([]);
  const [audits, setAudits] = useState<AuditEntry[]>([]);

  useEffect(() => {
    getSourceHealth(source.source_id).then(setHealth).catch(() => {});
    getSourceWatermark(source.source_id).then(setWatermark).catch(() => {});
    getSourceOverview(source.source_id).then(setOverview).catch(() => {});
    listCollectionRuns({ source_id: source.source_id, page: 1, page_size: 5 })
      .then((res) => setRuns(res.items))
      .catch(() => setRuns([]));
    listAudit({ entity_type: "data_source", entity_id: source.source_id, page: 1, page_size: 8 })
      .then((res) => setAudits(res.items))
      .catch(() => setAudits([]));
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
        <Descriptions.Item label="状态">
          <Tag color={source.enabled ? "success" : "default"}>{source.enabled ? "启用中" : "已停用"}</Tag>
        </Descriptions.Item>
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

      {health?.health_status === "degraded" && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`健康降级（DEGRADED）：近 ${overview?.entity_types ? "" : ""}${health.health_metrics ? `成功率 ${Math.round((Number((health.health_metrics as Record<string, unknown>).success_rate) ?? 0) * 100)}%` : "采集失败率偏高"}，源仍可用但需关注`}
          description={
            health.degraded_since ? `降级起始：${formatCnTime(health.degraded_since)}` : undefined
          }
        />
      )}
      {health?.last_error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={`最近错误：${health.last_error}`} />}

      {overview && (
        <Card size="small" title="资产规模概览" style={{ marginBottom: 12 }}>
          <Row gutter={[16, 16]}>
            <Col span={6}><Statistic title="表 / 视图" value={`${overview.entity_types.TABLE ?? 0} / ${overview.entity_types.VIEW ?? 0}`} valueStyle={{ fontSize: 16 }} /></Col>
            <Col span={6}><Statistic title="字段总数" value={overview.total_fields} valueStyle={{ fontSize: 16 }} /></Col>
            <Col span={6}><Statistic title="PII 资产" value={overview.by_sensitivity.PII ?? 0} valueStyle={{ fontSize: 16, color: (overview.by_sensitivity.PII ?? 0) > 0 ? "var(--danger)" : undefined }} /></Col>
            <Col span={6}><Statistic title="Schema 漂移" value={overview.drift_count} valueStyle={{ fontSize: 16, color: overview.drift_count > 0 ? "var(--warning)" : undefined }} /></Col>
          </Row>
        </Card>
      )}

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
                render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : "—"),
              },
            ]}
          />
        </Card>
      )}

      {(runs.length > 0 || audits.length > 0) && (
        <Collapse
          size="small"
          style={{ marginBottom: 12 }}
          items={[
            ...(runs.length > 0
              ? [{
                  key: "runs",
                  label: `采集运行历史（${runs.length}）`,
                  children: (
                    <Table
                      size="small"
                      rowKey="id"
                      pagination={false}
                      dataSource={runs}
                      columns={[
                        { title: "触发", dataIndex: "trigger", width: 70, render: (v: string) => (v === "manual" ? "手动" : v === "scheduled" ? "定时" : v) },
                        { title: "状态", dataIndex: "status", width: 90, render: (v: string) => <Tag color={v === "COMPLETED" ? "success" : v === "FAILED" ? "error" : "processing"}>{v === "COMPLETED" ? "成功" : v === "FAILED" ? "失败" : "进行中"}</Tag> },
                        { title: "扫描", dataIndex: "scanned", width: 60 },
                        { title: "注册", dataIndex: "registered", width: 60 },
                        { title: "耗时", dataIndex: "duration_seconds", width: 80, render: (v: number | null) => (v != null ? `${v}s` : "—") },
                        { title: "时间", dataIndex: "started_at", render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : "—") },
                      ]}
                    />
                  ),
                }]
              : []),
            ...(audits.length > 0
              ? [{
                  key: "audits",
                  label: `操作审计时间线（${audits.length}）`,
                  children: (
                    <Table
                      size="small"
                      rowKey="id"
                      pagination={false}
                      dataSource={audits}
                      columns={[
                        { title: "操作", dataIndex: "action", width: 120, render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span> },
                        { title: "操作人", dataIndex: "actor_name", width: 90, render: (v: string | null) => v ?? "—" },
                        { title: "时间", dataIndex: "created_at", render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : "—") },
                      ]}
                    />
                  ),
                }]
              : []),
          ]}
        />
      )}

      <Space wrap>
        <Button type="primary" icon={<EditOutlined />} onClick={() => onEdit(source)}>
          编辑
        </Button>
        <Popconfirm
          title={source.enabled ? "停用数据源" : "启用数据源"}
          description={
            source.enabled
              ? `停用「${source.name}」后不再参与定时调度与手动采集，采集目录与历史血缘保留。`
              : `启用「${source.name}」后恢复参与定时调度与手动采集。`
          }
          okText={source.enabled ? "确认停用" : "确认启用"}
          cancelText="取消"
          onConfirm={() => onToggleEnabled(source)}
        >
          <Button icon={source.enabled ? <StopOutlined /> : <PlayCircleOutlined />} loading={toggling}>
            {source.enabled ? "停用" : "启用"}
          </Button>
        </Popconfirm>
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
        <Button type="link" onClick={() => navigate(`/collection-tasks?source_id=${encodeURIComponent(source.source_id)}`)}>
          采集任务 →
        </Button>
        <Button type="link" onClick={() => navigate(`/lineage?source=${encodeURIComponent(source.source_id)}`)}>
          血缘图 →
        </Button>
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
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // URL 直达参数（?kw=）作为初始筛选，避免「先查全量再过滤」的竞态覆盖
  const urlKw = searchParams.get("kw") ?? "";
  // 健康状态下钻（?health=，总览仪表「数据源」资产卡片）作为初始筛选
  const urlHealth = searchParams.get("health") ?? "";
  // 责任人（Owner）下钻（?owner_id=，总览仪表 Owner 责任分布）
  const urlOwnerId = searchParams.get("owner_id");
  const [items, setItems] = useState<DataSource[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [keyword, setKeyword] = useState(urlKw);
  const [health, setHealth] = useState<string>(urlHealth);
  const [ownerId, setOwnerId] = useState<number | undefined>(
    urlOwnerId && /^\d+$/.test(urlOwnerId) ? Number(urlOwnerId) : undefined,
  );
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<DataSource | null>(null);
  const [detail, setDetail] = useState<DataSource | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [toggling, setToggling] = useState(false);
  // 批量启停/删除：多选行 + 请求进行中标记
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchLoading, setBatchLoading] = useState(false);
  // 批量调度：cron 输入弹窗
  const [scheduleModalOpen, setScheduleModalOpen] = useState(false);
  const [batchCron, setBatchCron] = useState("0 3 * * *");
  const [types, setTypes] = useState<SourceTypeInfo[]>(FALLBACK_TYPES);
  const [domainOptions, setDomainOptions] = useState<Array<{ value: string; label: string }>>([]);
  // 数据库枚举（测试连接通过后自动列出，供选择目标库）
  const [dbOptions, setDbOptions] = useState<string[]>([]);
  const [dbLoading, setDbLoading] = useState(false);
  const [dbEnumerated, setDbEnumerated] = useState(false);
  // Owner 选择：用户列表（GET /auth/users）
  const [userOptions, setUserOptions] = useState<UserBrief[]>([]);
  const [form] = Form.useForm();
  // 编辑回显时的连接配置明文快照（用于判断用户是否实际修改了连接字段）
  const editConfigRef = useRef<Record<string, unknown> | null>(null);
  // 编辑保存且连接配置变更后，引导"立即重新采集"的目标数据源
  const [recollectSource, setRecollectSource] = useState<string | null>(null);
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果
  const loadSeq = useRef(0);
  const sourceType = Form.useWatch("source_type", form);
  const watchedDatabase = Form.useWatch("database", form);
  const watchedSchema = Form.useWatch("schema", form);
  const domainWatch = Form.useWatch("domain", form) ?? "";

  // 支持从全局搜索栏经 ?kw= 直达定位；初始值已由 useState 承接，
  // 此处仅同步「URL 出现新筛选值」的场景，并保留用户手动清空筛选的能力。
  useEffect(() => {
    if (urlKw && urlKw !== keyword) setKeyword(urlKw);
    if (urlKw) setPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw]);

  // 响应 URL 健康状态参数变化（总览仪表「数据源」资产卡片二次下钻）
  useEffect(() => {
    if (urlHealth && urlHealth !== health) {
      setHealth(urlHealth);
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlHealth]);

  // 响应 URL 责任人参数变化（Owner 责任分布二次下钻）；ownerId 在 load 依赖中自动重查
  useEffect(() => {
    if (urlOwnerId && /^\d+$/.test(urlOwnerId) && Number(urlOwnerId) !== ownerId) {
      setOwnerId(Number(urlOwnerId));
      setPage(1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlOwnerId]);

  async function load(nextPage = page, nextPageSize = pageSize) {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      // P1-1: 服务端分页（后端返回 {items, total, page, page_size}）
      const resp = await listDataSources({
        keyword: keyword || undefined,
        health: health || undefined,
        owner_id: ownerId,
        page: nextPage,
        page_size: nextPageSize,
      });
      // 已有更新的请求发起，丢弃本次过时响应（防竞态覆盖）
      if (seq !== loadSeq.current) return;
      setItems(resp.items);
      setTotal(resp.total);
    } catch (err) {
      if (seq !== loadSeq.current) return;
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  // 统一返回上一入口：优先回退浏览器历史（总览资产卡片/全局搜索等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
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
  }, [keyword, health, ownerId]);

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

  async function handleToggleEnabled(source: DataSource) {
    setToggling(true);
    try {
      const target = !source.enabled;
      await updateDataSource(source.source_id, { enabled: target });
      message.success(target ? `数据源「${source.name}」已启用` : `数据源「${source.name}」已停用`);
      setDetail((prev) => (prev && prev.source_id === source.source_id ? { ...prev, enabled: target } : prev));
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    } finally {
      setToggling(false);
    }
  }

  function batchFailSummary(result: BatchSourceResult): string {
    // 失败清单：source_id（原因）——批量 207 语义下逐项标注，便于治理定位
    return result.failed
      .map((f) => `${f.source_id}（${f.message ?? f.error_code ?? "失败"}）`)
      .join("、");
  }

  async function handleBatchToggle(enabled: boolean) {
    if (selectedRowKeys.length === 0) return;
    setBatchLoading(true);
    try {
      const ids = selectedRowKeys.map(String);
      const result = await batchToggleDataSources(ids, enabled);
      const action = enabled ? "启用" : "停用";
      if (result.failed.length > 0) {
        message.warning(`${action}完成 ${result.succeeded.length} 个，失败 ${result.failed.length} 个：${batchFailSummary(result)}`);
      } else {
        message.success(`已${action} ${result.succeeded.length} 个数据源`);
      }
      setSelectedRowKeys([]);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量操作失败");
    } finally {
      setBatchLoading(false);
    }
  }

  async function handleBatchDelete() {
    if (selectedRowKeys.length === 0) return;
    setBatchLoading(true);
    try {
      const ids = selectedRowKeys.map(String);
      const result = await batchDeleteDataSources(ids);
      if (result.failed.length > 0) {
        message.warning(`删除完成 ${result.succeeded.length} 个，失败 ${result.failed.length} 个：${batchFailSummary(result)}`);
      } else {
        message.success(`已删除 ${result.succeeded.length} 个数据源`);
      }
      setSelectedRowKeys([]);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量删除失败");
    } finally {
      setBatchLoading(false);
    }
  }

  async function handleBatchTest() {
    if (selectedRowKeys.length === 0) return;
    setBatchLoading(true);
    try {
      const ids = selectedRowKeys.map(String);
      const result = await batchTestDataSources(ids);
      if (result.failed.length > 0) {
        message.warning(`探活成功 ${result.succeeded.length} 个，失败 ${result.failed.length} 个：${batchFailSummary(result)}`);
      } else {
        message.success(`探活正常：${result.succeeded.length} 个数据源连接可用`);
      }
      setSelectedRowKeys([]);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量探活失败");
    } finally {
      setBatchLoading(false);
    }
  }

  async function handleBatchSchedule() {
    if (selectedRowKeys.length === 0) return;
    setBatchLoading(true);
    setScheduleModalOpen(false);
    try {
      const ids = selectedRowKeys.map(String);
      const result = await batchScheduleDataSources(ids, batchCron);
      if (result.failed.length > 0) {
        message.warning(`调度设置成功 ${result.succeeded.length} 个，失败 ${result.failed.length} 个：${batchFailSummary(result)}`);
      } else {
        message.success(`已为 ${result.succeeded.length} 个数据源设置调度 ${batchCron}`);
      }
      setSelectedRowKeys([]);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量调度失败");
    } finally {
      setBatchLoading(false);
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
      enabled: source.enabled,
      port: typeInfo(types, source.source_type)?.default_port ?? undefined,
    });
    setDbOptions([]);
    setDbEnumerated(false);
    setModalOpen(true);
    // 治理字段回显（创建时留空）
    if (source.description != null) form.setFieldsValue({ description: source.description });
    if (source.owner_id != null) form.setFieldsValue({ owner_id: source.owner_id });
    if (source.include_patterns?.length) form.setFieldsValue({ include_patterns: source.include_patterns.join("\n") });
    if (source.exclude_patterns?.length) form.setFieldsValue({ exclude_patterns: source.exclude_patterns.join("\n") });
    if (source.quota) {
      form.setFieldsValue({
        quota_max_concurrency: (source.quota as Record<string, unknown>).max_concurrency,
        quota_max_scan_rows: (source.quota as Record<string, unknown>).max_scan_rows,
      });
    }
    // Owner 候选用户列表
    listUsers().then(setUserOptions).catch(() => setUserOptions([]));
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
        // 停用/启用：编辑表单开关显式提交（区别于不传的 PATCH 语义）
        payload.enabled = Boolean(values.enabled);
        // 治理字段（PATCH 语义：仅提交用户填写项）
        if (typeof values.description === "string" && values.description.trim()) {
          payload.description = values.description.trim();
        }
        if (values.owner_id != null && values.owner_id !== "") {
          payload.owner_id = Number(values.owner_id);
        }
        const parsePatterns = (raw: unknown): string[] | undefined => {
          if (typeof raw !== "string" || !raw.trim()) return undefined;
          return raw.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean);
        };
        const includePatterns = parsePatterns(values.include_patterns);
        const excludePatterns = parsePatterns(values.exclude_patterns);
        if (includePatterns) payload.include_patterns = includePatterns;
        if (excludePatterns) payload.exclude_patterns = excludePatterns;
        if (values.quota_max_concurrency != null || values.quota_max_scan_rows != null) {
          payload.quota = {
            max_concurrency: values.quota_max_concurrency != null ? Number(values.quota_max_concurrency) : undefined,
            max_scan_rows: values.quota_max_scan_rows != null ? Number(values.quota_max_scan_rows) : undefined,
          };
        }
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
    {
      title: "名称",
      dataIndex: "name",
      key: "name",
      minWidth: 240,
      ellipsis: true,
      render: (v: string, s: DataSource) => (
        <div>
          <div>{v}</div>
          <div className="muted" style={{ fontSize: 12, fontFamily: "monospace" }}>{s.source_id}</div>
        </div>
      ),
    },
    {
      title: "类型",
      dataIndex: "source_type",
      key: "type",
      width: 120,
      render: (v: string) => <Tag>{typeInfo(types, v)?.label ?? v}</Tag>,
    },
    { title: "域", dataIndex: "domain", key: "domain", width: 110, ellipsis: true },
    {
      title: "健康",
      dataIndex: "health_status",
      key: "health",
      width: 96,
      render: (v: string) => (
        <Tag color={v === "healthy" ? "success" : v === "degraded" ? "warning" : v === "unhealthy" ? "error" : "default"}>
          {SOURCE_HEALTH_LABEL[v] ?? v}
        </Tag>
      ),
    },
    {
      title: "资产（表 / PII / 漂移）",
      key: "assets",
      width: 150,
      render: (_: unknown, s: DataSource) => (
        <span className="mono" style={{ fontSize: 12 }}>
          {s.table_count ?? 0} 表 · PII {s.pii_count ?? 0} · 漂移 {s.drift_count ?? 0}
        </span>
      ),
    },
    {
      title: "最近采集",
      dataIndex: "last_collected_at",
      key: "last_collected",
      width: 130,
      render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span className="muted">—</span>),
    },
    { title: "调度", dataIndex: "schedule_cron", key: "schedule", width: 110, render: (v: string | null) => (v ? <span className="mono">{v}</span> : <span className="muted">—</span>) },
    {
      title: "状态",
      dataIndex: "enabled",
      key: "enabled",
      width: 80,
      render: (v: boolean) => <Tag color={v ? "success" : "default"}>{v ? "启用" : "停用"}</Tag>,
    },
    {
      title: "操作",
      key: "actions",
      width: 90,
      render: (_: unknown, s: DataSource) => (
        <Button type="link" onClick={() => setDetail(s)}>管理</Button>
      ),
    },
  ];

  const { columns: resizableColumns, components: resizableComponents } = useResizableColumns<DataSource>(
    columns,
    "unisense:data-sources-col-widths",
  );

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
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
          <Select
            allowClear
            placeholder="全部健康状态"
            style={{ width: 140 }}
            value={health || undefined}
            onChange={(v?: string) => { setHealth(v ?? ""); setPage(1); }}
            options={[
              { value: "healthy", label: "健康" },
              { value: "unhealthy", label: "异常" },
              { value: "unknown", label: "未知" },
            ]}
          />
          {selectedRowKeys.length > 0 && (
            <span style={{ color: "rgba(0,0,0,0.45)", fontSize: 13 }}>
              已选 {selectedRowKeys.length} 项
            </span>
          )}
          <Button
            icon={<PlayCircleOutlined />}
            onClick={() => handleBatchToggle(true)}
            disabled={selectedRowKeys.length === 0 || batchLoading}
          >
            批量启用
          </Button>
          <Popconfirm
            title="批量停用"
            description={`确定停用选中的 ${selectedRowKeys.length} 个数据源？停用后不再参与定时调度与手动采集，采集目录与历史血缘保留。`}
            okText="确认停用"
            onConfirm={() => handleBatchToggle(false)}
            disabled={selectedRowKeys.length === 0 || batchLoading}
          >
            <Button icon={<StopOutlined />} disabled={selectedRowKeys.length === 0 || batchLoading}>
              批量停用
            </Button>
          </Popconfirm>
          <Popconfirm
            title="批量删除"
            description={`确定删除选中的 ${selectedRowKeys.length} 个数据源？其采集目录、水位、漂移日志将一并清理，删除后原 ID 可重建同名数据源。`}
            okText="确认删除"
            okButtonProps={{ danger: true }}
            onConfirm={handleBatchDelete}
            disabled={selectedRowKeys.length === 0 || batchLoading}
          >
            <Button danger icon={<DeleteOutlined />} disabled={selectedRowKeys.length === 0 || batchLoading}>
              批量删除
            </Button>
          </Popconfirm>
          <Popconfirm
            title="批量探活"
            description={`用已存连接配置逐条探测选中的 ${selectedRowKeys.length} 个数据源，并更新健康状态。`}
            okText="开始探活"
            onConfirm={handleBatchTest}
            disabled={selectedRowKeys.length === 0 || batchLoading}
          >
            <Button icon={<ApiOutlined />} disabled={selectedRowKeys.length === 0 || batchLoading}>
              批量探活
            </Button>
          </Popconfirm>
          <Button
            icon={<ScheduleOutlined />}
            onClick={() => setScheduleModalOpen(true)}
            disabled={selectedRowKeys.length === 0 || batchLoading}
          >
            批量调度
          </Button>
        </Space>
        <Table
          dataSource={items}
          columns={resizableColumns}
          components={resizableComponents}
          rowKey="source_id"
          tableLayout="fixed"
          scroll={{ x: "max" }}
          loading={loading}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys),
          }}
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
            <Form.Item
              name="enabled"
              label="启用状态"
              valuePropName="checked"
              style={{ marginBottom: 16 }}
              tooltip="停用后该数据源不再参与定时调度，手动采集/刷新/异步入队也会被拒绝；采集目录与历史血缘保留"
            >
              <Switch checkedChildren="启用" unCheckedChildren="停用" />
            </Form.Item>
          )}
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
          <Divider style={{ margin: "8px 0" }} />
          <Form.Item name="description" label="用途描述" style={{ width: "100%" }}>
            <Input.TextArea rows={2} placeholder="该数据源的业务用途、责任范围（治理信息，不参与采集）" maxLength={2000} showCount />
          </Form.Item>
          <Space style={{ width: "100%" }}>
            <Form.Item name="owner_id" label="负责人" style={{ width: 220 }}>
              <Select
                allowClear
                showSearch
                placeholder="选择数据源负责人"
                optionFilterProp="label"
                options={userOptions
                  .filter((u) => u.status === "active")
                  .map((u) => ({ value: u.id, label: `${u.display_name}（${u.username}）` }))}
              />
            </Form.Item>
            <Form.Item name="quota_max_concurrency" label="并发上限" tooltip="扫描并发数（max_concurrency）" style={{ width: 120 }}>
              <InputNumber min={1} placeholder="默认" style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item name="quota_max_scan_rows" label="扫描行上限" tooltip="单次扫描行数上限（max_scan_rows），超出拒绝采集" style={{ width: 140 }}>
              <InputNumber min={1} placeholder="默认" style={{ width: "100%" }} />
            </Form.Item>
          </Space>
          <Form.Item
            name="include_patterns"
            label="表级白名单"
            tooltip="fnmatch 风格，每行一个；填写后仅采集匹配的表（如 orders_*, dim_*）。留空=采集全部"
            style={{ width: "100%" }}
          >
            <Input.TextArea rows={2} placeholder={"每行一个模式，如：\nods_*\ndwd_*\ndim_*}".replace("}*", "*}")} />
          </Form.Item>
          <Form.Item
            name="exclude_patterns"
            label="表级黑名单"
            tooltip="fnmatch 风格，每行一个；命中即排除（如 tmp_*, *_bak）。白名单优先于黑名单"
            style={{ width: "100%" }}
          >
            <Input.TextArea rows={2} placeholder={"每行一个模式，如：\ntmp_*\n*_bak".replace("_*}", "")} />
          </Form.Item>
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
          toggling={toggling}
          onClose={() => setDetail(null)}
          onEdit={(s) => { setDetail(null); openEdit(s); }}
          onDelete={handleDeleteSource}
          onToggleEnabled={handleToggleEnabled}
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

      {/* 批量调度：统一设置 cron */}
      <Modal
        title={`批量设置调度（${selectedRowKeys.length} 个数据源）`}
        open={scheduleModalOpen}
        onCancel={() => setScheduleModalOpen(false)}
        onOk={handleBatchSchedule}
        okText="批量设置"
        confirmLoading={batchLoading}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Select
            value={batchCron}
            onChange={(v) => setBatchCron(v)}
            style={{ width: "100%" }}
            options={[
              { value: "0 3 * * *", label: "每日 03:00（0 3 * * *）" },
              { value: "0 */6 * * *", label: "每 6 小时（0 */6 * * *）" },
              { value: "30 1 * * 1", label: "每周一 01:30（30 1 * * 1）" },
              { value: "0 1 1 * *", label: "每月 1 日 01:00（0 1 1 * *）" },
            ]}
          />
          <Input
            className="mono"
            value={batchCron}
            onChange={(e) => setBatchCron(e.target.value)}
            placeholder="自定义 cron 表达式"
          />
          <span className="muted" style={{ fontSize: 12 }}>
            将为选中的数据源统一覆盖调度表达式；停用的数据源不会触发定时采集。
          </span>
        </Space>
      </Modal>
    </div>
  );
}
