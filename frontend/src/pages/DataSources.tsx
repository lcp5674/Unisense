import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, InputNumber, Select, message, Space, Statistic, Row, Col, Descriptions, Alert, Progress, Collapse, Popconfirm, Switch, Divider, Tooltip, Radio } from "antd";
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
  listDataSourceTypes,
  listDomainTree,
  testDataSourceConnection,
  checkDataSourceConnection,
  listDataSourceDatabases,
  listDataSourceTables,
  listDriftLogs,
  listUsers,
  UnisenseApiError,
} from "../api";
import type { DataSource, SourceHealth, SourceOverview, Watermark, CollectResult, SourceTypeInfo, TestConnectionResult, SourceType, SubjectDomainTreeNode, DataSourceCreateRequest, DataSourceUpdateRequest, CollectionProgress, BatchSourceResult, CollectionRun, UserBrief } from "../types";
import type { DriftLogItem } from "../api";
import { ObjectView } from "../utils/display";
import { COLLECTION_MODE_LABEL, SOURCE_HEALTH_LABEL } from "../utils/enums";
import { formatCnTime } from "../utils/timeCn";
import { useResizableColumns } from "../components/ResizableTable";
import { AuditTimeline } from "./metric/AuditTimeline";
import { usePermission } from "../hooks/usePermission";

const FALLBACK_TYPES: SourceTypeInfo[] = [
  { source_type: "mysql", label: "MySQL", default_port: 3306, supports_database: true, supports_schema: false, description: "关系型数据库" },
  { source_type: "postgres", label: "PostgreSQL", default_port: 5432, supports_database: true, supports_schema: true, description: "关系型数据库" },
  { source_type: "hive", label: "Hive", default_port: 10000, supports_database: true, supports_schema: false, description: "数据仓库" },
  { source_type: "hive_metastore", label: "Hive Metastore", default_port: 3306, supports_database: true, supports_schema: false, description: "Hive 元数据直连（HMS backend 为 MySQL）" },
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

/** 解析「目标数据库」表单值 → 数组（多选数组 / 逗号分隔输入串 / 空 → []）。 */
function parseDatabases(raw: unknown): string[] {
  if (Array.isArray(raw)) return (raw as unknown[]).filter((x): x is string => typeof x === "string" && Boolean(x.trim()));
  if (typeof raw === "string" && raw.trim()) {
    return raw.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean);
  }
  return [];
}

