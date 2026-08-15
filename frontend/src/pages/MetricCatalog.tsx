import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Table, Input, Select, Button, Space, Tag, message, Tooltip, Descriptions, Drawer, Dropdown, Modal } from "antd";
import {
  ArrowLeftOutlined,
  SearchOutlined,
  ColumnWidthOutlined,
  PlusCircleOutlined,
  FileTextOutlined,
  DownloadOutlined,
  HeartOutlined,
  HeartFilled,
  UserOutlined,
  DeleteOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  ExclamationCircleOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  fetchCurrentUser,
  fetchDashboard,
  listDomainTree,
  listMetrics,
  listUsers,
  listFavorites,
  addFavorite,
  removeFavorite,
  submitReview,
  deleteMetric,
  UnisenseApiError,
} from "../api";
import type { MetricResponse, SubjectDomainTreeNode } from "../types";
import type { ColumnsType } from "antd/es/table";
import { useTracking } from "../hooks/useTracking";
import {
  AGGREGATION_LABEL,
  DW_LAYER_LABEL,
  FRESHNESS_LABEL,
  GRANULARITY_LABEL,
  METRIC_TYPE_LABEL,
  METRIC_TIER_LABEL,
  TIME_SEMANTICS_LABEL,
} from "../utils/enums";
import { formatCnTime } from "../utils/timeCn";

const STATUS_COLOR: Record<string, string> = {
  DRAFT: "default",
  EXPERIMENTAL: "processing",
  REVIEW: "warning",
  PUBLISHED: "success",
  DEPRECATED: "error",
};

const STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  EXPERIMENTAL: "实验",
  REVIEW: "审核",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
  DATA_SOURCE_DROPPED: "数据源下线",
};

// 健康度分级（backend metric_health_score：>=85 EXCELLENT / >=70 GOOD / >=55 WARNING / <55 CRITICAL）
const HEALTH_LABEL: Record<string, string> = {
  EXCELLENT: "优秀",
  GOOD: "良好",
  WARNING: "警告",
  CRITICAL: "严重",
};
const HEALTH_COLOR: Record<string, string> = {
  EXCELLENT: "green",
  GOOD: "blue",
  WARNING: "orange",
  CRITICAL: "red",
};

const TIER_OPTIONS = ["T1", "T2", "T3"].map((v) => ({ value: v, label: METRIC_TIER_LABEL[v] ?? v }));
const SORT_OPTIONS = [
  { value: "updated_at", label: "按更新时间" },
  { value: "created_at", label: "按创建时间" },
  { value: "version", label: "按版本号" },
  { value: "metric_code", label: "按编码" },
];

// 生命周期快筛预设
const LIFECYCLE_PRESETS = [
  { key: "created_7d", label: "最近7天创建", icon: <PlusCircleOutlined /> },
  { key: "stale_30d", label: "30天未更新", icon: <ClockCircleOutlined /> },
  { key: "deprecating", label: "即将废弃", icon: <ExclamationCircleOutlined /> },
];

// 递归展平主题域树 → code → 中文名 映射
function flattenDomains(nodes: SubjectDomainTreeNode[], acc: Map<string, string>) {
  for (const n of nodes) {
    acc.set(n.code, n.name);
    if (n.children?.length) flattenDomains(n.children, acc);
  }
}

// 口径摘要：聚合(字段) · 粒度 · 单位
function calibreSummary(r: MetricResponse): string {
  const agg = AGGREGATION_LABEL[r.aggregation] ?? r.aggregation;
  const gran = GRANULARITY_LABEL[r.granularity] ?? r.granularity;
  return `${agg} · ${gran} · ${r.unit}`;
}

