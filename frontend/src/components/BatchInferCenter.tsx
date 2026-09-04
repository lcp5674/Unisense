/**
 * 跨表批量 LLM 推断任务中心（方案 B：后端任务化）。
 *
 * 右下角悬浮：轮询「本人」批量任务（batch_llm_infer_task），有进行中任务时
 * 显示浮条（任意页面可见，含切换/刷新后恢复）；点击展开 Drawer 查看逐表进度、
 * 取消任务、最近完成结果。解决「批量推断切页后看不到进度/结果」的历史缺陷。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  Button,
  Drawer,
  Progress,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  CheckCircleOutlined,
  FireOutlined,
  StopOutlined,
} from "@ant-design/icons";
import {
  cancelBatchInferTask,
  listBatchInferTasks,
  type BatchInferTask,
  type BatchInferTaskProgressItem,
} from "../api";
import { formatCnTime } from "../utils/timeCn";

const RUNNING = new Set(["pending", "running"]);
const POLL_MS = 4000;
/** 任务进入终态后，完成摘要浮条的停留时长（ms）。避免「任务跑完浮条无声消失、用户不知结果」。 */
const FINISHED_NOTICE_MS = 10_000;

/**
 * 模块级批量任务「活动事件」总线（pub/sub）。
 *
 * 任务中心按状态机轮询（有运行中任务才 4s 轮询，无任务零请求）——但零请求意味着
 * 它无法自己感知「别的入口刚提交了任务」。DescriptionCoveragePanel 提交任务成功后
 * 调 notifyBatchInferActivity() 唤醒任务中心：立即刷新一次并（按需）恢复轮询。
 */
type ActivityListener = () => void;
const activityListeners = new Set<ActivityListener>();

/** 通知任务中心：有新任务活动（如提交批量推断），应立即刷新并恢复轮询。 */
export function notifyBatchInferActivity(): void {
  activityListeners.forEach((fn) => fn());
}

const STATUS_TAG: Record<string, { color: string; label: string }> = {
  pending: { color: "default", label: "排队中" },
  running: { color: "processing", label: "推断中" },
  done: { color: "success", label: "完成" },
  error: { color: "error", label: "失败" },
  cancelled: { color: "warning", label: "已取消" },
};

function taskDoneCount(t: BatchInferTask): number {
  return t.progress.filter((p) => p.status === "done").length;
}
function taskErrCount(t: BatchInferTask): number {
  return t.progress.filter((p) => p.status === "error").length;
}
function isFinished(t: BatchInferTask): boolean {
  return !RUNNING.has(t.status);
}

