import { useEffect, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, Select, message, Space, Statistic, Row, Col, Descriptions, Alert } from "antd";
import { PlusOutlined, ThunderboltOutlined, ScheduleOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  listDataSources,
  createDataSource,
  collectSource,
  scheduleSource,
  getSourceHealth,
  getSourceWatermark,
  UnisenseApiError,
} from "../api";
import type { DataSource, SourceHealth, Watermark, CollectResult } from "../types";

const SOURCE_TYPES = ["mysql", "postgres", "hive", "doris", "clickhouse", "kafka", "starrocks"];

function SourceDetailModal({
  source,
  onClose,
}: {
  source: DataSource;
  onClose: () => void;
}) {
  const [health, setHealth] = useState<SourceHealth | null>(null);
  const [watermark, setWatermark] = useState<Watermark | null>(null);
  const [collecting, setCollecting] = useState(false);
  const [collectResult, setCollectResult] = useState<CollectResult | null>(null);
  const [cron, setCron] = useState("0 3 * * *");
  const [scheduleMode, setScheduleMode] = useState("FULL");

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
      message.error(err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "采集失败");
    } finally {
      setCollecting(false);
    }
  }

  async function handleSchedule() {
    try {
      const res = await scheduleSource(source.source_id, cron, scheduleMode);
      message.success(`已调度：job ${res.job_id}（${res.status}）`);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "调度失败");
    }
  }

  return (
    <Modal open onCancel={onClose} footer={null} width={680} title={`数据源：${source.name}（${source.source_id}）`}>
      <Descriptions column={2} bordered size="small" style={{ marginBottom: 16 }}>
        <Descriptions.Item label="类型">{source.source_type}</Descriptions.Item>
        <Descriptions.Item label="域">{source.domain}</Descriptions.Item>
        <Descriptions.Item label="覆盖度">{Math.round(source.coverage * 100)}%</Descriptions.Item>
        <Descriptions.Item label="采集模式">{source.collection_mode}</Descriptions.Item>
        <Descriptions.Item label="健康状态">
          <Tag color={source.health_status === "healthy" ? "success" : source.health_status === "unhealthy" ? "error" : "default"}>
            {source.health_status}
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

      {health?.last_error && <Alert type="error" showIcon style={{ marginBottom: 12 }} message={`最近错误：${health.last_error}`} />}
      {collectResult && (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 12 }}
          message={`采集结果：注册 ${collectResult.registered} · PII ${collectResult.pii_registered} · 漂移 ${collectResult.drift_count}`}
          description={collectResult.drift_events?.length ? collectResult.drift_events.slice(0, 5).map((d) => `${d.entity_name} (${d.change_type})`).join("、") : "无 schema 漂移"}
        />
      )}

      <Space wrap>
        <Button type="primary" icon={<ThunderboltOutlined />} loading={collecting} onClick={handleCollect}>
          立即采集
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
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [detail, setDetail] = useState<DataSource | null>(null);
  const [form] = Form.useForm();

  async function load() {
    setLoading(true);
    try {
      setItems(await listDataSources());
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleCreate(values: Record<string, unknown>) {
    setLoading(true);
    try {
      await createDataSource({
        source_id: String(values.source_id),
        name: String(values.name),
        source_type: String(values.source_type) as DataSource["source_type"],
        domain: String(values.domain),
        cluster_id: values.cluster_id ? String(values.cluster_id) : null,
        connection_config: { host: String(values.host || ""), port: Number(values.port || 0), database: String(values.database || "") },
      });
      message.success("数据源已创建");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "创建失败");
    } finally {
      setLoading(false);
    }
  }

  const columns = [
    { title: "Source ID", dataIndex: "source_id", key: "source_id", render: (v: string) => <span className="mono">{v}</span> },
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "类型", dataIndex: "source_type", key: "type", width: 110, render: (v: string) => <Tag>{v}</Tag> },
    { title: "域", dataIndex: "domain", key: "domain", width: 130 },
    {
      title: "健康",
      dataIndex: "health_status",
      key: "health",
      width: 100,
      render: (v: string) => <Tag color={v === "healthy" ? "success" : v === "unhealthy" ? "error" : "default"}>{v}</Tag>,
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
          <p>接入数据源、采集元数据、登记目录并持续发现 schema 漂移。</p>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新建数据源</Button>
      </div>

      <Card extra={<Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button>}>
        <Table dataSource={items} columns={columns} rowKey="source_id" loading={loading} pagination={false} locale={{ emptyText: "暂无数据源" }} />
      </Card>

      <Modal title="新建数据源" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} confirmLoading={loading} okText="创建" width={560}>
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Space size={16} style={{ width: "100%" }}>
            <Form.Item name="source_id" label="Source ID" rules={[{ required: true, min: 2, max: 64 }]}>
              <Input className="mono" placeholder="如 mysql_finance" />
            </Form.Item>
            <Form.Item name="name" label="名称" rules={[{ required: true }]}>
              <Input placeholder="如 财务 MySQL" />
            </Form.Item>
          </Space>
          <Space size={16} style={{ width: "100%" }}>
            <Form.Item name="source_type" label="类型" rules={[{ required: true }]}>
              <Select options={SOURCE_TYPES.map((v) => ({ value: v, label: v }))} />
            </Form.Item>
            <Form.Item name="domain" label="业务域" rules={[{ required: true }]}>
              <Input placeholder="如 finance" />
            </Form.Item>
          </Space>
          <Form.Item name="cluster_id" label="集群 ID（可选）">
            <Input className="mono" />
          </Form.Item>
          <Space size={16} style={{ width: "100%" }}>
            <Form.Item name="host" label="Host" rules={[{ required: true }]}>
              <Input className="mono" placeholder="127.0.0.1" />
            </Form.Item>
            <Form.Item name="port" label="Port">
              <Input type="number" className="mono" defaultValue={3306} />
            </Form.Item>
            <Form.Item name="database" label="Database">
              <Input className="mono" />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      {detail && <SourceDetailModal source={detail} onClose={() => setDetail(null)} />}
    </div>
  );
}
