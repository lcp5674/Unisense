import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, Select, Input, Button, Form, Space, Tag, Table, Tabs, Alert, message, Row, Col, Drawer, Empty, Segmented } from "antd";
import { PlayCircleOutlined, SafetyCertificateOutlined, KeyOutlined, DatabaseOutlined, ReadOutlined, ArrowLeftOutlined, SearchOutlined, ApiOutlined } from "@ant-design/icons";
import {
  consumeDryRun,
  consumeQuery,
  consumeSemantic,
  listMetrics,
  listSnapshots,
  listApiClients,
  mintClientToken,
  getConsumeToken,
  getConsumeTokenExpiry,
  getConsumeTokenClientId,
  setConsumeToken,
  clearConsumeToken,
  CONSUME_TOKEN_CHANGED_EVENT,
  UnisenseApiError,
} from "../api";
import type { DimensionExpr, DryRunResponse, QueryResponse, SnapshotResponse, ClientResponse } from "../types";
import { useTracking } from "../hooks/useTracking";
import { usePermission } from "../hooks/usePermission";
import { ObjectView, kvText } from "../utils/display";
import { DATE_RANGE_LABEL, GRANULARITY_LABEL } from "../utils/enums";
import { formatCnRange } from "../utils/timeCn";
import { handleDegradedEngine, isDegradationError } from "../utils/apiErrorHandlers";
import type { UnisenseApiError as UnisenseApiErrorType } from "../utils/apiErrorHandlers";

// 执行计划等对象字段名 → 中文（可读展示，避免裸 JSON 直出）
const CHECK_LABEL: Record<string, string> = {
  granularity: "粒度校验",
  authorization: "权限校验",
  staleness: "时效校验",
  metric_status: "指标状态校验",
};

const VALUE_FIELD_LABEL: Record<string, string> = {
  value: "指标值",
  total: "总计",
  count: "数量",
};

// consume 通道错误提示引导：严格消费方通道（X-Api-Key client_id:secret / consume JWT）
// 与用户 JWT 不同，未签发消费令牌时后端返回 AUTH_APIKEY_INVALID（"X-Api-Key 格式应为
// client_id:secret"），对业务用户不直观——这里转成明确操作指引。
// mode="query"（内部用户 JWT 查询）时不提示「需要消费令牌」，直接展示后端错误信息。
function consumeErrorText(err: unknown, mode: "query" | "debug" = "debug"): string {
  if (err instanceof UnisenseApiError) {
    if (mode === "query") {
      return `${err.message}（${err.codeZh}）`;
    }
    if (err.code === "AUTH_APIKEY_INVALID" || err.code === "AUTH_APIKEY_MISSING") {
      return "需要消费令牌：请点击上方『从客户端签发令牌』后重试";
    }
    if (err.code === "FORBIDDEN_METRIC") {
      return err.message || "该指标未发布，不可消费";
    }
    return `${err.message}（${err.codeZh}）`;
  }
  return "操作失败";
}

function PlanView({ plan }: { plan: Record<string, unknown> }) {
  return <ObjectView data={plan} />;
}

// 查询结果：rows 数组 → 动态列表格；非数组/空结果 → 结构化字段视图
function QueryResultTable({ data }: { data: Record<string, unknown> }) {
  const rows = Array.isArray(data.rows) ? (data.rows as Array<Record<string, unknown>>) : [];
  if (rows.length > 0) {
    const cols = Object.keys(rows[0]).map((k) => ({
      title: k,
      dataIndex: k,
      key: k,
      ellipsis: true,
      render: (v: unknown) =>
        typeof v === "object" && v !== null ? (
          <span className="mono" style={{ fontSize: 12 }}>{JSON.stringify(v)}</span>
        ) : (
          String(v ?? "")
        ),
    }));
    return (
      <div>
        <Table size="small" dataSource={rows} columns={cols} rowKey={(_, i) => String(i)} pagination={{ pageSize: 10 }} />
        <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          共 {String(data.total ?? rows.length)} 行
          {data.elapsed_ms != null ? ` · 耗时 ${data.elapsed_ms} ms` : ""}
          {data.from_cache ? " · 来自缓存" : ""}
        </div>
      </div>
    );
  }
  return <ObjectView data={data} />;
}

