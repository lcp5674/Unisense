import { useEffect, useState } from "react";
import { Card, Table, Tag, Button, Modal, Form, Input, InputNumber, Select, message, Tabs, Alert } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import {
  listNotifications,
  listNotifyEvents,
  listSubscriptions,
  upsertSubscription,
  publishNotifyEvent,
  UnisenseApiError,
} from "../api";
import type { Notification, NotifyEventLog, SubscriptionPref } from "../types";
import { NOTIFY_STATUS_LABEL, QUALITY_LEVEL_LABEL } from "../utils/enums";

// 渠道 = 消息送达方式（面向业务用户，不用 webhook/sms 等英文码）
const CHANNEL_LABEL: Record<string, string> = {
  email: "邮件",
  webhook: "接口推送",
  sms: "短信",
  inapp: "站内消息",
  dingtalk: "钉钉",
  console: "控制台",
};

// 消息类型（业务术语，替代 metric.created 等内部事件码）
const EVENT_TYPE_LABEL: Record<string, string> = {
  "metric.created": "指标创建",
  "metric.published": "指标发布",
  "metric.deprecated": "指标废弃",
  "conflict.detected": "口径冲突",
  "quality.alert": "数据质量告警",
  "governance.grant": "权限变更",
  "lineage.change": "血缘变更",
  "system.notice": "系统公告",
};

// 所属模块（业务术语）
const SOURCE_LABEL: Record<string, string> = {
  metric: "指标",
  lineage: "血缘",
  quality: "数据质量",
  governance: "治理合规",
  semantic: "指标口径",
  system: "系统",
  scheduler: "定时任务",
};

// 关联对象（把 metric#5 这样的技术引用翻译成业务可读的「指标 #5」）
const REF_TYPE_LABEL: Record<string, string> = {
  metric: "指标",
  metric_definition: "指标",
  metric_version: "指标版本",
  metric_template: "指标模板",
  quality_rule: "质量规则",
  quality_event: "数据质量告警",
  conflict: "口径冲突",
  grant: "权限授权",
  term: "业务术语",
  dimension: "维度",
  lineage_edge: "血缘关系",
  data_source: "数据源",
  db_catalog: "目录实体",
  notification: "通知",
  reconciliation: "数据对账",
  benchmark: "参照基准",
};

const CHANNELS = ["email", "webhook", "sms", "inapp"];
const EVENT_TYPES = ["metric.created", "metric.published", "metric.deprecated", "conflict.detected", "quality.alert", "governance.grant", "lineage.change", "system.notice"];

function NotifListTab() {
  const [items, setItems] = useState<Notification[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const res = await listNotifications();
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
  }, []);

  const columns = [
    { title: "编号", dataIndex: "id", key: "id", width: 70 },
    { title: "标题", dataIndex: "title", key: "title" },
    { title: "内容", dataIndex: "body", key: "body", ellipsis: true },
    { title: "送达方式", dataIndex: "channel", key: "channel", width: 100, render: (v: string) => <Tag>{CHANNEL_LABEL[v] ?? v}</Tag> },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (v: string) => <Tag color={v === "SENT" ? "success" : v === "FAILED" ? "error" : "warning"}>{NOTIFY_STATUS_LABEL[v] ?? v}</Tag>,
    },
    {
      title: "关联对象",
      dataIndex: "ref_type",
      key: "ref",
      width: 140,
      render: (v: string | null, r: Notification) =>
        v ? `${REF_TYPE_LABEL[v] ?? v} #${r.ref_id}` : <span className="muted">—</span>,
    },
    { title: "送达时间", dataIndex: "sent_at", key: "sent", width: 170, render: (v: string | null) => v ?? <span className="muted">尚未送达</span> },
  ];

  return <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={{ pageSize: 20, total, showTotal: (t) => `共 ${t} 条` }} locale={{ emptyText: "暂无通知" }} />;
}

