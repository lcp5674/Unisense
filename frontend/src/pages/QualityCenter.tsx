import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, InputNumber, Select, message, Tabs, Space, Alert } from "antd";
import { PlusOutlined, ReloadOutlined, ThunderboltOutlined, LinkOutlined, ArrowLeftOutlined } from "@ant-design/icons";
import {
  listQualityRules,
  createQualityRule,
  updateQualityRule,
  deleteQualityRule,
  listQualityEvents,
  qualityEventAck,
  qualityEventResolve,
  qualityEventClose,
  qualityEventDetect,
  qualityEventConfirmRepair,
  listBenchmarks,
  importBenchmark,
  bindBenchmark,
  listReconciliationRecords,
  runReconciliation,
  confirmReconciliation,
  listMetrics,
  UnisenseApiError,
} from "../api";
import type { MetricResponse, QualityRule, QualityEvent, QualityBenchmark, ReconciliationRecord } from "../types";
import { ThresholdSummary } from "../utils/display";
import { RULE_TYPE_LABEL, RULE_MODE_LABEL, RECONCILIATION_STATUS_LABEL } from "../utils/enums";

const RULE_TYPES = ["COMPLETENESS", "ACCURACY", "TIMELINESS", "CONSISTENCY", "UNIQUENESS", "VALIDITY", "WAVE_DIFF", "CROSS_SOURCE"];
const SEVERITY_COLOR: Record<string, string> = { P0: "red", P1: "orange", P2: "default" };
const EVENT_STATUS: Record<string, { color: string; label: string }> = {
  OPEN: { color: "red", label: "待处理" },
  ACK: { color: "gold", label: "已确认" },
  RESOLVED: { color: "green", label: "已解决" },
  CLOSED: { color: "default", label: "已关闭" },
};

function useMetrics() {
  const [metrics, setMetrics] = useState<MetricResponse[]>([]);
  useEffect(() => {
    listMetrics({ page_size: 100 }).then((r) => setMetrics(r.items)).catch(() => {});
  }, []);
  return metrics;
}

