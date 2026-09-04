import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  App,
  AutoComplete,
  Button,
  Card,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Progress,
  Row,
  Col,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { formatCnTime } from "../utils/timeCn";
import { notifyBatchInferActivity } from "../components/BatchInferCenter";
import {
  cancelDpSyncScan,
  createDpRetryTask,
  forceCancelDpSyncScan,
  getDpSyncConfig,
  getDpSyncCurrentScan,
  getDpSyncMeta,
  getDpSyncScanStatus,
  getDpSyncStats,
  getDpSyncWatermark,
  getDpTicket,
  listDataSources,
  listDpSyncRuns,
  listDpTickets,
  listSourceDatabases,
  previewDpSyncExclude,
  resetDpSyncWatermark,
  resolveDpTicket,
  resolveDpSyncLlmDisabled,
  saveDpSyncConfig,
  scanDpSyncNow,
} from "../api";
import type {
  DataSource,
  DpExcludePreview,
  DpSyncConfig,
  DpSyncMeta,
  DpSyncRun,
  DpSyncScanProgress,
  DpSyncScanStatus,
  DpSyncStats,
  DpTicket,
  DpSyncTypeOption,
  DpSyncWatermarkInfo,
} from "../types";

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

/** 是否为「LLM 类型错误」可重试单（与后端 list_retryable_llm_tickets 同语义）：
 *  llm_fallback 全部；diverged/unparseable 且原因标记 LLM 已关闭/确认输出异常/
 *  兜底输出异常——LLM 当时未给出真实意见，恢复后可重跑。 */
function isLlmRetryable(t: DpTicket): boolean {
  if (t.status === "llm_fallback") return true;
  if (t.status !== "diverged" && t.status !== "unparseable") return false;
  const r = t.divergence_reason || "";
  return (
    r.startsWith("LLM 已关闭") ||
    r.startsWith("LLM 确认输出异常") ||
    r.startsWith("LLM 兜底输出异常")
  );
}

function fmt(v?: string | null): string {
  // 后端以 UTC 落库、MySQL 返回无偏移 naive 串——必须经 parseBackendTime 按 UTC
  // 解析再转上海时区，直接 dayjs(v) 会按浏览器本地时区误读导致差 8 小时。
  return formatCnTime(v);
}

/** 扫描阶段中文文案（progress.stage）。 */
const SCAN_STAGE_LABEL: Record<string, string> = {
  queued: "排队中",
  collecting: "拉取变更任务集",
  // 中性表述：所选节点类型包含非 SQL 类型（Shell/DataX 等）时，管线同样把它们
  // 的脚本按 SQL 文本尝试解析——避免误导为「只处理 SQL 节点」（原「解析 SQL 节点」）。
  parsing: "解析节点脚本并写血缘",
  done: "已完成",
  cancelled: "已取消",
};

/** 类型选项目录的展示文本（内置已识别 / 探测未识别 + 条数）。 */
function typeOptionLabel(o: DpSyncTypeOption): string {
  const suffix = o.known ? "" : "（未识别，可全选以覆盖）";
  return `${o.value} = ${o.label}${suffix} · ${o.count} 条`;
}

function scanStageText(stage?: string): string {
  return SCAN_STAGE_LABEL[stage ?? ""] ?? stage ?? "准备中";
}

/** parsing 阶段按「当前正在解析的节点类型」动态展示（progress.current_step_label）；
 *  无类型信息（如单个任务内的 step 未开始/未知）时回退静态文案。 */
function scanParsingText(progress?: DpSyncScanProgress): string {
  const label = progress?.current_step_label?.trim();
  if (progress?.stage === "parsing" && label) {
    return `正在解析 ${label} 节点并写血缘`;
  }
  return "解析节点脚本并写血缘";
}

//: 不承载 SQL 脚本的节点类型（血缘解析仅实现 SQL 内容）：扫描命中只会得到
//: no_flow/无法解析待抉择，不会产出血缘——配置页据此提示（不建议全选扫它们）。
const NON_PARSEABLE_STEP_TYPES: ReadonlySet<number> = new Set([2, 3, 5, 9, 15]);