/** 单任务进度条（含逐表状态 Tag）。 */
function TaskProgressCard({
  task,
  onCancel,
  cancelling,
}: {
  task: BatchInferTask;
  onCancel: (id: number) => void;
  cancelling: boolean;
}) {
  const done = taskDoneCount(task);
  const err = taskErrCount(task);
  const running = RUNNING.has(task.status);
  // 取消已请求但 worker 尚未收尾（协作取消：当前表跑完后才翻终态）→ 展示「停止中」
  const stopPending = running && task.cancel_requested;
  const percent =
    task.total > 0 ? Math.round(((done + err + task.cancelled) / task.total) * 100) : 0;
  const columns: ColumnsType<BatchInferTaskProgressItem> = [
    {
      title: "表",
      dataIndex: "entity_name",
      width: 220,
      render: (v: string, r) => (
        // 表名可能为无空格的英文长串，禁止溢出覆盖相邻列：完整换行展示（不截断）
        <div style={{ wordBreak: "break-word" }}>
          {v}
          {r.catalog_id != null && (
            <span className="muted" style={{ marginLeft: 4 }}>
              #{r.catalog_id}
            </span>
          )}
        </div>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (s: string) => {
        const cfg = STATUS_TAG[s] ?? { color: "default", label: s };
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    {
      title: "结果",
      dataIndex: "summary",
      // 结果为「；」连接的多段文本，同样完整换行展示，不用 ellipsis 截断
      render: (v: string) =>
        v ? (
          <div style={{ wordBreak: "break-word" }}>{v}</div>
        ) : (
          <span className="muted">-</span>
        ),
    },
  ];
  return (
    <div style={{ marginBottom: 16 }}>
      <Space style={{ width: "100%", justifyContent: "space-between" }}>
        <Space wrap>
          <FireOutlined style={{ color: "#fa541c" }} />
          <strong>批量推断 #{task.id}（共 {task.total} 张表）</strong>
          <Tag color={running ? (stopPending ? "orange" : "blue") : "default"}>
            {stopPending ? "停止中（当前表结束后取消）" : running ? "进行中" : "已结束"}
          </Tag>
          {task.finished_at && (
            <span className="muted" style={{ fontSize: 12 }}>
              {formatCnTime(task.finished_at)}
            </span>
          )}
        </Space>
        {stopPending && (
          <Button size="small" disabled icon={<StopOutlined />}>
            已请求取消
          </Button>
        )}
        {running && !stopPending && (
          <Button
            size="small"
            danger
            icon={<StopOutlined />}
            loading={cancelling}
            onClick={() => onCancel(task.id)}
          >
            取消任务
          </Button>
        )}
      </Space>
      <Progress
        percent={percent}
        size="small"
        status={isFinished(task) ? (err > 0 ? "exception" : "success") : "active"}
        format={() => `完成 ${done}/${task.total}`}
      />
      {task.error && <div className="muted" style={{ color: "#cf1322" }}>{task.error}</div>}
      <Table<BatchInferTaskProgressItem>
        size="small"
        rowKey={(r) => String(r.catalog_id)}
        columns={columns}
        dataSource={task.progress}
        pagination={false}
        tableLayout="fixed"
        scroll={{ y: 260 }}
      />
    </div>
  );
}

/** 右下角批量任务中心（Layout 挂载一次，全局可见）。 */
export function BatchInferCenter() {
  const [tasks, setTasks] = useState<BatchInferTask[]>([]);
  const [open, setOpen] = useState(false);
  const [cancellingId, setCancellingId] = useState<number | null>(null);
  // 刚进入终态的任务摘要（用于浮条短暂展示「本次批量推断结果」，避免任务完成时无声消失）
  const [finishedNotice, setFinishedNotice] = useState<BatchInferTask | null>(null);
  const mounted = useRef(true);
  const timerRef = useRef<number | null>(null);
  // 上一轮已知的各任务 status（识别「上轮 running/pending → 本轮终态」的任务 → 弹完成摘要）
  const lastStatusRef = useRef<Map<number, string>>(new Map());

  const stopPolling = useCallback(() => {
    if (timerRef.current != null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const rows = await listBatchInferTasks(30);
      if (!mounted.current) return;
      setTasks(rows);
      // 终态摘要：识别「上一轮 running/pending → 本轮已终态」的任务，弹完成摘要浮条
      // （避免任务很短时浮条一闪而过、用户不知本次批量推断结果）
      const last = lastStatusRef.current;
      const newlyFinished = rows.find(
        (t) => last.get(t.id) != null && RUNNING.has(last.get(t.id) as string) && isFinished(t),
      );
      if (newlyFinished) setFinishedNotice(newlyFinished);
      last.clear();
      rows.forEach((t) => last.set(t.id, t.status));
      // 状态机：有运行中任务 → 保持/恢复 4s 轮询；无任务 → 停止轮询（零请求）
      const hasRunning = rows.some((t) => RUNNING.has(t.status));
      if (hasRunning) {
        if (timerRef.current == null) {
          timerRef.current = window.setInterval(() => void refresh(), POLL_MS);
        }
      } else {
        stopPolling();
      }
    } catch {
      // 轮询失败静默（网络抖动/后端不可用时不打扰）
    }
  }, [stopPolling]);

  // 完成摘要浮条停留 FINISHED_NOTICE_MS 后自动消失
  useEffect(() => {
    if (!finishedNotice) return;
    const timer = window.setTimeout(() => {
      if (mounted.current) setFinishedNotice(null);
    }, FINISHED_NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [finishedNotice]);

  useEffect(() => {
    mounted.current = true;
    void refresh(); // 挂载探测一次：有进行中任务则恢复轮询，无任务保持零请求
    const onActivity = () => void refresh(); // 提交任务等事件 → 立即刷新（内部按需恢复轮询）
    activityListeners.add(onActivity);
    return () => {
      mounted.current = false;
      stopPolling();
      activityListeners.delete(onActivity);
    };
  }, [refresh, stopPolling]);

  const running = tasks.filter((t) => RUNNING.has(t.status));
  const finished = tasks.filter(isFinished).slice(0, 3);
  const stopping = running.some((t) => t.cancel_requested);
  // 无运行中任务但存在「刚完成」的摘要 → 展示完成浮条（有运行中任务时优先展示进行中浮条）
  const showDoneNotice = running.length === 0 && finishedNotice != null;
  const barStyle: CSSProperties = {
    position: "fixed",
    right: 24,
    bottom: 24,
    zIndex: 1000,
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "8px 14px",
    background: "#fff",
    borderRadius: 24,
    boxShadow: "0 4px 14px rgba(0,0,0,0.18)",
    cursor: "pointer",
  };

  async function handleCancel(id: number) {
    setCancellingId(id);
    try {
      const t = await cancelBatchInferTask(id);
      message.info(`已请求取消任务 #${id}`);
      setTasks((prev) => prev.map((x) => (x.id === id ? t : x)));
    } catch (err) {
      message.error(err instanceof Error ? err.message : "取消失败");
    } finally {
      setCancellingId(null);
    }
  }

  return (
    <>
      {running.length > 0 && (
        <div
          style={barStyle}
          onClick={() => setOpen(true)}
          data-testid="batch-infer-center-bar"
        >
          <CheckCircleOutlined style={{ color: "#fa541c" }} />
          <span>
            {stopping ? "批量推断停止中" : "批量推断进行中"}{" "}
            <strong>
              {running.reduce((s, t) => s + taskDoneCount(t) + taskErrCount(t), 0)}/
              {running.reduce((s, t) => s + t.total, 0)}
            </strong>
          </span>
          <span className="muted" style={{ fontSize: 12 }}>
            （点击查看）
          </span>
        </div>
      )}
      {showDoneNotice && finishedNotice && (
        <div
          style={barStyle}
          onClick={() => setOpen(true)}
          data-testid="batch-infer-done-bar"
        >
          <CheckCircleOutlined style={{ color: "#52c41a" }} />
          <span>
            批量推断 #{finishedNotice.id} 完成：
            <strong>成功 {finishedNotice.done} 表</strong>
            {finishedNotice.failed > 0 && (
              <span style={{ color: "#cf1322" }}> · 失败 {finishedNotice.failed} 表</span>
            )}
            {finishedNotice.cancelled > 0 && (
              <span> · 取消 {finishedNotice.cancelled} 表</span>
            )}
            {finishedNotice.added_total > 0 && (
              <span style={{ color: "#389e0d" }}> · 新增 {finishedNotice.added_total} 处</span>
            )}
          </span>
          <span className="muted" style={{ fontSize: 12 }}>
            （点击查看）
          </span>
        </div>
      )}
      <Drawer
        title="批量 LLM 推断任务中心"
        width={760}
        open={open}
        onClose={() => setOpen(false)}
        extra={
          <Button size="small" onClick={() => refresh()}>
            刷新
          </Button>
        }
      >
        {running.length === 0 && finished.length === 0 && (
          <div className="muted">暂无批量推断任务。在「描述缺失治理」勾选表后点击「批量推断」提交任务。</div>
        )}
        {running.map((t) => (
          <TaskProgressCard
            key={t.id}
            task={t}
            onCancel={handleCancel}
            cancelling={cancellingId === t.id}
          />
        ))}
        {running.length > 0 && finished.length > 0 && (
          <div style={{ margin: "12px 0 4px", fontWeight: 600 }}>最近完成</div>
        )}
        {finished.map((t) => (
          <TaskProgressCard
            key={t.id}
            task={t}
            onCancel={handleCancel}
            cancelling={false}
          />
        ))}
      </Drawer>
    </>
  );
}
