import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import {
  getDpSyncConfig,
  getDpSyncWatermark,
  getDpTicket,
  listDpSyncRuns,
  listDpTickets,
  resetDpSyncWatermark,
  resolveDpTicket,
  saveDpSyncConfig,
  scanDpSyncNow,
} from "../api";
import type { DpSyncConfig, DpSyncRun, DpTicket, DpSyncWatermarkInfo } from "../types";

const TICKET_STATUS_LABEL: Record<string, { text: string; color: string }> = {
  diverged: { text: "分歧待抉择", color: "orange" },
  llm_fallback: { text: "LLM 兜底参考", color: "blue" },
  unparseable: { text: "无法解析", color: "red" },
  pending: { text: "待处理", color: "gold" },
  resolved: { text: "已裁决", color: "green" },
  ignored: { text: "已忽略", color: "default" },
};

const RESOLUTION_LABEL: Record<string, string> = {
  accept_sqlglot: "采纳 sqlglot",
  accept_llm: "采纳 LLM",
  manual: "手动修正",
  ignore: "忽略节点",
};

function fmt(v?: string | null): string {
  return v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "—";
}

export function LineageDpSync() {
  const [activeTab, setActiveTab] = useState("config");

  const renderContent = () => {
    if (activeTab === "config") return <ConfigTab />;
    if (activeTab === "tickets") return <TicketsTab />;
    return <OpsTab />;
  };

  return (
    <div>
      <Card
        size="small"
        style={{ marginBottom: 12, borderRadius: 8 }}
        styles={{ body: { padding: "10px 16px" } }}
      >
        <Space split={<span style={{ color: "#d9d9d9" }}>|</span>} wrap>
          <span style={{ fontWeight: 600 }}>dp 调度血缘同步</span>
          <Button
            type={activeTab === "config" ? "link" : "text"}
            onClick={() => setActiveTab("config")}
          >
            同步配置
          </Button>
          <Button
            type={activeTab === "tickets" ? "link" : "text"}
            onClick={() => setActiveTab("tickets")}
          >
            待抉择
          </Button>
          <Button
            type={activeTab === "ops" ? "link" : "text"}
            onClick={() => setActiveTab("ops")}
          >
            运维
          </Button>
        </Space>
      </Card>
      {renderContent()}
    </div>
  );
}

