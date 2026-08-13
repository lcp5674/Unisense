import { useEffect, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Space, Statistic, Row, Col, Descriptions, Alert } from "antd";
import { PlusOutlined, ThunderboltOutlined, ScheduleOutlined, ReloadOutlined, ApiOutlined } from "@ant-design/icons";
import {
  listDataSources,
  createDataSource,
  collectSource,
  scheduleSource,
  getSourceHealth,
  getSourceWatermark,
  listDataSourceTypes,
  testDataSourceConnection,
  checkDataSourceConnection,
  UnisenseApiError,
} from "../api";
import type { DataSource, SourceHealth, Watermark, CollectResult, SourceTypeInfo, TestConnectionResult, SourceType } from "../types";
import { ObjectView } from "../utils/display";
import { COLLECTION_MODE_LABEL, SOURCE_HEALTH_LABEL } from "../utils/enums";

const FALLBACK_TYPES: SourceTypeInfo[] = [
  { source_type: "mysql", label: "MySQL", default_port: 3306, supports_database: true, supports_schema: false, description: "关系型数据库" },
  { source_type: "postgres", label: "PostgreSQL", default_port: 5432, supports_database: true, supports_schema: true, description: "关系型数据库" },
  { source_type: "hive", label: "Hive", default_port: 10000, supports_database: true, supports_schema: false, description: "数据仓库" },
  { source_type: "doris", label: "Doris", default_port: 9030, supports_database: true, supports_schema: false, description: "MPP 分析库" },
  { source_type: "clickhouse", label: "ClickHouse", default_port: 8123, supports_database: true, supports_schema: false, description: "列式分析库" },
  { source_type: "kafka", label: "Kafka", default_port: 9092, supports_database: false, supports_schema: false, description: "消息队列" },
  { source_type: "starrocks", label: "StarRocks", default_port: 9030, supports_database: true, supports_schema: false, description: "MPP 分析库" },
];

function typeInfo(types: SourceTypeInfo[], t: string): SourceTypeInfo | undefined {
  return types.find((x) => x.source_type === t);
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

function previewSourceId(sourceType: string | undefined, cfg: Record<string, unknown>, domain: string): string {
  const base = String(cfg.database || cfg.schema || domain || "default");
  const norm = base.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase() || "default";
  return `${sourceType || "?"}_${norm}`.slice(0, 64);
}

function SourceDetailModal({
  source,
  types,
  onClose,
}: {
  source: DataSource;
  types: SourceTypeInfo[];
  onClose: () => void;
}) {
  const [health, setHealth] = useState<SourceHealth | null>(null);
  const [watermark, setWatermark] = useState<Watermark | null>(null);
  const [collecting, setCollecting] = useState(false);
  const [collectResult, setCollectResult] = useState<CollectResult | null>(null);
  const [cron, setCron] = useState("0 3 * * *");
  const [scheduleMode, setScheduleMode] = useState("FULL");
  const [checking, setChecking] = useState(false);
  const [checkResult, setCheckResult] = useState<TestConnectionResult | null>(null);

  useEffect(() => {
    getSourceHealth(source.source_id).then(setHealth).catch(() => {});
    getSourceWatermark(source.source_id).then(setWatermark).catch(() => {});
  }, [source.source_id]);

  async function handleCollect() {
    setCollecting(true);
    try {
      const res = await collectSource(source.source_id, "FULL");
      setCollectResult(res);
      message.success(`采集完成：扫描 ${res.scanned} · 注册 ${res.registered} · PII ${res.pii_registered}`);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "采集失败");
    } finally {
      setCollecting(false);
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
      message.success(`已调度：job ${res.job_id}（${res.status}）`);
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
        <Descriptions.Item label="连接配置">{(source.connection_config_present ? "已配置（明文不下发）" : "未配置")}</Descriptions.Item>
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
      {collectResult && (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 12 }}
          message={`采集结果：注册 ${collectResult.registered} · PII ${collectResult.pii_registered} · 漂移 ${collectResult.drift_count}`}
          description={collectResult.drift_events?.length ? collectResult.drift_events.slice(0, 5).map((d) => `${d.entity_name} (${DRIFT_CHANGE_LABEL[d.change_type] ?? d.change_type})`).join("、") : "无 schema 漂移"}
        />
      )}

      <Space wrap>
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
      </Space>
    </Modal>
  );
}