function SourceDetailModal({
  source,
  types,
  onClose,
  onEdit,
  onDelete,
  onToggleEnabled,
  onScheduleSaved,
  deleting,
  toggling,
}: {
  source: DataSource;
  types: SourceTypeInfo[];
  onClose: () => void;
  onEdit: (source: DataSource) => void;
  onDelete: (source: DataSource) => void;
  onToggleEnabled: (source: DataSource) => void;
  /** 调度配置保存成功后触发（主列表刷新，保证「调度」列状态即时一致） */
  onScheduleSaved?: () => void;
  deleting: boolean;
  toggling: boolean;
}) {
  const navigate = useNavigate();
  const { can } = usePermission();
  const [health, setHealth] = useState<SourceHealth | null>(null);
  const [watermark, setWatermark] = useState<Watermark | null>(null);
  const [collecting, setCollecting] = useState(false);
  const [collectResult, setCollectResult] = useState<CollectResult | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [progress, setProgress] = useState<CollectionProgress | null>(null);
  const [progressMessages, setProgressMessages] = useState<string[]>([]);
  const abortRef = useRef<(() => void) | null>(null);
  const [cron, setCron] = useState(source.schedule_cron ?? "0 3 * * *");
  // 调度启停（独立于数据源 enabled：停用调度仅暂停自动定时，源仍可手动采集）
  const [scheduleEnabled, setScheduleEnabled] = useState(source.schedule_enabled ?? true);
  // 立即采集弹窗：模式 + 本次临时白/黑名单（仅本次生效，不污染数据源配置）
  const [collectModalOpen, setCollectModalOpen] = useState(false);
  const [collectMode, setCollectMode] = useState("FULL");
  const [collectInclude, setCollectInclude] = useState("");
  const [collectExclude, setCollectExclude] = useState("");
  const [checking, setChecking] = useState(false);
  const [checkResult, setCheckResult] = useState<TestConnectionResult | null>(null);
  const [driftLogs, setDriftLogs] = useState<DriftLogItem[]>([]);
  const [overview, setOverview] = useState<SourceOverview | null>(null);
  const [runs, setRuns] = useState<CollectionRun[]>([]);

  useEffect(() => {
    getSourceHealth(source.source_id).then(setHealth).catch(() => {});
    getSourceWatermark(source.source_id).then(setWatermark).catch(() => {});
    getSourceOverview(source.source_id).then(setOverview).catch(() => {});
    listCollectionRuns({ source_id: source.source_id, page: 1, page_size: 5 })
      .then((res) => setRuns(res.items))
      .catch(() => setRuns([]));
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
      dsd_count: Number(detail.dsd_count ?? 0),
      entities: (detail.entities as CollectResult["entities"]) ?? [],
      filtered_count: Number(detail.filtered_count ?? 0),
      filtered_names: (detail.filtered_names as CollectResult["filtered_names"]) ?? [],
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
    // 本次临时过滤：白/黑名单留空 = 不传（worker 回退到数据源配置）
    const include = collectInclude.trim()
      ? collectInclude.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean)
      : undefined;
    const exclude = collectExclude.trim()
      ? collectExclude.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean)
      : undefined;
    setCollecting(true);
    setCollectResult(null);
    setProgress(null);
    setProgressMessages([]);
    setJobId(null);
    setCollectModalOpen(false);
    try {
      const { job_id } = await collectSourceNow(source.source_id, collectMode, {
        include_patterns: include,
        exclude_patterns: exclude,
      });
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
    // 防静默覆盖：cron 已回显真实配置；空值/未变更时给出明确提示，不误提交默认值
    if (!cron.trim()) {
      message.warning("请填写 cron 表达式（如 0 3 * * *）");
      return;
    }
    if (cron.trim() === (source.schedule_cron ?? "").trim() && scheduleEnabled === (source.schedule_enabled ?? true)) {
      message.info("调度配置未变化");
      return;
    }
    try {
      const res = await scheduleSource(source.source_id, cron.trim(), scheduleEnabled);
      if (res?.scheduled) {
        if (scheduleEnabled) {
          message.success(`已启用定时调度：${res.cron}（${COLLECTION_MODE_LABEL[source.collection_mode] ?? source.collection_mode}）`);
        } else {
          message.success("已停用定时调度（cron 保留，源仍可手动采集）");
        }
      } else {
        message.success("调度配置已保存");
      }
      // 刷新主列表，保证「调度」列启停状态与详情弹窗即时一致
      onScheduleSaved?.();
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
          <Statistic title="最近采集" value={watermark?.last_collected_at ? formatCnTime(watermark.last_collected_at) : "从未"} valueStyle={{ fontSize: 16 }} />
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
          message={`采集结果：注册 ${collectResult.registered} · PII ${collectResult.pii_registered} · 漂移 ${collectResult.drift_count}${(collectResult.dsd_count ?? 0) > 0 ? ` · 下线指标 ${collectResult.dsd_count}` : ""}${(collectResult.filtered_count ?? 0) > 0 ? ` · 过滤跳过 ${collectResult.filtered_count} 张表` : ""}`}
          description={
            <div>
              <div style={{ marginBottom: 4 }}>
                {collectResult.drift_events?.length
                  ? collectResult.drift_events.slice(0, 5).map((d) => `${d.entity_name} (${DRIFT_CHANGE_LABEL[d.change_type] ?? d.change_type})`).join("、")
                  : "无 schema 漂移"}
              </div>
              {(collectResult.filtered_count ?? 0) > 0 && (
                <div style={{ marginBottom: 4, color: "var(--muted)" }}>
                  被白/黑名单过滤跳过的表（{collectResult.filtered_count} 张）：
                  {collectResult.filtered_names?.slice(0, 8).join("、")}
                  {(collectResult.filtered_count ?? 0) > 8 ? " …" : ""}
                </div>
              )}
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
          {
            key: "audits",
            label: "操作审计时间线",
            children: (
              <AuditTimeline
                entityType="data_source"
                entityId={source.source_id}
                emptyText="暂无该数据源的操作记录"
              />
            ),
          },
          ]}
        />

      <Space wrap>
        {can("data-source:edit") && (
          <Button type="primary" icon={<EditOutlined />} onClick={() => onEdit(source)}>
            编辑
          </Button>
        )}
        {can("data-source:edit") && (
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
        )}
        {can("data-source:collect") && (
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            loading={collecting}
            onClick={() => {
              setCollectMode(source.collection_mode === "INCREMENTAL" ? "INCREMENTAL" : "FULL");
              setCollectInclude("");
              setCollectExclude("");
              setCollectModalOpen(true);
            }}
          >
            立即采集
          </Button>
        )}
        {can("data-source:test-connection") && (
          <Tooltip title="对已保存的数据源实时探活，放行内网/私有地址；与创建表单的「测试连接」不同（后者受 SSRF 安全策略限制，拒绝内网/回环地址）">
            <Button icon={<ApiOutlined />} loading={checking} onClick={handleCheck}>
              测试连接
            </Button>
          </Tooltip>
        )}
        <Tooltip title="定时采集按数据源的默认采集模式执行（可在编辑表单修改）">
          <Input
            className="mono"
            value={cron}
            onChange={(e) => setCron(e.target.value)}
            style={{ width: 150 }}
            placeholder="cron"
          />
        </Tooltip>
        <Tooltip title="停用调度后保留 cron 配置但不自动触发，源仍可手动采集">
          <Switch
            checked={scheduleEnabled}
            onChange={setScheduleEnabled}
            checkedChildren="调度开"
            unCheckedChildren="调度关"
          />
        </Tooltip>
        {can("data-source:collect") && (
          <Button icon={<ScheduleOutlined />} onClick={handleSchedule}>保存调度</Button>
        )}
        <Button type="link" onClick={() => navigate(`/collection-tasks?source_id=${encodeURIComponent(source.source_id)}`)}>
          采集任务 →
        </Button>
        <Button type="link" onClick={() => navigate(`/lineage?source=${encodeURIComponent(source.source_id)}`)}>
          血缘图 →
        </Button>
        {can("data-source:delete") && (
          <Popconfirm
            title="删除数据源"
            description={`确定删除「${source.name}」？数据源将软删除，其采集目录保留以便追溯，可在采集目录按「已删除源」筛选查看；删除后原 ID 可重建同名数据源。`}
            okText="确认删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => onDelete(source)}
          >
            <Button danger icon={<DeleteOutlined />} loading={deleting}>删除</Button>
          </Popconfirm>
        )}
      </Space>

      {/* 立即采集弹窗：选择模式 + 本次临时白/黑名单（仅本次生效，不污染数据源配置） */}
      <Modal
        title={`立即采集：${source.name}`}
        open={collectModalOpen}
        onCancel={() => setCollectModalOpen(false)}
        onOk={handleCollect}
        confirmLoading={collecting}
        okText="开始采集"
        cancelText="取消"
        width={560}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Form layout="vertical" style={{ marginTop: 4 }}>
            <Form.Item
              label="本次采集模式"
              tooltip="默认跟随数据源的默认采集模式，可临时覆盖本次；不修改数据源保存的模式"
            >
              <Radio.Group
                value={collectMode}
                onChange={(e) => setCollectMode(e.target.value)}
                options={[
                  { value: "FULL", label: "全量（扫描全部匹配表）" },
                  { value: "INCREMENTAL", label: "增量（仅变更表）" },
                ]}
              />
            </Form.Item>
            <Form.Item
              label="本次临时白名单（仅采集匹配的表，留空=按数据源配置）"
              tooltip="fnmatch 风格，每行一个，如 ods_*、dim_*；仅本次采集生效，不修改数据源保存的规则"
            >
              <Input.TextArea
                rows={2}
                value={collectInclude}
                onChange={(e) => setCollectInclude(e.target.value)}
                placeholder={"每行一个模式，如：\nods_*\ndwd_*"}
              />
            </Form.Item>
            <Form.Item
              label="本次临时黑名单（命中即排除，留空=按数据源配置）"
              tooltip="fnmatch 风格，每行一个，如 tmp_*、*_bak；白名单优先于黑名单；仅本次采集生效"
            >
              <Input.TextArea
                rows={2}
                value={collectExclude}
                onChange={(e) => setCollectExclude(e.target.value)}
                placeholder={"每行一个模式，如：\ntmp_*\n*_bak"}
              />
            </Form.Item>
          </Form>
          <span className="muted" style={{ fontSize: 12 }}>
            本次临时过滤仅对这次采集生效，数据源保存的采集规则不会被修改。
          </span>
        </Space>
      </Modal>
    </Modal>
  );
}

