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
  batchApproveMetrics,
  batchRejectMetrics,
  batchDeprecateMetrics,
  UnisenseApiError,
} from "../api";
import type { MetricResponse, SubjectDomainTreeNode } from "../types";
import type { ColumnsType } from "antd/es/table";
import { useTracking } from "../hooks/useTracking";
import { usePermission } from "../hooks/usePermission";
import { usePersistentPageSize } from "../hooks/usePersistentPageSize";
import {
  AGGREGATION_LABEL,
  DW_LAYER_LABEL,
  FRESHNESS_LABEL,
  GRANULARITY_LABEL,
  METRIC_TYPE_LABEL,
  METRIC_TIER_LABEL,
  TIME_SEMANTICS_LABEL,
  UNIT_LABEL,
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
  REVIEW: "审核中",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
  DATA_SOURCE_DROPPED: "数据源下线",
};

// 批量操作动作中文名（结果提示用）
const BATCH_ACTION_LABEL: Record<string, string> = {
  submit: "提交审核",
  delete: "删除",
  approve: "通过",
  reject: "打回",
  deprecate: "下线",
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

// 递归展平主题域树 → code → 中文名 映射（同时记录 status 供停用域标识）
function flattenDomains(nodes: SubjectDomainTreeNode[], acc: Map<string, string>) {
  for (const n of nodes) {
    acc.set(n.code, n.name);
    if (n.children?.length) flattenDomains(n.children, acc);
  }
}
// 递归收集 code → status（active/inactive），供域下拉标识停用域
function collectDomainStatus(nodes: SubjectDomainTreeNode[], acc: Map<string, string>) {
  for (const n of nodes) {
    if (n.status) acc.set(n.code, n.status);
    if (n.children?.length) collectDomainStatus(n.children, acc);
  }
}

// 口径摘要：聚合(字段) · 粒度 · 单位
function calibreSummary(r: MetricResponse): string {
  const agg = AGGREGATION_LABEL[r.aggregation] ?? r.aggregation;
  const gran = GRANULARITY_LABEL[r.granularity] ?? r.granularity;
  const unit = UNIT_LABEL[r.unit] ?? r.unit;
  return `${agg} · ${gran} · ${unit}`;
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
  const [searchParams, setSearchParams] = useSearchParams();
  const { track } = useTracking();
  const urlKw = searchParams.get("kw") ?? "";
  const urlStatus = searchParams.get("status") ?? "";
  const urlOwnerId = searchParams.get("owner_id") ?? "";
  const urlDomain = searchParams.get("domain") ?? "";
  const urlTier = searchParams.get("tier") ?? "";
  const urlLifecycle = searchParams.get("lifecycle") ?? "";
  // URL 同步筛选状态（replace 模式，不产生历史堆栈）：业务域/分级/生命周期快筛也进 URL，
  // 让"销售域+已发布+最近7天创建"这类筛选视图可分享、刷新保持（TD §3 协作体验）
  const [items, setItems] = useState<MetricResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [keyword, setKeyword] = useState(urlKw);
  const [status, setStatus] = useState(urlStatus);
  const [ownerFilter, setOwnerFilter] = useState(urlOwnerId);
  const [domain, setDomain] = useState(urlDomain);
  const [tier, setTier] = useState(urlTier);
  const [sortBy, setSortBy] = useState<"updated_at" | "created_at" | "version" | "metric_code" | "name">("updated_at");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [domainOptions, setDomainOptions] = useState<Array<{ value: string; label: string }>>([]);
  // 主题域 code → status（active/inactive），供域下拉标识停用域
  const [domainStatusMap, setDomainStatusMap] = useState<Map<string, string>>(new Map());
  const [page, setPage] = useState(1);
  // 每页条数持久化（对齐 AssetMap/Dimensions 的 usePersistentPageSize 跨页记忆）
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.catalog.pageSize", 20);
  const [selected, setSelected] = useState<MetricResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [userMap, setUserMap] = useState<Map<number, string>>(new Map());
  const [domainMap, setDomainMap] = useState<Map<string, string>>(new Map());
  const [currentUserId, setCurrentUserId] = useState<number | undefined>(undefined);
  const [currentUserRole, setCurrentUserRole] = useState<string>("");
  const [myMetricsOnly, setMyMetricsOnly] = useState(false);
  const [lifecycleFilter, setLifecycleFilter] = useState<string | null>(urlLifecycle || null);
  const [urlSynced, setUrlSynced] = useState(false);
  useEffect(() => {
    if (!urlSynced) {
      setUrlSynced(true);
      return; // 首帧用 URL 初始值，不覆盖
    }
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (keyword) next.set("kw", keyword); else next.delete("kw");
        if (status) next.set("status", status); else next.delete("status");
        if (domain) next.set("domain", domain); else next.delete("domain");
        if (tier) next.set("tier", tier); else next.delete("tier");
        if (lifecycleFilter) next.set("lifecycle", lifecycleFilter); else next.delete("lifecycle");
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyword, status, domain, tier, lifecycleFilter]);

  // URL 直达 ?lifecycle= 时（分享/刷新），按快筛 key 计算真实日期区间；
  // 与 handleLifecycle 交互共用同一日期口径（created_7d=7天前起 / stale_30d=30天前止）
  useEffect(() => {
    if (lifecycleFilter === "created_7d" && !lifecycleDate.created_after) {
      setLifecycleDate({ created_after: new Date(Date.now() - 7 * 24 * 3600 * 1000).toISOString() });
    } else if (lifecycleFilter === "stale_30d" && !lifecycleDate.updated_before) {
      setLifecycleDate({ updated_before: new Date(Date.now() - 30 * 24 * 3600 * 1000).toISOString() });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lifecycleFilter]);
  // 生命周期快筛的真实日期区间（TD §13）：created_7d=7 天前起 / stale_30d=30 天前止
  const [lifecycleDate, setLifecycleDate] = useState<{ created_after?: string; updated_before?: string }>({});
  const [favorites, setFavorites] = useState<Set<string>>(new Set());
  // 只看收藏：客户端过滤当前页（后端 list 无收藏过滤参数）
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  // 批量操作权限点（方案 C 按钮级管控）：提交/删除=metric:create、通过/打回=metric:approve、
  // 下线=metric:deprecate。can() 控制批量操作按钮可用性；后端接口强制仍为最终边界。
  const { can } = usePermission();
  const canBatchManage = can("metric:create") || can("metric:approve") || can("metric:deprecate");
  // 批量操作确认弹窗：null=关闭 / submit=批量提交审核 / delete=批量删除 /
  // approve=批量通过 / reject=批量打回 / deprecate=批量下线
  const [batchAction, setBatchAction] = useState<
    "submit" | "delete" | "approve" | "reject" | "deprecate" | null
  >(null);
  const [batchBusy, setBatchBusy] = useState(false);
  // 批量操作失败明细：超 3 条时提供「查看明细」弹窗（避免 message 截断导致用户看不到全部失败）
  const [batchErrors, setBatchErrors] = useState<string[]>([]);
  const [batchErrorsOpen, setBatchErrorsOpen] = useState(false);
  // 批量提交审核的评审指派（TD §13）
  const [batchReviewerType, setBatchReviewerType] = useState<"user" | "domain" | null>(null);
  const [batchReviewerId, setBatchReviewerId] = useState<number | null>(null);
  // 批量打回原因 / 批量下线替代指标映射
  const [batchRejectReason, setBatchRejectReason] = useState("");
  const [batchSuccessors, setBatchSuccessors] = useState<Record<string, string>>({});
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
        const st = new Map<string, string>();
        collectDomainStatus(tree, st);
        setDomainStatusMap(st);
      }),
      fetchCurrentUser().then((u) => { setCurrentUserId(u.id); setCurrentUserRole(u.role); }).catch(() => {}),
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
  // 域筛选下拉选项也使用中文名；dashboard 域集合与 domain_tree 不一致时兜底保留原 code（避免空 label）
  // 停用域（inactive）加「（已停用）」标识，避免用户误以为该域仍活跃
  const domainFilterOptions = useMemo(
    () =>
      domainOptions.map((d) => ({
        value: d.value,
        label: domainStatusMap.get(d.value) === "inactive" ? `${domainName(d.value) || d.value}（已停用）` : domainName(d.value) || d.value,
      })),
    [domainOptions, domainName, domainStatusMap],
  );

  useEffect(() => {
    if (urlKw && urlKw !== keyword) setKeyword(urlKw);
    if (urlStatus && urlStatus !== status) setStatus(urlStatus);
    if (urlOwnerId && urlOwnerId !== ownerFilter) setOwnerFilter(urlOwnerId);
    if (urlDomain && urlDomain !== domain) setDomain(urlDomain);
    if (urlTier && urlTier !== tier) setTier(urlTier);
    if (urlLifecycle && urlLifecycle !== lifecycleFilter) setLifecycleFilter(urlLifecycle);
    if (urlKw || urlStatus || urlOwnerId || urlDomain || urlTier || urlLifecycle) setPage(1);
  }, [urlKw, urlStatus, urlOwnerId, urlDomain, urlTier, urlLifecycle]);

  async function load() {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      const res = await listMetrics({
        keyword,
        status,
        domain: domain || undefined,
        metric_tier: tier || undefined,
        owner_id: ownerFilter ? Number(ownerFilter) : myMetricsOnly ? currentUserId : undefined,
        created_after: lifecycleDate.created_after,
        updated_before: lifecycleDate.updated_before,
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
  }, [page, pageSize, status, domain, tier, sortBy, sortOrder, myMetricsOnly, currentUserId, ownerFilter, lifecycleDate]);

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
    const now = new Date();
    if (key === "created_7d") {
      const from = new Date(now.getTime() - 7 * 24 * 3600 * 1000);
      setKeyword("");
      setStatus("");
      setLifecycleDate({ created_after: from.toISOString() });
      setLifecycleFilter(key);
      setSortBy("created_at");
      setSortOrder("desc");
    } else if (key === "stale_30d") {
      const until = new Date(now.getTime() - 30 * 24 * 3600 * 1000);
      setKeyword("");
      setStatus("");
      setLifecycleDate({ updated_before: until.toISOString() });
      setLifecycleFilter(key);
      setSortBy("updated_at");
      setSortOrder("asc");
    } else if (key === "deprecating") {
      setKeyword("");
      setStatus("DEPRECATED");
      setLifecycleDate({});
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
    setBatchBusy(true);
    let ok = 0;
    const errors: string[] = [];
    try {
      if (batchAction === "approve") {
        const codes = selected.filter((m) => m.status === "REVIEW").map((m) => m.metric_code);
        if (!codes.length) {
          message.warning("勾选的指标中没有待评审（REVIEW）状态");
          return;
        }
        const res = await batchApproveMetrics(codes);
        ok = res.ok_count;
        res.results.filter((r) => !r.ok).forEach((r) => errors.push(`${r.metric_code}: ${r.message}`));
      } else if (batchAction === "reject") {
        if (!batchRejectReason.trim() || batchRejectReason.trim().length < 4) {
          message.warning("请填写驳回原因（至少 4 字）");
          return;
        }
        const codes = selected.filter((m) => m.status === "REVIEW").map((m) => m.metric_code);
        if (!codes.length) {
          message.warning("勾选的指标中没有待评审（REVIEW）状态");
          return;
        }
        const res = await batchRejectMetrics(codes, batchRejectReason.trim());
        ok = res.ok_count;
        res.results.filter((r) => !r.ok).forEach((r) => errors.push(`${r.metric_code}: ${r.message}`));
      } else if (batchAction === "deprecate") {
        const items = selected
          .filter((m) => m.status === "PUBLISHED" && batchSuccessors[m.metric_code])
          .map((m) => ({ metric_code: m.metric_code, successor_code: batchSuccessors[m.metric_code] }));
        if (!items.length) {
          message.warning("请为勾选的已发布指标填写替代指标");
          return;
        }
        const res = await batchDeprecateMetrics(items);
        ok = res.ok_count;
        res.results.filter((r) => !r.ok).forEach((r) => errors.push(`${r.metric_code}: ${r.message}`));
      } else {
        // submit / delete：逐条处理（submit 带评审指派）
        const targets = selected.filter((m) => m.status === "DRAFT");
        if (!targets.length) {
          message.warning("勾选的指标中没有草稿状态可操作");
          return;
        }
        for (const m of targets) {
          try {
            if (batchAction === "submit") {
              await submitReview(m.metric_code, "批量提交审核", {
                reviewer_id: batchReviewerType === "user" ? batchReviewerId : null,
                reviewer_type: batchReviewerType,
                reviewer_domain: m.domain,
              });
            } else {
              await deleteMetric(m.metric_code);
            }
            ok += 1;
          } catch (err) {
            errors.push(
              `${m.metric_code}: ${err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "失败"}`,
            );
          }
        }
      }
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量操作失败",
      );
    } finally {
      setBatchBusy(false);
      setBatchAction(null);
      if (ok) message.success(`${BATCH_ACTION_LABEL[batchAction] ?? "操作"}成功 ${ok} 个`);
      if (errors.length) {
        setBatchErrors(errors);
        if (errors.length <= 3) {
          message.error(errors.join("；"));
        } else {
          message.error(`批量操作失败 ${errors.length} 条（前 3 条：${errors.slice(0, 3).join("；")}…），点击「查看失败明细」查看全部`);
          setBatchErrorsOpen(true);
        }
      }
      setSelected([]);
      setBatchRejectReason("");
      setBatchSuccessors({});
      setBatchReviewerType(null);
      setBatchReviewerId(null);
      load();
    }
  }

  function exportCsv() {
    const header = [
      "metric_code", "name", "domain", "owner_id", "type", "status",
      "aggregation", "granularity", "unit", "dw_layer", "metric_tier",
      "pii_flag", "version", "created_at", "updated_at",
    ];
    const rows = items.map((m) =>
      [
        m.metric_code, m.name, domainName(m.domain), userName(m.owner_id), m.type,
        STATUS_LABEL[m.status] ?? m.status,
        AGGREGATION_LABEL[m.aggregation] ?? m.aggregation,
        GRANULARITY_LABEL[m.granularity] ?? m.granularity,
        m.unit && UNIT_LABEL[m.unit] ? UNIT_LABEL[m.unit] : m.unit, DW_LAYER_LABEL[m.dw_layer] ?? m.dw_layer, METRIC_TIER_LABEL[m.metric_tier] ?? m.metric_tier,
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
      render: (s: string) =>
        s === "DATA_SOURCE_DROPPED" ? (
          <Tooltip title="该指标的数据源已下线，可在指标详情页「源已恢复」或「确认退役」处理">
            <Tag color={STATUS_COLOR[s]}>{STATUS_LABEL[s] ?? s}</Tag>
          </Tooltip>
        ) : (
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

  const hasFilter = Boolean(
    keyword || status || domain || tier || ownerFilter || myMetricsOnly || lifecycleFilter || favoritesOnly,
  );
  const emptyGuide = useMemo(
    () => (
      <div style={{ padding: "16px 0", textAlign: "center" }}>
        <p className="muted">{hasFilter ? "没有匹配的指标，试试放宽或清除筛选条件" : "目录还是空的，创建第一个指标或从模板开始"}</p>
        <Space>
          {hasFilter ? (
            <Button icon={<ColumnWidthOutlined />} aria-label="清除筛选" onClick={() => {
              setKeyword("");
              setStatus("");
              setDomain("");
              setTier("");
              setLifecycleFilter(null);
              setLifecycleDate({});
              setMyMetricsOnly(false);
              setFavoritesOnly(false);
              setSortBy("updated_at");
              setSortOrder("desc");
              setPage(1);
            }}>
              清除筛选
            </Button>
          ) : (
            <>
              <Button type="primary" icon={<PlusCircleOutlined />} onClick={() => navigate("/create")}>
                创建指标
              </Button>
              <Button icon={<FileTextOutlined />} onClick={() => navigate("/templates")}>
                从模板创建
              </Button>
            </>
          )}
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
          <div className="page-kicker">指标资产 / 指标目录</div>
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
                {
                  key: "approve",
                  label: "批量通过（评审中）",
                  icon: <CheckCircleOutlined />,
                  disabled: !selected.some((m) => m.status === "REVIEW"),
                },
                {
                  key: "reject",
                  label: "批量打回（评审中）",
                  icon: <ClockCircleOutlined />,
                  disabled: !selected.some((m) => m.status === "REVIEW"),
                },
                {
                  key: "deprecate",
                  label: "批量下线（已发布）",
                  icon: <DeleteOutlined />,
                  disabled: !selected.some((m) => m.status === "PUBLISHED"),
                },
                { type: "divider" },
                {
                  key: "delete",
                  label: "批量删除（草稿）",
                  icon: <DeleteOutlined />,
                  danger: true,
                  // 后端 DELETE 仅 platform_admin 可执行；非平台管理员禁用，避免 403
                  disabled: !selected.some((m) => m.status === "DRAFT") || currentUserRole !== "platform_admin",
                },
              ],
              onClick: ({ key }) => setBatchAction(key as "submit" | "delete" | "approve" | "reject" | "deprecate"),
            }}
            trigger={["click"]}
          >
            <Button icon={<ThunderboltOutlined />} disabled={!selected.length || !canBatchManage}>
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
            { value: "DATA_SOURCE_DROPPED", label: "数据源下线" },
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
        <Tooltip title={favoritesOnly ? "仅过滤当前页命中的收藏，可在搜索框输入关键字缩小范围" : "只看我收藏的指标（当前页内过滤）"}>
          <Button
            type={favoritesOnly ? "primary" : "default"}
            icon={favoritesOnly ? <HeartFilled style={{ color: "#eb2f96" }} /> : <HeartOutlined />}
            onClick={() => setFavoritesOnly(!favoritesOnly)}
          >
            {favoritesOnly ? "只看收藏" : "我的收藏"}
          </Button>
        </Tooltip>
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
                setLifecycleDate({});
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
        <span className="muted">
          {favoritesOnly ? `当前页命中 ${displayItems.length} 条（全量 ${total} 条）` : `共 ${total} 条`}
        </span>
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
          onChange: (p, ps) => { setPage(p); onShowSizeChange(p, ps); },
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
        title={
          batchAction === "submit"
            ? "批量提交审核"
            : batchAction === "approve"
              ? "批量通过"
              : batchAction === "reject"
                ? "批量打回"
                : batchAction === "deprecate"
                  ? "批量下线"
                  : "批量删除草稿"
        }
        open={batchAction !== null}
        confirmLoading={batchBusy}
        onOk={runBatch}
        onCancel={() => setBatchAction(null)}
        okText={
          batchAction === "submit"
            ? "提交"
            : batchAction === "approve"
              ? "通过"
              : batchAction === "reject"
                ? "打回"
                : batchAction === "deprecate"
                  ? "下线"
                  : "删除"
        }
        okButtonProps={{ danger: batchAction === "delete" || batchAction === "deprecate" }}
      >
        {batchAction === "submit" && (
          <div>
            <p>
              将勾选的 <b>{selected.filter((m) => m.status === "DRAFT").length}</b> 个草稿指标提交审核
              （DRAFT → REVIEW）。非草稿状态的勾选项将被跳过。
            </p>
            <p style={{ marginTop: 12, marginBottom: 4 }}>
              评审指派（可选）：指定评审用户或域评审组，审批页仅被指派者可评审
            </p>
            <Space wrap>
              <Select
                style={{ width: 160 }}
                placeholder="不指派（域管理员兜底）"
                allowClear
                value={batchReviewerType ?? undefined}
                onChange={(v) => setBatchReviewerType(v ?? null)}
                options={[
                  { value: "user", label: "指定评审用户" },
                  { value: "domain", label: "域评审组" },
                ]}
              />
              {batchReviewerType === "user" && (
                <Select
                  style={{ width: 220 }}
                  placeholder="选择评审用户"
                  showSearch
                  optionFilterProp="label"
                  value={batchReviewerId ?? undefined}
                  onChange={(v) => setBatchReviewerId(v ?? null)}
                  options={[...userMap.entries()].map(([id, name]) => ({
                    value: id,
                    label: `${name}（${id}）`,
                  }))}
                />
              )}
              {batchReviewerType === "domain" && (
                <span className="muted" style={{ fontSize: 12 }}>
                  将按各指标自身域组建评审组（该域 domain_admin/reviewer 可评审）
                </span>
              )}
            </Space>
          </div>
        )}
        {batchAction === "approve" && (
          <p>
            将勾选的 <b>{selected.filter((m) => m.status === "REVIEW").length}</b> 个评审中指标通过并发布
            （REVIEW → PUBLISHED）。评审人指派校验由后端逐条执行，未通过项显示原因。
          </p>
        )}
        {batchAction === "reject" && (
          <div>
            <p>
              将勾选的 <b>{selected.filter((m) => m.status === "REVIEW").length}</b> 个评审中指标打回草稿
              （REVIEW → DRAFT）。
            </p>
            <Input.TextArea
              rows={2}
              placeholder="驳回原因（必填，至少 4 字）"
              value={batchRejectReason}
              onChange={(e) => setBatchRejectReason(e.target.value)}
            />
          </div>
        )}
        {batchAction === "deprecate" && (
          <div>
            <p>
              将勾选的 <b>{selected.filter((m) => m.status === "PUBLISHED").length}</b> 个已发布指标下线
              （PUBLISHED → DEPRECATED），须为每个指标填写替代指标编码。
            </p>
            {selected
              .filter((m) => m.status === "PUBLISHED")
              .map((m) => (
                <div key={m.metric_code} style={{ marginBottom: 8 }}>
                  <span className="mono" style={{ fontSize: 12, marginRight: 8 }}>
                    {m.metric_code}
                  </span>
                  <Input
                    style={{ width: 280 }}
                    placeholder="替代指标编码（须已发布）"
                    value={batchSuccessors[m.metric_code] ?? ""}
                    onChange={(e) =>
                      setBatchSuccessors((prev) => ({ ...prev, [m.metric_code]: e.target.value }))
                    }
                  />
                </div>
              ))}
          </div>
        )}
        {batchAction === "delete" && (
          <p>
            将删除勾选的 <b>{selected.filter((m) => m.status === "DRAFT").length}</b> 个草稿指标
            （软删除，仅 platform_admin 可执行）。此操作不可恢复。
          </p>
        )}
      </Modal>

      {/* 批量操作失败明细弹窗：完整展示所有失败项，避免 message 截断 */}
      <Modal
        title="批量操作失败明细"
        open={batchErrorsOpen}
        onCancel={() => setBatchErrorsOpen(false)}
        footer={
          <Button onClick={() => setBatchErrorsOpen(false)}>关闭</Button>
        }
      >
        <ul style={{ maxHeight: 320, overflow: "auto", paddingLeft: 18 }}>
          {batchErrors.map((e, i) => (
            <li key={i} className="mono" style={{ marginBottom: 6, fontSize: 12 }}>
              {e}
            </li>
          ))}
        </ul>
      </Modal>
    </div>
  );
}