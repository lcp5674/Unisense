import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Card, Table, Tag, Button, Modal, Form, Input, InputNumber, Select, Rate, message, Tabs, Space, Alert, Tooltip, Row, Col } from "antd";
import { StarOutlined } from "@ant-design/icons";
import { listFeedback, submitFeedback, updateFeedbackStatus, submitNps, fetchNpsStats, listUsers, getMetric, UnisenseApiError } from "../api";
import type { Feedback, NpsStats } from "../types";
import { formatCnTime, timeAgoCn } from "../utils/timeCn";

// ---- 展示映射（value=英文对接后端，label=中文展示） ----

const STATUS_ZH: Record<string, { label: string; color: string }> = {
  pending: { label: "待处理", color: "default" },
  in_progress: { label: "跟进中", color: "blue" },
  adopted: { label: "已采纳", color: "green" },
  rejected: { label: "已驳回", color: "red" },
};

// 反馈对象类型 → 业务术语（覆盖全站可反馈对象，含平台/表/字段等）
const TARGET_TYPE_ZH: Record<string, string> = {
  metric: "指标",
  term: "术语",
  report: "报表",
  dashboard: "仪表盘",
  platform: "平台",
  table: "数据表",
  field: "字段",
  source: "数据源",
  template: "指标模板",
  dimension: "维度",
  favorite: "收藏",
  todo: "待办",
  conflict: "口径冲突",
  nps: "满意度",
};

const TYPE_FILTER_OPTIONS = [
  { value: "metric", label: "指标" },
  { value: "term", label: "术语" },
  { value: "report", label: "报表" },
  { value: "dashboard", label: "仪表盘" },
];

const STATUS_FILTER_OPTIONS = [
  { value: "pending", label: "待处理" },
  { value: "in_progress", label: "跟进中" },
  { value: "adopted", label: "已采纳" },
  { value: "rejected", label: "已驳回" },
];

interface ProcessDraft {
  feedback: Feedback;
  status: string;
  note: string;
}