/* ==================== 同步配置 ==================== */
function ConfigTab() {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [configured, setConfigured] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const cfg = await getDpSyncConfig();
      if (cfg) {
        setConfigured(true);
        form.setFieldsValue({
          ...cfg,
          exclude_table_patterns: (cfg.exclude_table_patterns ?? []).join("\n"),
        });
      } else {
        setConfigured(false);
        form.setFieldsValue({
          enabled: false,
          source_id: "mysql_uncategorized",
          poll_interval_minutes: 5,
          llm_enabled: true,
          resolve_memory_enabled: true,
          owner_backfill: "orphan_only",
          exclude_table_patterns: "",
        });
      }
    } catch {
      message.error("加载 dp 同步配置失败");
    } finally {
      setLoading(false);
    }
  }, [form, message]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleSave = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      const payload: Partial<DpSyncConfig> = {
        enabled: values.enabled,
        source_id: values.source_id,
        poll_interval_minutes: values.poll_interval_minutes,
        task_type_filter: values.task_type_filter ?? [1],
        step_type_filter: values.step_type_filter ?? [7],
        exclude_table_patterns: String(values.exclude_table_patterns ?? "")
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean),
        llm_enabled: values.llm_enabled,
        resolve_memory_enabled: values.resolve_memory_enabled,
        owner_backfill: values.owner_backfill,
      };
      await saveDpSyncConfig(payload);
      message.success(configured ? "配置已保存（下轮轮询生效）" : "配置已创建并启用");
      setConfigured(true);
    } catch (e) {
      message.error(`保存失败：${(e as Error).message ?? e}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card
      title="同步配置"
      loading={loading}
      extra={
        <Button type="primary" loading={saving} onClick={handleSave}>
          保存
        </Button>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={
          configured
            ? "已配置。周期任务每分钟检查间隔，到点自动增量扫描 dp 任务 SQL 节点写入血缘。"
            : "尚未配置。保存后将创建默认配置（默认不启用：勾选「启用同步」才会开始轮询）。"
        }
      />
      <Form form={form} layout="vertical" style={{ maxWidth: 720 }}>
        <Space size={24} wrap>
          <Form.Item name="enabled" label="启用同步" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="poll_interval_minutes" label="轮询间隔（分钟）">
            <InputNumber min={1} max={60} style={{ width: 140 }} />
          </Form.Item>
          <Form.Item name="source_id" label="dp 数据源 source_id">
            <Input placeholder="mysql_uncategorized" style={{ width: 240 }} />
          </Form.Item>
        </Space>
        <Space size={24} wrap>
          <Form.Item name="task_type_filter" label="任务类型（type）">
            <Select
              mode="multiple"
              options={[{ value: 1, label: "1 = SQL 任务" }]}
              style={{ width: 200 }}
            />
          </Form.Item>
          <Form.Item name="step_type_filter" label="节点类型（task_step_type）">
            <Select
              mode="multiple"
              options={[{ value: 7, label: "7 = Hive/Spark SQL" }]}
              style={{ width: 220 }}
            />
          </Form.Item>
        </Space>
        <Form.Item
          name="exclude_table_patterns"
          label="排除表名正则（每行一条，命中源/目标表的边不入图）"
          tooltip="默认规则已含 tmp/temp/_bak/adhoc；此处追加自定义。留空 = 使用内置默认排除。"
        >
          <Input.TextArea rows={3} placeholder={"^tmp_\n_bak$"} />
        </Form.Item>
        <Space size={24} wrap>
          <Form.Item name="llm_enabled" label="LLM 确认 / 兜底" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="resolve_memory_enabled" label="裁决记忆复用" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="owner_backfill" label="资产 Owner 回填策略">
            <Select
              style={{ width: 200 }}
              options={[
                { value: "orphan_only", label: "仅孤儿回填（默认）" },
                { value: "never", label: "不回填" },
              ]}
            />
          </Form.Item>
        </Space>
        <Alert
          type="warning"
          showIcon
          message="启用后：后台将按间隔扫描 dp 数据源（source_id 对应数据源需已配置连接）。产物表资产 owner 为空时将按任务 director 回填（自动创建 disabled 影子用户，管理员可在用户管理配置中文名）。"
        />
      </Form>
    </Card>
  );
}

/* ==================== 待抉择 ==================== */
function TicketsTab() {
  const { message } = App.useApp();
  const [rows, setRows] = useState<DpTicket[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [status, setStatus] = useState<string>();
  const [keyword, setKeyword] = useState("");
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<DpTicket | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [acting, setActing] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const [manualText, setManualText] = useState("");
  const [reloadTick, setReloadTick] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listDpTickets({ status, keyword, page, page_size: pageSize });
      setRows(data.items);
      setTotal(data.total);
    } catch {
      message.error("加载待抉择单失败");
    } finally {
      setLoading(false);
    }
  }, [status, keyword, page, pageSize, message]);

  useEffect(() => {
    void load();
  }, [load, reloadTick]);

  const openDetail = async (id: number) => {
    setDetailLoading(true);
    try {
      const t = await getDpTicket(id);
      setDetail(t);
    } catch {
      message.error("加载详情失败");
    } finally {
      setDetailLoading(false);
    }
  };

  const doResolve = async (ticketId: number, resolution: string, manual?: unknown) => {
    setActing(true);
    try {
      await resolveDpTicket(ticketId, { resolution, manual_edges: manual });
      message.success("已裁决");
      setDetail(null);
      setReloadTick((x) => x + 1);
    } catch {
      message.error("裁决失败");
    } finally {
      setActing(false);
    }
  };

  const columns: ColumnsType<DpTicket> = [
    {
      title: "任务",
      dataIndex: "task_name",
      ellipsis: true,
      render: (v: string | null, r) => (
        <Tooltip title={`任务 #${r.task_id} · 节点 #${r.step_id}`}>
          <a onClick={() => void openDetail(r.id)}>{v || `任务 #${r.task_id}`}</a>
        </Tooltip>
      ),
    },
    { title: "产出表", dataIndex: "out_table", ellipsis: true, render: (v) => v || "—" },
    {
      title: "状态",
      dataIndex: "status",
      width: 130,
      render: (v: string) => {
        const m = TICKET_STATUS_LABEL[v] ?? { text: v, color: "default" };
        return <Tag color={m.color}>{m.text}</Tag>;
      },
    },
    {
      title: "原因",
      dataIndex: "divergence_reason",
      ellipsis: true,
      render: (v: string | null) => v || "—",
    },
    {
      title: "裁决",
      dataIndex: "resolution",
      width: 120,
      render: (v: string | null) => (v ? RESOLUTION_LABEL[v] ?? v : "—"),
    },
    { title: "创建", dataIndex: "created_at", width: 160, render: fmt },
  ];

  return (
    <Card
      title="待抉择（LLM 分歧 / 兜底 / 无法解析）"
      extra={
        <Space>
          <Select
            allowClear
            placeholder="状态筛选"
            style={{ width: 160 }}
            value={status}
            onChange={(v) => {
              setStatus(v);
              setPage(1);
            }}
            options={Object.entries(TICKET_STATUS_LABEL).map(([k, v]) => ({
              value: k,
              label: v.text,
            }))}
          />
          <Input.Search
            placeholder="任务 / 表 / SQL 关键字"
            style={{ width: 240 }}
            onSearch={(v) => {
              setKeyword(v);
              setPage(1);
            }}
          />
        </Space>
      }
    >
      <Table<DpTicket>
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={rows}
        size="middle"
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          showQuickJumper: true,
          showTotal: (t) => `共 ${t} 条`,
          onChange: (p, ps) => {
            setPage(p);
            setPageSize(ps);
          },
        }}
      />
      <Drawer
        width={860}
        title={`待抉择详情 · 任务 #${detail?.task_id ?? ""} / 节点 #${detail?.step_id ?? ""}`}
        open={detail !== null}
        onClose={() => setDetail(null)}
        loading={detailLoading}
        extra={
          detail && detail.status !== "resolved" && detail.status !== "ignored" ? (
            <Space>
              <Button
                onClick={() => void doResolve(detail.id, "accept_sqlglot")}
                loading={acting}
              >
                采纳 sqlglot
              </Button>
              <Button
                onClick={() => void doResolve(detail.id, "accept_llm")}
                loading={acting}
              >
                采纳 LLM
              </Button>
              <Button
                onClick={() => {
                  setManualText(
                    (detail.sqlglot_result?.table_edges ?? [])
                      .map((e) => `${e.source} -> ${e.target}`)
                      .join("\n")
                  );
                  setManualOpen(true);
                }}
              >
                手动修正
              </Button>
              <Button danger onClick={() => void doResolve(detail.id, "ignore")} loading={acting}>
                忽略节点
              </Button>
            </Space>
          ) : null
        }
      >
        {detail && (
          <>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="任务名">{detail.task_name || "—"}</Descriptions.Item>
              <Descriptions.Item label="产出表">{detail.out_table || "—"}</Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag color={TICKET_STATUS_LABEL[detail.status]?.color ?? "default"}>
                  {TICKET_STATUS_LABEL[detail.status]?.text ?? detail.status}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="原因">{detail.divergence_reason || "—"}</Descriptions.Item>
            </Descriptions>
            <Card size="small" title="SQL 原文" style={{ marginTop: 12 }}>
              <pre style={{ maxHeight: 220, overflow: "auto", fontSize: 12 }}>{detail.sql_text}</pre>
            </Card>
            <Card size="small" title="sqlglot 解析结果" style={{ marginTop: 12 }}>
              <Table
                size="small"
                rowKey={(_, i) => String(i ?? 0)}
                pagination={false}
                dataSource={(detail.sqlglot_result?.table_edges ?? []).map((e) => ({
                  ...e,
                }))}
                columns={[
                  { title: "源表", dataIndex: "source", ellipsis: true },
                  { title: "目标表", dataIndex: "target", ellipsis: true },
                ]}
                locale={{ emptyText: "无表级边" }}
              />
            </Card>
            {detail.llm_opinion && (
              <Card size="small" title="LLM 意见" style={{ marginTop: 12 }}>
                <pre style={{ maxHeight: 200, overflow: "auto", fontSize: 12, whiteSpace: "pre-wrap" }}>
                  {JSON.stringify(detail.llm_opinion, null, 2)}
                </pre>
              </Card>
            )}
          </>
        )}
      </Drawer>
      <Modal
        title="手动修正（每行一条：源表 -> 目标表）"
        open={manualOpen}
        onCancel={() => setManualOpen(false)}
        onOk={() => {
          const tableEdges = manualText
            .split("\n")
            .map((line) => {
              const parts = line.split("->").map((s) => s.trim());
              return parts.length === 2 ? { source: parts[0], target: parts[1] } : null;
            })
            .filter(Boolean);
          if (!detail) return;
          void doResolve(detail.id, "manual", { table_edges: tableEdges, field_mappings: [] });
          setManualOpen(false);
        }}
      >
        <Input.TextArea
          rows={10}
          value={manualText}
          onChange={(e) => setManualText(e.target.value)}
          placeholder={"wedw_ods.a -> wedw_dwd.t\nwedw_ods.b -> wedw_dwd.t"}
        />
      </Modal>
    </Card>
  );
}