// 展开行：完整口径定义 + 治理追溯
function ExpandContent({
  r,
  userName,
  domainName,
}: {
  r: MetricResponse;
  userName: (id: number | null | undefined) => string;
  domainName: (code: string) => string;
}) {
  const def = r.definition_json ?? {};
  const expression = typeof def.expression === "string" ? def.expression : undefined;
  const definition = typeof def.definition === "string" ? def.definition : undefined;
  const dependencies = Array.isArray(def.dependencies) ? def.dependencies.map((d) => String(d)) : [];
  const rawSource = def.source_fields ?? def.source_columns;
  const sourceFields = Array.isArray(rawSource) ? rawSource.map((s) => String(s)) : rawSource ? [String(rawSource)] : [];
  const sourceTables = Array.isArray(def.source_tables)
    ? def.source_tables.map((s) => String(s))
    : def.source_tables
      ? [String(def.source_tables)]
      : [];
  // 口径 SQL：兼容多种键名（etl_sql / sql / calculation_sql / query_sql / sql_template）
  const rawEtl = def.etl_sql ?? def.sql ?? def.calculation_sql ?? def.query_sql ?? def.sql_template;
  const etlSql = rawEtl == null ? "" : String(rawEtl);

  return (
    <div style={{ padding: "4px 8px" }}>
      <Descriptions column={2} size="small" bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label="业务域">{domainName(r.domain)}</Descriptions.Item>
        <Descriptions.Item label="指标类型">{METRIC_TYPE_LABEL[r.type] ?? r.type}</Descriptions.Item>
        <Descriptions.Item label="责任人">{userName(r.owner_id)}</Descriptions.Item>
        <Descriptions.Item label="备份责任人">{userName(r.backup_owner_id)}</Descriptions.Item>
        <Descriptions.Item label="提交人">{userName(r.submitted_by)}</Descriptions.Item>
        <Descriptions.Item label="审批人">{userName(r.approver_id)}</Descriptions.Item>
        <Descriptions.Item label="创建时间">
          <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(r.created_at)}</span>
        </Descriptions.Item>
        <Descriptions.Item label="数据分层">{DW_LAYER_LABEL[r.dw_layer] ?? r.dw_layer}</Descriptions.Item>
        <Descriptions.Item label="更新时效">{FRESHNESS_LABEL[r.freshness] ?? r.freshness}</Descriptions.Item>
        <Descriptions.Item label="时间语义">{TIME_SEMANTICS_LABEL[r.time_semantics] ?? r.time_semantics}</Descriptions.Item>
      </Descriptions>
      {definition && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">指标定义：</span>
          {definition}
        </p>
      )}
      {expression && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">计算口径：</span>
          <code className="mono">{expression}</code>
        </p>
      )}
      {sourceTables.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">关联数据表：</span>
          {sourceTables.map((t) => (
            <Tag key={t} className="mono">{t}</Tag>
          ))}
        </p>
      )}
      {dependencies.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">依赖指标：</span>
          {dependencies.map((d) => (
            <Tag key={d}>{d}</Tag>
          ))}
        </p>
      )}
      {sourceFields.length > 0 && (
        <p style={{ margin: "0 0 8px" }}>
          <span className="muted">来源字段：</span>
          {sourceFields.map((s) => (
            <Tag key={s}>{s}</Tag>
          ))}
        </p>
      )}
      {etlSql && (
        <div style={{ margin: "0 0 8px" }}>
          <span className="muted">口径 SQL：</span>
          <pre
            style={{
              background: "var(--paper)",
              padding: 8,
              borderRadius: 4,
              margin: "4px 0 0",
              fontSize: 12,
              overflow: "auto",
              maxHeight: 200,
            }}
          >
            {etlSql}
          </pre>
        </div>
      )}
      <details>
        <summary className="muted" style={{ cursor: "pointer" }}>完整口径 JSON</summary>
        <pre
          style={{
            background: "var(--paper)",
            padding: 8,
            borderRadius: 4,
            margin: "8px 0 0",
            fontSize: 12,
            overflow: "auto",
            maxHeight: 240,
          }}
        >
          {JSON.stringify(def, null, 2)}
        </pre>
      </details>
    </div>
  );
}

