/**
 * 全局 LLM 任务中心（右下角浮条 + Drawer，跨页面可见进度/结果/可取消）。
 *
 * 双源聚合（方案 A：采集批量推断 + dp 待抉择单 LLM 重试统一体验）：
 * - 采集批量推断（kind=catalog）：描述缺失治理「批量推断所选表」，batch_llm_infer_task
 * - dp 待抉择 LLM 重试（kind=dp）：dp 血缘同步「LLM 重试」，dp_ticket_retry_task
 *
 * 两源共用同一交互：有进行中任务时右下角浮条（任意页面可见，切换/刷新后恢复）；
 * 点击展开 Drawer 查看逐项进度、取消任务、最近完成；任务进入终态弹「完成摘要」
 * 停留数秒（避免跑完无声消失）。状态机轮询：有 running 任务才 4s 轮询，空闲零请求
 * （提交入口经 notifyBatchInferActivity 事件唤醒）。
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
  cancelDpRetryTask,
  listBatchInferTasks,
  listDpRetryTasks,
  type BatchInferTask,
  type BatchInferTaskProgressItem,
  type DpTicketRetryTask,
  type DpTicketRetryTaskProgressItem,
} from "../api";
import { formatCnTime } from "../utils/timeCn";

const RUNNING = new Set(["pending", "running"]);
const POLL_MS = 4000;
/** 任务进入终态后，完成摘要浮条的停留时长（ms）。 */
const FINISHED_NOTICE_MS = 10_000;

/**
 * 模块级 LLM 任务「活动事件」总线（pub/sub）。
 *
 * 任务中心按状态机轮询（有运行中任务才 4s 轮询，无任务零请求）——零请求意味着
 * 它无法自己感知「别的入口刚提交了任务」。DescriptionCoveragePanel / LineageDpSync
 * 提交任务成功后调 notifyBatchInferActivity() 唤醒任务中心：立即刷新并恢复轮询。
 */
type ActivityListener = (taskId?: number, kind?: "cat" | "dp") => void;
const activityListeners = new Set<ActivityListener>();

/**
 * 通知任务中心：有新任务活动（提交批量推断 / dp LLM 重试），应立即刷新并恢复轮询。
 * @param taskId 刚提交的任务 id（可选）——任务可能极快进入终态（LLM 不可用秒级失败），
 *   带上 id 让任务中心识别「刚提交的任务」：即使首次刷新就已终态也弹完成摘要。
 */
export function notifyBatchInferActivity(
  taskId?: number,
  kind: "cat" | "dp" = "cat",
): void {
  activityListeners.forEach((fn) => fn(taskId, kind));
}

const STATUS_TAG: Record<string, { color: string; label: string }> = {
  pending: { color: "default", label: "排队中" },
  running: { color: "processing", label: "处理中" },
  done: { color: "success", label: "完成" },
  error: { color: "error", label: "失败" },
  cancelled: { color: "warning", label: "已取消" },
};

function isRunningStatus(t: { status: string }): boolean {
  return RUNNING.has(t.status);
}
function isFinishedStatus(t: { status: string }): boolean {
  return !RUNNING.has(t.status);
}

// ---------- 采集批量推断卡片 ----------

function taskDoneCount(t: BatchInferTask): number {
  return t.progress.filter((p) => p.status === "done").length;
}
function taskErrCount(t: BatchInferTask): number {
  return t.progress.filter((p) => p.status === "error").length;
}