function RulesTab() {
  const [items, setItems] = useState<QualityRule[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  const metrics = useMetrics();

  async function load() {
    setLoading(true);
    try {
      const res = await listQualityRules({ page, page_size: pageSize });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize]);

  async function handleCreate(values: Record<string, unknown>) {
    try {
      let threshold: Record<string, unknown> = {};
      try {
        threshold = values.threshold ? JSON.parse(String(values.threshold)) : {};
      } catch {
        message.error("阈值需为合法 JSON");
        return;
      }
      await createQualityRule({
        metric_id: Number(values.metric_id),
        rule_type: String(values.rule_type),
        threshold,
        rule_mode: String(values.rule_mode ?? "static"),
        severity: String(values.severity ?? "P2"),
        enabled: true,
      });
      message.success("规则已创建");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "创建失败");
    }
  }

  async function handleToggle(r: QualityRule) {
    try {
      await updateQualityRule(r.id, { enabled: !r.enabled });
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  async function handleDelete(r: QualityRule) {
    try {
      await deleteQualityRule(r.id);
      message.success("规则已删除");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "删除失败");
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "指标", dataIndex: "metric_id", key: "metric", width: 90, render: (v: number) => <span className="mono">#{v}</span> },
    { title: "规则类型", dataIndex: "rule_type", key: "type", width: 140, render: (v: string) => <Tag>{RULE_TYPE_LABEL[v] ?? v}</Tag> },
    { title: "模式", dataIndex: "rule_mode", key: "mode", width: 120, render: (v: string) => RULE_MODE_LABEL[v] ?? v },
    { title: "阈值", dataIndex: "threshold", key: "threshold", render: (v: Record<string, unknown>) => <ThresholdSummary threshold={v} /> },
    { title: "严重度", dataIndex: "severity", key: "severity", width: 90, render: (v: string) => <Tag color={SEVERITY_COLOR[v]}>{v}</Tag> },
    {
      title: "启用",
      dataIndex: "enabled",
      key: "enabled",
      width: 80,
      render: (v: boolean) => <Tag color={v ? "success" : "default"}>{v ? "启用" : "停用"}</Tag>,
    },
    {
      title: "操作",
      key: "actions",
      width: 150,
      render: (_: unknown, r: QualityRule) => (
        <Space>
          <Button size="small" onClick={() => handleToggle(r)}>{r.enabled ? "停用" : "启用"}</Button>
          <Button size="small" danger onClick={() => handleDelete(r)}>删除</Button>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "space-between" }}>
        <Alert type="info" showIcon style={{ flex: 1 }} message="规则随指标 PUBLISHED 注册，按 T1/T2/T3 与数仓层差异化生效。" />
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)} style={{ marginLeft: 12 }}>新建规则</Button>
      </div>
      <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={{ current: page, pageSize, total, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100], onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showTotal: (t) => `共 ${t} 条` }} locale={{ emptyText: "暂无质量规则" }} />

      <Modal title="新建质量规则" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="创建">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="metric_id" label="指标" rules={[{ required: true }]}>
            <Select showSearch options={metrics.map((m) => ({ value: m.id, label: `${m.metric_code} · ${m.name}` }))} placeholder="选择指标" />
          </Form.Item>
          <Form.Item name="rule_type" label="规则类型" rules={[{ required: true }]}>
            <Select options={RULE_TYPES.map((v) => ({ value: v, label: RULE_TYPE_LABEL[v] ?? v }))} />
          </Form.Item>
          <Form.Item name="rule_mode" label="规则模式" initialValue="static">
            <Select options={["static", "dynamic_baseline", "yoy_woy", "cross_source"].map((v) => ({ value: v, label: RULE_MODE_LABEL[v] ?? v }))} />
          </Form.Item>
          <Form.Item name="severity" label="严重度" initialValue="P2">
            <Select options={["P0", "P1", "P2"].map((v) => ({ value: v, label: v }))} />
          </Form.Item>
          <Form.Item name="threshold" label="阈值 (JSON)" rules={[{ required: true }]}>
            <Input.TextArea rows={3} className="mono" placeholder='{"min": 0, "max": 1000000}' />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function EventsTab() {
  const [items, setItems] = useState<QualityEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  // 手动触发检测弹窗
  const [detectOpen, setDetectOpen] = useState(false);
  const [detectForm] = Form.useForm();
  const metrics = useMetrics();

  async function load() {
    setLoading(true);
    try {
      const res = await listQualityEvents({ status, page, page_size: pageSize });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, status]);

  async function act(eventId: number, fn: (id: number) => Promise<QualityEvent>, done: string) {
    try {
      await fn(eventId);
      message.success(done);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  // 手动触发检测：后端 POST /quality/events/detect，命中返回事件、未命中返回 null
  async function handleDetect(values: Record<string, unknown>) {
    try {
      const hit = await qualityEventDetect({
        metric_id: Number(values.metric_id),
        rule_type: String(values.rule_type),
        obs_value: Number(values.obs_value),
        rule_mode: values.rule_mode ? String(values.rule_mode) : null,
      });
      if (hit) {
        message.success(`检测命中，已生成异常事件 #${hit.id}`);
      } else {
        message.info("检测未命中（或该指标已有 OPEN 异常），未生成新事件");
      }
      setDetectOpen(false);
      detectForm.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "检测失败");
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "指标", dataIndex: "metric_id", key: "metric", width: 90, render: (v: number) => <span className="mono">#{v}</span> },
    { title: "级别", dataIndex: "level", key: "level", width: 80, render: (v: string) => <Tag color={SEVERITY_COLOR[v]}>{v}</Tag> },
    { title: "规则类型", dataIndex: "rule_type", key: "type", width: 130, render: (v: string) => <Tag>{RULE_TYPE_LABEL[v] ?? v}</Tag> },
    { title: "观测值", dataIndex: "obs_value", key: "obs", width: 100, render: (v: number | null) => v ?? <span className="muted">—</span> },
    { title: "阈值", dataIndex: "threshold", key: "thr", width: 100, render: (v: number | null) => v ?? <span className="muted">—</span> },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (v: string) => <Tag color={EVENT_STATUS[v]?.color}>{EVENT_STATUS[v]?.label ?? v}</Tag>,
    },
    { title: "时间", dataIndex: "created_at", key: "created", width: 170, render: (v: string | null) => v ?? "—" },
    {
      title: "操作",
      key: "actions",
      width: 280,
      render: (_: unknown, e: QualityEvent) => (
        <Space>
          {e.status === "OPEN" && <Button size="small" onClick={() => act(e.id, qualityEventAck, "已确认")}>确认</Button>}
          {(e.status === "OPEN" || e.status === "ACK") && <Button size="small" type="primary" onClick={() => act(e.id, qualityEventResolve, "已解决")}>解决</Button>}
          {/* 修复确认：Owner 已线下修复留痕（后端仅 OPEN 状态允许） */}
          {e.status === "OPEN" && <Button size="small" onClick={() => act(e.id, qualityEventConfirmRepair, "已确认修复")}>修复确认</Button>}
          {e.status !== "CLOSED" && <Button size="small" onClick={() => act(e.id, qualityEventClose, "已关闭")}>关闭</Button>}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
        <Select
          allowClear
          placeholder="全部状态"
          style={{ width: 140 }}
          value={status || undefined}
          onChange={(v) => { setStatus(v || ""); setPage(1); }}
          options={Object.entries(EVENT_STATUS).map(([k, v]) => ({ value: k, label: v.label }))}
        />
        <Button type="primary" icon={<ThunderboltOutlined />} onClick={() => setDetectOpen(true)}>手动检测</Button>
      </div>
      <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={{ current: page, pageSize, total, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100], onChange: (p, ps) => { setPage(p); setPageSize(ps); }, showTotal: (t) => `共 ${t} 条` }} locale={{ emptyText: "暂无质量事件" }} />

      {/* 手动触发质量检测弹窗 */}
      <Modal title="手动触发质量检测" open={detectOpen} onCancel={() => setDetectOpen(false)} onOk={() => detectForm.submit()} okText="检测">
        <Form form={detectForm} layout="vertical" onFinish={handleDetect} style={{ marginTop: 8 }}>
          <Form.Item name="metric_id" label="指标" rules={[{ required: true }]}>
            <Select showSearch options={metrics.map((m) => ({ value: m.id, label: `${m.metric_code} · ${m.name}` }))} placeholder="选择指标" />
          </Form.Item>
          <Form.Item name="rule_type" label="规则类型" rules={[{ required: true }]}>
            <Select placeholder="选择规则类型" options={RULE_TYPES.map((v) => ({ value: v, label: RULE_TYPE_LABEL[v] ?? v }))} />
          </Form.Item>
          <Form.Item name="rule_mode" label="规则模式">
            <Select allowClear placeholder="默认按规则自身模式" options={["static", "dynamic_baseline", "yoy_woy", "cross_source"].map((v) => ({ value: v, label: RULE_MODE_LABEL[v] ?? v }))} />
          </Form.Item>
          <Form.Item name="obs_value" label="观测值" rules={[{ required: true }]}>
            <InputNumber style={{ width: 200 }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function BenchmarksTab() {
  const [items, setItems] = useState<QualityBenchmark[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();
  // 基准绑定弹窗
  const [bindTarget, setBindTarget] = useState<QualityBenchmark | null>(null);
  const [bindForm] = Form.useForm();

  async function load() {
    setLoading(true);
    try {
      const res = await listBenchmarks({ page_size: 50 });
      setItems(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleImport(values: Record<string, unknown>) {
    try {
      await importBenchmark({
        source_id: String(values.source_id),
        metric_code: String(values.metric_code),
        bench_date: String(values.bench_date),
        bench_value: Number(values.bench_value),
        provider: String(values.provider),
        tolerance_pct: values.tolerance_pct !== undefined && values.tolerance_pct !== null ? Number(values.tolerance_pct) : null,
      });
      message.success("基准已导入");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "导入失败");
    }
  }

  // 绑定基准到目标指标（声明比对口径 / 容忍率）
  async function handleBind(values: Record<string, unknown>) {
    const target = bindTarget;
    if (!target) return;
    try {
      await bindBenchmark(target.id, {
        metric_code: values.metric_code ? String(values.metric_code) : null,
        tolerance_pct: values.tolerance_pct !== undefined && values.tolerance_pct !== null ? Number(values.tolerance_pct) : null,
      });
      message.success(`基准 #${target.id} 已绑定`);
      setBindTarget(null);
      bindForm.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "绑定失败");
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "数据源", dataIndex: "source_id", key: "source", render: (v: string) => <span className="mono">{v}</span> },
    { title: "指标", dataIndex: "metric_code", key: "metric", render: (v: string) => <span className="mono">{v}</span> },
    { title: "基准日期", dataIndex: "bench_date", key: "date", width: 120 },
    { title: "基准值", dataIndex: "bench_value", key: "value", width: 110 },
    { title: "提供方", dataIndex: "provider", key: "provider", width: 130 },
    { title: "容差%", dataIndex: "tolerance_pct", key: "tol", width: 90, render: (v: number | null) => (v !== null && v !== undefined ? `${v}%` : "—") },
    {
      title: "操作",
      key: "actions",
      width: 110,
      render: (_: unknown, b: QualityBenchmark) => (
        <Button size="small" icon={<LinkOutlined />} onClick={() => setBindTarget(b)}>绑定</Button>
      ),
    },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end" }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>导入基准</Button>
      </div>
      <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={false} locale={{ emptyText: "暂无基准" }} />

      <Modal title="导入质量基准" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="导入">
        <Form form={form} layout="vertical" onFinish={handleImport} style={{ marginTop: 8 }}>
          <Form.Item name="source_id" label="数据源 ID" rules={[{ required: true }]}>
            <Input className="mono" />
          </Form.Item>
          <Form.Item name="metric_code" label="指标编码" rules={[{ required: true }]}>
            <Input className="mono" />
          </Form.Item>
          <Form.Item name="bench_date" label="基准日期" rules={[{ required: true }]}>
            <Input placeholder="YYYY-MM-DD" className="mono" />
          </Form.Item>
          <Form.Item name="bench_value" label="基准值" rules={[{ required: true }]}>
            <InputNumber min={0.0001} style={{ width: 200 }} />
          </Form.Item>
          <Form.Item name="provider" label="提供方" rules={[{ required: true }]}>
            <Input placeholder="如 财务部 / 第三方" />
          </Form.Item>
          <Form.Item name="tolerance_pct" label="容差 (%)">
            <InputNumber min={0} max={100} style={{ width: 200 }} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 基准绑定弹窗：重声明比对目标指标与容忍率 */}
      <Modal
        title={`绑定基准 #${bindTarget?.id ?? ""}`}
        open={bindTarget != null}
        onCancel={() => setBindTarget(null)}
        onOk={() => bindForm.submit()}
        okText="绑定"
      >
        <p className="muted" style={{ marginBottom: 8 }}>
          {bindTarget ? `${bindTarget.provider} · ${bindTarget.metric_code} @${bindTarget.bench_date}` : ""}
        </p>
        <Form form={bindForm} layout="vertical" onFinish={handleBind} style={{ marginTop: 8 }}>
          <Form.Item name="metric_code" label="目标指标编码">
            <Input className="mono" placeholder="留空表示绑定到自身指标编码" />
          </Form.Item>
          <Form.Item name="tolerance_pct" label="容差 (%)">
            <InputNumber min={0} max={100} style={{ width: 200 }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function ReconciliationTab() {
  const [items, setItems] = useState<ReconciliationRecord[]>([]);
  const [benchmarks, setBenchmarks] = useState<QualityBenchmark[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  async function load() {
    setLoading(true);
    try {
      const res = await listReconciliationRecords({ page_size: 50 });
      setItems(res.items);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    listBenchmarks({ page_size: 50 }).then((r) => setBenchmarks(r.items)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleRun(values: Record<string, unknown>) {
    try {
      await runReconciliation({
        benchmark_id: Number(values.benchmark_id),
        metric_value: Number(values.metric_value),
        window: values.window ? String(values.window) : null,
      });
      message.success("对账已执行");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "执行失败");
    }
  }

  async function handleConfirm(r: ReconciliationRecord, decision: string) {
    try {
      await confirmReconciliation(r.id, decision, "前台确认");
      message.success("已确认");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "确认失败");
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "基准", dataIndex: "benchmark_id", key: "benchmark", width: 90, render: (v: number) => <span className="mono">#{v}</span> },
    { title: "指标", dataIndex: "metric_code", key: "metric", render: (v: string) => <span className="mono">{v}</span> },
    { title: "指标值", dataIndex: "metric_value", key: "mv", width: 100 },
    { title: "基准值", dataIndex: "bench_value", key: "bv", width: 100 },
    { title: "偏差%", dataIndex: "diff_pct", key: "diff", width: 100, render: (v: number) => <span style={{ color: Math.abs(v) > 5 ? "var(--danger)" : "var(--data)" }}>{v.toFixed(2)}%</span> },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (v: string) => <Tag color={v === "ALERT" ? "error" : v === "WARN" ? "warning" : v === "CONFIRMED" ? "success" : "default"}>{RECONCILIATION_STATUS_LABEL[v] ?? v}</Tag>,
    },
    {
      title: "操作",
      key: "actions",
      width: 190,
      render: (_: unknown, r: ReconciliationRecord) =>
        r.status !== "CONFIRMED" ? (
          <Space>
            <Button size="small" type="primary" onClick={() => handleConfirm(r, "reasonable")}>合理</Button>
            <Button size="small" danger onClick={() => handleConfirm(r, "caliber_error")}>口径错误</Button>
          </Space>
        ) : (
          <Tag color="success">已确认</Tag>
        ),
    },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end" }}>
        <Button type="primary" icon={<ReloadOutlined />} onClick={() => setModalOpen(true)}>执行对账</Button>
      </div>
      <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={false} locale={{ emptyText: "暂无对账记录" }} />

      <Modal title="执行基准对账" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="执行">
        <Form form={form} layout="vertical" onFinish={handleRun} style={{ marginTop: 8 }}>
          <Form.Item name="benchmark_id" label="基准" rules={[{ required: true }]}>
            <Select showSearch options={benchmarks.map((b) => ({ value: b.id, label: `${b.metric_code} · ${b.provider} @${b.bench_date}` }))} placeholder="选择基准" />
          </Form.Item>
          <Form.Item name="metric_value" label="当前指标值" rules={[{ required: true }]}>
            <InputNumber style={{ width: 200 }} />
          </Form.Item>
          <Form.Item name="window" label="统计窗口">
            <Input placeholder="如 2026-07" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

export function QualityCenter() {
  const navigate = useNavigate();

  // 统一返回上一入口：优先回退浏览器历史（总览快捷入口等），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  const tabItems = [
    { key: "rules", label: "质量规则", children: <RulesTab /> },
    { key: "events", label: "质量事件", children: <EventsTab /> },
    { key: "benchmarks", label: "基准库", children: <BenchmarksTab /> },
    { key: "reconcile", label: "基准对账", children: <ReconciliationTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">Governance / Quality</div>
          <h2>质量中心</h2>
          <p>规则、告警、基准与对账——指标质量的持续守护。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