export function DataSources() {
  const [items, setItems] = useState<DataSource[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [detail, setDetail] = useState<DataSource | null>(null);
  const [types, setTypes] = useState<SourceTypeInfo[]>(FALLBACK_TYPES);
  const [form] = Form.useForm();
  const sourceType = Form.useWatch("source_type", form);
  const watchedDatabase = Form.useWatch("database", form);
  const watchedSchema = Form.useWatch("schema", form);
  const domainWatch = Form.useWatch("domain", form) ?? "";

  async function load(nextPage = page, nextPageSize = pageSize) {
    setLoading(true);
    try {
      // P1-1: 服务端分页（后端返回 {items, total, page, page_size}）
      const resp = await listDataSources({ page: nextPage, page_size: nextPageSize });
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 类型切换时自动带出默认端口
  function handleTypeChange(t: string) {
    const info = typeInfo(types, t);
    if (info?.default_port) {
      form.setFieldValue("port", info.default_port);
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
      } else {
        message.error(`连接失败：${res.error ?? "未知错误"}`);
      }
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "测试失败");
    }
  }

  async function handleCreate(values: Record<string, unknown>) {
    setLoading(true);
    try {
      const payload: {
        name: string;
        source_type: DataSource["source_type"];
        domain: string;
        cluster_id?: string | null;
        connection_config: Record<string, unknown>;
      } = {
        name: String(values.name),
        source_type: String(values.source_type) as DataSource["source_type"],
        domain: String(values.domain),
        cluster_id: values.cluster_id ? String(values.cluster_id) : null,
        connection_config: buildConnectionConfig(values),
      };
      // source_id 不传 → 后端按 类型_库|域 自动生成
      await createDataSource(payload);
      message.success(`数据源已创建（${previewSourceId(String(values.source_type), buildConnectionConfig(values), String(values.domain))}）`);
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
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
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建数据源</Button>
      </div>

      <Card extra={<Button icon={<ReloadOutlined />} onClick={() => load()} loading={loading}>刷新</Button>}>
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

      <Modal title="新建数据源" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} confirmLoading={loading} okText="创建" width={620}>
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Space size={16} style={{ width: "100%" }}>
            <Form.Item name="name" label="名称" rules={[{ required: true }]} style={{ width: "100%" }}>
              <Input placeholder="如 财务 MySQL" />
            </Form.Item>
            <Form.Item name="domain" label="业务域" rules={[{ required: true }]} style={{ width: "100%" }}>
              <Input placeholder="如 finance" />
            </Form.Item>
          </Space>
          <Space size={16} style={{ width: "100%" }}>
            <Form.Item name="source_type" label="类型" rules={[{ required: true }]} style={{ width: "100%" }}>
              <Select
                placeholder="选择数据源类型"
                onChange={handleTypeChange}
                options={types.map((t) => ({ value: t.source_type, label: `${t.label}（${t.source_type}）` }))}
              />
            </Form.Item>
            <Form.Item name="cluster_id" label="集群 ID（可选）" style={{ width: "100%" }}>
              <Input className="mono" />
            </Form.Item>
          </Space>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={`Source ID 将由系统自动生成：${generated}`}
            description="不填 Database 时采集该实例下全部非系统库；填了则只采集指定库。"
          />
          <Space size={16} style={{ width: "100%" }} align="start">
            <Form.Item name="host" label="Host" rules={[{ required: true }]} style={{ width: "100%" }}>
              <Input className="mono" placeholder="127.0.0.1" />
            </Form.Item>
            <Form.Item name="port" label="Port" initialValue={3306} style={{ width: 130 }}>
              <Input type="number" className="mono" />
            </Form.Item>
          </Space>
          <Space size={16} style={{ width: "100%" }} align="start">
            <Form.Item
              name="database"
              label="Database（留空=采集全部库）"
              style={{ width: "100%" }}
              tooltip="指定库名则只采集该库；留空则枚举该实例下全部非系统库"
            >
              <Input className="mono" placeholder="留空则采集全部库" />
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

      {detail && <SourceDetailModal source={detail} types={types} onClose={() => setDetail(null)} />}
    </div>
  );
}