function FeedbackTab() {
  const [items, setItems] = useState<Feedback[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [targetType, setTargetType] = useState<string | undefined>();
  const [status, setStatus] = useState<string | undefined>();
  const [draft, setDraft] = useState<ProcessDraft | null>(null);
  const [updateLoading, setUpdateLoading] = useState(false);
  // 业务化解析：user_id → 用户名、target_id(metric) → 指标名
  const [usersMap, setUsersMap] = useState<Record<number, string>>({});
  const [metricNames, setMetricNames] = useState<Record<string, string>>({});
  const navigate = useNavigate();

  // 加载用户名单：反馈列表「用户」列展示用户名而非数字 ID
  useEffect(() => {
    listUsers()
      .then((us) => {
        const m: Record<number, string> = {};
        for (const u of us) m[u.id] = u.display_name || u.username;
        setUsersMap(m);
      })
      .catch(() => {});
  }, []);

  // 加载当前页指标类反馈的指标名（对象 ID → 业务含义）
  useEffect(() => {
    const codes = Array.from(
      new Set(
        items.filter((f) => f.target_type === "metric" && f.target_id).map((f) => f.target_id as string),
      ),
    );
    if (!codes.length) return;
    let alive = true;
    Promise.all(
      codes.map((code) => getMetric(code).catch(() => null)),
    ).then((metrics) => {
      if (!alive) return;
      const m: Record<string, string> = {};
      metrics.forEach((metric) => {
        if (metric) m[metric.metric_code] = metric.name;
      });
      setMetricNames((prev) => ({ ...prev, ...m }));
    });
    return () => {
      alive = false;
    };
  }, [items]);

  async function load(p = page, ps = pageSize, tt = targetType, st = status) {
    setLoading(true);
    try {
      const res = await listFeedback({ target_type: tt, status: st, page: p, page_size: ps });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(1, pageSize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function changeType(tt: string | undefined) {
    setTargetType(tt);
    setPage(1);
    load(1, pageSize, tt, status);
  }

  function changeStatus(st: string | undefined) {
    setStatus(st);
    setPage(1);
    load(1, pageSize, targetType, st);
  }

  async function confirmProcess() {
    if (!draft) return;
    setUpdateLoading(true);
    try {
      await updateFeedbackStatus(draft.feedback.id, draft.status, draft.note ? draft.note : null);
      message.success("处理完成");
      setDraft(null);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    } finally {
      setUpdateLoading(false);
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 64 },
    {
      title: "用户",
      dataIndex: "user_id",
      key: "user",
      width: 100,
      render: (v: number) => usersMap[v] ?? <span className="muted">#{v}</span>,
    },
    {
      title: "对象类型",
      dataIndex: "target_type",
      key: "targetType",
      width: 100,
      render: (v: string) => <Tag>{TARGET_TYPE_ZH[v] ?? v}</Tag>,
    },
    {
      title: "对象",
      dataIndex: "target_id",
      key: "targetId",
      width: 200,
      render: (v: string | null, f: Feedback) => {
        if (!v) return <span className="muted">—</span>;
        if (f.target_type === "metric") {
          const name = metricNames[v];
          return (
            <Button type="link" size="small" style={{ padding: 0 }} onClick={() => navigate(`/detail/${v}`)}>
              {name ? `${name}（${v}）` : v}
            </Button>
          );
        }
        return <span className="mono">{v}</span>;
      },
    },
    { title: "评分", dataIndex: "rating", key: "rating", width: 100, render: (v: number | null) => (v !== null ? <Rate disabled defaultValue={v} count={5} /> : <span className="muted">—</span>) },
    {
      title: "内容",
      dataIndex: "comment",
      key: "comment",
      ellipsis: true,
      render: (v: string | null) =>
        v ? (
          <Tooltip title={v} placement="topLeft">
            <span>{v}</span>
          </Tooltip>
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 94,
      render: (v: string) => {
        const s = STATUS_ZH[v] ?? { label: v, color: "default" };
        return <Tag color={s.color}>{s.label}</Tag>;
      },
    },
    {
      title: "处理人",
      dataIndex: "resolver_id",
      key: "resolver",
      width: 100,
      render: (v: number | null) => (v !== null ? usersMap[v] ?? <span className="mono">#{v}</span> : <span className="muted">—</span>),
    },
    {
      title: "处理时间",
      dataIndex: "resolved_at",
      key: "resolved",
      width: 120,
      render: (v: string | null) => (v ? <Tooltip title={formatCnTime(v)}><span>{timeAgoCn(v)}</span></Tooltip> : <span className="muted">—</span>),
    },
    {
      title: "提交时间",
      dataIndex: "created_at",
      key: "created",
      width: 120,
      render: (v: string) => (
        <Tooltip title={formatCnTime(v)}>
          <span>{timeAgoCn(v)}</span>
        </Tooltip>
      ),
    },
    {
      title: "处理",
      key: "actions",
      width: 210,
      render: (_: unknown, f: Feedback) => (
        <Space>
          <Button size="small" onClick={() => setDraft({ feedback: f, status: "in_progress", note: "" })}>跟进</Button>
          <Button size="small" type="primary" onClick={() => setDraft({ feedback: f, status: "adopted", note: "" })}>采纳</Button>
          <Button size="small" danger onClick={() => setDraft({ feedback: f, status: "rejected", note: "" })}>驳回</Button>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <span className="muted">类型：</span>
        <Select
          allowClear
          placeholder="全部类型"
          style={{ width: 120 }}
          options={TYPE_FILTER_OPTIONS}
          value={targetType}
          onChange={changeType}
        />
        <span className="muted">状态：</span>
        <Select
          allowClear
          placeholder="全部状态"
          style={{ width: 120 }}
          options={STATUS_FILTER_OPTIONS}
          value={status}
          onChange={changeStatus}
        />
      </Space>

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
          pageSizeOptions: [10, 20, 50],
          showTotal: (t) => `共 ${t} 条`,
          onChange: (p, ps) => {
            setPage(p);
            setPageSize(ps);
            load(p, ps, targetType, status);
          },
        }}
        locale={{ emptyText: "暂无反馈" }}
      />

      <Modal
        title={`${STATUS_ZH[draft?.status ?? ""]?.label ?? ""}反馈`}
        open={draft !== null}
        onCancel={() => setDraft(null)}
        onOk={confirmProcess}
        okText="确认处理"
        confirmLoading={updateLoading}
      >
        <div style={{ marginBottom: 12 }}>
          <span className="muted">反馈 #{draft?.feedback.id}：{draft?.feedback.comment ?? "（无内容）"}</span>
        </div>
        <div className="muted" style={{ marginBottom: 6 }}>处理说明：</div>
        <Input.TextArea
          value={draft?.note ?? ""}
          onChange={(e) => setDraft((d) => (d ? { ...d, note: e.target.value } : d))}
          placeholder="请输入处理说明（如：已反馈产品，排期支持）"
          rows={3}
        />
      </Modal>
    </div>
  );
}

function SubmitFeedbackTab() {
  const [form] = Form.useForm();

  async function handleSubmit(values: Record<string, unknown>) {
    try {
      await submitFeedback({
        target_type: String(values.target_type),
        target_id: values.target_id ? String(values.target_id) : null,
        rating: values.rating !== undefined ? Number(values.rating) : null,
        comment: values.comment ? String(values.comment) : null,
      });
      message.success("反馈已提交");
      form.resetFields();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "提交失败");
    }
  }

  return (
    <Card title="提交反馈" size="small">
      <Form form={form} layout="vertical" onFinish={handleSubmit} style={{ maxWidth: 520 }}>
        <Form.Item name="target_type" label="对象类型" rules={[{ required: true }]}>
          <Select options={TYPE_FILTER_OPTIONS} />
        </Form.Item>
        <Form.Item name="target_id" label="对象 ID">
          <Input className="mono" />
        </Form.Item>
        <Form.Item name="rating" label="评分">
          <Rate count={5} />
        </Form.Item>
        <Form.Item name="comment" label="意见">
          <Input.TextArea rows={3} />
        </Form.Item>
        <Button type="primary" htmlType="submit">提交反馈</Button>
      </Form>
    </Card>
  );
}

function NpsScores({ stats }: { stats: NpsStats | null }) {
  const score = stats?.score;
  const scoreColor = score !== undefined ? (score >= 50 ? "var(--ok)" : score >= 0 ? "var(--signal)" : "var(--danger)") : "var(--text-tertiary)";
  return (
    <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
      <Col xs={24} sm={6}>
        <Card size="small">
          <div className="muted">NPS 得分</div>
          <div style={{ fontFamily: "var(--font-display)", fontSize: 40, fontWeight: 700, color: scoreColor }}>
            {score !== undefined ? score : "—"}
          </div>
        </Card>
      </Col>
      <Col xs={24} sm={6}>
        <Card size="small">
          <div className="muted">推荐者（9-10）</div>
          <div style={{ fontSize: 28, fontWeight: 600, color: "var(--ok)" }}>{stats?.promoters ?? "—"}</div>
        </Card>
      </Col>
      <Col xs={24} sm={6}>
        <Card size="small">
          <div className="muted">被动者（7-8）</div>
          <div style={{ fontSize: 28, fontWeight: 600, color: "var(--signal)" }}>{stats?.passives ?? "—"}</div>
        </Card>
      </Col>
      <Col xs={24} sm={6}>
        <Card size="small">
          <div className="muted">贬损者（0-6）</div>
          <div style={{ fontSize: 28, fontWeight: 600, color: "var(--danger)" }}>{stats?.detractors ?? "—"}</div>
        </Card>
      </Col>
    </Row>
  );
}

function NpsTab() {
  const [modalOpen, setModalOpen] = useState(false);
  const [score, setScore] = useState<number>(8);
  const [stats, setStats] = useState<NpsStats | null>(null);
  const [form] = Form.useForm();

  function loadStats() {
    fetchNpsStats().then(setStats).catch(() => {});
  }

  useEffect(() => {
    loadStats();
  }, []);

  async function handleSubmit(values: Record<string, unknown>) {
    try {
      await submitNps({
        score: score,
        comment: values.comment ? String(values.comment) : null,
        target_type: String(values.target_type ?? "platform"),
        target_id: values.target_id ? String(values.target_id) : null,
      });
      message.success(`NPS ${score}/10 已提交，感谢反馈`);
      setModalOpen(false);
      form.resetFields();
      loadStats();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "提交失败");
    }
  }

  return (
    <div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }} message="净推荐值（NPS）调查——0-10 分，9-10 为推荐者。" />
      <NpsScores stats={stats} />
      <Button type="primary" icon={<StarOutlined />} onClick={() => setModalOpen(true)}>参与 NPS 调查</Button>

      <Modal title="NPS 调查" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="提交">
        <div style={{ textAlign: "center", padding: "12px 0" }}>
          <div className="muted" style={{ marginBottom: 8 }}>你有多大可能向同事推荐 Unisense？</div>
          <div style={{ fontFamily: "var(--font-display)", fontSize: 48, fontWeight: 700, color: score >= 9 ? "var(--ok)" : score >= 7 ? "var(--signal)" : "var(--danger)" }}>
            {score}
          </div>
          <InputNumber min={0} max={10} value={score} onChange={(v) => setScore(v ?? 8)} style={{ width: 140 }} />
        </div>
        <Form form={form} layout="vertical" onFinish={handleSubmit}>
          <Form.Item name="target_type" label="对象类型" initialValue="platform">
            <Select options={[{ value: "platform", label: "平台" }, { value: "metric", label: "指标" }, { value: "dashboard", label: "仪表盘" }]} />
          </Form.Item>
          <Form.Item name="comment" label="原因（可选）">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

export function FeedbackCenter() {
  const tabItems = [
    { key: "feedback", label: "用户反馈", children: <FeedbackTab /> },
    { key: "submit", label: "提交反馈", children: <SubmitFeedbackTab /> },
    { key: "nps", label: "NPS 调查", children: <NpsTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Operation / Feedback</div>
          <h2>用户反馈</h2>
          <p>反馈闭环处理与满意度（NPS）采集——面向平台运营。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}