/** 采集批量推断单任务卡片（逐表进度 + 取消 + 停止中态）。 */
function BatchTaskCard({
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
  const running = isRunningStatus(task);
  // 取消已请求但 worker 尚未收尾（协作取消：当前表跑完后才翻终态）→ 展示「停止中」
  const stopPending = running && task.cancel_requested;
  const percent =
    task.total > 0
      ? Math.round(((done + err + task.cancelled) / task.total) * 100)
      : 0;
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
        status={!running ? (err > 0 ? "exception" : "success") : "active"}
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

// ---------- dp 待抉择单 LLM 重试卡片 ----------

function dpRetryProgress(t: DpTicketRetryTask) {
  return {
    done: t.progress.filter((p) => p.status === "done").length,
    err: t.progress.filter((p) => p.status === "error").length,
  };
}

/** dp 待抉择 LLM 重试单任务卡片（逐张单进度 + 终态语义计数）。 */
function DpRetryTaskCard({
  task,
  onCancel,
  cancelling,
}: {
  task: DpTicketRetryTask;
  onCancel: (id: number) => void;
  cancelling: boolean;
}) {
  const { done, err } = dpRetryProgress(task);
  const running = isRunningStatus(task);
  const stopPending = running && task.cancel_requested;
  const percent =
    task.total > 0
      ? Math.round(((done + err + task.cancelled) / task.total) * 100)
      : 0;
  const columns: ColumnsType<DpTicketRetryTaskProgressItem> = [
    {
      title: "待抉择单（产出表）",
      dataIndex: "out_table",
      width: 240,
      render: (v: string, r) => (
        <div style={{ wordBreak: "break-word" }}>
          {v || r.task_name || "-"}
          {r.ticket_id != null && (
            <span className="muted" style={{ marginLeft: 4 }}>
              #{r.ticket_id}
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
          <CheckCircleOutlined style={{ color: "#722ed1" }} />
          <strong>LLM 重试 #{task.id}（共 {task.total} 张单）</strong>
          <Tag color={running ? (stopPending ? "orange" : "blue") : "default"}>
            {stopPending ? "停止中（当前单结束后取消）" : running ? "进行中" : "已结束"}
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
        status={!running ? (err > 0 ? "exception" : "success") : "active"}
        format={() => `完成 ${done}/${task.total}`}
      />
      {!running && (task.counts.auto_resolved > 0 || task.counts.refreshed > 0) && (
        <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>
          自动采纳 {task.counts.auto_resolved} · 刷新意见 {task.counts.refreshed} · 保留{" "}
          {task.counts.kept} · 失败 {task.counts.failed}
        </div>
      )}
      {task.error && <div className="muted" style={{ color: "#cf1322" }}>{task.error}</div>}
      <Table<DpTicketRetryTaskProgressItem>
        size="small"
        rowKey={(r) => String(r.ticket_id)}
        columns={columns}
        dataSource={task.progress}
        pagination={false}
        tableLayout="fixed"
        scroll={{ y: 260 }}
      />
    </div>
  );
}

/** 右下角全局 LLM 任务中心（Layout 挂载一次，全局可见；采集批量 + dp 重试双源）。 */
export function BatchInferCenter() {
  const [tasks, setTasks] = useState<BatchInferTask[]>([]);
  const [dpTasks, setDpTasks] = useState<DpTicketRetryTask[]>([]);
  const [open, setOpen] = useState(false);
  const [cancellingId, setCancellingId] = useState<number | null>(null);
  // 刚进入终态的任务摘要（浮条短暂展示本次结果，避免完成时无声消失）
  const [finishedNotice, setFinishedNotice] = useState<BatchInferTask | null>(null);
  const [dpFinishedNotice, setDpFinishedNotice] = useState<DpTicketRetryTask | null>(null);
  const mounted = useRef(true);
  const timerRef = useRef<number | null>(null);
  // 上一轮已知的各任务 status（识别「上轮 running/pending → 本轮终态」的任务 → 弹完成摘要）
  const lastStatusRef = useRef<Map<string, string>>(new Map());
  // 本次会话「刚提交」的任务 id（事件唤醒时登记，key 含源前缀防两源撞号）
  const pendingSubmitRef = useRef<Set<string>>(new Set());

  const stopPolling = useCallback(() => {
    if (timerRef.current != null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const refresh = useCallback(async () => {
    const catalogRows: BatchInferTask[] = [];
    const dpRows: DpTicketRetryTask[] = [];
    // 两源独立容错：一个源失败不影响另一源展示
    try {
      catalogRows.push(...(await listBatchInferTasks(30)));
    } catch {
      // 轮询失败静默（网络抖动/后端不可用时不打扰）
    }
    try {
      dpRows.push(...(await listDpRetryTasks(30)));
    } catch {
      // 同上
    }
    if (!mounted.current) return;
    setTasks(catalogRows);
    setDpTasks(dpRows);

    // 完成摘要：识别「上轮 running/pending → 本轮终态」或「刚提交且本轮已终态」的任务
    const last = lastStatusRef.current;
    const key = (kind: string, id: number) => `${kind}:${id}`;
    let catalogFinished: BatchInferTask | undefined;
    for (const t of catalogRows) {
      if (!isFinishedStatus(t)) continue;
      const prev = last.get(key("cat", t.id));
      const justSubmitted = pendingSubmitRef.current.delete(key("cat", t.id));
      if ((prev != null && RUNNING.has(prev)) || justSubmitted) {
        catalogFinished = t;
        break;
      }
    }
    let dpFinished: DpTicketRetryTask | undefined;
    for (const t of dpRows) {
      if (!isFinishedStatus(t)) continue;
      const prev = last.get(key("dp", t.id));
      const justSubmitted = pendingSubmitRef.current.delete(key("dp", t.id));
      if ((prev != null && RUNNING.has(prev)) || justSubmitted) {
        dpFinished = t;
        break;
      }
    }
    if (catalogFinished) setFinishedNotice(catalogFinished);
    if (dpFinished) setDpFinishedNotice(dpFinished);

    last.clear();
    catalogRows.forEach((t) => last.set(key("cat", t.id), t.status));
    dpRows.forEach((t) => last.set(key("dp", t.id), t.status));

    // 状态机：任一源有运行中任务 → 保持/恢复 4s 轮询；全无 → 停止轮询（零请求）
    const hasRunning =
      catalogRows.some((t) => isRunningStatus(t)) || dpRows.some((t) => isRunningStatus(t));
    if (hasRunning) {
      if (timerRef.current == null) {
        timerRef.current = window.setInterval(() => void refresh(), POLL_MS);
      }
    } else {
      stopPolling();
    }
  }, [stopPolling]);

  // 完成摘要浮条停留 FINISHED_NOTICE_MS 后自动消失
  useEffect(() => {
    if (!finishedNotice && !dpFinishedNotice) return;
    const timer = window.setTimeout(() => {
      if (mounted.current) {
        setFinishedNotice(null);
        setDpFinishedNotice(null);
      }
    }, FINISHED_NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [finishedNotice, dpFinishedNotice]);

  useEffect(() => {
    mounted.current = true;
    void refresh(); // 挂载探测一次：有进行中任务则恢复轮询，无任务保持零请求
    const onActivity = (taskId?: number, kind: "cat" | "dp" = "cat") => {
      if (taskId != null) pendingSubmitRef.current.add(`${kind}:${taskId}`);
      void refresh(); // 立即刷新（内部按需恢复轮询/弹摘要）
    };
    activityListeners.add(onActivity);
    return () => {
      mounted.current = false;
      stopPolling();
      activityListeners.delete(onActivity);
    };
  }, [refresh, stopPolling]);

  const running = tasks.filter((t) => isRunningStatus(t));
  const finished = tasks.filter((t) => isFinishedStatus(t)).slice(0, 3);
  const dpRunning = dpTasks.filter((t) => isRunningStatus(t));
  const dpFinished = dpTasks.filter((t) => isFinishedStatus(t)).slice(0, 3);
  const stopping = running.some((t) => t.cancel_requested) || dpRunning.some((t) => t.cancel_requested);
  const hasRunningAny = running.length + dpRunning.length > 0;
  // 无运行中任务但存在「刚完成」摘要 → 展示完成浮条（有运行中任务时优先进行中浮条）
  const showDoneNotice = !hasRunningAny && (finishedNotice != null || dpFinishedNotice != null);
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
    // 取消时按源分派：优先匹配 dp（两源 id 可能撞号，cancel 用精确源）
    const isDp = dpRunning.some((t) => t.id === id);
    try {
      if (isDp) {
        const t = await cancelDpRetryTask(id);
        message.info(`已请求取消 dp 重试任务 #${id}`);
        setDpTasks((prev) => prev.map((x) => (x.id === id ? t : x)));
      } else {
        const t = await cancelBatchInferTask(id);
        message.info(`已请求取消任务 #${id}`);
        setTasks((prev) => prev.map((x) => (x.id === id ? t : x)));
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : "取消失败");
    } finally {
      setCancellingId(null);
    }
  }

  const catProcessed = running.reduce((s, t) => s + taskDoneCount(t) + taskErrCount(t), 0);
  const catTotalCount = running.reduce((s, t) => s + t.total, 0);
  const dpProcessed = dpRunning.reduce(
    (s, t) => s + dpRetryProgress(t).done + dpRetryProgress(t).err,
    0,
  );
  const dpTotalCount = dpRunning.reduce((s, t) => s + t.total, 0);

  const catLive = running.length > 0;
  const dpLive = dpRunning.length > 0;
  const barKindText = catLive && dpLive ? "LLM 任务" : catLive ? "批量推断" : "LLM 重试";

  return (
    <>
      {hasRunningAny && (
        <div
          style={barStyle}
          onClick={() => setOpen(true)}
          data-testid="batch-infer-center-bar"
        >
          <CheckCircleOutlined style={{ color: "#fa541c" }} />
          <span>{`${barKindText} ${stopping ? "停止中" : "进行中"}`}{" "}
            <strong>
              {catProcessed + dpProcessed}/{catTotalCount + dpTotalCount}
            </strong>
          </span>
          <span className="muted" style={{ fontSize: 12 }}>
            （点击查看）
          </span>
        </div>
      )}
      {showDoneNotice && (
        <div
          style={barStyle}
          onClick={() => setOpen(true)}
          data-testid="batch-infer-done-bar"
        >
          <CheckCircleOutlined style={{ color: "#52c41a" }} />
          {finishedNotice && (
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
          )}
          {dpFinishedNotice && (
            <span>
              LLM 重试 #{dpFinishedNotice.id} 完成：
              <strong style={{ color: "#389e0d" }}>
                自动采纳 {dpFinishedNotice.counts.auto_resolved}
              </strong>
              {dpFinishedNotice.counts.refreshed > 0 && (
                <span> · 刷新意见 {dpFinishedNotice.counts.refreshed}</span>
              )}
              {dpFinishedNotice.counts.kept > 0 && (
                <span> · 保留 {dpFinishedNotice.counts.kept}</span>
              )}
              {dpFinishedNotice.counts.failed > 0 && (
                <span style={{ color: "#cf1322" }}>
                  {" "}
                  · 失败 {dpFinishedNotice.counts.failed}
                </span>
              )}
            </span>
          )}
          <span className="muted" style={{ fontSize: 12 }}>
            （点击查看）
          </span>
        </div>
      )}
      <Drawer
        title="LLM 任务中心"
        width={760}
        open={open}
        onClose={() => setOpen(false)}
        extra={
          <Button size="small" onClick={() => refresh()}>
            刷新
          </Button>
        }
      >
        {!hasRunningAny && finished.length === 0 && dpFinished.length === 0 && (
          <div className="muted">
            暂无 LLM 任务。在「描述缺失治理」勾选表后点击「批量推断」，或在 dp 血缘同步「待抉择」
            中点击「LLM 重试」提交任务。
          </div>
        )}
        {running.length > 0 && (
          <div style={{ margin: "4px 0", fontWeight: 600 }}>批量推断（进行中）</div>
        )}
        {running.map((t) => (
          <BatchTaskCard
            key={`cat-${t.id}`}
            task={t}
            onCancel={handleCancel}
            cancelling={cancellingId === t.id}
          />
        ))}
        {dpRunning.length > 0 && (
          <div style={{ margin: "4px 0", fontWeight: 600 }}>dp LLM 重试（进行中）</div>
        )}
        {dpRunning.map((t) => (
          <DpRetryTaskCard
            key={`dp-${t.id}`}
            task={t}
            onCancel={handleCancel}
            cancelling={cancellingId === t.id}
          />
        ))}
        {(running.length > 0 || dpRunning.length > 0) &&
          (finished.length > 0 || dpFinished.length > 0) && (
            <div style={{ margin: "12px 0 4px", fontWeight: 600 }}>最近完成</div>
          )}
        {finished.map((t) => (
          <BatchTaskCard
            key={`catf-${t.id}`}
            task={t}
            onCancel={handleCancel}
            cancelling={false}
          />
        ))}
        {dpFinished.map((t) => (
          <DpRetryTaskCard
            key={`dpf-${t.id}`}
            task={t}
            onCancel={handleCancel}
            cancelling={false}
          />
        ))}
      </Drawer>
    </>
  );
}
