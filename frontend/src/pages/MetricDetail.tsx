import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Input,
  message,
  Modal,
  Radio,
  Select,
  Space,
  Tag,
  Tabs,
  Tooltip,
  Typography,
} from "antd";
import {
  HeartOutlined,
  ReadOutlined,
  SendOutlined,
  ThunderboltOutlined,
  BellOutlined,
  ExperimentOutlined,
  RollbackOutlined,
  RiseOutlined,
  ArrowLeftOutlined,
  ArrowRightOutlined,
  EditOutlined,
  RobotOutlined,
} from "@ant-design/icons";
import { usePermission } from "../hooks/usePermission";
import {
  addFavorite,
  approveMetric,
  deprecateMetric,
  recoverSourceDropped,
  confirmDeprecateDropped,
  emergencyPublishMetric,
  fetchArchivedMetric,
  fetchCurrentUser,
  fetchRelatedMetrics,
  getMetric,
  getMetricHealth,
  listFavorites,
  listSubscriptions,
  listUsers,
  listVersions,
  piiReview,
  promoteMetric,
  removeFavorite,
  rollbackMetric,
  submitReview,
  suggestRenameName,
  updateMetric,
  upsertSubscription,
  UnisenseApiError,
} from "../api";
import type {
  ArchivedMetricResponse,
  CurrentUser,
  MetricHealth,
  MetricResponse,
  MetricUpdateRequest,
  MetricVersionResponse,
  RecommendItem,
  RenameSuggestItem,
  SubscriptionPref,
  UserBrief,
} from "../types";
import { useTracking } from "../hooks/useTracking";
import { enumLabel, METRIC_TYPE_LABEL, METRIC_TIER_LABEL, AGGREGATION_LABEL, TIME_SEMANTICS_LABEL, FRESHNESS_LABEL, DW_LAYER_LABEL, SERVING_MODE_LABEL, ADDITIVITY_LABEL, GRANULARITY_LABEL, UNIT_LABEL, RULING_DECISION_LABEL } from "../utils/enums";
import { formatCnTime, formatCnDate } from "../utils/timeCn";
import { HealthCard } from "./metric/HealthCard";
import { QualitySnapshot } from "./metric/QualitySnapshot";
import { LineageImpact } from "./metric/LineageImpact";
import { VersionHistory } from "./metric/VersionHistory";
import { AuditTimeline } from "./metric/AuditTimeline";
import { RelatedDimensions } from "./metric/RelatedDimensions";

const { Paragraph } = Typography;

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

// 推荐/血缘边类型 → 中文（与 Dashboard 的推荐流展示口径一致）
const EDGE_TYPE_LABEL: Record<string, string> = {
  DERIVED_FROM: "派生自",
  CONSUMED_BY: "被消费",
  LINEAGE: "关联",
  POPULAR: "热门",
  RECENT: "最新",
};

const ROLE_LABEL: Record<string, string> = {
  platform_admin: "平台管理员",
  domain_admin: "域管理员",
  metric_owner: "指标 Owner",
  reviewer: "评审人",
  compliance_officer: "合规官",
  analyst: "分析师",
  viewer: "访客",
};

const SENSITIVE_ROLES = ["platform_admin", "domain_admin", "compliance_officer"];

// Owner 责任链：将 owner_id 渲染为可读的用户名 + 角色
function OwnerChain({ metric, users }: { metric: MetricResponse; users: UserBrief[] }) {
  const byId = new Map(users.map((u) => [u.id, u]));
  function cell(uid: number | null | undefined) {
    if (uid == null) return <span className="muted">未配置</span>;
    const u = byId.get(uid);
    return (
      <span>
        <strong>{u?.display_name || `用户 #${uid}`}</strong>
        {u && <Tag style={{ marginLeft: 6 }}>{ROLE_LABEL[u.role] ?? u.role}</Tag>}
        {u?.domain && <span className="muted"> · {u.domain}</span>}
      </span>
    );
  }
  return (
    <Descriptions column={3} size="small">
      <Descriptions.Item label="指标 Owner">{cell(metric.owner_id)}</Descriptions.Item>
      <Descriptions.Item label="备份 Owner">{cell(metric.backup_owner_id)}</Descriptions.Item>
      <Descriptions.Item label="有效版本">
        {metric.effective_version ? `v${metric.effective_version}` : <span className="muted">—</span>}
      </Descriptions.Item>
    </Descriptions>
  );
}

// 废弃替代链：废弃指标的消费方要一眼看到"该用哪个替代"
function DeprecatedChain({ metric }: { metric: MetricResponse }) {
  if (metric.status !== "DEPRECATED") return null;
  return (
    <Alert
      type="warning"
      showIcon
      message="该指标已废弃"
      description={
        <Space direction="vertical" size={2}>
          <span>
            替代指标：{metric.successor_code ? <span className="mono">{metric.successor_code}</span> : <span className="muted">未指定</span>}
          </span>
          {metric.sunset_until && <span className="muted">日落时间：{formatCnDate(metric.sunset_until)}</span>}
          {metric.deprecated_at && <span className="muted">废弃时间：{formatCnTime(metric.deprecated_at)}</span>}
        </Space>
      }
      style={{ marginBottom: 16 }}
    />
  );
}