function SubscriptionsTab() {
  const [items, setItems] = useState<SubscriptionPref[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm();

  async function load() {
    setLoading(true);
    try {
      const res = await listSubscriptions();
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

  async function handleCreate(values: Record<string, unknown>) {
    try {
      await upsertSubscription({
        channel: String(values.channel),
        event_type: String(values.event_type),
        enabled: true,
        threshold: values.threshold !== undefined && values.threshold !== null ? Number(values.threshold) : null,
      });
      message.success("订阅已保存");
      setModalOpen(false);
      form.resetFields();
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "保存失败");
    }
  }

  const columns = [
    { title: "送达方式", dataIndex: "channel", key: "channel", width: 130, render: (v: string) => <Tag>{CHANNEL_LABEL[v] ?? v}</Tag> },
    { title: "消息类型", dataIndex: "event_type", key: "event", render: (v: string) => EVENT_TYPE_LABEL[v] ?? v },
    { title: "告警阈值", dataIndex: "threshold", key: "threshold", width: 100, render: (v: number | null) => v ?? <span className="muted">—</span> },
    { title: "是否启用", dataIndex: "enabled", key: "enabled", width: 90, render: (v: boolean) => <Tag color={v ? "success" : "default"}>{v ? "是" : "否"}</Tag> },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end" }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>新增订阅</Button>
      </div>
      <Table dataSource={items} columns={columns} rowKey={(r) => `${r.user_id}-${r.channel}-${r.event_type}`} loading={loading} pagination={false} locale={{ emptyText: "暂无订阅" }} />

      <Modal title="新增订阅" open={modalOpen} onCancel={() => setModalOpen(false)} onOk={() => form.submit()} okText="保存">
        <Form form={form} layout="vertical" onFinish={handleCreate} style={{ marginTop: 8 }}>
          <Form.Item name="channel" label="送达方式" rules={[{ required: true }]}>
            <Select options={CHANNELS.map((c) => ({ value: c, label: CHANNEL_LABEL[c] ?? c }))} />
          </Form.Item>
          <Form.Item name="event_type" label="消息类型" rules={[{ required: true }]}>
            <Select options={EVENT_TYPES.map((c) => ({ value: c, label: EVENT_TYPE_LABEL[c] ?? c }))} />
          </Form.Item>
          <Form.Item name="threshold" label="告警阈值（可选）" extra="用于数据质量告警，达到该阈值时才推送。">
            <InputNumber min={1} style={{ width: 200 }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function EventLogTab() {
  const [items, setItems] = useState<NotifyEventLog[]>([]);
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const res = await listNotifyEvents();
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

  const columns = [
    { title: "编号", dataIndex: "id", key: "id", width: 70 },
    { title: "消息类型", dataIndex: "event_type", key: "event", render: (v: string) => EVENT_TYPE_LABEL[v] ?? v },
    { title: "所属模块", dataIndex: "source", key: "source", width: 110, render: (v: string | null) => (v ? SOURCE_LABEL[v] ?? v : <span className="muted">—</span>) },
    { title: "重要程度", dataIndex: "level", key: "level", width: 90, render: (v: string) => <Tag color={v === "ERROR" ? "error" : v === "WARN" ? "warning" : "default"}>{QUALITY_LEVEL_LABEL[v] ?? v}</Tag> },
    { title: "已推送", dataIndex: "notified", key: "notified", width: 90, render: (v: boolean) => <Tag color={v ? "success" : "default"}>{v ? "是" : "否"}</Tag> },
    { title: "触发时间", dataIndex: "created_at", key: "created", width: 170 },
  ];

  return <Table dataSource={items} columns={columns} rowKey="id" loading={loading} pagination={{ pageSize: 20 }} locale={{ emptyText: "暂无消息记录" }} />;
}

function PublishTab() {
  const [form] = Form.useForm();

  async function handlePublish(values: Record<string, unknown>) {
    try {
      const res = await publishNotifyEvent({
        event_type: String(values.event_type),
        source: values.source ? String(values.source) : null,
        level: String(values.level ?? "INFO"),
        payload: { note: values.note ? String(values.note) : null },
      });
      message.success(`消息已发送：生成 ${res.notifications} 条通知，成功送达 ${res.delivered} 条`);
      form.resetFields();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "发送失败");
    }
  }

  return (
    <div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }} message="手动发送的消息会按您的订阅设置推送到对应渠道（邮件 / 接口推送 / 短信 / 站内消息）。" />
      <Card title="发送消息" size="small">
        <Form form={form} layout="vertical" onFinish={handlePublish}>
          <Form.Item name="event_type" label="消息类型" rules={[{ required: true }]}>
            <Select options={EVENT_TYPES.map((c) => ({ value: c, label: EVENT_TYPE_LABEL[c] ?? c }))} />
          </Form.Item>
          <Form.Item name="source" label="所属模块">
            <Select allowClear options={["metric", "lineage", "quality", "governance", "semantic", "system", "scheduler"].map((c) => ({ value: c, label: SOURCE_LABEL[c] ?? c }))} />
          </Form.Item>
          <Form.Item name="level" label="重要程度" initialValue="INFO">
            <Select options={["INFO", "WARN", "ERROR"].map((c) => ({ value: c, label: QUALITY_LEVEL_LABEL[c] ?? c }))} />
          </Form.Item>
          <Form.Item name="note" label="补充说明">
            <Input.TextArea rows={2} />
          </Form.Item>
          <Button type="primary" htmlType="submit">发送消息</Button>
        </Form>
      </Card>
    </div>
  );
}

export function Notifications() {
  const tabItems = [
    { key: "list", label: "我的通知", children: <NotifListTab /> },
    { key: "subs", label: "订阅设置", children: <SubscriptionsTab /> },
    { key: "events", label: "消息记录", children: <EventLogTab /> },
    { key: "publish", label: "发送消息", children: <PublishTab /> },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="page-kicker">Collaboration / Notifications</div>
          <h2>通知中心</h2>
          <p>集中查看系统发送给您的消息、管理接收偏好，并跟踪每条消息的送达情况。</p>
        </div>
      </div>
      <Card styles={{ body: { paddingTop: 8 } }}>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
