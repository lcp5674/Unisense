import { useEffect, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, InputNumber, Select, Rate, message, Tabs, Space, Statistic, Row, Col, Alert } from "antd";
import { StarOutlined } from "@ant-design/icons";
import {
  fetchObsMetricsQuality,
  fetchObsMetricsApi,
  fetchObsMetricsNotifications,
  fetchObsMetricsLineage,
  listFeedback,
  submitFeedback,
  updateFeedbackStatus,
  submitNps,
  UnisenseApiError,
} from "../api";
import type { Feedback } from "../types";

function MetricsTab() {
  const [quality, setQuality] = useState<{ by_level: Record<string, number>; by_status: Record<string, number>; total: number } | null>(null);
  const [api, setApi] = useState<Record<string, number> | null>(null);
  const [notif, setNotif] = useState<{ by_status: Record<string, number>; event_total: number; event_notified: number } | null>(null);
  const [lineage, setLineage] = useState<{ edges: number } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetchObsMetricsQuality(),
      fetchObsMetricsApi(),
      fetchObsMetricsNotifications(),
      fetchObsMetricsLineage(),
    ])
      .then(([q, a, n, l]) => {
        setQuality(q);
        setApi(a);
        setNotif(n);
        setLineage(l);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) return null;

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Statistic title="质量事件" value={quality?.total ?? 0} />
        </Col>
        <Col span={6}>
          <Statistic title="事件已通知" value={notif?.event_notified ?? 0} suffix={`/ ${notif?.event_total ?? 0}`} />
        </Col>
        <Col span={6}>
          <Statistic title="血缘边数" value={lineage?.edges ?? 0} />
        </Col>
        <Col span={6}>
          <Statistic title="API 动作类型" value={api ? Object.keys(api).length : 0} />
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={8}>
          <Card title="质量事件级别分布" size="small">
            {Object.entries(quality?.by_level ?? {}).map(([k, v]) => (
              <div key={k} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "1px solid var(--line-soft)" }}>
                <Tag color={k === "ERROR" ? "error" : k === "WARN" ? "warning" : "default"}>{k}</Tag>
                <span className="mono">{v}</span>
              </div>
            ))}
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card title="通知投递状态" size="small">
            {Object.entries(notif?.by_status ?? {}).map(([k, v]) => (
              <div key={k} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "1px solid var(--line-soft)" }}>
                <Tag color={k === "FAILED" ? "error" : k === "SENT" ? "success" : "warning"}>{k}</Tag>
                <span className="mono">{v}</span>
              </div>
            ))}
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card title="API 动作分布" size="small">
            {Object.entries(api ?? {}).slice(0, 12).map(([k, v]) => (
              <div key={k} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "1px solid var(--line-soft)" }}>
                <span className="mono" style={{ fontSize: 12 }}>{k}</span>
                <span className="mono">{v}</span>
              </div>
            ))}
          </Card>
        </Col>
      </Row>
    </div>
  );
}

function FeedbackTab() {
  const [items, setItems] = useState<Feedback[]>([]);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const res = await listFeedback();
      setItems(res.items);
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

  async function handleStatus(f: Feedback, status: string) {
    try {
      await updateFeedbackStatus(f.id, status, "前台处理");
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "更新失败");
    }
  }

  const columns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "用户", dataIndex: "user_id", key: "user", width: 80 },
    { title: "对象类型", dataIndex: "target_type", key: "targetType", width: 110, render: (v: string) => <Tag>{v}</Tag> },
    { title: "对象 ID", dataIndex: "target_id", key: "targetId", width: 160, render: (v: string | null) => v ?? <span className="muted">—</span> },
    { title: "评分", dataIndex: "rating", key: "rating", width: 100, render: (v: number | null) => (v !== null ? <Rate disabled defaultValue={v} count={5} /> : <span className="muted">—</span>) },
    { title: "内容", dataIndex: "comment", key: "comment", ellipsis: true },
    { title: "时间", dataIndex: "created_at", key: "created", width: 170 },
    {
      title: "处理",
      key: "actions",
      width: 220,
      render: (_: unknown, f: Feedback) => (
        <Space>
          <Button size="small" onClick={() => handleStatus(f, "in_progress")}>跟进</Button>
          <Button size="small" type="primary" onClick={() => handleStatus(f, "adopted")}>采纳</Button>
          <Button size="small" danger onClick={() => handleStatus(f, "rejected")}>驳回</Button>
        </Space>
      ),
    },
  ];

  return <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={{ pageSize: 20 }} locale={{ emptyText: "暂无反馈" }} />;
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
      message.error(err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "提交失败");
    }
  }

  return (
    <Card title="提交反馈" size="small">
      <Form form={form} layout="vertical" onFinish={handleSubmit} style={{ maxWidth: 520 }}>
        <Form.Item name="target_type" label="对象类型" rules={[{ required: true }]}>
          <Select options={["metric", "term", "report", "dashboard"].map((v) => ({ value: v, label: v }))} />
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

function NpsTab() {
  const [modalOpen, setModalOpen] = useState(false);
  const [score, setScore] = useState<number>(8);
  const [form] = Form.useForm();

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
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message} (${err.code})` : "提交失败");
    }
  }

  return (
    <div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }} message="净推荐值（NPS）调查——0-10 分，9-10 为推荐者。" />
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

export function Observability() {
  const tabItems = [
    { key: "metrics", label: "概览指标", children: <MetricsTab /> },
    { key: "feedback", label: "用户反馈", children: <FeedbackTab /> },
    { key: "submit", label: "提交反馈", children: <SubmitFeedbackTab /> },
    { key: "nps", label: "NPS 调查", children: <NpsTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Intelligence / Observability</div>
          <h2>可观测中心</h2>
          <p>质量/通知/血缘运行指标 + 用户反馈闭环与 NPS。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