// 仲裁裁决标记（TD §12.4）：胜方「权威口径」/ 共存方「已裁定共存」，悬停展示裁决明细
function ArbitrationMarkTag({ metric }: { metric: MetricResponse }) {
  const mark = metric.arbitration_mark;
  if (!mark) return null;
  if (mark.status === "coexist") {
    return (
      <Tooltip
        title={
          <Space direction="vertical" size={0}>
            <span>冲突 {mark.conflict_id} · 裁决：保留差异</span>
            <span className="muted">与 {mark.opposite_code ?? "对方"} 共存，均非唯一权威</span>
            {mark.rename_required && (
              <span className="muted">仲裁要求本指标改名以区分口径，请点击「去改名」</span>
            )}
            {mark.ruled_at && <span className="muted">{formatCnTime(mark.ruled_at)}</span>}
          </Space>
        }
      >
        <Tag color={mark.rename_required ? "orange" : "blue"}>
          {mark.rename_required ? "仲裁要求改名" : "已裁定共存"}
        </Tag>
      </Tooltip>
    );
  }
  return (
    <Tooltip
      title={
        <Space direction="vertical" size={0}>
          <span>冲突 {mark.conflict_id} · 裁决：{RULING_DECISION_LABEL[mark.decision ?? ""] ?? mark.decision ?? "选为权威"}</span>
          <span className="muted">权威口径，落败方 {mark.opposite_code ?? "—"} 已废弃/作废</span>
          {mark.ruled_at && <span className="muted">{formatCnTime(mark.ruled_at)}</span>}
        </Space>
      }
    >
      <Tag color="green">权威口径</Tag>
    </Tooltip>
  );
}

// 仲裁作废指标醒目引导（TD §12.4「落败方 metric 转别名/废弃」的可寻址落地）：
// 历史链接直访被软删的落败方时，以「非错误风格」展示作废原因 + 权威指标跳转，
// 而非红色错误卡片 / 裸「指标不存在」。
function ArchivedMetricCard({
  code,
  successorCode,
  mark,
}: {
  code: string;
  successorCode: string;
  mark: Record<string, unknown> | null;
}) {
  const navigate = useNavigate();
  const conflictId = mark?.conflict_id ? String(mark.conflict_id) : null;
  const ruledAt = mark?.ruled_at ? String(mark.ruled_at) : null;
  const decision = mark?.decision ? String(mark.decision) : null;
  return (
    <Alert
      type="warning"
      showIcon
      message={`指标「${code}」已因口径裁决作废`}
      description={
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <div className="muted">
            该指标在冲突仲裁中被裁定为落败方
            {decision ? `（${RULING_DECISION_LABEL[decision] ?? decision} 决策）` : "（已作废）"}
            {conflictId ? `，相关冲突 ${conflictId}` : ""}
            {ruledAt ? `，裁决于 ${formatCnTime(ruledAt)}` : ""}。
            作废指标不再作为可消费口径，下方历史详情仅供追溯。
          </div>
          {successorCode ? (
            <Button
              type="primary"
              icon={<ArrowRightOutlined />}
              onClick={() => navigate(`/detail/${successorCode}`)}
            >
              查看权威指标：{successorCode}
            </Button>
          ) : (
            <span className="muted">该作废指标未指定权威替代指标（无可消费口径）。</span>
          )}
        </Space>
      }
      style={{ marginBottom: 16 }}
    />
  );
}

// 作废指标历史详情：展示历史口径定义，供追溯（作废不可消费，但历史口径应可见）
function ArchivedDetailPanel({ detail }: { detail: ArchivedMetricResponse | null }) {
  if (!detail?.metric) return null;
  const m = detail.metric;
  return (
    <Card title="作废指标历史详情（仅供追溯）" size="small" style={{ marginBottom: 16 }}>
      <Descriptions column={2} size="small" bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label="指标编码">
          <span className="mono">{m.metric_code}</span>
        </Descriptions.Item>
        <Descriptions.Item label="指标名称">{m.name}</Descriptions.Item>
        <Descriptions.Item label="业务域">{m.domain}</Descriptions.Item>
        <Descriptions.Item label="粒度">{m.granularity}</Descriptions.Item>
        <Descriptions.Item label="指标类型">{METRIC_TYPE_LABEL[m.type] ?? m.type}</Descriptions.Item>
        <Descriptions.Item label="状态">
          <Tag color="orange">已作废</Tag>
        </Descriptions.Item>
      </Descriptions>
      <DefinitionCard metric={m} />
    </Card>
  );
}