export function DataSources() {
  const navigate = useNavigate();
  const { can } = usePermission();
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
  // 搜索输入防抖（Med 5）：inputValue 即时更新保证输入响应，keyword 延迟 350ms
  // 更新触发查询，避免每击键发 load + listDataSourceTypes + listDomainTree 三个请求
  const [keywordInput, setKeywordInput] = useState(urlKw);
  const keywordTimer = useRef<number | null>(null);
  const handleKeywordChange = useCallback((value: string) => {
    setKeywordInput(value);
    if (keywordTimer.current !== null) window.clearTimeout(keywordTimer.current);
    keywordTimer.current = window.setTimeout(() => setKeyword(value), 350);
  }, []);
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
  // 表级联枚举（选中目标库后按库分组列出表，供选择采集范围）
  const [tableOptions, setTableOptions] = useState<Record<string, string[]>>({});
  const [tableLoading, setTableLoading] = useState(false);
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

  // 类型切换时自动带出默认端口，并清空已枚举的数据库/表列表
  function handleTypeChange(t: string) {
    const info = typeInfo(types, t);
    if (info?.default_port) {
      form.setFieldValue("port", info.default_port);
    }
    // hive_metastore 的 database 是必填连接凭据（HMS 元数据库名），自动预填默认 hive；
    // 用户可改，其余类型保持原值不动
    if (info?.source_type === "hive_metastore") {
      const cur = form.getFieldValue("database");
      if (!cur) form.setFieldValue("database", "hive");
    }
    setDbOptions([]);
    setTableOptions({});
    form.setFieldValue("selected_tables", []);
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
      if (res.databases.length === 0) {
        message.info("未发现可采集的数据库（可留空采集全部库）");
      } else {
        message.success(`已枚举到 ${res.databases.length} 个数据库`);
      }
    } catch (err) {
      setDbOptions([]);
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "枚举数据库失败");
    } finally {
      setDbLoading(false);
    }
  }

  /** 枚举选中目标库下的表（按库分组），供表级联选（防旧请求覆盖新选择）。 */
  async function loadTables(dbs: string[]) {
    const values = form.getFieldsValue();
    if (!values.source_type || !values.host || !dbs.length) {
      setTableOptions({});
      return;
    }
    const cfg = buildConnectionConfig(values);
    setTableLoading(true);
    try {
      const res = await listDataSourceTables({
        source_type: String(values.source_type) as SourceType,
        connection_config: cfg,
        databases: dbs,
      });
      const currentDbs = parseDatabases(form.getFieldValue("databases"));
      const kept: Record<string, string[]> = {};
      for (const db of dbs) {
        if (currentDbs.includes(db) && res.tables[db]) kept[db] = res.tables[db];
      }
      setTableOptions(kept);
    } catch (err) {
      setTableOptions({});
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "枚举表失败");
    } finally {
      setTableLoading(false);
    }
  }

  /** 目标库变化：清空表级联选择并重新枚举新库下的表。 */
  function handleDatabasesChange(dbs: string[]) {
    setTableOptions({});
    form.setFieldValue("selected_tables", []);
    if (dbs.length) loadTables(dbs);
  }

  /** 表选择变化：为每个目标库生成 include_patterns（选表库→库.表；未选表库→库.* 整库）。 */
  function handleTablesChange(tables: string[]) {
    const dbs = parseDatabases(form.getFieldValue("databases"));
    if (!dbs.length) return;
    const patterns = dbs.map((db) => {
      const sel = tables.filter((t) => t.startsWith(`${db}.`));
      return sel.length ? sel : [`${db}.*`];
    });
    form.setFieldValue("include_patterns", patterns.flat().join("\n"));
  }

  /** 从 include_patterns 反解可视化选表（库.表 → 选表；库.* 与裸模式 → 跳过）。 */
  function parsePatternsToTables(patterns: string[] | null | undefined): string[] {
    if (!patterns?.length) return [];
    const tables: string[] = [];
    for (const p of patterns) {
      const dot = p.indexOf(".");
      if (dot <= 0) continue;
      const rest = p.slice(dot + 1);
      if (rest === "*" || rest.includes("*") || rest.includes("?")) continue;
      tables.push(`${p.slice(0, dot)}.${rest}`);
    }
    return tables;
  }

  function buildConnectionConfig(values: Record<string, unknown>): Record<string, unknown> {
    const cfg: Record<string, unknown> = {};
    // 空 host 剔除（Med 9）：编辑模式非 admin 未回显 host，提交空值会覆盖真实
    // host 使源不可用；新建时由表单校验保证必填，编辑模式允许留空（=保持原配置），
    // 故测试连接前置拦截（handleTest）缺 host 的情况，此处仅为防御。
    if (values.host && String(values.host).trim()) {
      cfg.host = String(values.host).trim();
    }
    if (values.port) cfg.port = Number(values.port);
    // hive_metastore 的 database 是纯连接凭据（HMS 元数据库名），漏填默认 hive——
    // 与后端 schemas 默认值一致，避免无库直连报 1046 'No database selected'
    const sourceTypeValue = String(values.source_type || "");
    if (values.database) {
      cfg.database = String(values.database);
    } else if (sourceTypeValue === "hive_metastore") {
      cfg.database = "hive";
    }
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
    // 编辑模式 Host 非必填（留空=保持原配置），空 Host 会生成缺连接地址的配置，
    // 后端以 422 拒绝且提示不可读。这里前置拦截，给出明确可读的指引。
    if (!cfg.host) {
      message.warning("请填写 Host（连接地址）后再测试连接");
      return;
    }
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
      }
    } catch (err) {
      // 422 多为请求校验失败（如连接配置缺字段），后端 detail 不进入统一信封，
      // 这里给出可读提示而非「请求失败 (HTTP 422)（HTTP_ERROR）」
      message.error(
        err instanceof UnisenseApiError && err.status === 422
          ? "连接配置不完整，请检查 Host / 端口 / 凭据后重试"
          : err instanceof UnisenseApiError
            ? `${err.message}（${err.codeZh}）`
            : "测试失败",
      );
    }
  }

  function openCreate() {
    setEditTarget(null);
    form.resetFields();
    form.setFieldsValue({ source_type: undefined, port: 3306, databases: [], collection_mode: "FULL" });
    setDbOptions([]);
    setTableOptions({});
    form.setFieldValue("selected_tables", []);
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
    setModalOpen(true);
    // 治理字段回显（创建时留空）
    if (source.description != null) form.setFieldsValue({ description: source.description });
    if (source.owner_id != null) form.setFieldsValue({ owner_id: source.owner_id });
    if (source.include_patterns?.length) form.setFieldsValue({ include_patterns: source.include_patterns.join("\n") });
    if (source.exclude_patterns?.length) form.setFieldsValue({ exclude_patterns: source.exclude_patterns.join("\n") });
    // 多目标库回显（数组；枚举库后 Select multiple 展示 tags，未枚举时 Input 逗号展示）
    form.setFieldsValue({ databases: source.databases ? [...source.databases] : [] });
    // 表级联回显：从 include_patterns 反解已选表（库.表 → 选中；库.*/裸模式 → 整库不选）
    form.setFieldValue("selected_tables", parsePatternsToTables(source.include_patterns));
    // 默认采集模式回显
    form.setFieldsValue({ collection_mode: source.collection_mode || "FULL" });
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
        }
        // 连接配置就绪（host 可读）后自动枚举库与表，使级联选表立即可见（best-effort）
        if (source.databases?.length) {
          loadDatabases();
          loadTables(source.databases);
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
    const parsePatterns = (raw: unknown): string[] | undefined => {
      if (typeof raw !== "string" || !raw.trim()) return undefined;
      return raw.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean);
    };
    // 高级连接选项折叠面板内的字段（include/exclude）不在 onFinish values 中，
    // 须从表单 store 读取（forceRender 保证 DOM 挂载，但 onFinish 不收集折叠面板字段）
    const includePatterns = parsePatterns(form.getFieldValue("include_patterns"));
    const excludePatterns = parsePatterns(form.getFieldValue("exclude_patterns"));
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
        if (includePatterns) payload.include_patterns = includePatterns;
        if (excludePatterns) payload.exclude_patterns = excludePatterns;
        // 多目标库（PATCH 语义：仅当与原有配置不同才提交；[] 表示清空回全部库）
        const dbList = parseDatabases(values.databases);
        const prevDbs = editTarget.databases ?? [];
        if (JSON.stringify(dbList) !== JSON.stringify(prevDbs)) {
          payload.databases = dbList;
        }
        // 默认采集模式（PATCH 语义：仅当与原有配置不同才提交）
        const newMode = String(values.collection_mode ?? "FULL");
        if (newMode !== (editTarget.collection_mode || "FULL")) {
          payload.collection_mode = newMode;
        }
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
        // 多目标库：选择/输入了目标库则提交（多库采集），否则留空按连接库/全部非系统库
        const dbList = parseDatabases(values.databases);
        if (dbList.length) payload.databases = dbList;
        // 表级白/黑名单（可视化选表自动生成，亦可高级模式手填）
        if (includePatterns) payload.include_patterns = includePatterns;
        if (excludePatterns) payload.exclude_patterns = excludePatterns;
        // 默认采集模式
        payload.collection_mode = String(values.collection_mode ?? "FULL");
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
  // Hive Metastore 的 database 是纯连接凭据（HMS 元数据库名），区别于普通关系库的"可选连接库"
  const isHms = selType?.source_type === "hive_metastore";
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
      title: "覆盖度",
      key: "coverage",
      width: 100,
      render: (_: unknown, s: DataSource) => {
        const c = Math.round((s.coverage ?? 0) * 100);
        return (
          <Progress
            percent={c}
            size="small"
            strokeColor={c > 0 ? undefined : "#d9d9d9"}
            format={(p) => `${p}%`}
          />
        );
      },
    },
    {
      title: "资产 / 采集",
      key: "assets",
      width: 180,
      render: (_: unknown, s: DataSource) => (
        <div style={{ fontSize: 12 }}>
          <div className="mono">
            {s.table_count ?? 0} 表 · PII {s.pii_count ?? 0} · 漂移 {s.drift_count ?? 0}
          </div>
          <div className="mono" style={{ color: (s.failed_count ?? 0) > 0 ? "var(--danger)" : "rgba(0,0,0,0.45)" }}>
            累计扫描 {s.scanned_count ?? 0} · 失败 {s.failed_count ?? 0}
          </div>
        </div>
      ),
    },
    {
      title: "最近采集",
      dataIndex: "last_collected_at",
      key: "last_collected",
      width: 130,
      render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span className="muted">—</span>),
    },
    {
      title: "采集模式",
      dataIndex: "collection_mode",
      key: "mode",
      width: 96,
      render: (v: string) => <Tag color={v === "FULL" ? "blue" : "purple"}>{COLLECTION_MODE_LABEL[v] ?? v}</Tag>,
    },
    {
      title: "调度",
      key: "schedule",
      width: 130,
      render: (_: unknown, s: DataSource) => {
        if (!s.schedule_cron) return <span className="muted">—</span>;
        return (
          <div>
            <div className="mono">{s.schedule_cron}</div>
            <Tag color={s.schedule_enabled ? "green" : "default"} style={{ marginTop: 2 }}>
              {s.schedule_enabled ? "已启用" : "已停用"}
            </Tag>
          </div>
        );
      },
    },
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
        {can("data-source:create") && (
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建数据源</Button>
        )}
      </div>

      <Card extra={<Button icon={<ReloadOutlined />} onClick={() => load()} loading={loading}>刷新</Button>}>
        <Space style={{ marginBottom: 12 }}>
          <Input.Search
            placeholder="搜索数据源名称 / ID"
            allowClear
            style={{ width: 260 }}
            value={keywordInput}
            onChange={(e) => handleKeywordChange(e.target.value)}
            onSearch={() => {
              // 回车/点搜索立即查询（绕过防抖），并清空待触发 timer
              if (keywordTimer.current !== null) window.clearTimeout(keywordTimer.current);
              setKeyword(keywordInput);
              setPage(1);
              load(1, pageSize);
            }}
          />
          <Select
            allowClear
            placeholder="全部健康状态"
            style={{ width: 140 }}
            value={health || undefined}
            onChange={(v?: string) => { setHealth(v ?? ""); setPage(1); }}
            options={[
              { value: "healthy", label: "健康" },
              { value: "degraded", label: "降级" },
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
            disabled={!can("data-source:edit") || selectedRowKeys.length === 0 || batchLoading}
          >
            批量启用
          </Button>
          <Popconfirm
            title="批量停用"
            description={`确定停用选中的 ${selectedRowKeys.length} 个数据源？停用后不再参与定时调度与手动采集，采集目录与历史血缘保留。`}
            okText="确认停用"
            onConfirm={() => handleBatchToggle(false)}
            disabled={!can("data-source:edit") || selectedRowKeys.length === 0 || batchLoading}
          >
            <Button icon={<StopOutlined />} disabled={!can("data-source:edit") || selectedRowKeys.length === 0 || batchLoading}>
              批量停用
            </Button>
          </Popconfirm>
          <Popconfirm
            title="批量删除"
            description={`确定删除选中的 ${selectedRowKeys.length} 个数据源？数据源将软删除，其采集目录保留以便追溯，可在采集目录按「已删除源」筛选查看；删除后原 ID 可重建同名数据源。`}
            okText="确认删除"
            okButtonProps={{ danger: true }}
            onConfirm={handleBatchDelete}
            disabled={!can("data-source:delete") || selectedRowKeys.length === 0 || batchLoading}
          >
            <Button danger icon={<DeleteOutlined />} disabled={!can("data-source:delete") || selectedRowKeys.length === 0 || batchLoading}>
              批量删除
            </Button>
          </Popconfirm>
          <Popconfirm
            title="批量探活"
            description={`用已存连接配置逐条探测选中的 ${selectedRowKeys.length} 个数据源，并更新健康状态。`}
            okText="开始探活"
            onConfirm={handleBatchTest}
            disabled={!can("data-source:test-connection") || selectedRowKeys.length === 0 || batchLoading}
          >
            <Button icon={<ApiOutlined />} disabled={!can("data-source:test-connection") || selectedRowKeys.length === 0 || batchLoading}>
              批量探活
            </Button>
          </Popconfirm>
          <Button
            icon={<ScheduleOutlined />}
            onClick={() => setScheduleModalOpen(true)}
            disabled={!can("data-source:collect") || selectedRowKeys.length === 0 || batchLoading}
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
                popupMatchSelectWidth={false}
                dropdownStyle={{ width: 360 }}
                options={types.map((t) => ({ value: t.source_type, label: `${t.label}（${t.source_type}）` }))}
                optionRender={(opt) => {
                  const t = typeInfo(types, String(opt.value));
                  return (
                    <div style={{ padding: "3px 0" }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span style={{ fontWeight: 500 }}>{opt.label}</span>
                        {t?.default_port ? (
                          <Tag style={{ marginInlineStart: "auto" }}>端口 {t.default_port}</Tag>
                        ) : null}
                      </div>
                      {t?.description && (
                        <div
                          style={{
                            fontSize: 12,
                            color: "rgba(0,0,0,0.45)",
                            marginTop: 2,
                            whiteSpace: "normal",
                            lineHeight: 1.5,
                          }}
                        >
                          {t.description}
                        </div>
                      )}
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
            description="连接库为纯连接凭据，采集范围由下方「目标数据库」决定；留空=采集该实例下全部非系统库。"
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
            <Form.Item label=" " style={{ width: 120 }}>
              <Button icon={<DatabaseOutlined />} loading={dbLoading} onClick={loadDatabases} block>
                枚举库
              </Button>
            </Form.Item>
          </Space>
          <Form.Item
            name="databases"
            label="目标数据库（多选，留空=全部库）"
            tooltip="点「枚举库」后可在此多选实例下的库；仅采集所选库，其余库不在采集范围。留空=采集全部非系统库"
            getValueFromEvent={(e) => (e?.target ? e.target.value : e)}
            normalize={(v: string | string[] | undefined) =>
              Array.isArray(v)
                ? v
                : v && typeof v === "string" && v.trim()
                  ? v.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean)
                  : []
            }
          >
            {dbOptions.length ? (
              <Select
                data-testid="target-db-select"
                mode="multiple"
                allowClear
                showSearch
                placeholder="选择目标库（可多选，留空=全部库）"
                optionFilterProp="label"
                loading={dbLoading}
                options={dbOptions.map((d) => ({ value: d, label: d }))}
                onChange={handleDatabasesChange}
              />
            ) : (
              <Input className="mono" placeholder="多个库用逗号分隔，留空=全部库" />
            )}
          </Form.Item>
          <Form.Item
            name="selected_tables"
            label="采集范围表（可选）"
            tooltip="选中目标库后自动枚举其下的表；不选=该库全部表（生成 库.*），勾选部分表则仅采集所选表（生成 库.表）。所选表自动写入「高级连接选项 → 表级白名单」"
            getValueFromEvent={(e) => (e?.target ? e.target.value : e)}
          >
            {Object.keys(tableOptions).length ? (
              <Select
                data-testid="target-table-select"
                mode="multiple"
                allowClear
                showSearch
                placeholder="选择要采集的表（不选=整库）"
                optionFilterProp="label"
                loading={tableLoading}
                options={Object.entries(tableOptions).map(([db, tables]) => ({
                  label: db,
                  options: tables.map((t) => ({ value: `${db}.${t}`, label: `${db}.${t}` })),
                }))}
                onChange={handleTablesChange}
              />
            ) : (
              <Input className="mono" placeholder="选择目标库后自动枚举表；不选=该库全部表" disabled />
            )}
          </Form.Item>
          <Form.Item
            name="collection_mode"
            label="默认采集模式"
            tooltip="定时调度与手动采集默认按此模式执行；立即采集可临时覆盖本次，不修改此配置"
            style={{ width: "100%" }}
          >
            <Radio.Group
              options={[
                { value: "FULL", label: "全量（扫描全部匹配表）" },
                { value: "INCREMENTAL", label: "增量（仅变更表）" },
              ]}
            />
          </Form.Item>
          <Space size={16} style={{ width: "100%" }} align="start">
            <Form.Item name="user" label="User" style={{ width: "100%" }}>
              <Input className="mono" placeholder="连接账号" />
            </Form.Item>
            <Form.Item name="password" label="Password" style={{ width: "100%" }}>
              <Input.Password className="mono" placeholder="连接密码" />
            </Form.Item>
          </Space>
          <Collapse
            ghost
            style={{ marginBottom: 16 }}
            items={[
              {
                key: "advanced",
                label: "高级连接选项",
                // 面板默认折叠但内容必须常驻挂载：折叠时 Form.Item 不渲染会丢字段值
                // （database/include_patterns 编辑回显与提交读取依赖其注册）
                forceRender: true,
                children: (
                  <Space direction="vertical" style={{ width: "100%" }}>
                    <Form.Item
                      name="database"
                      label={isHms ? "HMS 元数据库名（必填）" : "连接库（纯连接凭据）"}
                      style={{ width: "100%" }}
                      tooltip={
                        isHms
                          ? "Hive Metastore 元数据库名（HMS backend 库），必填连接凭据，漏填将导致采集报「No database selected」。Hive 默认元库名为 hive，已自动预填，如你的元库名不同请修改"
                          : "连接实例时使用的默认库，仅作连接凭据、不影响采集范围；采集范围由上方「目标数据库」决定"
                      }
                    >
                      {dbOptions.length ? (
                        <Select
                          showSearch
                          allowClear
                          placeholder={isHms ? "HMS 元数据库名（默认 hive）" : "全部库（默认）"}
                          optionFilterProp="label"
                          loading={dbLoading}
                          options={dbOptions.map((d) => ({ value: d, label: d }))}
                        />
                      ) : (
                        <Input className="mono" placeholder={isHms ? "HMS 元数据库名，默认 hive" : "连接默认库（可选）"} />
                      )}
                    </Form.Item>
                    {selType?.supports_schema && (
                      <Form.Item name="schema" label="Schema" style={{ width: "100%" }} tooltip="PostgreSQL 库内 schema，默认 public">
                        <Input className="mono" placeholder="public" />
                      </Form.Item>
                    )}
                    <Form.Item
                      name="include_patterns"
                      label="表级白名单（高级模式）"
                      tooltip="fnmatch 风格，每行一个；填写后仅采集匹配的表（如 库.orders_*）。上方「采集范围表」勾选会自动生成此处。留空=采集全部"
                      style={{ width: "100%" }}
                    >
                      <Input.TextArea rows={2} placeholder={"每行一个模式，如：\n库名.orders_*\n库名.dim_*}".replace("}*", "*}")} />
                    </Form.Item>
                    <Form.Item
                      name="exclude_patterns"
                      label="表级黑名单（高级模式）"
                      tooltip="fnmatch 风格，每行一个；命中即排除（如 库.tmp_*）。白名单优先于黑名单"
                      style={{ width: "100%" }}
                    >
                      <Input.TextArea rows={2} placeholder={"每行一个模式，如：\n库名.tmp_*".replace("_*}", "")} />
                    </Form.Item>
                  </Space>
                ),
              },
            ]}
          />
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
          <Space style={{ marginBottom: 8 }}>
            {can("data-source:test-connection") && (
              <Button icon={<ApiOutlined />} onClick={handleTest}>
                测试连接
              </Button>
            )}
            <span className="muted" style={{ fontSize: 12 }}>创建前验证 Host / 端口 / 凭据可达性</span>
          </Space>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 8 }}
            message="内网 / 本机地址会被安全策略拦截，属预期行为"
            description="「测试连接」受 SSRF 安全策略保护，内网（192.168.x / 10.x 等）与本机（localhost / 127.0.0.1）地址会被拒绝，这不是配置错误。内网数据库可直接填写配置后创建，创建完成后在详情弹窗点「探活」验证连接（探活放行内网地址）。"
          />
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
          onScheduleSaved={() => load()}
        />
      )}

      {/* 编辑保存且连接配置变更 → 引导重新采集（需 data-source:collect） */}
      {can("data-source:collect") && (
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
      )}

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