export function MetricCatalog() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const { track } = useTracking();
  const urlKw = searchParams.get("kw") ?? "";
  const urlStatus = searchParams.get("status") ?? "";
  const [items, setItems] = useState<MetricResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [keyword, setKeyword] = useState(urlKw);
  const [status, setStatus] = useState(urlStatus);
  const [domain, setDomain] = useState("");
  const [tier, setTier] = useState("");
  const [sortBy, setSortBy] = useState<"updated_at" | "created_at" | "version" | "metric_code" | "name">("updated_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [domainOptions, setDomainOptions] = useState<Array<{ value: string; label: string }>>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [selected, setSelected] = useState<MetricResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [userMap, setUserMap] = useState<Map<number, string>>(new Map());
  const [domainMap, setDomainMap] = useState<Map<string, string>>(new Map());
  const [currentUserId, setCurrentUserId] = useState<number | undefined>(undefined);
  const [myMetricsOnly, setMyMetricsOnly] = useState(false);
  const [lifecycleFilter, setLifecycleFilter] = useState<string | null>(null);
  const [favorites, setFavorites] = useState<Set<string>>(new Set());
  // 只看收藏：客户端过滤当前页（后端 list 无收藏过滤参数）
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  // 批量操作确认弹窗：null=关闭 / submit=批量提交审核 / delete=批量删除
  const [batchAction, setBatchAction] = useState<"submit" | "delete" | null>(null);
  const [batchBusy, setBatchBusy] = useState(false);
  // 预览抽屉
  const [previewMetric, setPreviewMetric] = useState<MetricResponse | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const loadSeq = useRef(0);

  // 域列表
  useEffect(() => {
    fetchDashboard()
      .then((d) => setDomainOptions(Object.keys(d.by_domain ?? {}).map((v) => ({ value: v, label: v }))))
      .catch(() => setDomainOptions([]));
  }, []);

  // 用户/域中文名映射
  useEffect(() => {
    Promise.all([
      listUsers().then((u) => setUserMap(new Map(u.map((x) => [x.id, x.display_name || x.username])))),
      listDomainTree().then((tree) => {
        const m = new Map<string, string>();
        flattenDomains(tree, m);
        setDomainMap(m);
      }),
      fetchCurrentUser().then((u) => setCurrentUserId(u.id)).catch(() => {}),
      listFavorites()
        .then((favs) =>
          setFavorites(
            new Set(favs.filter((f) => f.asset_type === "METRIC").map((f) => f.asset_id)),
          ),
        )
        .catch(() => {}),
    ]).catch(() => {});
  }, []);

  const userName = useMemo(
    () => (id: number | null | undefined) => (id == null ? "—" : (userMap.get(id) ?? `#${id}`)),
    [userMap],
  );
  const domainName = useMemo(
    () => (code: string) => (code ? (domainMap.get(code) ?? code) : "—"),
    [domainMap],
  );
  // 域筛选下拉选项也使用中文名
  const domainFilterOptions = useMemo(
    () => domainOptions.map((d) => ({ value: d.value, label: domainName(d.value) })),
    [domainOptions, domainName],
  );

  useEffect(() => {
    if (urlKw && urlKw !== keyword) setKeyword(urlKw);
    if (urlStatus && urlStatus !== status) setStatus(urlStatus);
    if (urlKw || urlStatus) setPage(1);
  }, [urlKw, urlStatus]);

  async function load() {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listMetrics({
        keyword,
        status,
        domain: domain || undefined,
        metric_tier: tier || undefined,
        owner_id: myMetricsOnly ? currentUserId : undefined,
        sort_by: sortBy,
        sort_order: sortOrder,
        page,
        page_size: pageSize,
      });
      if (seq !== loadSeq.current) return;
      setItems(res.items);
      setTotal(res.total);
      setSelected([]);
    } catch (err) {
      if (seq !== loadSeq.current) return;
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败",
      );
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, [page, pageSize, status, domain, tier, sortBy, sortOrder, myMetricsOnly, currentUserId]);

  function handleSearch() {
    if (keyword) {
      track("metric_search", undefined, "metric", { keyword });
    }
    setPage(1);
    load();
  }

  // 统一返回上一入口：优先按来源标记精确返回（总览仪表/推荐流/血缘视图等入口），
  // 无来源标记时回退浏览器历史（历史栈有上一页才回退），URL 直达则兜底总览仪表。
  // 说明：SPA 中 window.history.length 是跨站点累计的（含浏览器历史），不能作为
  // "是否有上一页"的可靠判据，故来源标记优先于 history.length 判断。
  function handleBack() {
    const from = (location.state as { from?: string } | null)?.from;
    if (from === "dashboard" || from === "recommend") {
      navigate("/dashboard");
      return;
    }
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  function handleLifecycle(key: string) {
    if (key === "created_7d") {
      setKeyword("");
      setStatus("");
      setLifecycleFilter(key);
      // 后端不支持按创建时间筛，用排序引导
      setSortBy("created_at");
      setSortOrder("desc");
    } else if (key === "stale_30d") {
      setKeyword("");
      setStatus("");
      setLifecycleFilter(key);
      setSortBy("updated_at");
      setSortOrder("asc");
    } else if (key === "deprecating") {
      setKeyword("");
      setStatus("DEPRECATED");
      setLifecycleFilter(key);
    }
    setPage(1);
  }

  // 收藏切换（心形列）
  async function toggleFavorite(code: string) {
    const fav = favorites.has(code);
    try {
      if (fav) {
        await removeFavorite("METRIC", code);
        setFavorites((prev) => {
          const next = new Set(prev);
          next.delete(code);
          return next;
        });
        message.success("已取消收藏");
      } else {
        await addFavorite("METRIC", code);
        setFavorites((prev) => new Set(prev).add(code));
        message.success("已收藏");
      }
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "收藏操作失败");
    }
  }

  // 只看收藏：客户端过滤当前页
  const displayItems = useMemo(
    () => (favoritesOnly ? items.filter((m) => favorites.has(m.metric_code)) : items),
    [items, favoritesOnly, favorites],
  );

  // 批量操作执行：逐条提交审核 / 删除，收集成功与失败明细
  async function runBatch() {
    if (!batchAction || !selected.length) return;
    const targets = selected.filter((m) => m.status === "DRAFT");
    if (!targets.length) {
      message.warning("勾选的指标中没有草稿状态可操作");
      setBatchAction(null);
      return;
    }
    setBatchBusy(true);
    let ok = 0;
    const errors: string[] = [];
    for (const m of targets) {
      try {
        if (batchAction === "submit") await submitReview(m.metric_code);
        else await deleteMetric(m.metric_code);
        ok += 1;
      } catch (err) {
        errors.push(
          `${m.metric_code}: ${err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "失败"}`,
        );
      }
    }
    setBatchBusy(false);
    setBatchAction(null);
    if (ok) message.success(`${batchAction === "submit" ? "提交审核" : "删除"}成功 ${ok} 个`);
    if (errors.length) message.error(errors.slice(0, 3).join("；"));
    setSelected([]);
    load();
  }

  function exportCsv() {
    const header = [
      "metric_code", "name", "domain", "owner_id", "type", "status",
      "aggregation", "granularity", "unit", "dw_layer", "metric_tier",
      "pii_flag", "version", "created_at", "updated_at",
    ];
    const rows = items.map((m) =>
      [
        m.metric_code, m.name, m.domain, m.owner_id, m.type, m.status,
        m.aggregation, m.granularity, m.unit, m.dw_layer, m.metric_tier,
        m.pii_flag ? "PII" : "", m.version, formatCnTime(m.created_at), formatCnTime(m.updated_at),
      ]
        .map((c) => `"${String(c).replace(/"/g, '""')}"`)
        .join(","),
    );
    const blob = new Blob([[header.join(","), ...rows].join("\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `metric-catalog-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const columns: ColumnsType<MetricResponse> = [
    {
      title: "编码",
      dataIndex: "metric_code",
      key: "metric_code",
      width: 190,
      render: (text: string) => (
        <Button type="link" style={{ padding: 0 }} onClick={(e) => { e.stopPropagation(); navigate(`/detail/${text}`); }}>
          {text}
        </Button>
      ),
    },
    { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
    {
      title: "收藏",
      key: "fav",
      width: 56,
      align: "center",
      render: (_: unknown, r: MetricResponse) => (
        <Button
          type="text"
          size="small"
          aria-label={favorites.has(r.metric_code) ? "取消收藏" : "收藏"}
          icon={
            favorites.has(r.metric_code) ? (
              <HeartFilled style={{ color: "#eb2f96" }} />
            ) : (
              <HeartOutlined />
            )
          }
          onClick={(e) => {
            e.stopPropagation();
            toggleFavorite(r.metric_code);
          }}
        />
      ),
    },
    {
      title: "业务域",
      dataIndex: "domain",
      key: "domain",
      width: 110,
      render: (v: string) => domainName(v),
    },
    {
      title: "责任人",
      key: "owner",
      width: 110,
      ellipsis: true,
      render: (_: unknown, r: MetricResponse) => userName(r.owner_id),
    },
    { title: "类型", dataIndex: "type", key: "type", width: 90, render: (v: string) => METRIC_TYPE_LABEL[v] ?? v },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (s: string) => (
        <Tag color={STATUS_COLOR[s]}>{STATUS_LABEL[s] ?? s}</Tag>
      ),
    },
    {
      title: "口径摘要",
      key: "calibre",
      width: 180,
      ellipsis: true,
      render: (_: unknown, r: MetricResponse) => {
        const def = r.definition_json ?? {};
        const expr = typeof def.expression === "string" ? def.expression : undefined;
        const text = calibreSummary(r);
        return expr ? (
          <Tooltip title={`计算口径：${expr}`}>
            <span style={{ fontSize: 12 }}>{text}</span>
          </Tooltip>
        ) : (
          <span style={{ fontSize: 12 }}>{text}</span>
        );
      },
    },
    {
      title: "分层",
      dataIndex: "dw_layer",
      key: "dw_layer",
      width: 110,
      render: (v: string) => DW_LAYER_LABEL[v] ?? v,
    },
    { title: "分级", dataIndex: "metric_tier", key: "tier", width: 70, render: (v: string) => <Tag>{METRIC_TIER_LABEL[v] ?? v}</Tag> },
    {
      title: "治理徽章",
      key: "badges",
      width: 220,
      render: (_: unknown, r: MetricResponse) => (
        <Space size={4} wrap>
          {r.pii_flag && (
            <Tag color={r.compliance_reviewed ? "green" : "orange"}>{r.compliance_reviewed ? "PII 已复核" : "PII 待复核"}</Tag>
          )}
          {r.emergency_publish && <Tag color="volcano">紧急</Tag>}
          {r.pending_conflict && <Tag color="orange">冲突</Tag>}
          {r.pending_version && <Tag color="purple" icon={<ThunderboltOutlined />}>版本待确认</Tag>}
          {r.gray_tenant_ids && r.gray_tenant_ids.length > 0 && <Tag color="purple">灰度</Tag>}
          {!r.pii_flag && !r.emergency_publish && !r.pending_conflict && !r.pending_version && !r.gray_tenant_ids && <span className="muted">—</span>}
        </Space>
      ),
    },
    {
      title: "健康",
      key: "health",
      width: 90,
      render: (_: unknown, r: MetricResponse) =>
        r.health_level ? (
          <Tooltip title={`健康分 ${r.health_score ?? 0}/100（${HEALTH_LABEL[r.health_level] ?? r.health_level}）`}>
            <Tag color={HEALTH_COLOR[r.health_level]}>{HEALTH_LABEL[r.health_level] ?? r.health_level}</Tag>
          </Tooltip>
        ) : (
          <span className="muted">未评分</span>
        ),
    },
    {
      title: "版本",
      dataIndex: "version",
      key: "version",
      width: 70,
      render: (v: number) => `v${v}`,
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      key: "updated_at",
      width: 170,
      render: (v: string) => <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span>,
    },
  ];

  const hasFilter = Boolean(keyword || status || domain || tier || myMetricsOnly || lifecycleFilter || favoritesOnly);
  const emptyGuide = useMemo(
    () => (
      <div style={{ padding: "16px 0", textAlign: "center" }}>
        <p className="muted">{hasFilter ? "没有匹配的指标，试试放宽筛选条件" : "目录还是空的，创建第一个指标或从模板开始"}</p>
        <Space>
          <Button type="primary" icon={<PlusCircleOutlined />} onClick={() => navigate("/create")}>
            创建指标
          </Button>
          <Button icon={<FileTextOutlined />} onClick={() => navigate("/templates")}>
            从模板创建
          </Button>
        </Space>
      </div>
    ),
    [hasFilter],
  );

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">Assets / Catalog</div>
          <h2>指标目录</h2>
          <p>全量指标定义——按状态/域/分级/关键词检索；展开行查看口径与治理追溯。</p>
        </div>
        <Space wrap>
          <Button type="primary" icon={<PlusCircleOutlined />} onClick={() => navigate("/create")}>
            创建指标
          </Button>
          <Button icon={<FileTextOutlined />} onClick={() => navigate("/templates")}>
            从模板创建
          </Button>
          <Tooltip title="将当前筛选结果导出为 CSV">
            <Button icon={<DownloadOutlined />} onClick={exportCsv} disabled={!items.length}>
              导出
            </Button>
          </Tooltip>
          <Button
            type="primary"
            icon={<ColumnWidthOutlined />}
            disabled={selected.length !== 2}
            onClick={() => selected.length === 2 && navigate(`/compare?a=${selected[0].metric_code}&b=${selected[1].metric_code}`)}
          >
            对比所选
          </Button>
          <Dropdown
            menu={{
              items: [
                {
                  key: "submit",
                  label: "批量提交审核（草稿）",
                  icon: <CheckCircleOutlined />,
                  disabled: !selected.some((m) => m.status === "DRAFT"),
                },
                { type: "divider" },
                {
                  key: "delete",
                  label: "批量删除（草稿）",
                  icon: <DeleteOutlined />,
                  danger: true,
                  disabled: !selected.some((m) => m.status === "DRAFT"),
                },
              ],
              onClick: ({ key }) => setBatchAction(key as "submit" | "delete"),
            }}
            trigger={["click"]}
          >
            <Button icon={<ThunderboltOutlined />} disabled={!selected.length}>
              批量操作
            </Button>
          </Dropdown>
        </Space>
      </div>

      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          placeholder="搜索指标名/编码"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onPressEnter={handleSearch}
          prefix={<SearchOutlined />}
          style={{ width: 220 }}
        />
        <Button type="primary" onClick={handleSearch} icon={<SearchOutlined />}>
          搜索
        </Button>
        <Select
          value={domain || undefined}
          onChange={(v) => { setDomain(v || ""); setPage(1); }}
          style={{ width: 130 }}
          allowClear
          placeholder="全部域"
          options={domainFilterOptions}
        />
        <Select
          value={status || undefined}
          onChange={(v) => { setStatus(v || ""); setPage(1); }}
          style={{ width: 130 }}
          allowClear
          placeholder="全部状态"
          options={[
            { value: "DRAFT", label: "草稿" },
            { value: "EXPERIMENTAL", label: "实验" },
            { value: "REVIEW", label: "审核" },
            { value: "PUBLISHED", label: "已发布" },
            { value: "DEPRECATED", label: "已废弃" },
          ]}
        />
        <Select
          value={tier || undefined}
          onChange={(v) => { setTier(v || ""); setPage(1); }}
          style={{ width: 110 }}
          allowClear
          placeholder="全部分级"
          options={TIER_OPTIONS}
        />
        <Select
          value={sortBy}
          onChange={setSortBy}
          style={{ width: 130 }}
          options={SORT_OPTIONS}
        />
        <Button
          size="small"
          type={sortOrder === "asc" ? "primary" : "default"}
          onClick={() => setSortOrder((o) => (o === "asc" ? "desc" : "asc"))}
        >
          {sortOrder === "asc" ? "升序 ↑" : "降序 ↓"}
        </Button>
        {currentUserId && (
          <Button
            type={myMetricsOnly ? "primary" : "default"}
            icon={<UserOutlined />}
            onClick={() => setMyMetricsOnly(!myMetricsOnly)}
          >
            {myMetricsOnly ? "我的指标" : "全部指标"}
          </Button>
        )}
        <Button
          type={favoritesOnly ? "primary" : "default"}
          icon={favoritesOnly ? <HeartFilled style={{ color: "#eb2f96" }} /> : <HeartOutlined />}
          onClick={() => setFavoritesOnly(!favoritesOnly)}
        >
          {favoritesOnly ? "只看收藏" : "我的收藏"}
        </Button>
        {LIFECYCLE_PRESETS.map((p) => (
          <Button
            key={p.key}
            size="small"
            type={lifecycleFilter === p.key ? "primary" : "default"}
            icon={p.icon}
            onClick={() => {
              if (lifecycleFilter === p.key) {
                setLifecycleFilter(null);
                setStatus("");
                setSortBy("updated_at");
                setPage(1);
              } else {
                handleLifecycle(p.key);
              }
            }}
          >
            {p.label}
          </Button>
        ))}
        <span className="muted">共 {total} 条</span>
      </Space>

      <Table
        dataSource={displayItems}
        columns={columns}
        rowKey="metric_code"
        loading={loading}
        rowSelection={{
          selectedRowKeys: selected.map((s) => s.metric_code),
          onChange: (_, rows) => setSelected(rows),
        }}
        expandable={{
          expandedRowRender: (r) => <ExpandContent r={r} userName={userName} domainName={domainName} />,
        }}
        scroll={{ x: 1500 }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: [10, 20, 50, 100],
          onChange: (p, ps) => { setPage(p); setPageSize(ps); },
          showTotal: (t) => `共 ${t} 条`,
        }}
        onRow={(record) => ({
          onClick: () => {
            setPreviewMetric(record);
            setPreviewOpen(true);
          },
          style: { cursor: "pointer" },
        })}
        locale={{ emptyText: emptyGuide }}
      />

      <Drawer
        title={previewMetric ? `${previewMetric.metric_code} · ${previewMetric.name}` : "指标预览"}
        open={previewOpen}
        onClose={() => setPreviewOpen(false)}
        width={600}
        extra={
          previewMetric ? (
            <Button type="primary" onClick={() => { setPreviewOpen(false); navigate(`/detail/${previewMetric.metric_code}`); }}>
              查看完整详情 →</Button>
          ) : null
        }
      >
        {previewMetric && (
          <ExpandContent r={previewMetric} userName={userName} domainName={domainName} />
        )}
      </Drawer>

      <Modal
        title={batchAction === "submit" ? "批量提交审核" : "批量删除草稿"}
        open={batchAction !== null}
        confirmLoading={batchBusy}
        onOk={runBatch}
        onCancel={() => setBatchAction(null)}
        okText={batchAction === "submit" ? "提交" : "删除"}
        okButtonProps={{ danger: batchAction === "delete" }}
      >
        {batchAction === "submit" ? (
          <p>
            将勾选的 <b>{selected.filter((m) => m.status === "DRAFT").length}</b> 个草稿指标提交审核
            （DRAFT → REVIEW）。非草稿状态的勾选项将被跳过。
          </p>
        ) : (
          <p>
            将删除勾选的 <b>{selected.filter((m) => m.status === "DRAFT").length}</b> 个草稿指标
            （软删除，仅 platform_admin 可执行）。此操作不可恢复。
          </p>
        )}
      </Modal>
    </div>
  );
}