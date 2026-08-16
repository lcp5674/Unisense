import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  message,
} from "antd";
import { ReloadOutlined, ArrowLeftOutlined, RedoOutlined, EyeOutlined } from "@ant-design/icons";
import { listCollectionJobs, getCollectionJob, collectSourceNow, listDataSources, UnisenseApiError } from "../api";
import type { CollectionJob, DataSource } from "../types";
import { formatCnTime } from "../utils/timeCn";
import { usePermission } from "../hooks/usePermission";

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
  const canCollect = usePermission().can("data-source:collect");
  const [searchParams] = useSearchParams();
  // 任务状态下钻（?status=，总览仪表「采集任务」资产卡片）作为初始筛选
  const urlStatus = searchParams.get("status") ?? "";
  const [items, setItems] = useState<CollectionJob[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [sources, setSources] = useState<DataSource[]>([]);
  const [sourceId, setSourceId] = useState<string | undefined>(undefined);
  const [status, setStatus] = useState<string>(urlStatus);
  const [loading, setLoading] = useState(false);
  const [retrying, setRetrying] = useState<string | null>(null);
  // 任务详情抽屉
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailJob, setDetailJob] = useState<CollectionJob | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await listCollectionJobs({
        limit: pageSize,
        offset: (page - 1) * pageSize,
        source_id: sourceId,
        status: status || undefined,
      });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [sourceId, status, page, pageSize]);

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

  /** 打开任务详情抽屉：拉取最新状态（含完整 error / 进度消息列表）。 */
  async function openDetail(job: CollectionJob) {
    setDetailJob(job);
    setDetailOpen(true);
    setDetailLoading(true);
    try {
      const fresh = await getCollectionJob(job.job_id);
      if (fresh) setDetailJob(fresh);
    } catch {
      /* 详情刷新失败保留列表行数据 */
    } finally {
      setDetailLoading(false);
    }
  }

  /** 失败任务重试：复用 collect-now 重新投递采集。 */
  async function handleRetry(job: CollectionJob) {
    if (!job.source_id) {
      message.warning("该任务缺少数据源标识，无法重试");
      return;
    }
    setRetrying(job.job_id);
    try {
      await collectSourceNow(job.source_id);
      message.success("已重新投递采集任务，可在列表中查看新任务");
      load();
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "重试失败",
      );
    } finally {
      setRetrying(null);
    }
  }

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
        v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span style={{ color: "#999" }}>—</span>,
    },
    {
      title: "操作",
      key: "action",
      width: 140,
      render: (_: unknown, r: CollectionJob) => (
        <Space size={0}>
          <Button type="link" size="small" icon={<EyeOutlined />} onClick={() => openDetail(r)}>
            详情
          </Button>
          {r.status === "FAILED" && canCollect && (
            <Button
              type="link"
              size="small"
              danger
              icon={<RedoOutlined />}
              loading={retrying === r.job_id}
              onClick={() => handleRetry(r)}
            >
              重试
            </Button>
          )}
        </Space>
      ),
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
            onChange={(v?: string) => { setSourceId(v || undefined); setPage(1); }}
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
            onChange={(v?: string) => { setStatus(v ?? ""); setPage(1); }}
            options={["QUEUED", "RUNNING", "COMPLETED", "FAILED"].map((s) => ({
              value: s,
              label: STATUS_LABEL[s] ?? s,
            }))}
          />
        </Space>
        <Table<CollectionJob>
          rowKey="job_id"
          loading={loading}
          dataSource={items}
          columns={columns}
          size="middle"
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50, 100],
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p, ps) => { setPage(p); setPageSize(ps); },
          }}
        />
      </Space>
    </Card>

      <Drawer
        title="采集任务详情"
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width={640}
      >
        {detailJob && (
          <Descriptions
            size="small"
            column={1}
            bordered
            items={[
              { key: "job", label: "任务 ID", children: <span className="mono">{detailJob.job_id}</span> },
              {
                key: "source",
                label: "数据源",
                children: detailJob.source_id ? (
                  <span className="mono">{detailJob.source_id}</span>
                ) : (
                  <span className="muted">—</span>
                ),
              },
              { key: "kind", label: "类型", children: kindTag(detailJob.kind) },
              { key: "status", label: "状态", children: statusTag(detailJob.status) },
              {
                key: "created",
                label: "创建时间",
                children: detailJob.created_at ? formatCnTime(detailJob.created_at) : "—",
              },
              {
                key: "actor",
                label: "触发人 ID",
                children: detailJob.actor_id != null ? detailJob.actor_id : <span className="muted">—</span>,
              },
              {
                key: "error",
                label: "错误信息",
                children: detailJob.detail?.error ? (
                  <span className="mono" style={{ color: "#cf1322", whiteSpace: "pre-wrap" }}>
                    {String(detailJob.detail.error)}
                  </span>
                ) : (
                  <span className="muted">—</span>
                ),
              },
              {
                key: "progress",
                label: "进度消息",
                children: (() => {
                  const msgs = (detailJob.detail?.progress as { messages?: string[] } | undefined)?.messages;
                  if (!msgs || msgs.length === 0) {
                    return detailLoading ? <span className="muted">加载中…</span> : <span className="muted">—</span>;
                  }
                  return (
                    <ul style={{ margin: 0, paddingLeft: 16, maxHeight: 260, overflow: "auto" }}>
                      {msgs.map((m, i) => (
                        <li key={i} style={{ fontSize: 12 }}>{m}</li>
                      ))}
                    </ul>
                  );
                })(),
              },
            ]}
          />
        )}
        {!detailJob && <Empty description="无任务数据" />}
      </Drawer>
    </div>
  );
}