export function QueryWorkspace() {
  const { can } = usePermission();
  const canExecute = can("query:execute");
  // 双入口：query=指标查询（内部用户 JWT，无需令牌/客户端，展示全部已发布指标）；
  // debug=消费接入调试（选客户端→签发令牌→以接入方视角收敛与调试）。
  const [mode, setMode] = useState<"query" | "debug">("query");
  const [metricOptions, setMetricOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [metricCode, setMetricCode] = useState<string | undefined>(undefined);
  // 接入方授权范围：取「首个 ACTIVE 客户端」的域/白名单，指标下拉据此收敛——
  // 与 handleMintToken 使用同一客户端，保证「能看到的指标 = 实际能消费的指标」，
  // 从源头消除 FORBIDDEN_DOMAIN/FORBIDDEN_METRIC 403（后端仍 fail-closed 兜底）。
  const [clientScope, setClientScope] = useState<{ domain: string | null; whitelist: string[] | null }>({
    domain: null,
    whitelist: null,
  });
  // ACTIVE 客户端列表 + 用户显式选择的客户端：初始「全部指标（平台内部视角）」，
  // 仅当用户选中某客户端（或令牌已绑定某客户端）时才按其授权范围收敛——
  // 不再隐式取「首个 ACTIVE 客户端」，避免 E2E/无关客户端的 scope 绑架指标列表。
  const [clients, setClients] = useState<ClientResponse[]>([]);
  const [clientId, setClientId] = useState<string | undefined>(undefined);
  const [dateRange, setDateRange] = useState("last_30d");
  const [granularity, setGranularity] = useState<string | undefined>(undefined);
  const [comparison, setComparison] = useState<string | undefined>(undefined);
  const [dimInputs, setDimInputs] = useState<Array<{ name: string; value: string }>>([{ name: "", value: "" }]);
  const [acceptStale, setAcceptStale] = useState(false);
  const [dryRun, setDryRun] = useState<DryRunResponse | null>(null);
  const [query, setQuery] = useState<QueryResponse | null>(null);
  const [snapshots, setSnapshots] = useState<SnapshotResponse[]>([]);
  const [busy, setBusy] = useState<"dry" | "query" | "snap" | "semantic" | null>(null);
  const [tokenOk, setTokenOk] = useState(!!getConsumeToken());
  const [tokenExpiry, setTokenExpiry] = useState<number | null>(getConsumeTokenExpiry());
  const [degraded, setDegraded] = useState(false);
  const [degradedMessage, setDegradedMessage] = useState("");
  // 指标语义（只读拉取 GET /consume/metrics/{code}/semantic）抽屉状态
  const [semanticOpen, setSemanticOpen] = useState(false);
  const [semanticData, setSemanticData] = useState<DryRunResponse | null>(null);
  const { track } = useTracking();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  // 统一返回上一入口：优先回退浏览器历史（总览快捷入口等），无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  // 将预设键翻译为后端要求的 YYYY-MM-DD,YYYY-MM-DD 格式
  function translateDateRange(key: string): string {
    const today = new Date();
    // 用本地日期格式化（toISOString 是 UTC，本地凌晨会差一天）
    const fmt = (d: Date) =>
      `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    switch (key) {
      case "today":
        return fmt(today) + "," + fmt(today);
      case "last_7d":
        return fmt(new Date(today.getTime() - 7 * 86400000)) + "," + fmt(today);
      case "last_30d":
        return fmt(new Date(today.getTime() - 30 * 86400000)) + "," + fmt(today);
      case "last_90d":
        return fmt(new Date(today.getTime() - 90 * 86400000)) + "," + fmt(today);
      case "ytd":
        return today.getFullYear() + "-01-01," + fmt(today);
      case "last_365d":
        return fmt(new Date(today.getTime() - 365 * 86400000)) + "," + fmt(today);
      default:
        return key; // 已是 YYYY-MM-DD,YYYY-MM-DD 格式原样返回
    }
  }

  useEffect(() => {
    // 支持详情页「试算」入口带参直达：?metric_code=xxx 初始化指标选择
    const q = searchParams.get("metric_code");
    if (q) setMetricCode(q);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // P5（审查修复）：指标下拉改服务端搜索——初始加载前 100 条，搜索时按关键词请求
    // 初始为「指标查询」模式：展示全部已发布指标（内部用户视角），不做客户端收敛。
    loadMetricOptions("", { domain: null, whitelist: null });
    loadClients();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 加载 ACTIVE 客户端列表供「消费接入调试」选择器使用；若当前令牌已绑定某客户端
  // （JWT sub），在调试模式下自动选中该客户端并按其授权范围收敛——保证「能看到的 = 能消费的」。
  // 指标查询模式不自动收敛（内部用户看全部已发布指标）。
  function loadClients() {
    listApiClients()
      .then((list) => {
        const actives = list.filter((c) => c.status === "ACTIVE");
        setClients(actives);
        const bound = getConsumeTokenClientId();
        const matched = actives.find((c) => c.client_id === bound);
        if (matched && mode === "debug") {
          setClientId(matched.client_id);
          const scope = {
            domain: matched.scope_domain ?? null,
            whitelist: matched.metric_whitelist ?? null,
          };
          setClientScope(scope);
          loadMetricOptions("", scope);
        }
      })
      .catch(() => {});
  }

  // 双入口切换：指标查询（全量）/ 消费接入调试（按所选/令牌绑定客户端收敛）
  function handleModeChange(m: "query" | "debug") {
    setMode(m);
    if (m === "query") {
      setClientScope({ domain: null, whitelist: null });
      loadMetricOptions("", { domain: null, whitelist: null });
    } else {
      // 调试模式：优先令牌绑定的客户端，其次用户显式选择的客户端；都无则全量。
      const bound = getConsumeTokenClientId();
      const boundClient = clients.find((c) => c.client_id === bound);
      const c = boundClient ?? clients.find((x) => x.client_id === clientId);
      if (c) setClientId(c.client_id);
      const scope = { domain: c?.scope_domain ?? null, whitelist: c?.metric_whitelist ?? null };
      setClientScope(scope);
      loadMetricOptions("", scope);
    }
  }

  // 用户显式切换消费客户端：按其授权范围收敛指标下拉（未选 = 全部指标）
  function handleClientChange(id: string | undefined) {
    setClientId(id);
    const c = clients.find((x) => x.client_id === id);
    const scope = { domain: c?.scope_domain ?? null, whitelist: c?.metric_whitelist ?? null };
    setClientScope(scope);
    loadMetricOptions("", scope);
  }

  // P5：指标下拉服务端搜索（防抖 300ms；关键词为空回到前 100 条）
  const metricSearchTimer = useRef<number | null>(null);
  function loadMetricOptions(
    keyword: string,
    scope: { domain: string | null; whitelist: string[] | null } = clientScope,
  ) {
    // 消费侧只允许消费 PUBLISHED 指标：未发布（DRAFT/REVIEW）指标在后端 FORBIDDEN_METRIC，
    // 下拉直接过滤避免用户选到不可消费的指标（403 从源头消除）。
    // 同时按接入方授权范围收敛：scope_domain 非空 → 仅该域指标；metric_whitelist 非空 →
    // 仅白名单内指标；PII 指标须在白名单显式列出（与后端 _assert_authorized 同口径）。
    const wl = scope.whitelist;
    listMetrics({
      keyword: keyword || undefined,
      page_size: 100,
      status: "PUBLISHED",
      domain: scope.domain ?? undefined,
    })
      .then((res) => {
        const items = res.items.filter((m) => {
          if (wl && wl.length > 0 && !wl.includes(m.metric_code)) return false;
          if (m.pii_flag && !(wl ?? []).includes(m.metric_code)) return false;
          return true;
        });
        setMetricOptions(
          items.map((m) => ({ value: m.metric_code, label: `${m.metric_code} · ${m.name}` })),
        );
      })
      .catch(() => {});
  }
  function handleMetricSearch(kw: string) {
    if (metricSearchTimer.current !== null) window.clearTimeout(metricSearchTimer.current);
    metricSearchTimer.current = window.setTimeout(() => loadMetricOptions(kw.trim()), 300);
  }
  // 消费令牌状态与 localStorage 强一致：request() 在 401/过期时可能清除令牌，
  // 组件需实时同步，避免 UI 显示「已就绪」而实际无令牌（后续请求报 X-Api-Key 误导）。
  useEffect(() => {
    const sync = () => {
      setTokenOk(!!getConsumeToken());
      setTokenExpiry(getConsumeTokenExpiry());
    };
    sync();
    window.addEventListener(CONSUME_TOKEN_CHANGED_EVENT, sync);
    return () => window.removeEventListener(CONSUME_TOKEN_CHANGED_EVENT, sync);
  }, []);

  // 令牌过期自动失效：每分钟检查一次，过期即清除并回到「需要消费令牌」状态
  useEffect(() => {
    if (!tokenExpiry) return;
    const timer = window.setInterval(() => {
      if (tokenExpiry <= Date.now()) {
        clearConsumeToken();
        setTokenOk(false);
        setTokenExpiry(null);
      }
    }, 60000);
    return () => window.clearInterval(timer);
  }, [tokenExpiry]);

  function buildRequest(): DimensionExpr[] {
    return dimInputs.filter((d) => d.name.trim() && d.value.trim()).map((d) => ({ name: d.name.trim(), value: d.value.trim() }));
  }

  async function handleDryRun() {
    if (!metricCode) { message.warning("请选择指标"); return; }
    setBusy("dry");
    try {
      const res = await consumeDryRun({
        metric_code: metricCode,
        dimensions: buildRequest(),
        date_range: translateDateRange(dateRange),
        granularity,
        comparison,
        accept_stale: acceptStale,
      }, { forceUser: mode === "query" });
      setDryRun(res);
      setQuery(null);
      track("consume_dry_run", metricCode, "metric");
    } catch (err) {
      message.error(consumeErrorText(err, mode));
    } finally {
      setBusy(null);
    }
  }

  async function handleQuery() {
    if (!metricCode) { message.warning("请选择指标"); return; }
    setBusy("query");
    try {
      const res = await consumeQuery({
        metric_code: metricCode,
        dimensions: buildRequest(),
        date_range: translateDateRange(dateRange),
        granularity,
        comparison,
        accept_stale: acceptStale,
      }, { forceUser: mode === "query" });
      setQuery(res);
      setDryRun(null);
      track("consume_query", metricCode, "metric");
    } catch (err) {
      if (err instanceof UnisenseApiError && isDegradationError(err as unknown as UnisenseApiErrorType)) {
        const { isDegraded: isDg, message: dgMsg } = handleDegradedEngine(err as unknown as UnisenseApiErrorType);
        if (isDg) {
          setDegraded(true);
          setDegradedMessage(dgMsg);
        }
      }
      message.error(consumeErrorText(err, mode));
    } finally {
      setBusy(null);
    }
  }

  async function handleSnapshots() {
    if (!metricCode) { message.warning("请选择指标"); return; }
    setBusy("snap");
    try {
      setSnapshots(await listSnapshots(metricCode, 50, { forceUser: mode === "query" }));
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载快照失败");
    } finally {
      setBusy(null);
    }
  }

  // 指标语义：只读拉取消费侧语义（GET /consume/metrics/{code}/semantic），不执行/不写/不计费
  async function handleSemantic() {
    if (!metricCode) { message.warning("请选择指标"); return; }
    setBusy("semantic");
    try {
      const res = await consumeSemantic(metricCode, { forceUser: mode === "query" });
      setSemanticData(res);
      setSemanticOpen(true);
      track("consume_semantic", metricCode, "metric");
    } catch (err) {
      message.error(consumeErrorText(err, mode));
    } finally {
      setBusy(null);
    }
  }

  async function handleMintToken() {
    try {
      const c = clients.find((x) => x.client_id === clientId);
      if (!c) {
        message.warning("请先在下方选择消费客户端（ACTIVE 的 API 客户端）");
        return;
      }
      const { access_token } = await mintClientToken(c.client_id);
      setConsumeToken(access_token);
      setTokenOk(true);
      setTokenExpiry(getConsumeTokenExpiry());
      // 同步授权范围（与下拉收敛使用同一客户端），并刷新指标列表（域/白名单可能已变）
      const scope = { domain: c.scope_domain ?? null, whitelist: c.metric_whitelist ?? null };
      setClientScope(scope);
      loadMetricOptions("", scope);
      message.success(`已使用客户端 ${c.client_id} 签发消费令牌`);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "签发失败");
    }
  }

  const snapColumns = [
    { title: "ID", dataIndex: "id", key: "id", width: 70 },
    { title: "版本", dataIndex: "version", key: "version", width: 80, render: (v: number) => `v${v}` },
    { title: "维度", dataIndex: "dims", key: "dims", render: (v: Record<string, unknown>) => <span style={{ fontSize: 12 }}>{kvText(v)}</span> },
    { title: "时间范围", dataIndex: "date_range", key: "date_range", render: (v: string) => <span style={{ fontSize: 12 }}>{formatCnRange(v)}</span> },
    { title: "值", dataIndex: "value_json", key: "value", render: (v: Record<string, unknown>) => <span className="mono" style={{ fontSize: 12 }}>{kvText(v, VALUE_FIELD_LABEL)}</span> },
    {
      title: "质量",
      dataIndex: "quality_flag",
      key: "quality",
      width: 90,
      render: (v: string | null) => (v ? <Tag color="warning">{v}</Tag> : <Tag color="success">正常</Tag>),
    },
    { title: "来源", dataIndex: "generated_by", key: "generated_by", width: 110 },
  ];

  const tabItems = [
    {
      key: "query",
      label: "查询执行",
      children: (
        <div>
          {degraded && (
            <Alert
              type="warning"
              showIcon
              banner
              style={{ marginBottom: 16 }}
              message={degradedMessage || "查询引擎暂不可用，请稍后重试"}
              closable
              onClose={() => setDegraded(false)}
            />
          )}
          {mode === "debug" ? (
            <>
              <Alert
                type={tokenOk ? "success" : "warning"}
                showIcon
                icon={<KeyOutlined />}
                style={{ marginBottom: 16 }}
                message={tokenOk ? "消费令牌已就绪（角色 consume）" : "需要消费令牌"}
                description={
                  tokenOk ? (
                    <span>
                      当前使用客户端令牌调用 /consume/query。
                      {tokenExpiry ? ` 令牌剩余 ${Math.max(0, Math.round((tokenExpiry - Date.now()) / 60000))} 分钟。` : ""}{" "}
                      <a onClick={() => { clearConsumeToken(); setTokenOk(false); setTokenExpiry(null); }}>清除令牌</a>
                    </span>
                  ) : (
                    <span>
                      /consume/query 由 API 客户端（X-Api-Key + consume JWT）鉴权。可到「API 客户端」创建后一键签发。
                    </span>
                  )
                }
                action={
                  !tokenOk && canExecute && (
                    <Button size="small" onClick={handleMintToken}>从客户端签发令牌</Button>
                  )
                }
              />

              <Space style={{ marginBottom: 16, width: "100%" }} wrap>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  消费客户端：
                  <Select
                    style={{ width: 300, marginLeft: 8 }}
                    placeholder="全部指标（平台内部视角）"
                    allowClear
                    value={clientId}
                    onChange={handleClientChange}
                    options={clients.map((c) => ({
                      value: c.client_id,
                      label: c.scope_domain ? `${c.client_id}（域：${c.scope_domain}）` : c.client_id,
                    }))}
                  />
                </span>
                <span className="muted" style={{ fontSize: 12 }}>
                  未选择时展示全部已发布指标；选择客户端后仅展示其授权域/白名单内的指标，签发令牌也使用该客户端。
                </span>
              </Space>
            </>
          ) : (
            <Alert
              type="info"
              showIcon
              icon={<SafetyCertificateOutlined />}
              style={{ marginBottom: 16 }}
              message="指标查询（平台内部视角）"
              description="使用你的登录身份直接查询已发布指标，无需消费令牌；如需以接入方视角调试消费接口（选客户端→签发令牌→按授权域收敛），请切换到上方『消费接入调试』。"
            />
          )}

          <Form layout="vertical">
            <Row gutter={[16, 0]}>
              <Col xs={24} md={12}>
                <Form.Item label="指标">
                  <Select
                    showSearch
                    value={metricCode}
                    onChange={setMetricCode}
                    options={metricOptions}
                    placeholder="选择指标编码（输入关键词搜索）"
                    filterOption={false}
                    onSearch={handleMetricSearch}
                    notFoundContent={metricOptions.length === 0 ? "无匹配指标" : undefined}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} md={12}>
                <Form.Item label="日期范围">
                  <Select
                    value={dateRange}
                    onChange={setDateRange}
                    options={["today", "last_7d", "last_30d", "last_90d", "ytd", "last_365d"].map((v) => ({ value: v, label: DATE_RANGE_LABEL[v] ?? v }))}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} md={8}>
                <Form.Item label="粒度">
                  <Select
                    allowClear
                    value={granularity}
                    onChange={setGranularity}
                    placeholder="按日 / 周 / 月 / 季"
                    options={["day", "week", "month", "quarter"].map((v) => ({ value: v, label: GRANULARITY_LABEL[v] ?? v }))}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} md={8}>
                <Form.Item label="对比">
                  <Select
                    allowClear
                    value={comparison}
                    onChange={setComparison}
                    placeholder="MoM / YoY"
                    options={[{ value: "MoM", label: "MoM 环比" }, { value: "YoY", label: "YoY 同比" }]}
                  />
                </Form.Item>
              </Col>
              <Col xs={24} md={8}>
                <Form.Item label="允许陈旧数据">
                  <Select value={acceptStale ? "true" : "false"} onChange={(v) => setAcceptStale(v === "true")} options={[{ value: "false", label: "否" }, { value: "true", label: "是" }]} />
                </Form.Item>
              </Col>
            </Row>

            <Form.Item label="维度过滤（名称 / 值）">
              <Space direction="vertical" style={{ width: "100%" }}>
                {dimInputs.map((d, i) => (
                  <Space key={i} style={{ display: "flex" }}>
                    <Input
                      placeholder="维度名（如 channel）"
                      className="mono"
                      value={d.name}
                      style={{ width: 220 }}
                      onChange={(e) => setDimInputs((prev) => prev.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
                    />
                    <Input
                      placeholder="维度值（如 app）"
                      className="mono"
                      value={d.value}
                      style={{ width: 220 }}
                      onChange={(e) => setDimInputs((prev) => prev.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                    />
                    <Button
                      type="text"
                      danger
                      disabled={dimInputs.length === 1}
                      onClick={() => setDimInputs((prev) => prev.filter((_, j) => j !== i))}
                    >
                      删除
                    </Button>
                  </Space>
                ))}
                <Button size="small" onClick={() => setDimInputs((prev) => [...prev, { name: "", value: "" }])}>
                  + 添加维度
                </Button>
              </Space>
            </Form.Item>

            <Space>
              <Button icon={<SafetyCertificateOutlined />} onClick={handleDryRun} loading={busy === "dry"} disabled={!canExecute}>
                语义校验（dry-run）
              </Button>
              <Button type="primary" icon={<PlayCircleOutlined />} onClick={handleQuery} loading={busy === "query"} disabled={!canExecute}>
                执行查询
              </Button>
              <Button icon={<DatabaseOutlined />} onClick={handleSnapshots} loading={busy === "snap"}>
                加载快照
              </Button>
              <Button icon={<ReadOutlined />} onClick={handleSemantic} loading={busy === "semantic"}>
                指标语义
              </Button>
            </Space>
          </Form>

          {dryRun && (
            <div style={{ marginTop: 20 }}>
              <Alert
                type={dryRun.status === "ok" ? "success" : "error"}
                showIcon
                message={`语义校验：${dryRun.status === "ok" ? "通过" : "被拒绝"}`}
                style={{ marginBottom: 12 }}
              />
              {dryRun.checks && dryRun.checks.length > 0 && (
                <Card size="small" title="校验项" style={{ marginBottom: 12 }}>
                  {dryRun.checks.map((c, i) => {
                    const ok = (c as { ok?: boolean }).ok;
                    const checkName = CHECK_LABEL[String((c as { check?: string }).check ?? "")] ?? String((c as { check?: string }).check ?? "校验");
                    const detail = c.detail != null ? String(c.detail) : "";
                    return (
                      <div key={i} style={{ marginBottom: 4 }}>
                        <Tag color={ok ? "success" : "error"}>{ok ? "通过" : "未通过"}</Tag>
                        <span style={{ fontSize: 13 }}>{checkName}</span>
                        {detail && <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>{detail}</span>}
                      </div>
                    );
                  })}
                </Card>
              )}
              <Card size="small" title="执行计划" style={{ marginBottom: 12 }}>
                <PlanView plan={dryRun.execution_plan} />
              </Card>
              <Card size="small" title="元信息">
                <PlanView plan={dryRun.meta} />
              </Card>
            </div>
          )}

          {query && (
            <div style={{ marginTop: 20 }}>
              <Alert
                type={query.degraded ? "warning" : "success"}
                showIcon
                message={query.degraded ? "查询降级执行（部分能力不可用）" : "查询成功"}
                style={{ marginBottom: 12 }}
              />
              {query.data ? (
                <Card size="small" title="查询结果" style={{ marginBottom: 12 }}>
                  <QueryResultTable data={query.data} />
                </Card>
              ) : (
                <Alert type="info" message="无结果数据" style={{ marginBottom: 12 }} />
              )}
              <Card size="small" title="执行计划" style={{ marginBottom: 12 }}>
                <PlanView plan={query.execution_plan} />
              </Card>
              <Card size="small" title="元信息">
                <PlanView plan={query.meta} />
              </Card>
            </div>
          )}

          {snapshots.length > 0 && (
            <Card size="small" title={`快照（${snapshots.length}）`} style={{ marginTop: 20 }}>
              <Table dataSource={snapshots} columns={snapColumns} rowKey="id" size="small" pagination={{ pageSize: 10 }} />
            </Card>
          )}

          <Drawer
            title={`指标语义：${semanticData?.metric_code ?? metricCode ?? ""}`}
            open={semanticOpen}
            onClose={() => setSemanticOpen(false)}
            width={680}
          >
            {semanticData ? (
              <>
                <Alert
                  type={semanticData.status === "ok" ? "success" : "error"}
                  showIcon
                  message={`语义校验：${semanticData.status === "ok" ? "通过" : "被拒绝"}`}
                  style={{ marginBottom: 12 }}
                />
                {semanticData.checks && semanticData.checks.length > 0 && (
                  <Card size="small" title="校验项" style={{ marginBottom: 12 }}>
                    {semanticData.checks.map((c, i) => {
                      const ok = (c as { ok?: boolean }).ok;
                      const checkName = CHECK_LABEL[String((c as { check?: string }).check ?? "")] ?? String((c as { check?: string }).check ?? "校验");
                      const detail = c.detail != null ? String(c.detail) : "";
                      return (
                        <div key={i} style={{ marginBottom: 4 }}>
                          <Tag color={ok ? "success" : "error"}>{ok ? "通过" : "未通过"}</Tag>
                          <span style={{ fontSize: 13 }}>{checkName}</span>
                          {detail && <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>{detail}</span>}
                        </div>
                      );
                    })}
                  </Card>
                )}
                <Card size="small" title="执行计划" style={{ marginBottom: 12 }}>
                  <PlanView plan={semanticData.execution_plan} />
                </Card>
                <Card size="small" title="元信息">
                  <PlanView plan={semanticData.meta} />
                </Card>
              </>
            ) : (
              <Empty description="暂无语义数据" />
            )}
          </Drawer>
        </div>
      ),
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">Consumption / Query</div>
          <h2>查询工作台</h2>
          <p>基于指标语义的查询——先 dry-run 校验口径，再安全执行。</p>
        </div>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
            <Segmented
              value={mode}
              onChange={(v) => handleModeChange(v as "query" | "debug")}
              options={[
                { label: "指标查询", value: "query", icon: <SearchOutlined /> },
                { label: "消费接入调试", value: "debug", icon: <ApiOutlined /> },
              ]}
            />
            <Tag
              color={mode === "query" ? "blue" : "purple"}
              style={{ marginInlineEnd: 0, fontSize: 12 }}
            >
              {mode === "query" ? "内部用户 · 免令牌" : "模拟接入方"}
            </Tag>
          </div>
          <div className="muted" style={{ fontSize: 12, textAlign: "right", maxWidth: 420 }}>
            {mode === "query"
              ? "以你的登录身份直查全部已发布指标，无需消费令牌与客户端。"
              : "选择 API 客户端 → 签发令牌 → 以接入方视角调试（按授权域/白名单收敛）。"}
          </div>
        </div>
      </div>
      <Card><Tabs items={tabItems} /></Card>
    </div>
  );
}