/* ==================== 运维 ==================== */
function OpsTab() {
  const { message } = App.useApp();
  const [watermark, setWatermark] = useState<Record<string, DpSyncWatermarkInfo | null>>({});
  const [runs, setRuns] = useState<DpSyncRun[]>([]);
  const [runsTotal, setRunsTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [scanResult, setScanResult] = useState<Record<string, unknown> | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [wm, runData] = await Promise.all([
        getDpSyncWatermark(),
        listDpSyncRuns({ page: 1, page_size: 10 }),
      ]);
      setWatermark(wm);
      setRuns(runData.items);
      setRunsTotal(runData.total);
    } catch {
      message.error("加载运维数据失败");
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleReset = async () => {
    Modal.confirm({
      title: "重置增量水位",
      content: "重置后下轮扫描将自动全量（幂等安全，重复边不产生）。确认重置？",
      onOk: async () => {
        await resetDpSyncWatermark();
        message.success("水位已重置（下轮全量）");
        void load();
      },
    });
  };

  const handleScanNow = async () => {
    setScanning(true);
    try {
      const result = await scanDpSyncNow();
      setScanResult(result);
      void load();
    } catch {
      message.error("扫描失败（可能正在扫描或数据源不可达）");
    } finally {
      setScanning(false);
    }
  };

  return (
    <Space direction="vertical" style={{ width: "100%" }} size={12}>
      <Card
        title="增量水位"
        extra={
          <Space>
            <Button type="primary" loading={scanning} onClick={handleScanNow}>
              立即扫描一轮
            </Button>
            <Button onClick={handleReset}>重置水位（触发全量）</Button>
          </Space>
        }
      >
        {scanResult && (
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 12 }}
            message={`本轮扫描完成：任务 ${String(scanResult.scanned_tasks ?? 0)} / 节点 ${String(
              scanResult.scanned_steps ?? 0
            )}，直入 ${String(scanResult.parsed_ok ?? 0)}，LLM 确认 ${String(
              scanResult.llm_confirmed ?? 0
            )}，分歧 ${String(scanResult.diverged ?? 0)}，兜底 ${String(
              scanResult.llm_fallback ?? 0
            )}，无法解析 ${String(scanResult.unparseable ?? 0)}`}
          />
        )}
        <Descriptions size="small" column={2} bordered>
          <Descriptions.Item label="任务水位">
            {watermark.task ? fmt(watermark.task.last_max_update) : "未扫描（首次为全量）"}
          </Descriptions.Item>
          <Descriptions.Item label="任务上次扫描">
            {watermark.task ? fmt(watermark.task.last_scan_at) : "—"}
          </Descriptions.Item>
          <Descriptions.Item label="节点水位">
            {watermark.step ? fmt(watermark.step.last_max_update) : "未扫描"}
          </Descriptions.Item>
          <Descriptions.Item label="节点上次扫描">
            {watermark.step ? fmt(watermark.step.last_scan_at) : "—"}
          </Descriptions.Item>
        </Descriptions>
      </Card>
      <Card title="运行记录" loading={loading}>
        <Table<DpSyncRun>
          rowKey="id"
          size="small"
          dataSource={runs}
          pagination={{
            total: runsTotal,
            pageSize: 10,
            showTotal: (t) => `共 ${t} 条`,
          }}
          columns={[
            { title: "时间", dataIndex: "run_at", width: 160, render: fmt },
            {
              title: "状态",
              dataIndex: "status",
              width: 90,
              render: (v: string) => (
                <Tag color={v === "success" ? "green" : v === "running" ? "blue" : "red"}>
                  {v === "success" ? "成功" : v === "running" ? "运行中" : "失败"}
                </Tag>
              ),
            },
            { title: "任务/节点", width: 110, render: (_, r) => `${r.scanned_tasks}/${r.scanned_steps}` },
            { title: "直入", dataIndex: "parsed_ok", width: 70 },
            { title: "LLM 确认", dataIndex: "llm_confirmed", width: 90 },
            { title: "分歧", dataIndex: "diverged", width: 70 },
            { title: "兜底", dataIndex: "llm_fallback", width: 70 },
            { title: "无法解析", dataIndex: "unparseable", width: 90 },
            { title: "LLM 调用", dataIndex: "llm_calls", width: 90 },
            { title: "耗时(ms)", dataIndex: "duration_ms", width: 90 },
            {
              title: "错误",
              dataIndex: "error",
              ellipsis: true,
              render: (v: string | null) => v || "—",
            },
          ]}
        />
        <Alert
          type="info"
          showIcon
          style={{ marginTop: 12 }}
          message={
            <>
              提示：待抉择单会随时间积累，请在「待抉择」Tab 及时裁决。未裁决节点不会写入正式血缘。
            </>
          }
        />
      </Card>
    </Space>
  );
}
