import { useEffect, useState } from "react";
import { Button, Table, Tabs, Tag, message, Alert } from "antd";
import {
  listQualityEvents,
  listQualityRules,
  listSnapshots,
  qualityEventAck,
  qualityEventResolve,
  qualityEventClose,
  UnisenseApiError,
} from "../../api";
import type { QualityEvent, QualityRule, SnapshotResponse } from "../../types";
import { ThresholdSummary } from "../../utils/display";
import { RULE_TYPE_LABEL, RULE_MODE_LABEL } from "../../utils/enums";

const EVENT_STATUS: Record<string, { color: string; label: string }> = {
  OPEN: { color: "error", label: "开启" },
  ACK: { color: "processing", label: "已确认" },
  RESOLVED: { color: "warning", label: "已解决" },
  CLOSED: { color: "default", label: "已关闭" },
};

const SEVERITY_COLOR: Record<string, string> = {
  P0: "red",
  P1: "orange",
  P2: "blue",
};

export function QualitySnapshot({ metricId, metricCode }: { metricId: number; metricCode: string }) {
  const [events, setEvents] = useState<QualityEvent[]>([]);
  const [rules, setRules] = useState<QualityRule[]>([]);
  const [snapshots, setSnapshots] = useState<SnapshotResponse[]>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      const [ev, ru, sn] = await Promise.all([
        listQualityEvents({ metric_id: metricId, page_size: 20 }).catch(() => ({ items: [] as QualityEvent[] })),
        listQualityRules({ metric_id: metricId, page_size: 20 }).catch(() => ({ items: [] as QualityRule[] })),
        listSnapshots(metricCode, 10).catch(() => [] as SnapshotResponse[]),
      ]);
      setEvents(ev.items);
      setRules(ru.items);
      setSnapshots(sn);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [metricId, metricCode]);

  async function act(fn: () => Promise<unknown>, okMsg: string) {
    try {
      await fn();
      message.success(okMsg);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    }
  }

  const eventColumns = [
    { title: "级别", dataIndex: "level", key: "level", width: 80, render: (v: string) => <Tag color={SEVERITY_COLOR[v]}>{v}</Tag> },
    { title: "规则", dataIndex: "rule_type", key: "type", render: (v: string) => <Tag>{RULE_TYPE_LABEL[v] ?? v}</Tag> },
    { title: "观测值", dataIndex: "obs_value", key: "obs", width: 90, render: (v: number | null) => v ?? <span className="muted">—</span> },
    { title: "阈值", dataIndex: "threshold", key: "thr", width: 90, render: (v: number | null) => v ?? <span className="muted">—</span> },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 90,
      render: (v: string) => <Tag color={EVENT_STATUS[v]?.color}>{EVENT_STATUS[v]?.label ?? v}</Tag>,
    },
    { title: "时间", dataIndex: "created_at", key: "created", render: (v: string | null) => v ?? "—" },
    {
      title: "操作",
      key: "action",
      width: 210,
      render: (_: unknown, e: QualityEvent) => (
        <>
          {e.status === "OPEN" && (
            <Button size="small" onClick={() => act(() => qualityEventAck(e.id), "已确认")}>确认</Button>
          )}
          {(e.status === "OPEN" || e.status === "ACK") && (
            <Button size="small" style={{ marginLeft: 8 }} onClick={() => act(() => qualityEventResolve(e.id), "已解决")}>
              解决
            </Button>
          )}
          {e.status !== "CLOSED" && (
            <Button size="small" style={{ marginLeft: 8 }} onClick={() => act(() => qualityEventClose(e.id), "已关闭")}>
              关闭
            </Button>
          )}
        </>
      ),
    },
  ];

  const ruleColumns = [
    { title: "类型", dataIndex: "rule_type", key: "type", render: (v: string) => <Tag>{RULE_TYPE_LABEL[v] ?? v}</Tag> },
    { title: "模式", dataIndex: "rule_mode", key: "mode", width: 130, render: (v: string) => RULE_MODE_LABEL[v] ?? v },
    {
      title: "严重度",
      dataIndex: "severity",
      key: "severity",
      width: 90,
      render: (v: string) => <Tag color={SEVERITY_COLOR[v]}>{v}</Tag>,
    },
    {
      title: "启用",
      dataIndex: "enabled",
      key: "enabled",
      width: 80,
      render: (v: boolean) => <Tag color={v ? "success" : "default"}>{v ? "启用" : "停用"}</Tag>,
    },
    { title: "阈值", dataIndex: "threshold", key: "threshold", render: (v: Record<string, unknown>) => <ThresholdSummary threshold={v} /> },
  ];

  const snapshotColumns = [
    { title: "版本", dataIndex: "version", key: "version", width: 80, render: (v: number) => `v${v}` },
    { title: "时间范围", dataIndex: "date_range", key: "range" },
    { title: "质量标记", dataIndex: "quality_flag", key: "qf", width: 120, render: (v: string | null) => v ? <Tag color={v === "GOOD" ? "success" : "warning"}>{v}</Tag> : <span className="muted">—</span> },
    { title: "生成方式", dataIndex: "generated_by", key: "gen", width: 120, render: (v: string) => v },
    { title: "生成时间", dataIndex: "generated_at", key: "genat", render: (v: string) => v },
  ];

  return (
    <Tabs
      size="small"
      items={[
        {
          key: "events",
          label: `质量事件 (${events.length})`,
          children: (
            <Table
              dataSource={events}
              columns={eventColumns}
              rowKey="id"
              size="small"
              pagination={false}
              loading={loading}
              locale={{ emptyText: "暂无质量事件" }}
            />
          ),
        },
        {
          key: "rules",
          label: `生效规则 (${rules.length})`,
          children: (
            <Table
              dataSource={rules}
              columns={ruleColumns}
              rowKey="id"
              size="small"
              pagination={false}
              loading={loading}
              locale={{ emptyText: "暂无生效规则（随指标 PUBLISHED 自动注册）" }}
            />
          ),
        },
        {
          key: "snapshots",
          label: `消费快照 (${snapshots.length})`,
          children: (
            <Table
              dataSource={snapshots}
              columns={snapshotColumns}
              rowKey="id"
              size="small"
              pagination={false}
              loading={loading}
              locale={{ emptyText: "暂无快照" }}
            />
          ),
        },
      ]}
    />
  );
}

// 展示型：质量总体健康提示（有 OPEN 事件时给出告警条）
export function QualityAlert({ events }: { events: QualityEvent[] }) {
  const open = events.filter((e) => e.status === "OPEN");
  if (!open.length) return null;
  return (
    <Alert
      type="warning"
      showIcon
      message={`该指标有 ${open.length} 个待处理质量事件`}
      description="质量事件可能影响口径可信度，建议前往质量中心处理。"
      style={{ marginBottom: 16 }}
    />
  );
}