// 口径定义结构化展示：表达式 / 关联数据表 / 依赖 / 来源字段 / ETL SQL
function DefinitionCard({ metric }: { metric: MetricResponse }) {
  const def = metric.definition_json ?? {};
  const expression = typeof def.expression === "string" ? def.expression : undefined;
  const dependencies = Array.isArray(def.dependencies) ? def.dependencies : [];
  const rawSource = def.source_fields ?? def.source_columns;
  const sourceFields: string[] = Array.isArray(rawSource)
    ? rawSource.map((s) => String(s))
    : rawSource
      ? [String(rawSource)]
      : [];
  const sourceTables: string[] = Array.isArray(def.source_tables)
    ? def.source_tables.map((s) => String(s))
    : def.source_tables
      ? [String(def.source_tables)]
      : [];
  const rawEtl = def.etl_sql ?? def.sql;
  const etlSql = rawEtl == null ? "" : String(rawEtl);
  return (
    <Card title="口径定义" size="small" style={{ marginBottom: 16 }}>
      <Descriptions column={1} size="small" bordered>
        {expression && (
          <Descriptions.Item label="表达式">
            <code className="mono">{expression}</code>
          </Descriptions.Item>
        )}
        {sourceTables.length > 0 && (
          <Descriptions.Item label="关联数据表">
            {sourceTables.map((t) => (
              <Tag key={t} className="mono">{t}</Tag>
            ))}
          </Descriptions.Item>
        )}
        {dependencies.length > 0 && (
          <Descriptions.Item label="依赖指标">
            {dependencies.map((d) => (
              <Tag key={String(d)}>{String(d)}</Tag>
            ))}
          </Descriptions.Item>
        )}
        {sourceFields.length > 0 && (
          <Descriptions.Item label="来源字段">
            {sourceFields.map((s) => (
              <Tag key={s}>{s}</Tag>
            ))}
          </Descriptions.Item>
        )}
        {etlSql && (
          <Descriptions.Item label="口径 SQL">
            <pre style={{ background: "var(--paper)", padding: 8, borderRadius: 4, margin: 0, fontSize: 12, overflow: "auto" }}>
              {etlSql}
            </pre>
          </Descriptions.Item>
        )}
        <Descriptions.Item label="完整 JSON">
          <pre style={{ background: "var(--paper)", padding: 8, borderRadius: 4, margin: 0, fontSize: 12, overflow: "auto", maxHeight: 240 }}>
            {JSON.stringify(def, null, 2)}
          </pre>
        </Descriptions.Item>
      </Descriptions>
    </Card>
  );
}