/** 返回所选节点类型中「无法解析为血缘」的类型 label（配置页警示用）。 */
function unparseableStepTypeLabels(
  selected: number[] | undefined,
  options: DpSyncTypeOption[]
): string[] {
  const byValue = new Map(options.map((o) => [o.value, o]));
  const labels: string[] = [];
  for (const v of selected ?? []) {
    const opt = byValue.get(v);
    if (opt && NON_PARSEABLE_STEP_TYPES.has(v)) {
      labels.push(opt.label);
    }
  }
  return labels;
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
  // 节点类型选择实时跟踪——命中不可解析类型时给出警示（避免「全选=全部」把
  // Shell/DataX/清表等非 SQL 节点扫成满屏待抉择单的误解）
  const stepTypeFilter = Form.useWatch("step_type_filter", form);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [configured, setConfigured] = useState(false);
  const [sources, setSources] = useState<DataSource[]>([]);
  const [meta, setMeta] = useState<DpSyncMeta | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewResult, setPreviewResult] = useState<DpExcludePreview | null>(null);
  // 元数据库名选项：选定数据源后经 /dimensions/source-databases 拉取该源真实库列表
  const [schemaOptions, setSchemaOptions] = useState<{ value: string; label: string }[]>([]);
  const [schemaLoading, setSchemaLoading] = useState(false);

  // 按数据源拉取真实库列表供「元数据库名」选项框选择；失败静默降级（保留手动输入兜底）
  const refreshSchemaOptions = useCallback(async (sourceId?: string) => {
    const sid = sourceId ?? (form.getFieldValue("source_id") as string | undefined);
    if (!sid) {
      setSchemaOptions([]);
      return;
    }
    setSchemaLoading(true);
    try {
      const r = await listSourceDatabases(sid);
      setSchemaOptions((r.databases ?? []).map((d) => ({ value: d, label: d })));
    } catch {
      // 源不可达/未配置私网放行：不阻塞配置，保留手输
      setSchemaOptions([]);
    } finally {
      setSchemaLoading(false);
    }
  }, [form]);

  // 数据源下拉 + dp 类型/默认规则目录（失败不阻塞配置加载）
  useEffect(() => {
    listDataSources({ page: 1, page_size: 200 })
      .then((res) => setSources(res.items ?? []))
      .catch(() => setSources([]));
    getDpSyncMeta()
      .then(setMeta)
      .catch(() => setMeta(null));
  }, []);

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
        // 已有配置：按已选数据源拉一次真实库列表（供下拉回显/核对）
        void refreshSchemaOptions(cfg.source_id);
      } else {
        setConfigured(false);
        form.setFieldsValue({
          enabled: false,
          source_id: "mysql_uncategorized",
          // 与后端 create_default_config 一致：元库库名默认 dp_stable，可按环境改
          schema_name: "dp_stable",
          poll_interval_minutes: 5,
          // 与后端 create_default_config 一致：默认仅 SQL 任务 / Hive-Spark SQL 节点
          task_type_filter: [1],
          step_type_filter: [7],
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
  }, [form, message, refreshSchemaOptions]);

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
        // dp 元库所在数据库名（后端 _safe_table_name 白名单校验，缺省回退 dp_stable）
        schema_name: String(values.schema_name ?? "").trim() || "dp_stable",
        poll_interval_minutes: Number(values.poll_interval_minutes),
        // 空数组 = 全部类型（含未识别）；未配置时后端默认仅 SQL 任务/Hive SQL
        task_type_filter: values.task_type_filter ?? [],
        step_type_filter: values.step_type_filter ?? [],
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

  /** 排除规则校验 + 命中量预览（连 dp 源统计产出表命中）。 */
  const handlePreviewExclude = async () => {
    const vals = await form.validateFields(["source_id"]).catch(() => null);
    if (!vals?.source_id) {
      message.warning("请先选择 dp 数据源，再预览排除规则命中量");
      return;
    }
    const patterns = String(form.getFieldValue("exclude_table_patterns") ?? "")
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    setPreviewBusy(true);
    setPreviewResult(null);
    try {
      const res = await previewDpSyncExclude({
        source_id: vals.source_id,
        patterns,
      });
      setPreviewResult(res);
    } catch (e) {
      message.error(`预览失败：${(e as Error).message ?? e}`);
    } finally {
      setPreviewBusy(false);
    }
  };

  const renderExcludePreview = () => {
    if (!previewResult) return null;
    const r = previewResult;
    if (!r.reachable) {
      return (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 8 }}
          message="无法预览命中量"
          description={r.error || "dp 数据源不可达或查询失败，请检查连接配置"}
        />
      );
    }
    const invalid = (r.invalid_patterns ?? []).map(
      (x) => `${x.pattern}：${x.error}`
    );
    return (
      <Alert
        type={r.matched ? "warning" : "success"}
        showIcon
        style={{ marginTop: 8 }}
        message={`命中 ${r.matched ?? 0} / ${r.total ?? 0} 张任务产出表`}
        description={
          <Space direction="vertical" size={4} style={{ width: "100%" }}>
            {(r.samples ?? []).length > 0 && (
              <span>
                样例：
                {(r.samples ?? []).slice(0, 6).map((s) => (
                  <Tag key={s.table} style={{ marginBottom: 4 }}>
                    {s.table}
                  </Tag>
                ))}
                {(r.samples ?? []).length > 6 ? "…" : ""}
              </span>
            )}
            {invalid.length > 0 && (
              <span style={{ color: "#cf1322" }}>
                正则不合法（未参与匹配）：{invalid.join("；")}
              </span>
            )}
            <span style={{ color: "#999" }}>{r.note}</span>
          </Space>
        }
      />
    );
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
      <Form form={form} layout="vertical" style={{ maxWidth: 980 }}>
        <Card
          type="inner"
          size="small"
          title="基础设置"
          style={{ marginBottom: 16 }}
          styles={{ body: { paddingBottom: 0 } }}
        >
          <Row gutter={24}>
            <Col xs={24} sm={8}>
              <Form.Item
                name="enabled"
                label="启用同步"
                valuePropName="checked"
                extra="停用后不再轮询/解析（已写入血缘保留）"
              >
                <Switch />
              </Form.Item>
            </Col>
            <Col xs={24} sm={8}>
              <Form.Item
                name="poll_interval_minutes"
                label="轮询间隔（分钟）"
                extra="1~1440（最长 24 小时），修改即时生效，无需重启"
              >
                <AutoComplete
                  style={{ width: "100%" }}
                  options={[5, 15, 30, 60, 120, 360, 720, 1440].map((v) => ({
                    value: String(v),
                    label: `${v} 分钟${v >= 60 ? `（${v / 60} 小时）` : ""}`,
                  }))}
                  filterOption={false}
                  placeholder="选择档位或输入分钟数"
                />
              </Form.Item>
            </Col>
            <Col xs={24} sm={8}>
              <Form.Item
                name="source_id"
                label="dp 数据源"
                extra="下拉选择已配置的数据源（需含 dp 元库 dispatch_task 等）"
                rules={[{ required: true, message: "请选择 dp 数据源" }]}
              >
                <Select
                  showSearch
                  placeholder="选择 dp 数据源"
                  optionFilterProp="label"
                  options={sources.map((s) => ({
                    value: s.source_id,
                    label: `${s.source_id} · ${s.name}（${s.source_type}）`,
                  }))}
                  onChange={(v) => {
                    // 换源后旧库名通常不适用：清空待用户从新源真实库列表中重选
                    form.setFieldsValue({ schema_name: undefined });
                    void refreshSchemaOptions(v);
                  }}
                />
              </Form.Item>
            </Col>
            <Col xs={24} sm={8}>
              <Form.Item
                name="schema_name"
                label="元数据库名"
                extra="dp 元库（dispatch_task/dispatch_task_step）所在数据库；下拉为所选数据源的真实库，也可手动输入"
                rules={[
                  { required: true, message: "请选择或输入 dp 元库所在数据库名" },
                  {
                    pattern: /^[A-Za-z0-9_]+$/,
                    message: "仅允许字母/数字/下划线（库名，不含点）",
                  },
                ]}
              >
                <AutoComplete
                  style={{ width: "100%" }}
                  options={schemaOptions}
                  allowClear
                  placeholder="选择或输入库名（如 dp_stable）"
                  filterOption={(input, option) =>
                    (option?.value ?? "").toLowerCase().includes(input.toLowerCase())
                  }
                  notFoundContent={schemaLoading ? "加载库列表中…" : "未获取到库列表，可手动输入"}
                />
              </Form.Item>
            </Col>
          </Row>
        </Card>

        <Card
          type="inner"
          size="small"
          title="同步范围"
          style={{ marginBottom: 16 }}
          styles={{ body: { paddingBottom: 0 } }}
        >
          <Row gutter={24}>
            <Col xs={24} md={12}>
              <Form.Item
                name="task_type_filter"
                label="任务类型（dispatch_task.type）"
                extra={
                  <Space size={8} wrap>
                    <span style={{ color: "#999" }}>留空 = 全部任务类型；默认仅 SQL 任务。</span>
                    <a
                      onClick={() =>
                        form.setFieldValue(
                          "task_type_filter",
                          (meta?.task_types ?? []).map((o) => o.value)
                        )
                      }
                    >
                      全选
                    </a>
                    <a onClick={() => form.setFieldValue("task_type_filter", [])}>
                      清空（=全部）
                    </a>
                  </Space>
                }
              >
                <Select
                  mode="multiple"
                  showSearch
                  optionFilterProp="label"
                  placeholder={meta?.reachable === false && meta?.reason ? "类型枚举：配置源不可达，仅显示已知值" : "选择任务类型"}
                  options={(meta?.task_types ?? [
                    { value: 1, label: "数据抽取（SQL 加工）", known: true, count: 0 },
                    { value: 3, label: "Shell 任务", known: true, count: 0 },
                    { value: 4, label: "混合加工任务", known: true, count: 0 },
                    { value: 10, label: "DataX 同步任务", known: true, count: 0 },
                    { value: 15, label: "接口同步任务", known: true, count: 0 },
                  ]).map((o) => ({ value: o.value, label: typeOptionLabel(o) }))}
                  style={{ width: "100%" }}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item
                name="step_type_filter"
                label="节点类型（dispatch_task_step.task_step_type）"
                extra={
                  <Space size={8} wrap>
                    <span style={{ color: "#999" }}>留空 = 全部节点类型；默认仅 Hive/Spark SQL。</span>
                    <a
                      onClick={() =>
                        form.setFieldValue(
                          "step_type_filter",
                          (meta?.step_types ?? []).map((o) => o.value)
                        )
                      }
                    >
                      全选
                    </a>
                    <a onClick={() => form.setFieldValue("step_type_filter", [])}>
                      清空（=全部）
                    </a>
                  </Space>
                }
              >
                <Select
                  mode="multiple"
                  showSearch
                  optionFilterProp="label"
                  placeholder={meta?.reachable === false && meta?.reason ? "类型枚举：配置源不可达，仅显示已知值" : "选择节点类型"}
                  options={(meta?.step_types ?? [
                    { value: 2, label: "DataX 同步", known: true, count: 0 },
                    { value: 3, label: "Shell 脚本", known: true, count: 0 },
                    { value: 4, label: "SQL 执行脚本", known: true, count: 0 },
                    { value: 5, label: "清表脚本（TRUNCATE）", known: true, count: 0 },
                    { value: 6, label: "Oracle SQL/PLSQL 脚本", known: true, count: 0 },
                    { value: 7, label: "Hive/Spark SQL", known: true, count: 0 },
                    { value: 9, label: "上报配置节点", known: true, count: 0 },
                    { value: 15, label: "接口同步配置", known: true, count: 0 },
                  ]).map((o) => ({ value: o.value, label: typeOptionLabel(o) }))}
                  style={{ width: "100%" }}
                />
              </Form.Item>
            </Col>
          </Row>
          {unparseableStepTypeLabels(stepTypeFilter, meta?.step_types ?? []).length >
            0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message="所选节点类型包含无法解析为血缘的类型"
              description={`${unparseableStepTypeLabels(
                stepTypeFilter,
                meta?.step_types ?? []
              ).join("、")} 的节点脚本不是 SQL（如 DataX 配置/Shell 脚本/清表/上报 ID/接口同步配置），系统只会把脚本按 SQL 文本尝试解析，绝大多数落「无流转」或「无法解析」待抉择单，不会产出血缘——若希望解析到真实血缘，请只保留承载 SQL 的节点类型（Hive/Spark SQL、SQL 执行脚本等）。`}
            />
          )}
          <Form.Item
            name="exclude_table_patterns"
            label="追加排除的表名正则（每行一条）"
            tooltip="命中这些正则的表（库.表 全名匹配）不入血缘图。系统内置默认排除始终生效（见下方列表），这里填写的会叠加到内置规则之上。留空 = 仅内置默认排除。"
          >
            <Input.TextArea rows={3} placeholder={"每行一条正则，如：\n^dwd_.*_temp$\n.*_history$"} />
          </Form.Item>
          <Space direction="vertical" size={4} style={{ width: "100%", marginBottom: 16 }}>
            <Space size={4} wrap>
              <span style={{ fontSize: 12, color: "#666" }}>
                内置默认排除（始终生效）：
              </span>
              {(meta?.exclude_defaults ?? ["(^|\\.)(tmp|temp)[\\d_]*$", "(^|\\.)tmp_", "_bak$", "(^|\\.)adhoc"]).map(
                (p) => (
                  <Tag key={p} style={{ fontFamily: "monospace", fontSize: 11 }}>
                    {p}
                  </Tag>
                )
              )}
            </Space>
            <Space size={8}>
              <Button size="small" loading={previewBusy} onClick={handlePreviewExclude}>
                校验并预览命中量
              </Button>
              {!meta?.reachable && meta?.reason && (
                <span style={{ fontSize: 12, color: "#999" }}>
                  类型枚举与默认规则已加载；命中预览需 dp 源可达（{meta.reason}）
                </span>
              )}
            </Space>
            {renderExcludePreview()}
          </Space>
        </Card>

        <Card
          type="inner"
          size="small"
          title="LLM 与裁决"
          style={{ marginBottom: 16 }}
          styles={{ body: { paddingBottom: 0 } }}
        >
          <Row gutter={24}>
            <Col xs={24} md={8}>
              <Form.Item
                name="llm_enabled"
                label="LLM 确认 / 兜底"
                valuePropName="checked"
                extra="关闭后纯 sqlglot 解析，分歧全进待抉择"
              >
                <Switch />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item
                name="resolve_memory_enabled"
                label="裁决记忆复用"
                valuePropName="checked"
                extra="SQL 未变时自动复用上次裁决，不再重复待抉择"
              >
                <Switch />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item
                name="owner_backfill"
                label="资产 Owner 回填策略"
                extra="仅孤儿回填 = 只在资产 owner 为空时回填"
              >
                <Select
                  style={{ width: "100%" }}
                  options={[
                    { value: "orphan_only", label: "仅孤儿回填（默认）" },
                    { value: "never", label: "不回填" },
                  ]}
                />
              </Form.Item>
            </Col>
          </Row>
        </Card>

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
  // 批量 LLM 重试的勾选集（仅 isLlmRetryable 行可勾选；跨页保留，提交后清空）
  const [selectedKeys, setSelectedKeys] = useState<number[]>([]);
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

  const doResolveLlmDisabled = async () => {
    Modal.confirm({
      title: "处置 LLM 关闭期待抉择单",
      content:
        "将「LLM 已关闭」标记的复杂节点待抉择单批量采纳 sqlglot 结果入库（这些单无真实语义分歧、sqlglot 结果完整）。确认批量处置？",
      onOk: async () => {
        setActing(true);
        try {
          const r = await resolveDpSyncLlmDisabled();
          message.success(`已处置：采纳 ${r.resolved}、排除 ${r.skipped}、失败 ${r.failed}`);
          setReloadTick((x) => x + 1);
        } catch {
          message.error("批量处置失败");
        } finally {
          setActing(false);
        }
      },
    });
  };

  const doRetryLlm = async (ticketIds?: number[]) => {
    const scopeLabel = ticketIds
      ? `待抉择单 #${ticketIds.join(", ")}`
      : "全部 LLM 失败/兜底低置信的待抉择单";
    Modal.confirm({
      title: ticketIds ? "LLM 重试（单条）" : "LLM 重试（批量）",
      content: `将重新调用本地 LLM 解析「${scopeLabel}」。任务将在后台执行，进度与结果实时显示在右下角「LLM 任务中心」（可随时取消、跨页面可见）。确认提交？`,
      onOk: async () => {
        setActing(true);
        try {
          const r = await createDpRetryTask(
            ticketIds ? { ticket_ids: ticketIds } : {},
          );
          if (r.task) {
            message.success(
              `LLM 重试任务已提交（#${r.task.id}，共 ${r.task.total} 张单）——进度与结果在右下角「LLM 任务中心」实时查看`,
            );
            notifyBatchInferActivity(r.task.id, "dp");
          } else {
            message.info("没有可重试的 LLM 失败/兜底待抉择单");
          }
          setDetail(null);
          setSelectedKeys([]);
          setReloadTick((x) => x + 1);
        } catch {
          message.error("LLM 重试任务提交失败");
          setSelectedKeys([]);
        } finally {
          setActing(false);
        }
      },
    });
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
        <Space wrap>
          <Button
            type="primary"
            ghost
            onClick={() =>
              void (selectedKeys.length ? doRetryLlm(selectedKeys) : doRetryLlm())
            }
            loading={acting}
          >
            {selectedKeys.length ? `LLM 重试（已选 ${selectedKeys.length}）` : "LLM 重试"}
          </Button>
          <Button onClick={() => void doResolveLlmDisabled()} loading={acting}>
            处置 LLM 关闭期单
          </Button>
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
        rowSelection={{
          selectedRowKeys: selectedKeys,
          onChange: (keys) => setSelectedKeys(keys as number[]),
          getCheckboxProps: (r) => ({ disabled: !isLlmRetryable(r) }),
        }}
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
              {isLlmRetryable(detail) && (
                <Button
                  type="primary"
                  onClick={() => void doRetryLlm([detail.id])}
                  loading={acting}
                >
                  LLM 重试
                </Button>
              )}
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
  const [stats, setStats] = useState<DpSyncStats | null>(null);
  const [loading, setLoading] = useState(false);
  // 手动扫描：后台异步执行 → 轮询状态实时展示进度/取消/异常（不再同步等待）
  const [scanning, setScanning] = useState(false);
  const [scanTaskId, setScanTaskId] = useState<number | null>(null);
  const [scanStatus, setScanStatus] = useState<DpSyncScanStatus | null>(null);
  const pollTimer = useRef<number | null>(null);
  // 两段式取消（B 方案）：cancel 请求时间（本地）+ 是否已请求 + 是否超时可强制终止
  const cancelStartRef = useRef<number | null>(null);
  const [cancelRequested, setCancelRequested] = useState(false);
  const [forceArmed, setForceArmed] = useState(false);
  const FORCE_WAIT_MS = 8000; // 协作取消等待上限：超过则显示「强制终止」入口

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) {
      window.clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
    cancelStartRef.current = null;
    setCancelRequested(false);
    setForceArmed(false);
    setScanning(false);
  }, []);

  useEffect(
    () => () => {
      if (pollTimer.current !== null) window.clearInterval(pollTimer.current);
    },
    []
  );

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [wm, runData, statsData] = await Promise.all([
        getDpSyncWatermark(),
        listDpSyncRuns({ page: 1, page_size: 10 }),
        getDpSyncStats(),
      ]);
      setWatermark(wm);
      setRuns(runData.items);
      setRunsTotal(runData.total);
      setStats(statsData);
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

  const finishScan = useCallback(
    async (st: DpSyncScanStatus) => {
      stopPolling();
      setScanStatus(st);
      void load();
      if (st.status === "failed") {
        message.error(`扫描失败：${st.error || "未知错误（详见运行记录）"}`, 6);
      } else if (st.status === "cancelled") {
        message.info(st.message || "扫描已取消");
      } else if (st.status === "success") {
        message.success(st.message || "全量扫描完成");
      }
    },
    [message, stopPolling, load]
  );

  const pollOnce = useCallback(
    async (taskId: number) => {
      try {
        const st = await getDpSyncScanStatus(taskId);
        setScanStatus(st);
        // 协作取消超时武装：请求取消后超过等待上限仍未结束 → 显示「强制终止」
        if (
          st.status === "running" &&
          (st.cancel_requested || cancelStartRef.current !== null)
        ) {
          const since =
            cancelStartRef.current ??
            (st.cancel_requested_at
              ? new Date(st.cancel_requested_at).getTime()
              : Date.now());
          if (Date.now() - since > FORCE_WAIT_MS) setForceArmed(true);
        }
        if (
          st.status === "success" ||
          st.status === "failed" ||
          st.status === "cancelled"
        ) {
          await finishScan(st);
        }
      } catch {
        stopPolling();
        message.error("查询扫描状态失败（可能任务已随进程结束）");
      }
    },
    [finishScan, message, stopPolling]
  );

  // 切走页面/Tab 再回来：若后端仍有运行中的手动扫描，自动接上进度轮询，无需
  // 重新点「立即扫描」——任务跑在 backend 进程内不受页面切换影响（仅进程重启
  // 会丢，registry 查询不到时此 effect 静默不打扰）。
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const cur = await getDpSyncCurrentScan();
        if (cancelled || !cur?.running || cur.task_id == null) return;
        setScanning(true);
        setScanStatus(cur);
        setScanTaskId(cur.task_id);
        message.info("检测到扫描仍在运行，已恢复实时进度跟踪");
        void pollOnce(cur.task_id);
        pollTimer.current = window.setInterval(() => {
          void pollOnce(cur.task_id as number);
        }, 1500);
      } catch {
        // 查询失败（如任务已随进程结束）不打扰，运行记录表仍可见
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pollOnce, message]);

  const handleScanNow = async () => {
    setScanning(true);
    setScanStatus(null);
    setScanTaskId(null);
    try {
      const submit = await scanDpSyncNow();
      // 被节流拒绝时后端返回 task_id=null（无任务可跟踪），不得把 null 拼进
      // status 轮询 URL（会 422）；直接提示并复位，不启动轮询。
      if (submit.task_id == null || submit.status === "throttled") {
        stopPolling();
        setScanning(false);
        message.warning(
          submit.message || "触发过于频繁，请稍候再试（全量扫描为重操作）",
          4
        );
        return;
      }
      // 进入这里 task_id 必非 null（上面已 return），用局部 const 承接便于闭包引用
      const taskId: number = submit.task_id;
      setScanTaskId(taskId);
      if (submit.already_running) {
        message.info("已有扫描任务在运行，正在跟踪其进度");
      }
      // 立即拉一次 + 1.5s 轮询实时进度
      void pollOnce(taskId);
      pollTimer.current = window.setInterval(() => {
        void pollOnce(taskId);
      }, 1500);
    } catch {
      stopPolling();
      message.error("提交扫描失败");
    }
  };

  const handleCancelScan = async () => {
    if (scanTaskId === null) return;
    try {
      const r = await cancelDpSyncScan(scanTaskId);
      if (r.cancelled) {
        cancelStartRef.current = Date.now();
        setCancelRequested(true);
        setForceArmed(false);
        message.info("正在停止扫描：等待当前步骤完成后停止（长时间未停可强制终止）");
      }
    } catch {
      message.error("取消失败，请稍后重试");
    }
  };

  const handleForceCancelScan = async () => {
    if (scanTaskId === null) return;
    Modal.confirm({
      title: "强制终止扫描",
      content:
        "将在当前步骤处理点立即停止（未完成部分不落库，已提交部分保留，水位不推进）。若当前步骤正在写库可能中断事务，由服务端回滚兜底。确认强制终止？",
      okText: "强制终止",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const r = await forceCancelDpSyncScan(scanTaskId as number);
          if (r.cancelled) {
            setForceArmed(false);
            message.info("强制终止中：将在当前步骤处理点立即停止", 4);
          }
        } catch {
          message.error("强制终止失败，请稍后重试");
        }
      },
    });
  };

  const scanProgress = scanStatus?.progress;
  const scanPercent =
    scanProgress && scanProgress.total > 0
      ? Math.min(100, Math.round((scanProgress.processed / scanProgress.total) * 100))
      : 0;
  const scanResult = scanStatus?.result ?? null;

  return (
    <Space direction="vertical" style={{ width: "100%" }} size={12}>
      <Card title="统计概览" size="small" loading={loading && stats === null}>
        {!stats ? (
          <Typography.Text type="secondary">暂无统计数据</Typography.Text>
        ) : (
          <>
            {!stats.dp_reachable && (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 12 }}
                message="dp 数据源不可达"
                description={stats.dp_unreachable_reason ?? undefined}
              />
            )}
            <Row gutter={16}>
              <Col span={6}>
                <Statistic
                  title="DP 任务总量（活跃）"
                  value={stats.task_total ?? "—"}
                  suffix={
                    stats.task_total !== null ? (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        / 节点 {stats.step_total ?? "—"}
                      </Typography.Text>
                    ) : undefined
                  }
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="血缘表节点"
                  value={stats.lineage.table_nodes}
                  suffix={
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      / 活跃边 {stats.lineage.table_edges}
                    </Typography.Text>
                  }
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="字段级血缘（条）"
                  value={stats.lineage.field_mappings}
                />
              </Col>
              <Col span={6}>
                <Statistic
                  title="待抉择工单"
                  value={Object.values(stats.pending_tickets).reduce((a, b) => a + b, 0)}
                  valueStyle={
                    Object.values(stats.pending_tickets).reduce((a, b) => a + b, 0) > 0
                      ? { color: "#d46b08" }
                      : undefined
                  }
                />
              </Col>
            </Row>
            <Row gutter={16} style={{ marginTop: 16 }}>
              <Col span={12}>
                {stats.last_full_scan ? (
                  <>
                    <Space size={4} wrap>
                      <Typography.Text strong>最近全量轮</Typography.Text>
                      <Tag color="green">
                        解析成功 {stats.last_full_scan.parse_success_total ?? "—"}
                      </Tag>
                      <Tag color="orange">
                        解析失败 {stats.last_full_scan.parse_fail_total ?? "—"}
                      </Tag>
                      <Tag color="blue">
                        扫描 {stats.last_full_scan.scanned_tasks} 任务 /{" "}
                        {stats.last_full_scan.scanned_steps} 节点
                      </Tag>
                      <Typography.Text type="secondary">
                        {formatCnTime(stats.last_full_scan.run_at)}
                      </Typography.Text>
                    </Space>
                    {stats.last_full_scan.parse_rate !== null &&
                      stats.last_full_scan.parse_rate !== undefined && (
                        <Progress
                          percent={stats.last_full_scan.parse_rate}
                          size="small"
                          status="normal"
                          strokeColor="#52c41a"
                          style={{ maxWidth: 420, marginTop: 4, marginBottom: 0 }}
                          format={(p) => `成功率 ${p}%`}
                        />
                      )}
                  </>
                ) : (
                  <Typography.Text type="secondary">
                    尚无成功的全量扫描记录
                  </Typography.Text>
                )}
              </Col>
              <Col span={12}>
                <Space size={4} wrap>
                  <Typography.Text strong>
                    历史累计（{stats.cumulative.runs} 次成功轮）
                  </Typography.Text>
                  <Tag color="green">
                    解析成功 {stats.cumulative.parse_success_total ?? "—"}
                  </Tag>
                  <Tag color="orange">
                    解析失败 {stats.cumulative.parse_fail_total ?? "—"}
                  </Tag>
                  {stats.cumulative.parse_rate !== null &&
                    stats.cumulative.parse_rate !== undefined && (
                      <Typography.Text type="secondary">
                        累计成功率 {stats.cumulative.parse_rate}%
                      </Typography.Text>
                    )}
                </Space>
              </Col>
            </Row>
          </>
        )}
      </Card>
      <Card
        title="增量水位"
        extra={
          <Space>
            <Button type="primary" loading={scanning} onClick={handleScanNow}>
              立即全量扫描
            </Button>
            {scanning && scanTaskId !== null && (
              <>
                <Button
                  danger
                  onClick={handleCancelScan}
                  disabled={cancelRequested}
                  loading={cancelRequested}
                >
                  {cancelRequested ? "正在停止…" : "取消扫描"}
                </Button>
                {forceArmed && !scanStatus?.force_stop && (
                  <Button danger type="primary" onClick={handleForceCancelScan}>
                    强制终止
                  </Button>
                )}
              </>
            )}
            <Button onClick={handleReset}>重置水位（触发全量）</Button>
          </Space>
        }
      >
        {scanning && scanStatus?.status === "running" && (
          <Alert
            type={cancelRequested || scanStatus.force_stop ? "warning" : "info"}
            showIcon
            style={{ marginBottom: 12 }}
            message={
              <Space direction="vertical" style={{ width: "100%" }} size={4}>
                <span>
                  {scanStatus.force_stop
                    ? "强制终止中：将在当前步骤处理点立即停止…"
                    : cancelRequested
                      ? "正在停止扫描：等待当前步骤完成后停止…"
                      : `扫描中：${
                          scanProgress?.stage === "parsing"
                            ? scanParsingText(scanProgress)
                            : scanStageText(scanProgress?.stage)
                        }`}
                  {!cancelRequested &&
                    !scanStatus.force_stop &&
                    `（已处理 ${scanProgress?.processed ?? 0} / ${scanProgress?.total ?? 0} 个任务）`}
                  {scanProgress?.current_task_id
                    ? ` · 当前任务 #${scanProgress.current_task_id}`
                    : ""}
                </span>
                <Progress
                  percent={scanPercent}
                  status={
                    cancelRequested || scanStatus.force_stop
                      ? "exception"
                      : "active"
                  }
                  style={{ width: "100%" }}
                />
                {cancelRequested && !forceArmed && !scanStatus.force_stop && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    已在步骤边界停止，通常数秒内结束；若长时间未停可点「强制终止」。
                  </Typography.Text>
                )}
              </Space>
            }
          />
        )}
        {!scanning && scanStatus && scanStatus.status === "success" && (
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 12 }}
            message={`本轮扫描完成：任务 ${String(scanResult?.scanned_tasks ?? 0)} / 节点 ${String(
              scanResult?.scanned_steps ?? 0
            )}，直入 ${String(scanResult?.parsed_ok ?? 0)}，LLM 确认 ${String(
              scanResult?.llm_confirmed ?? 0
            )}，分歧 ${String(scanResult?.diverged ?? 0)}，兜底 ${String(
              scanResult?.llm_fallback ?? 0
            )}，无法解析 ${String(scanResult?.unparseable ?? 0)}`}
          />
        )}
        {!scanning && scanStatus && scanStatus.status === "failed" && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message="扫描失败"
            description={
              <Typography.Text code copyable style={{ whiteSpace: "pre-wrap" }}>
                {scanStatus.error || "未知错误"}
              </Typography.Text>
            }
          />
        )}
        {!scanning && scanStatus && scanStatus.status === "cancelled" && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={scanStatus.message || "扫描已取消"}
            description="已处理结果保留，水位未推进——下轮扫描将从原水位重扫未完成任务。"
          />
        )}
        <Descriptions size="small" column={2} bordered>
          <Descriptions.Item label="任务水位">
            {watermark?.task?.last_max_update
              ? fmt(watermark.task.last_max_update)
              : "未扫描（首次为全量）"}
          </Descriptions.Item>
          <Descriptions.Item label="任务上次扫描">
            {watermark?.task?.last_scan_at ? fmt(watermark.task.last_scan_at) : "—"}
          </Descriptions.Item>
          <Descriptions.Item label="节点水位">
            {watermark?.step?.last_max_update ? fmt(watermark.step.last_max_update) : "未扫描"}
          </Descriptions.Item>
          <Descriptions.Item label="节点上次扫描">
            {watermark?.step?.last_scan_at ? fmt(watermark.step.last_scan_at) : "—"}
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
                <Tag
                  color={
                    v === "success"
                      ? "green"
                      : v === "running"
                        ? "blue"
                        : v === "cancelled"
                          ? "orange"
                          : "red"
                  }
                >
                  {v === "success"
                    ? "成功"
                    : v === "running"
                      ? "运行中"
                      : v === "cancelled"
                        ? "已取消"
                        : "失败"}
                </Tag>
              ),
            },
            {
              title: "模式",
              dataIndex: "scan_mode",
              width: 90,
              render: (v: string) =>
                v === "full" ? (
                  <Tag color="blue">全量</Tag>
                ) : (
                  <Tooltip title="周期增量扫描（按水位）；0/0 = 无变更，属正常">
                    <Tag>增量</Tag>
                  </Tooltip>
                ),
            },
            {
              title: "任务/节点",
              width: 100,
              render: (_, r) =>
                r.scan_mode === "incremental" &&
                r.scanned_tasks === 0 &&
                r.scanned_steps === 0 ? (
                  <span style={{ color: "#999" }}>0/0 空扫</span>
                ) : (
                  `${r.scanned_tasks}/${r.scanned_steps}`
                ),
            },
            { title: "直入", dataIndex: "parsed_ok", width: 70 },
            { title: "LLM 确认", dataIndex: "llm_confirmed", width: 90 },
            { title: "分歧", dataIndex: "diverged", width: 70 },
            { title: "兜底", dataIndex: "llm_fallback", width: 70 },
            { title: "无法解析", dataIndex: "unparseable", width: 90 },
            { title: "LLM 调用", dataIndex: "llm_calls", width: 90 },
            {
              title: "字段血缘",
              key: "field_lineage",
              width: 110,
              render: (_: unknown, r: DpSyncRun) => {
                const written = Number(r.field_mappings_written ?? 0);
                const degraded = Number(r.field_edges_degraded ?? 0);
                if (!written && !degraded) return <span style={{ color: "var(--text-tertiary)" }}>—</span>;
                return (
                  <Tooltip title="字段级映射写入数 / 其中 SELECT * 降级（无源表 schema）">
                    <span>
                      <Tag color={written ? "green" : "default"}>{written}</Tag>
                      {degraded > 0 && <Tag color="orange">降 {degraded}</Tag>}
                    </span>
                  </Tooltip>
                );
              },
            },
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
