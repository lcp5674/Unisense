import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Card, Space, Table, Tag, Tooltip, message } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { listCollectionJobs, UnisenseApiError } from "../api";
import type { CollectionJob } from "../types";

const STATUS_LABEL: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "采集中",
  COMPLETED: "已完成",
  FAILED: "失败",
};

function statusTag(v: string) {
  const color =
    v === "COMPLETED"
      ? "success"
      : v === "FAILED"
        ? "error"
        : v === "RUNNING"
          ? "processing"
          : "default";
  return <Tag color={color}>{STATUS_LABEL[v] ?? v}</Tag>;
}

function detailText(detail: Record<string, unknown> | undefined): string {
  if (!detail) return "";
  const parts: string[] = [];
  if (detail.mode) parts.push(`模式=${detail.mode}`);
  if (detail.scanned !== undefined) parts.push(`扫描=${detail.scanned}`);
  if (detail.registered !== undefined) parts.push(`注册=${detail.registered}`);
  if (detail.failed_count !== undefined) parts.push(`失败=${detail.failed_count}`);
  if (detail.pii_registered !== undefined) parts.push(`PII=${detail.pii_registered}`);
  if (detail.error) parts.push(String(detail.error));
  return parts.join(" · ");
}

export function CollectionTasks() {
  const [jobs, setJobs] = useState<CollectionJob[]>([]);
  const [loading, setLoading] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await listCollectionJobs({ limit: 50 });
      setJobs(res);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    load();
    // 异步任务状态在后台变化，每 5s 轮询刷新（对齐 worker 秒级回写节奏）
    timerRef.current = setInterval(load, 5000);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [load]);

  const columns = [
    {
      title: "任务 ID",
      dataIndex: "job_id",
      key: "jobId",
      ellipsis: true,
      render: (v: string) => (
        <Tooltip title={v}>
          <span className="mono" style={{ fontSize: 12 }}>
            {v}
          </span>
        </Tooltip>
      ),
    },
    {
      title: "数据源",
      dataIndex: "source_id",
      key: "sourceId",
      ellipsis: true,
      render: (v?: string) =>
        v ? <span className="mono" style={{ fontSize: 12 }}>{v}</span> : <span style={{ color: "#999" }}>—</span>,
    },
    { title: "状态", dataIndex: "status", key: "status", width: 110, render: (v: string) => statusTag(v) },
    {
      title: "进度 / 结果",
      key: "detail",
      ellipsis: true,
      render: (_: unknown, r: CollectionJob) => (
        <span className="mono" style={{ fontSize: 12 }}>
          {detailText(r.detail) || "—"}
        </span>
      ),
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "createdAt",
      width: 190,
      render: (v?: string | null) =>
        v ? new Date(v).toLocaleString("zh-CN") : <span style={{ color: "#999" }}>—</span>,
    },
  ];

  return (
    <Card
      title={
        <span>
          采集任务中心
          <span className="page-eyebrow">Collection Jobs · 异步采集进度与结果</span>
        </span>
      }
      extra={
        <Button icon={<ReloadOutlined />} onClick={() => { setLoading(true); load(); }} loading={loading}>
          刷新
        </Button>
      }
      style={{ marginBottom: 16 }}
    >
      <Space direction="vertical" style={{ width: "100%" }} size={16}>
        <Table<CollectionJob>
          rowKey="job_id"
          loading={loading}
          dataSource={jobs}
          columns={columns}
          size="middle"
          pagination={jobs.length > 10 ? { pageSize: 10 } : false}
        />
      </Space>
    </Card>
  );
}
