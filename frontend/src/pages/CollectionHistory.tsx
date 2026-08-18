import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import type { Dayjs } from "dayjs";
import { listDataSources, listDriftLogs, listCollectionRuns, getCollectionRunDetail } from "../api";
import type { DataSource, CollectionRun } from "../types";
import type { DriftLogItem } from "../api";
import { formatCnTime } from "../utils/timeCn";

const CHANGE_TYPE_LABEL: Record<string, { label: string; color: string }> = {
  ADD_COLUMN: { label: "新增列", color: "green" },
  DROP_COLUMN: { label: "删除列", color: "red" },
  TYPE_CHANGE: { label: "类型变更", color: "orange" },
  SCHEMA_CHANGED: { label: "Schema 变更", color: "gold" },
};

const RUN_STATUS_LABEL: Record<string, { label: string; color: string }> = {
  RUNNING: { label: "运行中", color: "processing" },
  COMPLETED: { label: "成功", color: "success" },
  FAILED: { label: "失败", color: "error" },
};

const TRIGGER_LABEL: Record<string, { label: string; color: string }> = {
  manual: { label: "手动", color: "default" },
  scheduled: { label: "定时", color: "blue" },
};

function changeTag(v: string) {
  const meta = CHANGE_TYPE_LABEL[v];
  return <Tag color={meta?.color ?? "default"}>{meta?.label ?? v}</Tag>;
}

function runStatusTag(v: string) {
  const meta = RUN_STATUS_LABEL[v];
  return <Tag color={meta?.color ?? "default"}>{meta?.label ?? v}</Tag>;
}

function triggerTag(v: string) {
  const meta = TRIGGER_LABEL[v];
  return <Tag color={meta?.color ?? "default"}>{meta?.label ?? v}</Tag>;
}

function diffTag(label: string, color: string) {
  return <Tag color={color} style={{ marginInlineEnd: 4 }}>{label}</Tag>;
}

/** 渲染 diff_json 明细：新增/删除/类型变更列。 */
function diffDetail(diff: Record<string, unknown> | null | undefined) {
  if (!diff) return <span className="muted">—</span>;
  const added = Array.isArray(diff.added) ? (diff.added as string[]) : [];
  const removed = Array.isArray(diff.removed) ? (diff.removed as string[]) : [];
  const changed = Array.isArray(diff.changed) ? (diff.changed as Array<Record<string, unknown>>) : [];
  if (added.length === 0 && removed.length === 0 && changed.length === 0) {
    return <span className="muted">—</span>;
  }
  return (
    <div style={{ lineHeight: 2 }}>
      {added.map((n) => diffTag(`+${n}`, "green"))}
      {removed.map((n) => diffTag(`-${n}`, "red"))}
      {changed.map((c, i) => {
        const name = String(c?.name ?? "");
        const after = c?.after as Record<string, unknown> | undefined;
        const afterType = after?.type ? String(after.type) : "";
        return <span key={i}>{diffTag(`~${name}${afterType ? ` (${afterType})` : ""}`, "orange")}</span>;
      })}
    </div>
  );
}

/** 耗时格式化：>60s 显示分秒，否则秒。 */
function durationText(seconds?: number | null) {
  if (seconds == null) return <span className="muted">—</span>;
  if (seconds >= 60) {
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return <span className="mono" style={{ fontSize: 12 }}>{m}分{s}秒</span>;
  }
  return <span className="mono" style={{ fontSize: 12 }}>{seconds}s</span>;
}

/** 模式展示：增量降级为全量时标注实际执行模式。 */
function modeText(run: CollectionRun) {
  if (run.effective_mode && run.effective_mode !== run.mode) {
    return (
      <Tooltip title={`请求 ${run.mode}，实际执行 ${run.effective_mode}（增量降级）`}>
        <span className="mono" style={{ fontSize: 12 }}>
          {run.mode}→{run.effective_mode}
        </span>
      </Tooltip>
    );
  }
  return <span className="mono" style={{ fontSize: 12 }}>{run.mode}</span>;
}

