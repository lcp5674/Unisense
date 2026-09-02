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
import { QUALITY_LEVEL_LABEL, QUALITY_SEVERITY_LABEL, RULE_TYPE_LABEL, RULE_MODE_LABEL } from "../../utils/enums";
import { formatCnTime, formatCnRange } from "../../utils/timeCn";
import { usePermission } from "../../hooks/usePermission";

/** 指标状态中文（消费上下文，仅快照区用） */
const CONSUMABLE_STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  REVIEW: "审核中",
  PUBLISHED: "已发布",
  EXPERIMENTAL: "灰度发布",
  DEPRECATED: "已废弃",
};

/**
 * 快照读的「不可消费」前置判定：返回非空即跳过快照请求（不发注定 403 的请求）。
 * - DRAFT/REVIEW：未发布，任何角色不可消费
 * - DEPRECATED：仅平台管理员可审计回溯，非管理员直接拦截
 * - 其余状态（PUBLISHED/EXPERIMENTAL）不前置拦截，交后端状态闸门（灰度白名单等）
 */
function snapshotBlockReason(status: string | undefined, isAdmin: boolean): string | null {
  if (!status) return null;
  if (status === "DRAFT" || status === "REVIEW") {
    return `指标当前为「${CONSUMABLE_STATUS_LABEL[status] ?? status}」状态，未发布，暂无消费快照`;
  }
  if (status === "DEPRECATED" && !isAdmin) {
    return "指标已废弃（DEPRECATED），仅平台管理员可审计回溯历史快照";
  }
  return null;
}

/** 快照读取的「无权限」错误码集合（FORBIDDEN 系列）：覆盖域/白名单/PII/废弃/灰度拒绝。 */
const SNAPSHOT_FORBIDDEN_CODES = new Set([
  "FORBIDDEN",
  "FORBIDDEN_DOMAIN",
  "FORBIDDEN_METRIC",
  "FORBIDDEN_PII",
  "FORBIDDEN_DEPRECATED",
]);

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

export function QualitySnapshot({
  metricId,
  metricCode,
  status,
  canReadSnapshot,
}: {
  metricId: number;
  metricCode: string;
  status?: string;
  /** 详情端点带的前置标记：false=无快照读权限（PDP），直接展示引导、不发注定 403 的请求 */
  canReadSnapshot?: boolean | null;
}) {
  const { snapshot: perm } = usePermission();
  const isAdmin = perm?.roles?.includes("platform_admin") ?? false;
  const [events, setEvents] = useState<QualityEvent[]>([]);
  const [rules, setRules] = useState<QualityRule[]>([]);
  const [snapshots, setSnapshots] = useState<SnapshotResponse[]>([]);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    // 前置判定：状态级（DRAFT/REVIEW/DEPRECATED）或 PDP 级（can_read_snapshot=false）
    // 命中任一即直接展示引导文案，不发注定 403 的快照请求（消除控制台 403 红字）。
    const statusBlocked = snapshotBlockReason(status, isAdmin);
    const blocked =
      statusBlocked ??
      (canReadSnapshot === false
        ? "当前账号未获得该指标所属域的数据读取授权，如需查看请联系域管理员配置授权（grants）或指标白名单。"
        : null);
    const snapPromise = blocked
      ? Promise.resolve({ data: [] as SnapshotResponse[], error: blocked as string | null })
      : listSnapshots(metricCode, 10)
          .then((d) => ({ data: d, error: null as string | null }))
          .catch((err: unknown) => ({
            data: [] as SnapshotResponse[],
            error:
              err instanceof UnisenseApiError && SNAPSHOT_FORBIDDEN_CODES.has(err.code)
                ? err.message
                : null,
          }));
    try {
      const [ev, ru, sn] = await Promise.all([
        listQualityEvents({ metric_id: metricId, page_size: 20 }).catch(() => ({ items: [] as QualityEvent[] })),
        listQualityRules({ metric_id: metricId, page_size: 20 }).catch(() => ({ items: [] as QualityRule[] })),
        snapPromise,
      ]);
      setEvents(ev.items);
      setRules(ru.items);
      setSnapshots(sn.data);
      setSnapshotError(sn.error);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [metricId, metricCode, status, isAdmin]);

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
    { title: "级别", dataIndex: "level", key: "level", width: 80, render: (v: string) => <Tag color={SEVERITY_COLOR[v]}>{QUALITY_LEVEL_LABEL[v] ?? v}</Tag> },
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
    { title: "时间", dataIndex: "created_at", key: "created", render: (v: string | null) => (v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : "—") },
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
      render: (v: string) => <Tag color={SEVERITY_COLOR[v]}>{QUALITY_SEVERITY_LABEL[v] ?? v}</Tag>,
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
    { title: "时间范围", dataIndex: "date_range", key: "range", render: (v: string) => <span style={{ fontSize: 12 }}>{formatCnRange(v)}</span> },
    { title: "质量标记", dataIndex: "quality_flag", key: "qf", width: 120, render: (v: string | null) => v ? <Tag color={v === "GOOD" ? "success" : "warning"}>{v}</Tag> : <span className="muted">—</span> },
    { title: "生成方式", dataIndex: "generated_by", key: "gen", width: 120, render: (v: string) => v },
    { title: "生成时间", dataIndex: "generated_at", key: "genat", render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> },
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
          label: snapshotError ? "消费快照（不可用）" : `消费快照 (${snapshots.length})`,
          children: snapshotError ? (
            <Alert
              type="warning"
              showIcon
              message="该指标消费快照当前不可查看"
              description={
                status === "DRAFT" || status === "REVIEW" || (status === "DEPRECATED" && !isAdmin)
                  ? `${snapshotError}。指标发布（PUBLISHED）后即可在消费侧查看历史快照。`
                  : `${snapshotError}。当前账号未获得该指标所属域的数据读取授权，如需查看请联系域管理员配置授权（grants）或指标白名单。`
              }
            />
          ) : (
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
