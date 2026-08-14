import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Button, Card, Select, Space, Table, Tag, Tooltip, message } from "antd";
import { ReloadOutlined, ArrowLeftOutlined } from "@ant-design/icons";
import { listCollectionJobs, listDataSources, UnisenseApiError } from "../api";
import type { CollectionJob, DataSource } from "../types";

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

/** 任务来源标记：定时调度 vs 手动触发。 */
function kindTag(kind?: string) {
  if (kind === "scheduled") return <Tag color="blue">定时</Tag>;
  if (kind === "manual") return <Tag>手动</Tag>;
  return <span style={{ color: "#999" }}>—</span>;
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
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // 任务状态下钻（?status=，总览仪表「采集任务」资产卡片）作为初始筛选
  const urlStatus = searchParams.get("status") ?? "";
  const [jobs, setJobs] = useState<CollectionJob[]>([]);
  const [sources, setSources] = useState<DataSource[]>([]);
  const [sourceId, setSourceId] = useState<string | undefined>(undefined);
  const [status, setStatus] = useState<string>(urlStatus);
  const [loading, setLoading] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await listCollectionJobs({ limit: 50, source_id: sourceId, status: status || undefined });
      setJobs(res);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [sourceId, status]);

  // 统一返回上一入口：优先回退浏览器历史（总览资产卡片等入口），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  useEffect(() => {
    setLoading(true);
    load();
    // 异步任务状态在后台变化，每 5s 轮询刷新（对齐 worker 秒级回写节奏）
    timerRef.current = setInterval(load, 5000);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [load]);

  // 数据源筛选选项：供"按数据源查看任务"使用
  useEffect(() => {
    listDataSources({ page: 1, page_size: 100 })
      .then((res) => setSources(res.items))
      .catch(() => setSources([]));
  }, []);

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
    { title: "类型", dataIndex: "kind", key: "kind", width: 80, render: (v?: string) => kindTag(v) },
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
    <div>
      <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 8 }}>
        返回
      </Button>
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
        <Space wrap>
          <Select
            allowClear
            showSearch
            placeholder="按数据源筛选"
            style={{ width: 260 }}
            value={sourceId}
            onChange={(v?: string) => setSourceId(v || undefined)}
            options={sources.map((s) => ({
              value: s.source_id,
              label: `${s.name}（${s.source_id}）`,
            }))}
            optionFilterProp="label"
          />
          <Select
            allowClear
            placeholder="全部状态"
            style={{ width: 130 }}
            value={status || undefined}
            onChange={(v?: string) => setStatus(v ?? "")}
            options={["QUEUED", "RUNNING", "COMPLETED", "FAILED"].map((s) => ({
              value: s,
              label: STATUS_LABEL[s] ?? s,
            }))}
          />
        </Space>
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
    </div>
  );
}