// 订阅变更通知：真实对接 notify 订阅（IN_APP 渠道 + 事件类型）
function SubscribeModal({
  open,
  onClose,
  onChanged,
}: {
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [checked, setChecked] = useState<Record<string, boolean>>({
    "metric.update": true,
    "quality.alert": true,
    "lineage.change": false,
  });
  const [busy, setBusy] = useState(false);

  const EVENT_LABELS: Record<string, string> = {
    "metric.update": "指标口径更新",
    "quality.alert": "质量告警",
    "lineage.change": "血缘变更",
  };

  async function save() {
    setBusy(true);
    try {
      for (const [eventType, enabled] of Object.entries(checked)) {
        await upsertSubscription({ channel: "IN_APP", event_type: eventType, enabled });
      }
      onChanged();
      onClose();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "订阅失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title="订阅此指标相关通知"
      open={open}
      onOk={save}
      confirmLoading={busy}
      onCancel={onClose}
      okText="保存订阅"
    >
      <Paragraph type="secondary">
        通过应用内通知接收该指标生命周期的变更提醒。取消勾选即取消对应订阅。
      </Paragraph>
      <Checkbox.Group
        value={Object.entries(checked).filter(([, v]) => v).map(([k]) => k)}
        onChange={(vals) =>
          setChecked((prev) =>
            Object.fromEntries(Object.keys(prev).map((k) => [k, vals.includes(k)])),
          )
        }
        style={{ display: "flex", flexDirection: "column", gap: 8 }}
        options={Object.entries(EVENT_LABELS).map(([value, label]) => ({ value, label }))}
      />
    </Modal>
  );
}

export function MetricDetail() {
  const { code } = useParams<{ code: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [metric, setMetric] = useState<MetricResponse | null>(null);
  // 仲裁作废指标（METRIC_ARCHIVED）：软删 + successor 的历史链接直访时，
  // 展示「醒目引导 + 历史详情 + 跳转权威指标」，而非仅一张错误卡片
  const [archived, setArchived] = useState<{
    successorCode: string;
    mark: Record<string, unknown> | null;
    detail: ArchivedMetricResponse | null;
  } | null>(null);
  const [showArchivedModal, setShowArchivedModal] = useState(false);
  const [versions, setVersions] = useState<MetricVersionResponse[]>([]);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [users, setUsers] = useState<UserBrief[]>([]);
  const [health, setHealth] = useState<MetricHealth | null>(null);
  const [favorited, setFavorited] = useState(false);
  const [subscribed, setSubscribed] = useState(false);
  const [subscribeOpen, setSubscribeOpen] = useState(false);
  const [related, setRelated] = useState<RecommendItem[]>([]);
  const [emergencyOpen, setEmergencyOpen] = useState(false);
  const [emergencyReason, setEmergencyReason] = useState("");
  const [grayOpen, setGrayOpen] = useState(false);
  const [deprecateOpen, setDeprecateOpen] = useState(false);
  const [successor, setSuccessor] = useState("");
  // 提交评审弹窗（TD §13）：可指派评审用户或域评审组
  const [submitOpen, setSubmitOpen] = useState(false);
  const [submitReviewerType, setSubmitReviewerType] = useState<"user" | "domain" | null>(null);
  const [submitReviewerId, setSubmitReviewerId] = useState<number | null>(null);
  // 仲裁「保留差异+指定改名」→ Owner 在详情页改名（TD §12.4）
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [renameReason, setRenameReason] = useState("");
  // 仲裁改名建议：LLM 生成区分性名称候选（TD §12.4，用户抉择或编辑后提交）
  const [suggesting, setSuggesting] = useState(false);
  const [renameSuggestions, setRenameSuggestions] = useState<RenameSuggestItem[]>([]);
  const [renameSuggestLoaded, setRenameSuggestLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const { track } = useTracking();

  // 来源感知返回：来自仪表盘/推荐 → 返回仪表盘；来自待办中心 → 返回待办中心；其他回退浏览器历史（无上页兜底仪表盘）。
  // 说明：SPA 中 window.history.length 跨站点累计不可靠，来源标记优先于 history.length 判断。
  const fromState = (location.state as { from?: string } | null)?.from;
  const fromDashboard = fromState === "dashboard";
  const fromTodo = fromState === "todo";
  // 资产地图-变更追踪跳入：精确回到变更追踪 Tab（?tab=changes），避免 history.back 丢内部 Tabs 状态
  const fromAssetmapChanges = fromState === "assetmap-changes";
  function handleBack() {
    if (fromDashboard) {
      navigate("/dashboard");
      return;
    }
    if (fromTodo) {
      navigate("/todo");
      return;
    }
    if (fromAssetmapChanges) {
      navigate("/assetmap?tab=changes");
      return;
    }
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }
  // 与其他页面布局一致：仅保留 ArrowLeftOutlined 图标作为唯一箭头，文字固定为"返回"（不再带 ← 前缀，避免双箭头）。
  // 来源感知只影响跳转目标（待办/仪表盘），不影响文案——全站返回按钮视觉统一。
  const backLabel = "返回";

  async function load() {
    if (!code) return;
    setLoading(true);
    setArchived(null);
    try {
      const [m, vs, me, favs, healthRes, userList, subs, rel] = await Promise.all([
        getMetric(code),
        listVersions(code),
        fetchCurrentUser(),
        listFavorites().catch(() => [] as { asset_type: string; asset_id: string }[]),
        getMetricHealth(code).catch(() => null),
        listUsers().catch(() => [] as UserBrief[]),
        listSubscriptions().catch(() => ({ items: [] as SubscriptionPref[] })),
        fetchRelatedMetrics(code).catch(() => [] as RecommendItem[]),
      ]);
      setMetric(m);
      setVersions(vs);
      setCurrentUser(me);
      setFavorited(favs.some((f) => f.asset_type === "METRIC" && f.asset_id === code));
      setHealth(healthRes);
      setUsers(userList);
      setSubscribed(
        subs.items.some(
          (s) => s.channel === "IN_APP" && ["metric.update", "quality.alert"].includes(s.event_type) && s.enabled,
        ),
      );
      setRelated(rel);
      track("metric_detail_view", code, "metric");
    } catch (err) {
      // 仲裁作废指标（METRIC_ARCHIVED）：后端返回结构化错误（detail 含 successor_code），
      // 渲染「醒目引导 + 历史详情 + 跳转权威指标」，而非裸「指标不存在」。
      if (err instanceof UnisenseApiError && err.code === "METRIC_ARCHIVED") {
        const detail = err.detail ?? {};
        const mark = (detail.arbitration_mark as Record<string, unknown> | null) ?? null;
        // 双保险读取权威指标：detail.successor_code 优先，arbitration_mark 兜底
        // （兼容旧版错误响应 / 历史数据未写独立 successor_code 列的场景）
        const successorCode = String(
          detail.successor_code ?? (mark?.successor_code ? mark.successor_code : "") ?? "",
        );
        setArchived({ successorCode, mark, detail: null });
        setMetric(null);
        // 并行拉取作废指标的历史详情（口径定义/版本），供页面主体展示（best-effort）
        fetchArchivedMetric(code)
          .then((d) => {
            setArchived((prev) =>
              prev ? { ...prev, detail: d } : { successorCode: d.successor_code ?? successorCode, mark: d.arbitration_mark ?? mark, detail: d },
            );
          })
          .catch(() => {
            /* 详情拉取失败不阻塞引导展示 */
          });
        // 首次进入作废页弹出醒目引导（localStorage 记住「不再提示」）
        const dismissed = localStorage.getItem("unisense:archived_banner_dismissed") === "1";
        if (!dismissed) setShowArchivedModal(true);
        return;
      }
      // eslint-disable-next-line no-alert
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  async function runAction(fn: () => Promise<unknown>, okMsg: string) {
    setBusy(true);
    try {
      await fn();
      message.success(okMsg + "成功");
      await load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : okMsg + "失败");
    } finally {
      setBusy(false);
    }
  }

  // 响应仲裁「保留差异+指定改名」：修改指标名称并清除 rename_required 标记
  // （后端 update_metric 检测到 name 变更 + rename_required 时自动清除并记录 resolved_at）。
  async function handleRename() {
    if (!metric) return;
    const name = renameValue.trim();
    if (!name) {
      message.warning("请输入新的指标名称");
      return;
    }
    if (name === metric.name) {
      message.warning("新名称与当前名称一致，无需改名");
      return;
    }
    if (renameReason.trim().length < 4) {
      message.warning("请填写变更原因（至少 4 字）");
      return;
    }
    const req: MetricUpdateRequest = { name, change_reason: renameReason.trim() };
    await runAction(() => updateMetric(metric.metric_code, req), "指标改名");
    setRenameOpen(false);
    setRenameValue("");
    setRenameReason("");
  }

  // 仲裁改名建议：调后端 LLM 生成区分性名称候选（best-effort，LLM 不可用降级规则），
  // 用户点选候选填入输入框后可继续编辑，也可直接手动输入。
  async function handleSuggestRename() {
    if (!metric) return;
    setSuggesting(true);
    setRenameSuggestLoaded(false);
    try {
      const res = await suggestRenameName(
        metric.metric_code,
        metric.arbitration_mark?.rename_opposite_code ?? undefined,
      );
      setRenameSuggestions(res.suggestions ?? []);
      if (res.suggestions && res.suggestions.length > 0) {
        setRenameValue(res.suggestions[0].name);
      }
      setRenameSuggestLoaded(true);
    } catch (err) {
      setRenameSuggestions([]);
      setRenameSuggestLoaded(true);
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "名称建议生成失败",
      );
    } finally {
      setSuggesting(false);
    }
  }

  if (!metric) {
    if (loading) return <Card loading />;
    if (archived) {
      return (
        <div>
          <div className="page-head">
            <div>
              <Button
                type="link"
                icon={<ArrowLeftOutlined />}
                onClick={handleBack}
                style={{ padding: 0, marginBottom: 4 }}
              >
                {backLabel}
              </Button>
              <div className="page-kicker">Assets / Detail</div>
              <h2>指标已作废</h2>
              <p>
                <span className="mono">{code}</span>
              </p>
            </div>
          </div>
          <ArchivedMetricCard
            code={code ?? ""}
            successorCode={archived.successorCode}
            mark={archived.mark}
          />
          <ArchivedDetailPanel detail={archived.detail} />
          {/* 首次进入作废页的醒目引导弹窗：告知作废原因 + 一键跳转权威指标 */}
          <Modal
            open={showArchivedModal}
            title="指标已作废"
            okText={archived.successorCode ? "查看权威指标" : "知道了"}
            cancelText="留在本页"
            okButtonProps={{ disabled: false }}
            onOk={() => {
              if (archived.successorCode) navigate(`/detail/${archived.successorCode}`);
              else setShowArchivedModal(false);
            }}
            onCancel={() => setShowArchivedModal(false)}
            afterClose={() => setShowArchivedModal(false)}
          >
            <Space direction="vertical" size={8}>
              <span>
                该指标已因口径裁决作废，不再作为可消费口径。
                {archived.successorCode
                  ? `权威指标为「${archived.successorCode}」，请使用权威指标口径。`
                  : "该作废指标未指定权威替代指标。"}
              </span>
              <Checkbox
                onChange={(e) => {
                  if (e.target.checked) localStorage.setItem("unisense:archived_banner_dismissed", "1");
                }}
              >
                不再提示
              </Checkbox>
            </Space>
          </Modal>
        </div>
      );
    }
    return (
      <Card>
        <Paragraph type="secondary">指标不存在</Paragraph>
      </Card>
    );
  }

  const role = currentUser?.role || "";
  const isAdmin = role === "platform_admin" || role === "domain_admin";
  const isOwnerOrAdmin = isAdmin || role === "metric_owner";
  const piiMasked = metric.pii_flag && !SENSITIVE_ROLES.includes(role);

  // 按钮级权限点（细粒度管控，方案 C）：与后端 require_roles 对齐——
  // 提交评审=metric:create、发布/灰度=metric:approve、PII 复核=pii:review、
  // 紧急发布=metric:emergency-publish、废弃=metric:deprecate、
  // 全量发布/回滚/改名=metric:edit（写操作，owner 归属由后端 PDP 强制）。
  // can() 控制按钮可见性；后端接口强制仍为最终边界，二者不互相替代。
  const { can, loading: permLoading } = usePermission();
  // 权限快照加载完成前按钮一律不渲染（避免 fail-open 让无权用户短暂看到「审批通过」等）
  const permReady = !permLoading;
  const canApprove = permReady && can("metric:approve");
  const canDeprecate = permReady && can("metric:deprecate");
  const canEdit = permReady && can("metric:edit");
  const canPii = permReady && can("pii:review");
  const canEmergency = permReady && can("metric:emergency-publish");
  const canCreate = permReady && can("metric:create");
  const canInferDesc = permReady && can("metric:infer-description");
  const piiUnreviewed = metric.pii_flag && !metric.compliance_reviewed;

  const headerActions = (
    <Space wrap>
      <Button
        type={favorited ? "primary" : "default"}
        icon={<HeartOutlined />}
        onClick={() =>
          runAction(
            () => (favorited ? removeFavorite("METRIC", metric.metric_code) : addFavorite("METRIC", metric.metric_code)),
            favorited ? "取消收藏" : "收藏",
          )
        }
      >
        {favorited ? "已收藏" : "收藏"}
      </Button>
      <Button
        type={subscribed ? "primary" : "default"}
        icon={<BellOutlined />}
        onClick={() => setSubscribeOpen(true)}
      >
        订阅通知
      </Button>
      <Button icon={<ReadOutlined />} onClick={() => navigate(`/guide/${metric.metric_code}`)}>
        消费指南
      </Button>
    </Space>
  );

  const actions = (
    <Space wrap style={{ marginBottom: 16 }}>
      {(metric.status === "DRAFT" || metric.status === "EXPERIMENTAL" || metric.status === "DEPRECATED") && canCreate && (
        <Button
          icon={<SendOutlined />}
          loading={busy}
          onClick={() => {
            setSubmitReviewerType(null);
            setSubmitReviewerId(null);
            setSubmitOpen(true);
          }}
        >
          {metric.status === "DEPRECATED" ? "重新提交评审" : "提交评审"}
        </Button>
      )}
      {metric.status === "REVIEW" && canApprove && (
        <Button
          type="primary"
          loading={busy}
          onClick={() => runAction(() => approveMetric(metric.metric_code, {}), "审批通过")}
          disabled={piiUnreviewed}
        >
          审批通过{piiUnreviewed ? "（需先 PII 复核）" : ""}
        </Button>
      )}
      {metric.status === "REVIEW" && canApprove && (
        <Button icon={<ExperimentOutlined />} loading={busy} onClick={() => setGrayOpen(true)}>
          灰度发布
        </Button>
      )}
      {metric.status === "EXPERIMENTAL" && isOwnerOrAdmin && canEdit && (
        <>
          <Button icon={<RiseOutlined />} loading={busy} onClick={() => runAction(() => promoteMetric(metric.metric_code), "全量发布")}>
            全量发布
          </Button>
          <Button icon={<RollbackOutlined />} loading={busy} onClick={() => runAction(() => rollbackMetric(metric.metric_code), "回滚")}>
            回滚
          </Button>
        </>
      )}
      {canPii && piiUnreviewed && (
        <Button loading={busy} onClick={() => runAction(() => piiReview(metric.metric_code), "PII 复核")}>
          PII 合规复核
        </Button>
      )}
      {(metric.status === "DRAFT" || metric.status === "REVIEW") && canEmergency && (
        <Button danger icon={<ThunderboltOutlined />} loading={busy} onClick={() => setEmergencyOpen(true)}>
          紧急发布
        </Button>
      )}
      {(metric.status === "PUBLISHED" || metric.status === "EXPERIMENTAL") && isOwnerOrAdmin && canDeprecate && (
        <Button danger loading={busy} onClick={() => setDeprecateOpen(true)}>
          废弃
        </Button>
      )}
      {metric.status === "DATA_SOURCE_DROPPED" && isOwnerOrAdmin && canDeprecate && (
        <>
          <Button
            icon={<RiseOutlined />}
            loading={busy}
            onClick={() => runAction(() => recoverSourceDropped(metric.metric_code), "源已恢复")}
          >
            源已恢复
          </Button>
          <Button danger loading={busy} onClick={() => setDeprecateOpen(true)}>
            确认退役
          </Button>
        </>
      )}
      {metric.arbitration_mark?.rename_required && isOwnerOrAdmin && canEdit && (
        <Button
          type="primary"
          icon={<EditOutlined />}
          loading={busy}
          onClick={() => {
            setRenameValue(metric.name);
            setRenameReason("");
            setRenameSuggestions([]);
            setRenameSuggestLoaded(false);
            setRenameOpen(true);
          }}
        >
          去改名
        </Button>
      )}
    </Space>
  );

  const badgeArea = (
    <Space size={4} wrap>
      {metric.pii_flag && (
        <Tag color="red">PII{metric.compliance_reviewed ? " 已复核" : " 待复核"}</Tag>
      )}
      {metric.emergency_publish && <Tag color="volcano">紧急发布</Tag>}
      {metric.pending_conflict && <Tag color="orange">口径冲突待处理</Tag>}
      <ArbitrationMarkTag metric={metric} />
      {metric.gray_tenant_ids && metric.gray_tenant_ids.length > 0 && (
        <Tag color="purple">灰度 {metric.gray_tenant_ids.length} 租户</Tag>
      )}
    </Space>
  );

  return (
    <div>
      <div className="page-head">
        <div>
          <Button
            type="link"
            icon={<ArrowLeftOutlined />}
            onClick={handleBack}
            style={{ padding: 0, marginBottom: 4 }}
          >
            {backLabel}
          </Button>
          <div className="page-kicker">Assets / Detail</div>
          <h2>
            {metric.name}{" "}
            <Tag color={STATUS_COLOR[metric.status]}>{STATUS_LABEL[metric.status]}</Tag>
            {metric.metric_tier && <Tag>{METRIC_TIER_LABEL[metric.metric_tier] ?? metric.metric_tier}</Tag>}
            {badgeArea}
          </h2>
          <p>
            <span className="mono">{metric.metric_code}</span>
            <span style={{ margin: "0 8px" }}>·</span>
            {metric.domain}
            <span style={{ margin: "0 8px" }}>·</span>
            v{metric.version}
          </p>
        </div>
        {headerActions}
      </div>

      {piiMasked && (
        <Alert
          type="warning"
          showIcon
          message="口径已按数据分级脱敏"
          description="您当前角色无权查看该 PII 指标的完整口径定义，以下内容已做脱敏处理。"
          style={{ marginBottom: 16 }}
        />
      )}

      <DeprecatedChain metric={metric} />

      {health && <HealthCard health={health} />}

      <Card size="small" style={{ marginBottom: 16 }}>
        <Descriptions column={3} bordered size="small">
          <Descriptions.Item label="编码">{metric.metric_code}</Descriptions.Item>
          <Descriptions.Item label="域">{metric.domain}</Descriptions.Item>
          <Descriptions.Item label="类型">{enumLabel(METRIC_TYPE_LABEL, metric.type)}</Descriptions.Item>
          <Descriptions.Item label="分级">{enumLabel(METRIC_TIER_LABEL, metric.metric_tier)}</Descriptions.Item>
          <Descriptions.Item label="聚合">{enumLabel(AGGREGATION_LABEL, metric.aggregation)}</Descriptions.Item>
          <Descriptions.Item label="粒度">{enumLabel(GRANULARITY_LABEL, metric.granularity)}</Descriptions.Item>
          <Descriptions.Item label="单位">{UNIT_LABEL[metric.unit] ?? metric.unit}</Descriptions.Item>
          <Descriptions.Item label="币种">{metric.currency || <span className="muted">—</span>}</Descriptions.Item>
          <Descriptions.Item label="数据分层">{enumLabel(DW_LAYER_LABEL, metric.dw_layer)}</Descriptions.Item>
          <Descriptions.Item label="时间语义">{enumLabel(TIME_SEMANTICS_LABEL, metric.time_semantics)}</Descriptions.Item>
          <Descriptions.Item label="新鲜度">{enumLabel(FRESHNESS_LABEL, metric.freshness)}</Descriptions.Item>
          <Descriptions.Item label="SLA">{metric.sla || <span className="muted">—</span>}</Descriptions.Item>
          <Descriptions.Item label="服务模式">{enumLabel(SERVING_MODE_LABEL, metric.serving_mode)}</Descriptions.Item>
          <Descriptions.Item label="可加性">{enumLabel(ADDITIVITY_LABEL, metric.additivity)}</Descriptions.Item>
          <Descriptions.Item label="非可加维度">
            {metric.non_additive_dimensions?.length ? metric.non_additive_dimensions.join(", ") : <span className="muted">—</span>}
          </Descriptions.Item>
          <Descriptions.Item label="版本">v{metric.version}</Descriptions.Item>
        </Descriptions>
      </Card>

      <Card size="small" title="Owner 责任链" style={{ marginBottom: 16 }}>
        <OwnerChain metric={metric} users={users} />
      </Card>

      <DefinitionCard metric={metric} />

      {actions}

      <Card size="small">
        <Tabs
          items={[
            { key: "quality", label: "质量快照", children: <QualitySnapshot metricId={metric.id} metricCode={metric.metric_code} /> },
            { key: "lineage", label: "血缘影响", children: <LineageImpact metricCode={metric.metric_code} /> },
            { key: "versions", label: `版本历史 (${versions.length})`, children: <VersionHistory metricCode={metric.metric_code} versions={versions} onChanged={load} /> },
            { key: "dims", label: "关联维度", children: <RelatedDimensions metricId={metric.id} /> },
            { key: "audit", label: "变更审计", children: <AuditTimeline metricCode={metric.metric_code} /> },
          ]}
        />
      </Card>

      {/* 场景化推荐：看过此指标的人还看了（GET /recommend/metrics/{code}/related，空则隐藏） */}
      {related.length > 0 && (
        <Card size="small" title="看过此指标的人还看了" style={{ marginBottom: 16 }}>
          <div>
            {related.map((r) => (
              <div
                key={r.metric_id}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: 12,
                  padding: "10px 0",
                  borderBottom: "1px solid var(--line-soft)",
                  cursor: "pointer",
                }}
                onClick={() => {
                  track("recommend_click", r.metric_id, "metric");
                  navigate(`/detail/${r.metric_id}`);
                }}
              >
                <span className="mono" style={{ fontWeight: 600 }}>{r.metric_id}</span>
                <span className="muted" style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {r.reason ? r.reason : `血缘 · ${EDGE_TYPE_LABEL[r.edge_type] ?? r.edge_type}`}
                </span>
              </div>
            ))}
          </div>
        </Card>
      )}

      <SubscribeModal open={subscribeOpen} onClose={() => setSubscribeOpen(false)} onChanged={load} />

      <Modal
        title="紧急发布（跳过评审，PII 门禁不可跳）"
        open={emergencyOpen}
        onOk={() => {
          // 前端拦截：原因必填且至少 10 字（与后端 MetricEmergencyPublishRequest 一致），
          // 避免把 422 校验错误甩给后端接口
          const reason = emergencyReason.trim();
          if (reason.length < 10) {
            message.warning("紧急发布原因至少 10 字");
            return;
          }
          runAction(() => emergencyPublishMetric(metric.metric_code, reason), "紧急发布").then(() => {
            setEmergencyOpen(false);
            setEmergencyReason("");
          });
        }}
        confirmLoading={busy}
        onCancel={() => setEmergencyOpen(false)}
        okText="确认紧急发布"
        okButtonProps={{ danger: true }}
      >
        <Input.TextArea
          placeholder="紧急发布原因（必填，至少 10 字）"
          value={emergencyReason}
          onChange={(e) => setEmergencyReason(e.target.value)}
          rows={3}
        />
      </Modal>

      <Modal
        title="灰度发布（EXPERIMENTAL，可指定白名单租户）"
        open={grayOpen}
        onOk={() =>
          runAction(() => approveMetric(metric.metric_code, { mode: "experimental" }), "灰度发布").then(() =>
            setGrayOpen(false),
          )
        }
        confirmLoading={busy}
        onCancel={() => setGrayOpen(false)}
        okText="确认灰度发布"
      >
        <Paragraph type="secondary">
          发布为灰度实验状态（EXPERIMENTAL）。可在后续通过「全量发布」转正式、或「回滚」退回上一正式版本。
        </Paragraph>
      </Modal>

      <Modal
        title={metric.status === "DATA_SOURCE_DROPPED" ? "确认退役（数据源下线）" : "废弃指标"}
        open={deprecateOpen}
        onOk={() => {
          const action =
            metric.status === "DATA_SOURCE_DROPPED"
              ? () => confirmDeprecateDropped(metric.metric_code, successor)
              : () => deprecateMetric(metric.metric_code, successor);
          runAction(action, "确认退役").then(() => {
            setDeprecateOpen(false);
            setSuccessor("");
          });
        }}
        confirmLoading={busy}
        onCancel={() => setDeprecateOpen(false)}
        okText="确认废弃"
        okButtonProps={{ danger: true }}
      >
        <Input
          placeholder="替代指标编码（选填，须为已发布指标；留空表示无替代）"
          value={successor}
          onChange={(e) => setSuccessor(e.target.value)}
        />
        <p className="muted" style={{ marginTop: 8 }}>
          留空表示该指标无替代（直接废弃下线）；填写后废弃指标的消费方会看到「替代指标」引导。
        </p>
      </Modal>

      {/* 提交评审弹窗（TD §13）：可指派评审用户或域评审组，审批页仅被指派者可评审 */}
      <Modal
        title="提交评审"
        open={submitOpen}
        onOk={() =>
          runAction(
            () =>
              submitReview(metric.metric_code, "提交评审", {
                reviewer_id: submitReviewerType === "user" ? submitReviewerId : null,
                reviewer_type: submitReviewerType,
                reviewer_domain: metric.domain,
              }),
            "提交评审",
          ).then(() => setSubmitOpen(false))
        }
        confirmLoading={busy}
        onCancel={() => setSubmitOpen(false)}
        okText="提交评审"
      >
        <Paragraph type="secondary">
          提交后将进入评审状态（DRAFT → REVIEW），由指定评审人通过或打回。
        </Paragraph>
        <Space wrap style={{ marginTop: 8 }}>
          <Select
            style={{ width: 180 }}
            placeholder="评审指派（可选）"
            allowClear
            value={submitReviewerType ?? undefined}
            onChange={(v) => {
              setSubmitReviewerType(v ?? null);
              setSubmitReviewerId(null);
            }}
            options={[
              { value: "user", label: "指定评审用户" },
              { value: "domain", label: "域评审组" },
            ]}
          />
          {submitReviewerType === "user" && (
            <Select
              style={{ width: 220 }}
              placeholder="选择评审用户"
              showSearch
              optionFilterProp="label"
              value={submitReviewerId ?? undefined}
              onChange={(v) => setSubmitReviewerId(v ?? null)}
              options={users.map((u) => ({ value: u.id, label: `${u.display_name || u.username}（${u.id}）` }))}
            />
          )}
          {submitReviewerType === "domain" && (
            <span className="muted" style={{ fontSize: 12 }}>
              由 <b>{metric.domain}</b> 域评审组评审（该域 domain_admin/reviewer 可评审）
            </span>
          )}
          {submitReviewerType === null && (
            <span className="muted" style={{ fontSize: 12 }}>
              不指派 → 由域管理员兜底评审
            </span>
          )}
        </Space>
      </Modal>

      {/* 仲裁「保留差异+指定改名」：Owner 在详情页改名（TD §12.4，改 name 区分同名不同义） */}
      <Modal
        title="指标改名（响应仲裁要求）"
        open={renameOpen}
        onOk={handleRename}
        confirmLoading={busy}
        onCancel={() => setRenameOpen(false)}
        okText="确认改名"
      >
        <Paragraph type="secondary">
          该指标在冲突{" "}
          <span className="mono">{metric?.arbitration_mark?.conflict_id ?? ""}</span> 仲裁中被指定改名，
          以与{" "}
          <span className="mono">{metric?.arbitration_mark?.rename_opposite_code ?? "对方指标"}</span>{" "}
          区分同名不同义口径。可先用 AI 生成名称建议，采纳或编辑后提交；修改名称后仲裁标记将自动清除。
        </Paragraph>
        <Space.Compact style={{ width: "100%", marginTop: 8 }}>
          <Input
            placeholder="新的指标名称"
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
          />
          {canInferDesc && (
            <Button icon={<RobotOutlined />} loading={suggesting} onClick={handleSuggestRename}>
              AI 生成名称建议
            </Button>
          )}
        </Space.Compact>
        {renameSuggestLoaded && renameSuggestions.length > 0 && (
          <div style={{ marginTop: 10 }}>
            <Radio.Group
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              style={{ width: "100%" }}
            >
              <Space direction="vertical" style={{ width: "100%" }}>
                {renameSuggestions.map((s, i) => (
                  <Radio key={`${s.name}-${i}`} value={s.name} style={{ width: "100%" }}>
                    <Space direction="vertical" size={0}>
                      <span>{s.name}</span>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {s.source === "llm" ? "AI 生成 · " : "规则兜底 · "}
                        {s.reason}
                      </Typography.Text>
                    </Space>
                  </Radio>
                ))}
              </Space>
            </Radio.Group>
          </div>
        )}
        {renameSuggestLoaded && renameSuggestions.length === 0 && (
          <Typography.Text type="secondary" style={{ display: "block", marginTop: 8, fontSize: 12 }}>
            未生成名称建议，请手动输入新名称。
          </Typography.Text>
        )}
        <Input.TextArea
          placeholder="变更原因（至少 4 字，将写入版本记录）"
          value={renameReason}
          onChange={(e) => setRenameReason(e.target.value)}
          rows={2}
          style={{ marginTop: 8 }}
        />
      </Modal>
    </div>
  );
}