export function CollectionHistory() {
  // ---- 采集记录 tab 状态 ----
  const [sources, setSources] = useState<DataSource[]>([]);
  const [srcLoading, setSrcLoading] = useState(false);
  const [runs, setRuns] = useState<CollectionRun[]>([]);
  const [runsTotal, setRunsTotal] = useState(0);
  const [runPage, setRunPage] = useState(1);
  const [runPageSize, setRunPageSize] = useState(10);
  const [runSourceId, setRunSourceId] = useState<string>("");
  const [runStatus, setRunStatus] = useState<string>("");
  const [runTrigger, setRunTrigger] = useState<string>("");
  const [runTimeRange, setRunTimeRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [runsLoading, setRunsLoading] = useState(false);
  const [runsError, setRunsError] = useState(false);
  // 统计摘要（近 200 次，随过滤联动）
  const [summary, setSummary] = useState({ total: 0, completed: 0, failed: 0, scanned: 0, registered: 0 });
  // 详情抽屉
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailRun, setDetailRun] = useState<CollectionRun | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  // ---- 变更追踪 tab 状态 ----
  const [driftSourceId, setDriftSourceId] = useState<string>("");
  const [driftEntity, setDriftEntity] = useState<string>("");
  const [driftInput, setDriftInput] = useState<string>("");
  const [driftLogs, setDriftLogs] = useState<DriftLogItem[]>([]);
  const [driftTotal, setDriftTotal] = useState(0);
  const [driftPage, setDriftPage] = useState(1);
  const [driftPageSize, setDriftPageSize] = useState(10);
  const [driftLoading, setDriftLoading] = useState(false);
  const [driftError, setDriftError] = useState(false);

  // 加载数据源列表（两 tab 共用）
  const loadSources = useCallback(async () => {
    setSrcLoading(true);
    try {
      const res = await listDataSources({ page_size: 100 });
      setSources(res.items);
    } catch {
      /* 数据源加载失败不影响主体 */
    } finally {
      setSrcLoading(false);
    }
  }, []);

  // 加载采集运行历史（表格 + 统计摘要）
  const loadRuns = useCallback(async () => {
    setRunsLoading(true);
    setRunsError(false);
    try {
      const timeParams = runTimeRange
        ? {
            started_after: runTimeRange[0].startOf("day").toISOString(),
            started_before: runTimeRange[1].endOf("day").toISOString(),
          }
        : {};
      const [pageRes, summaryRes] = await Promise.all([
        listCollectionRuns({
          source_id: runSourceId || undefined,
          status: runStatus || undefined,
          trigger: runTrigger || undefined,
          ...timeParams,
          page: runPage,
          page_size: runPageSize,
        }),
        listCollectionRuns({
          source_id: runSourceId || undefined,
          status: runStatus || undefined,
          trigger: runTrigger || undefined,
          ...timeParams,
          page: 1,
          page_size: 200,
        }),
      ]);
      setRuns(pageRes.items);
      setRunsTotal(pageRes.total);
      const agg = summaryRes.items;
      setSummary({
        total: summaryRes.total,
        completed: agg.filter((r) => r.status === "COMPLETED").length,
        failed: agg.filter((r) => r.status === "FAILED").length,
        scanned: agg.reduce((acc, r) => acc + (r.scanned || 0), 0),
        registered: agg.reduce((acc, r) => acc + (r.registered || 0), 0),
      });
    } catch {
      setRuns([]);
      setRunsTotal(0);
      setRunsError(true);
    } finally {
      setRunsLoading(false);
    }
  }, [runSourceId, runStatus, runTrigger, runPage, runPageSize, runTimeRange]);

  // 加载漂移日志
  const loadDrift = useCallback(async () => {
    if (!driftSourceId) return;
    setDriftLoading(true);
    setDriftError(false);
    try {
      const res = await listDriftLogs(driftSourceId, {
        entity_name: driftEntity || undefined,
        page: driftPage,
        page_size: driftPageSize,
      });
      setDriftLogs(res.items);
      setDriftTotal(res.total);
    } catch {
      setDriftLogs([]);
      setDriftTotal(0);
      setDriftError(true);
    } finally {
      setDriftLoading(false);
    }
  }, [driftSourceId, driftEntity, driftPage, driftPageSize]);

  // drift 实体名输入防抖（300ms）：driftInput 即时更新，driftEntity 延迟写入查询值
  const driftInputTimer = useRef<number | null>(null);
  const handleDriftInputChange = useCallback((value: string) => {
    setDriftInput(value);
    if (driftInputTimer.current !== null) window.clearTimeout(driftInputTimer.current);
    driftInputTimer.current = window.setTimeout(() => {
      setDriftEntity(value);
      setDriftPage(1);
    }, 300);
  }, []);
  // 组件卸载时清理防抖定时器
  useEffect(() => () => {
    if (driftInputTimer.current !== null) window.clearTimeout(driftInputTimer.current);
  }, []);

  useEffect(() => { loadSources(); }, [loadSources]);
  useEffect(() => { loadRuns(); }, [loadRuns]);
  useEffect(() => { loadDrift(); }, [loadDrift]);

  /** 打开运行详情抽屉：拉取完整明细（failed_specs / drift_events）。 */
  async function openRunDetail(run: CollectionRun) {
    setDetailRun(run);
    setDetailOpen(true);
    setDetailLoading(true);
    try {
      const fresh = await getCollectionRunDetail(run.id);
      if (fresh) setDetailRun(fresh);
    } catch {
      /* 详情刷新失败保留列表行数据 */
    } finally {
      setDetailLoading(false);
    }
  }

  const runColumns = [
    {
      title: "开始时间",
      dataIndex: "started_at",
      key: "startedAt",
      width: 170,
      render: (v?: string | null) =>
        v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span className="muted">—</span>,
    },
    {
      title: "数据源",
      dataIndex: "source_id",
      key: "source",
      ellipsis: true,
      render: (v: string, r: CollectionRun) => (
        <Tooltip title={v}>
          <span className="mono" style={{ fontSize: 12 }}>{r.source_name ?? v}</span>
        </Tooltip>
      ),
    },
    { title: "触发", dataIndex: "trigger", key: "trigger", width: 70, render: (v: string) => triggerTag(v) },
    { title: "模式", dataIndex: "mode", key: "mode", width: 110, render: (_: string, r: CollectionRun) => modeText(r) },
    { title: "状态", dataIndex: "status", key: "status", width: 80, render: (v: string) => runStatusTag(v) },
    { title: "扫描", dataIndex: "scanned", key: "scanned", width: 64, align: "right" as const, render: (v: number) => v ?? 0 },
    { title: "注册", dataIndex: "registered", key: "registered", width: 64, align: "right" as const, render: (v: number) => v ?? 0 },
    { title: "PII", dataIndex: "pii_registered", key: "pii", width: 56, align: "right" as const, render: (v: number) => (v ? <Tag color="red">{v}</Tag> : v ?? 0) },
    {
      title: "失败",
      dataIndex: "failed_count",
      key: "failed",
      width: 56,
      align: "right" as const,
      render: (v: number) => (v ? <span style={{ color: "#cf1322" }}>{v}</span> : v ?? 0),
    },
    { title: "漂移", dataIndex: "drift_count", key: "drift", width: 56, align: "right" as const, render: (v: number) => (v ? <Tag color="gold">{v}</Tag> : v ?? 0) },
    { title: "废弃", dataIndex: "deprecated_count", key: "deprecated", width: 56, align: "right" as const, render: (v: number) => v ?? 0 },
    { title: "下线指标", dataIndex: "dsd_count", key: "dsd", width: 72, align: "right" as const, render: (v?: number) => (v ? <Tag color="orange">{v}</Tag> : v ?? 0) },
    { title: "耗时", dataIndex: "duration_seconds", key: "duration", width: 84, render: (v?: number | null) => durationText(v) },
    {
      title: "操作",
      key: "action",
      width: 70,
      render: (_: unknown, r: CollectionRun) => (
        <Button type="link" size="small" onClick={() => openRunDetail(r)}>详情</Button>
      ),
    },
  ];

  const driftColumns = [
    {
      title: "实体",
      dataIndex: "entity_name",
      key: "entity",
      ellipsis: true,
      render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{v}</span>,
    },
    { title: "变更类型", dataIndex: "change_type", key: "changeType", width: 110, render: (v: string) => changeTag(v) },
    {
      title: "变更明细",
      key: "diff",
      width: 260,
      render: (_: unknown, r: DriftLogItem) => diffDetail(r.diff_json),
    },
    {
      title: "签名",
      key: "sig",
      width: 100,
      render: (_: unknown, r: DriftLogItem) => (
        <Tooltip title={`前: ${r.before_signature ?? "—"}\n后: ${r.after_signature ?? "—"}`}>
          <span className="mono" style={{ fontSize: 11, color: "#999" }}>
            {(r.after_signature ?? "").slice(0, 8)}…
          </span>
        </Tooltip>
      ),
    },
    {
      title: "检测时间",
      dataIndex: "detected_at",
      key: "detectedAt",
      width: 170,
      render: (v: string | null) =>
        v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span className="muted">—</span>,
    },
  ];

  const runsTab = (
    <Space direction="vertical" style={{ width: "100%" }} size={12}>
      <Row gutter={12}>
        <Col span={5}>
          <Card size="small">
            <Statistic title="采集次数" value={summary.total} />
            <div className="muted" style={{ fontSize: 12 }}>{summary.total > 200 ? "统计近 200 次" : "含成功与失败"}</div>
          </Card>
        </Col>
        <Col span={5}>
          <Card size="small">
            <Statistic title="成功" value={summary.completed} valueStyle={{ color: "#3f8600" }} />
            <div className="muted" style={{ fontSize: 12 }}>COMPLETED</div>
          </Card>
        </Col>
        <Col span={5}>
          <Card size="small">
            <Statistic title="失败" value={summary.failed} valueStyle={{ color: summary.failed ? "#cf1322" : "#3f8600" }} />
            <div className="muted" style={{ fontSize: 12 }}>FAILED</div>
          </Card>
        </Col>
        <Col span={4}>
          <Card size="small">
            <Statistic title="累计扫描" value={summary.scanned} />
            <div className="muted" style={{ fontSize: 12 }}>实体数</div>
          </Card>
        </Col>
        <Col span={5}>
          <Card size="small">
            <Statistic title="累计注册" value={summary.registered} />
            <div className="muted" style={{ fontSize: 12 }}>实体数</div>
          </Card>
        </Col>
      </Row>
      <Space wrap>
        <Select
          allowClear
          showSearch
          placeholder="全部数据源"
          style={{ width: 220 }}
          value={runSourceId || undefined}
          onChange={(v) => { setRunSourceId(v ?? ""); setRunPage(1); }}
          loading={srcLoading}
          filterOption={(input, opt) => ((opt?.label as string ?? "").toLowerCase().includes(input.toLowerCase()))}
          options={sources.map((s) => ({ value: s.source_id, label: `${s.name} (${s.source_id})` }))}
        />
        <Select
          allowClear
          placeholder="全部状态"
          style={{ width: 120 }}
          value={runStatus || undefined}
          onChange={(v) => { setRunStatus(v ?? ""); setRunPage(1); }}
          options={["RUNNING", "COMPLETED", "FAILED"].map((s) => ({ value: s, label: RUN_STATUS_LABEL[s].label }))}
        />
        <Select
          allowClear
          placeholder="全部触发"
          style={{ width: 110 }}
          value={runTrigger || undefined}
          onChange={(v) => { setRunTrigger(v ?? ""); setRunPage(1); }}
          options={["manual", "scheduled"].map((s) => ({ value: s, label: TRIGGER_LABEL[s].label }))}
        />
        <DatePicker.RangePicker
          placeholder={["开始日期", "结束日期"]}
          value={runTimeRange}
          onChange={(v) => { setRunTimeRange(v as [Dayjs, Dayjs] | null); setRunPage(1); }}
          allowClear
          style={{ width: 240 }}
        />
        <Button icon={<ReloadOutlined />} onClick={loadRuns} loading={runsLoading}>刷新</Button>
      </Space>
      {runsError && (
        <Alert
          type="error"
          showIcon
          message="采集运行记录加载失败"
          description="请检查后端服务是否可用，或点击下方重试按钮重新加载。"
          action={<Button size="small" onClick={loadRuns}>重试</Button>}
        />
      )}
      <Table<CollectionRun>
        rowKey="id"
        loading={runsLoading}
        dataSource={runs}
        columns={runColumns}
        size="middle"
        scroll={{ x: 1100 }}
        pagination={{
          current: runPage,
          pageSize: runPageSize,
          total: runsTotal,
          showSizeChanger: true,
          pageSizeOptions: [10, 20, 50, 100],
          showTotal: (t) => `共 ${t} 条`,
          onChange: (p, ps) => { setRunPage(p); setRunPageSize(ps); },
        }}
        locale={{ emptyText: <Empty description="暂无采集运行记录" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
      />
    </Space>
  );

  const driftTab = (
    <Space direction="vertical" style={{ width: "100%" }} size={12}>
      <Space wrap>
        <Select
          allowClear
          showSearch
          placeholder="选择数据源查看 Schema 变更"
          style={{ width: 240 }}
          value={driftSourceId || undefined}
          onChange={(v) => { setDriftSourceId(v ?? ""); setDriftPage(1); }}
          loading={srcLoading}
          filterOption={(input, opt) => ((opt?.label as string ?? "").toLowerCase().includes(input.toLowerCase()))}
          options={sources.map((s) => ({ value: s.source_id, label: `${s.name} (${s.source_id})` }))}
        />
        <input
          placeholder="按实体名过滤"
          value={driftInput}
          onChange={(e) => handleDriftInputChange(e.target.value)}
          style={{ width: 200, height: 32, padding: "0 11px", border: "1px solid #d9d9d9", borderRadius: 6 }}
        />
        <Button icon={<ReloadOutlined />} onClick={() => { setDriftPage(1); loadDrift(); }} loading={driftLoading}>刷新</Button>
      </Space>
      {driftError && (
        <Alert
          type="error"
          showIcon
          message="Schema 变更记录加载失败"
          description="请检查后端服务是否可用，或点击下方重试按钮重新加载。"
          action={<Button size="small" onClick={() => { setDriftPage(1); loadDrift(); }}>重试</Button>}
        />
      )}
      {!driftSourceId ? (
        <Empty description="请在上方选择数据源，查看其 Schema 变更记录（新增列/删除列/类型变更）" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <Table<DriftLogItem>
          rowKey={(r) => `${r.entity_name}-${r.change_type}-${r.detected_at}`}
          loading={driftLoading}
          dataSource={driftLogs}
          columns={driftColumns}
          size="middle"
          scroll={{ x: 800 }}
          pagination={{
            current: driftPage,
            pageSize: driftPageSize,
            total: driftTotal,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50, 100],
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p, ps) => { setDriftPage(p); setDriftPageSize(ps); },
          }}
          locale={{
            emptyText: (
              <Empty description={`${driftSourceId} 暂无 Schema 变更记录（首次采集或采集后无变更时为空）`} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ),
          }}
        />
      )}
      {driftSourceId && driftTotal === 0 && !driftLoading && (
        <Alert
          type="info"
          showIcon
          message="无变更记录"
          description="该数据源自上次采集以来未发生 Schema 变更。若有变更，会在下一次采集后出现在此处。"
        />
      )}
    </Space>
  );

  const detail = detailRun;
  return (
    <Card
      title={
        <span>
          采集记录
          <span className="page-eyebrow">Collection History · 采集运行历史 + Schema 变更追踪 · GB/T 36073 §6.4</span>
        </span>
      }
    >
      <Tabs
        defaultActiveKey="runs"
        items={[
          { key: "runs", label: "采集记录", children: runsTab },
          { key: "drift", label: "变更追踪", children: driftTab },
        ]}
      />

      <Drawer
        title="采集运行详情"
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width={680}
      >
        {detail && (
          <>
            <Descriptions
              size="small"
              column={2}
              bordered
              style={{ marginBottom: 16 }}
              items={[
                { key: "source", label: "数据源", span: 2, children: <span className="mono">{detail.source_name ?? detail.source_id}</span> },
                { key: "started", label: "开始时间", children: detail.started_at ? formatCnTime(detail.started_at) : "—" },
                { key: "finished", label: "结束时间", children: detail.finished_at ? formatCnTime(detail.finished_at) : "—" },
                { key: "trigger", label: "触发方式", children: triggerTag(detail.trigger) },
                { key: "status", label: "状态", children: runStatusTag(detail.status) },
                { key: "mode", label: "采集模式", children: modeText(detail) },
                { key: "duration", label: "耗时", children: durationText(detail.duration_seconds) },
                { key: "actor", label: "触发人", children: detail.actor_name ?? (detail.actor_id != null ? `#${detail.actor_id}` : <span className="muted">—</span>) },
                { key: "job", label: "任务 ID", span: 2, children: detail.job_id ? <span className="mono">{detail.job_id}</span> : <span className="muted">—</span> },
                { key: "metrics", label: "指标", span: 2, children: (
                  <Space size={12} wrap>
                    <span>扫描 <b>{detail.scanned}</b></span>
                    <span>注册 <b>{detail.registered}</b></span>
                    <span>PII <b>{detail.pii_registered}</b></span>
                    <span>失败 <b style={{ color: "#cf1322" }}>{detail.failed_count}</b></span>
                    <span>漂移 <b>{detail.drift_count}</b></span>
                    <span>废弃 <b>{detail.deprecated_count}</b></span>
                    <span>下线指标 <b>{detail.dsd_count ?? 0}</b></span>
                    <span>覆盖率 <b>{detail.coverage != null ? `${Math.round(detail.coverage * 100)}%` : "—"}</b></span>
                  </Space>
                )},
                { key: "error", label: "错误信息", span: 2, children: detail.error ? (
                  <span className="mono" style={{ color: "#cf1322", whiteSpace: "pre-wrap" }}>{detail.error}</span>
                ) : <span className="muted">—</span> },
                { key: "failedSpecs", label: "失败实体", span: 2, children: detailLoading ? <span className="muted">加载中…</span> : (
                  detail.detail?.failed_specs && detail.detail.failed_specs.length > 0 ? (
                    <ul style={{ margin: 0, paddingLeft: 16 }}>
                      {detail.detail.failed_specs.map((f, i) => (
                        <li key={i} style={{ fontSize: 12 }}>
                          <span className="mono">{f.entity_name}</span>：<span className="mono" style={{ color: "#cf1322" }}>{f.error}</span>
                        </li>
                      ))}
                    </ul>
                  ) : <span className="muted">—</span>
                )},
                { key: "driftEvents", label: "漂移事件", span: 2, children: (
                  detail.detail?.drift_events && detail.detail.drift_events.length > 0 ? (
                    <Space size={4} wrap>
                      {detail.detail.drift_events.map((d, i) => (
                        <Tooltip key={i} title={d.entity_name}>
                          <Tag color="gold">{d.change_type}：{d.entity_name}</Tag>
                        </Tooltip>
                      ))}
                    </Space>
                  ) : <span className="muted">—</span>
                )},
              ]}
            />
            {detail.detail?.degrade_reason && (
              <Alert type="warning" showIcon style={{ marginTop: 8 }} message={`增量降级原因：${detail.detail.degrade_reason}`} />
            )}
          </>
        )}
        {!detail && <Empty description="无运行数据" />}
      </Drawer>
    </Card>
  );
}
