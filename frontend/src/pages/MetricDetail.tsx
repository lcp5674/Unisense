import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  AutoComplete,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Form,
  Input,
  message,
  Modal,
  Radio,
  Segmented,
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
  CheckCircleOutlined,
  DeleteOutlined,
} from "@ant-design/icons";
import { usePermission } from "../hooks/usePermission";
import {
  addFavorite,
  approveMetric,
  deprecateMetric,
  deleteMetric,
  recoverSourceDropped,
  confirmDeprecateDropped,
  emergencyPublishMetric,
  completeEmergencyReview,
  bindMetricTerm,
  fetchArchivedMetric,
  fetchCurrentUser,
  fetchRelatedMetrics,
  getMetric,
  getMetricHealth,
  inferMetricDescription,
  listCatalogs,
  listDictItems,
  listDimensions,
  listDomainTree,
  listFavorites,
  listMetrics,
  listSubscriptions,
  listTerms,
  listUsers,
  listVersions,
  piiReview,
  promoteMetric,
  removeFavorite,
  rollbackMetric,
  submitReview,
  suggestRenameName,
  updateMetric,
  updateMetricDescription,
  upsertSubscription,
  notifyUnknownDictValues,
  verifyDictValues,
  UnisenseApiError,
} from "../api";
import type {
  ArchivedMetricResponse,
  SubjectDomainTreeNode,
  CurrentUser,
  Dimension,
  MetricHealth,
  MetricListResponse,
  MetricResponse,
  MetricUpdateRequest,
  MetricVersionResponse,
  RecommendItem,
  RenameSuggestItem,
  SubscriptionPref,
  SystemDictItem,
  UserBrief,
} from "../types";
import { useTracking } from "../hooks/useTracking";
import { enumLabel, METRIC_TYPE_LABEL, METRIC_TIER_LABEL, AGGREGATION_LABEL, TIME_SEMANTICS_LABEL, FRESHNESS_LABEL, DW_LAYER_LABEL, SERVING_MODE_LABEL, ADDITIVITY_LABEL, GRANULARITY_LABEL, UNIT_LABEL, RULING_DECISION_LABEL, METRIC_STATUS_COLOR, METRIC_STATUS_LABEL, METRIC_RELATION_EDGE_LABEL } from "../utils/enums";
import { formatCnTime, formatCnDate } from "../utils/timeCn";
import { HealthCard } from "./metric/HealthCard";
import { QualitySnapshot } from "./metric/QualitySnapshot";
import { LineageImpact } from "./metric/LineageImpact";
import { VersionHistory } from "./metric/VersionHistory";
import { AuditTimeline } from "./metric/AuditTimeline";
import { RelatedDimensions } from "./metric/RelatedDimensions";

const { Paragraph } = Typography;

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

// 常用变更原因快捷选项：高频操作（改名/紧急发布）的原因输入可一键填充再编辑，避免每次手写
const COMMON_CHANGE_REASONS = ["口径修正", "字段调整", "粒度调整", "单位变更", "逻辑优化", "需求变更"];

// 弹窗内 Select/AutoComplete 下拉面板自适应内容宽度（popupMatchSelectWidth=false）：
// 长选项（如「去重计数 (COUNT_DISTINCT)」「供应商粒度 (supplier)」）不再被截断为省略号，
// 下拉选项完整展示；minWidth 保证窄触发框下下拉仍可读
const DROPDOWN_FULL_WIDTH = {
  popupMatchSelectWidth: false,
  styles: { popup: { root: { minWidth: 280 } } },
};

// 字典未收录值引导弹窗的字典类型中文名（对齐参照数据管理 DICT_TYPE_LABELS）
const EDIT_DICT_TYPE_LABEL: Record<string, string> = {
  granularity: "粒度",
  unit: "单位",
  aggregation: "聚合方式",
  currency: "币种",
  dw_layer: "数仓层",
  freshness: "新鲜度",
  time_semantics: "时间语义",
  metric_tier: "指标分级",
};

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
function ArchivedDetailPanel({
  detail,
  domainName,
  domainInactive,
}: {
  detail: ArchivedMetricResponse | null;
  domainName: (code: string) => string;
  domainInactive: (code: string) => boolean;
}) {
  if (!detail?.metric) return null;
  const m = detail.metric;
  return (
    <Card title="作废指标历史详情（仅供追溯）" size="small" style={{ marginBottom: 16 }}>
      <Descriptions column={2} size="small" bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label="指标编码">
          <span className="mono">{m.metric_code}</span>
        </Descriptions.Item>
        <Descriptions.Item label="指标名称">{m.name}</Descriptions.Item>
        <Descriptions.Item label="业务域">
          {domainName(m.domain)}
          {domainInactive(m.domain) && <Tag style={{ marginLeft: 6 }} color="default">已停用</Tag>}
        </Descriptions.Item>
        <Descriptions.Item label="粒度">{GRANULARITY_LABEL[m.granularity] ?? m.granularity}</Descriptions.Item>
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
  const navigate = useNavigate();
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
            {dependencies.map((d) => {
              const depCode = String(d);
              return (
                <Tag
                  key={depCode}
                  className="mono"
                  style={{ cursor: "pointer", marginRight: 4 }}
                  onClick={() => navigate(`/detail/${encodeURIComponent(depCode)}`)}
                  title={`查看依赖指标 ${depCode}`}
                >
                  {depCode}
                </Tag>
              );
            })}
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
  // 主数据加载失败信息（区分「加载失败可重试」与「指标不存在」——网络/500 不应误导为不存在）
  const [loadError, setLoadError] = useState<string | null>(null);
  // 主题域 code → 中文名 / 状态（供业务域展示中文名 + 停用标识，与目录页一致）
  const [domainMap, setDomainMap] = useState<Map<string, string>>(new Map());
  const [domainStatusMap, setDomainStatusMap] = useState<Map<string, string>>(new Map());
  // 业务描述 LLM 推断 loading（第 8 轮：详情页补描述生成入口）
  const [descInferring, setDescInferring] = useState(false);
  const [descEditOpen, setDescEditOpen] = useState(false);
  const [descDraft, setDescDraft] = useState("");
  const [descSaving, setDescSaving] = useState(false);
  // 关联术语（P2-11 术语绑定写路径）：搜索式 Select 选项 + 防抖搜索
  const [termOptions, setTermOptions] = useState<Array<{ value: number; label: string }>>([]);
  const [termSearching, setTermSearching] = useState(false);
  const termSearchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
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
  // 指标编辑弹窗（TD §13）：DRAFT/REVIEW 草稿可修改名称/粒度/单位/口径后重提，
  // 消除"驳回后只能原样重提或删了重建"的闭环缺口（变更原因必填 + 乐观锁 row_version）
  const [editOpen, setEditOpen] = useState(false);
  const [editForm] = Form.useForm();
  const [editSaving, setEditSaving] = useState(false);
  const [editGranularityOptions, setEditGranularityOptions] = useState<
    Array<{ value: string; label: string }>
  >([]);
  const [editUnitOptions, setEditUnitOptions] = useState<Array<{ value: string; label: string }>>(
    [],
  );
  // 编辑弹窗关联维度（对齐注册页惰性选择）：从平台维度清单多选，写入口径 dimensions
  const [editDimensionOptions, setEditDimensionOptions] = useState<
    Array<{ value: string; label: string }>
  >([]);
  const [editDims, setEditDims] = useState<string[]>([]);
  // 编辑弹窗依赖指标（派生/复合指标）：从已发布指标选择，写入口径 dependencies
  const [editDepOptions, setEditDepOptions] = useState<Array<{ value: string; label: string }>>(
    [],
  );
  const [editDeps, setEditDeps] = useState<string[]>([]);
  // 编辑弹窗维度/依赖多选是否被用户修改（区分"未改保留"与"清空移除"）：
  // 未改 → 保留原口径；清空（dirty + 空）→ 从口径移除对应键（与解绑能力对称）
  const [editDimsDirty, setEditDimsDirty] = useState(false);
  const [editDepsDirty, setEditDepsDirty] = useState(false);
  // 编辑弹窗「落地表（source_table）」可搜索选择：血缘差异同步建「指标↔落地表」边，
  // 注册页②有源表选择、编辑弹窗此前缺失——用户无法改指标落地表（只能手写 JSON）
  const [editSourceTable, setEditSourceTable] = useState("");
  const [editSourceTableOptions, setEditSourceTableOptions] = useState<
    Array<{ value: string; label: string }>
  >([]);
  const [editSourceTableDirty, setEditSourceTableDirty] = useState(false);
  // 编辑弹窗「治理属性」（币种/聚合/时间语义/新鲜度/数仓层/分级）：
  // 指标创建后治理字段此前不可改（分层纠正/时效调整/分级晋升/币种修正只能重建指标）。
  // 后端 MetricUpdateRequest 已支持（非破坏性，不触发版本递增），前端补齐编辑入口。
  const [editGovValues, setEditGovValues] = useState<Record<string, string>>({});
  const [editGovDirty, setEditGovDirty] = useState<Set<string>>(new Set());
  const [editGovOptions, setEditGovOptions] = useState<
    Record<string, Array<{ value: string; label: string }>>
  >({});
  // 编辑弹窗口径 JSON 即时校验（对齐注册页惰性设计）：输入即报错，避免提交时才发现语法问题
  const [editDefinitionError, setEditDefinitionError] = useState<string | null>(null);
  // 编辑弹窗保存前「字典未收录值」治理引导：收集到未收录值后暂存待保存请求并弹引导，
  // 有收录权限（dict:create）引导前往参照数据管理收录、无权限确认后通知管理员收录/打回。
  // 不直接静默保存脏值——治理者能第一时间发现并处置（对齐方案 B 的「(不在字典中)」标记）。
  const [editUnknownValues, setEditUnknownValues] = useState<
    Array<{ dict_type: string; value: string }> | null
  >(null);
  const [pendingEditReq, setPendingEditReq] = useState<MetricUpdateRequest | null>(null);
  const [editUnknownNotifySaving, setEditUnknownNotifySaving] = useState(false);
  // 编辑弹窗口径定义编辑模式：expression（表达式/JSON）↔ sql（SQL 模式，对齐注册页）。
  // 开发人员可直接以 SQL 描述口径（后端 sqlglot 校验语法，sql 变更与表达式同级触发版本确认）；
  // 存量 SQL 模式指标（definition_json 含 sql/etl_sql）打开弹窗时自动落到 SQL 模式。
  const [editDefMode, setEditDefMode] = useState<"expression" | "sql">("expression");
  const [editSqlText, setEditSqlText] = useState("");
  const [renameSuggestLoaded, setRenameSuggestLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const { track } = useTracking();

  // 编辑弹窗字典（粒度/单位/治理/币种）+ 平台维度清单 + 已发布指标（依赖选项）：挂载时加载一次
  // 防御式加载：编辑弹窗辅助数据失败/返回 undefined 绝不拖垮详情页主体——每个查询独立降级为空值。
  useEffect(() => {
    const safeDict = async (code: string): Promise<SystemDictItem[]> => {
      try {
        return (await listDictItems(code)) ?? [];
      } catch {
        return [] as SystemDictItem[];
      }
    };
    const safeDims = async (): Promise<{ items: Dimension[] }> => {
      try {
        return (await listDimensions({ page_size: 100 })) ?? { items: [] as Dimension[] };
      } catch {
        return { items: [] as Dimension[] };
      }
    };
    const safeMetrics = async (): Promise<MetricListResponse> => {
      try {
        return (
          (await listMetrics({ page_size: 100, status: "PUBLISHED" })) ?? {
            items: [] as MetricResponse[],
            total: 0,
            page: 1,
            page_size: 100,
          }
        );
      } catch {
        return { items: [] as MetricResponse[], total: 0, page: 1, page_size: 100 };
      }
    };
    Promise.all([
      safeDict("granularity"),
      safeDict("unit"),
      safeDict("dw_layer"),
      safeDict("freshness"),
      safeDict("time_semantics"),
      safeDict("metric_tier"),
      safeDict("aggregation"),
      safeDict("currency"),
      safeDims(),
      safeMetrics(),
    ]).then(([g, u, dl, fr, ts, mt, ag, cur, dims, metrics]) => {
      const opts = (items: SystemDictItem[]) =>
        items
          .filter((it) => it.status === "active")
          .map((it) => ({ value: it.code, label: `${it.label} (${it.code})` }));
      setEditGranularityOptions(opts(g));
      setEditUnitOptions(opts(u));
      setEditGovOptions({
        dw_layer: opts(dl),
        freshness: opts(fr),
        time_semantics: opts(ts),
        metric_tier: opts(mt),
        aggregation: opts(ag),
        currency: opts(cur),
      });
      setEditDimensionOptions(
        (dims.items ?? [])
          .filter((d) => d.status === "PUBLISHED")
          .map((d) => ({ value: d.dim_code, label: d.name })),
      );
      setEditDepOptions(
        (metrics.items ?? []).map((m) => ({ value: m.metric_code, label: m.name })),
      );
    });
  }, []);

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
    setLoadError(null);
    try {
      const [m, vs, me, favs, healthRes, userList, domainTree, subs, rel] = await Promise.all([
        getMetric(code),
        listVersions(code),
        fetchCurrentUser(),
        listFavorites().catch(() => [] as { asset_type: string; asset_id: string }[]),
        getMetricHealth(code).catch(() => null),
        listUsers().catch(() => [] as UserBrief[]),
        listDomainTree().catch(() => [] as SubjectDomainTreeNode[]),
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
      {
        const dMap = new Map<string, string>();
        const stMap = new Map<string, string>();
        const walk = (nodes: SubjectDomainTreeNode[]) => {
          for (const n of nodes) {
            dMap.set(n.code, n.name);
            if (n.status) stMap.set(n.code, n.status);
            if (n.children?.length) walk(n.children);
          }
        };
        walk(domainTree);
        setDomainMap(dMap);
        setDomainStatusMap(stMap);
      }
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
      const reason = err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败，请稍后重试";
      message.error(reason);
      // 记录失败原因（非「指标不存在」的临时故障），供页面级失败态展示与重试
      setMetric(null);
      setLoadError(reason);
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

  // 关联术语搜索（P2-11 术语绑定写路径）：防抖调用 listTerms（仅 PUBLISHED 可绑定）
  function handleTermSearch(q: string) {
    if (termSearchTimer.current) clearTimeout(termSearchTimer.current);
    termSearchTimer.current = setTimeout(async () => {
      setTermSearching(true);
      try {
        const res = await listTerms({ search: q.trim() || undefined, status: "PUBLISHED", page_size: 20 });
        setTermOptions(
          (res.items ?? []).map((t) => ({ value: t.id, label: `${t.name}（${t.term_code}）` })),
        );
      } catch {
        // 术语搜索失败不阻断绑定流程
      } finally {
        setTermSearching(false);
      }
    }, 300);
  }

  // 绑定/解绑术语（termId=null 解绑）
  function bindTerm(termId: number | null) {
    if (!metric) return;
    void runAction(() => bindMetricTerm(metric.metric_code, termId), termId ? "术语绑定" : "术语解绑");
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
    const req: MetricUpdateRequest = {
      name,
      change_reason: renameReason.trim(),
      row_version: metric.row_version, // 跨请求乐观锁：他人已改则 409 拒绝（防静默覆盖）
    };
    await runAction(() => updateMetric(metric.metric_code, req), "指标改名");
    setRenameOpen(false);
    setRenameValue("");
    setRenameReason("");
  }

  // 编辑弹窗（TD §13）：打开时回填当前值（口径 JSON 序列化），保存走 updateMetric
  // （乐观锁 row_version + 变更原因必填）。DRAFT/REVIEW 草稿借此可修改后重提。
  function openEdit() {
    if (!metric) return;
    // 遗留值兜底：存量指标的粒度/单位可能是字典未收录的历史值（如 granularity="daily"），
    // 若不在选项中将作为兜底选项加入——避免 Select 显示空、保存时被静默清空（数据丢失）。
    // 方案 B：兜底选项 label 追加「(不在字典中)」提示，value 保留原值——既不让用户误选为正常选项，
    // 也能让治理者一眼识别历史脏数据、决策是否补录字典。
    const ensureInOptions = (
      opts: Array<{ value: string; label: string }>,
      val: string | undefined,
    ) =>
      val && !opts.some((o) => o.value === val)
        ? [{ value: val, label: `${val} (不在字典中)` }, ...opts]
        : opts;
    setEditGranularityOptions((prev) => ensureInOptions(prev, metric.granularity));
    setEditUnitOptions((prev) => ensureInOptions(prev, metric.unit));
    const def = metric.definition_json ?? {};
    const rawDims = Array.isArray(def.dimensions) ? def.dimensions.map((d) => String(d)) : [];
    // 落地表回填 + 当前值兜底选项（遗留值可显示可保留）
    const rawSrcTable = typeof def.source_table === "string" ? def.source_table : "";
    setEditSourceTable(rawSrcTable);
    setEditSourceTableOptions((prev) => ensureInOptions(prev, rawSrcTable || undefined));
    setEditSourceTableDirty(false);
    // 治理属性回填 + 遗留值兜底（字典未收录的历史值可显示可保留，防静默清空）
    const govInit: Record<string, string> = {};
    for (const f of ["dw_layer", "freshness", "time_semantics", "metric_tier"]) {
      const v = (metric as unknown as Record<string, unknown>)[f];
      if (typeof v === "string") {
        govInit[f] = v;
        setEditGovOptions((prev) => ({
          ...prev,
          [f]: ensureInOptions(prev[f] ?? [], v),
        }));
      }
    }
    if (metric.currency) {
      govInit.currency = metric.currency;
      setEditGovOptions((prev) => ({
        ...prev,
        currency: ensureInOptions(prev.currency ?? [], metric.currency ?? undefined),
      }));
    }
    // 聚合方式独立字段（口径变更，与粒度/单位同级）：其选项也需当前值兜底
    // （字典未收录的历史聚合值可显示可保留，防静默清空——对齐治理字段 ensureInOptions）
    if (metric.aggregation) {
      setEditGovOptions((prev) => ({
        ...prev,
        aggregation: ensureInOptions(prev.aggregation ?? [], metric.aggregation),
      }));
    }
    setEditGovValues(govInit);
    setEditGovDirty(new Set());
    // 口径定义编辑模式：存量 SQL 模式指标（definition_json 含 sql/etl_sql）自动落 SQL 模式，
    // 预填 SQL 文本；表达式模式预填完整 JSON（表达式/来源字段等结构化口径）。
    const rawSql = typeof def.sql === "string" ? def.sql : typeof def.etl_sql === "string" ? def.etl_sql : "";
    const isSqlMode = rawSql.trim().length > 0;
    setEditDefMode(isSqlMode ? "sql" : "expression");
    setEditSqlText(rawSql);
    editForm.setFieldsValue({
      name: metric.name,
      granularity: metric.granularity,
      unit: metric.unit,
      aggregation: metric.aggregation, // 聚合方式属口径变更，与粒度/单位同级回填
      definition_json: isSqlMode ? "" : Object.keys(def).length ? JSON.stringify(def, null, 2) : "",
    });
    setEditDims(rawDims);
    setEditDeps(
      metric.type === "atomic"
        ? []
        : (Array.isArray(def.dependencies) ? def.dependencies.map((d) => String(d)) : []),
    );
    setEditDimsDirty(false);
    setEditDepsDirty(false);
    setEditDefinitionError(null);
    setEditOpen(true);
  }

  async function handleSubmitEdit() {
    if (!metric) return;
    try {
      const values = await editForm.validateFields();
      let definitionJson: Record<string, unknown> | undefined;
      if (editDefMode === "sql") {
        // SQL 模式：口径主体为 { sql }（后端 sqlglot 校验语法），保留原口径中
        // 非 sql/etl_sql 的结构化字段（measure_column/source_fields/period 等），
        // 避免切换编辑模式导致字段丢失。
        const sql = editSqlText.trim();
        if (!sql) {
          message.error("口径 SQL 模式请输入 SQL 语句");
          return;
        }
        const base: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(metric.definition_json ?? {})) {
          // SQL 模式口径主体为 { sql }：排除 sql/etl_sql（旧 SQL 被新 SQL 取代），
          // 并排除 expression——旧表达式是表达式模式的遗留，与 sql 并存即两个矛盾口径主体
          // （DefinitionCard 会同时展示「计算口径」与「口径 SQL」，误导消费者）。
          if (k === "sql" || k === "etl_sql" || k === "expression") continue;
          base[k] = v;
        }
        definitionJson = { ...base, sql };
      } else {
        const rawDef = String(values.definition_json ?? "").trim();
        if (rawDef) {
          try {
            definitionJson = JSON.parse(rawDef);
          } catch {
            message.error("口径定义不是合法 JSON，请修正后重试");
            return;
          }
        }
      }
      // 关联维度选择器合入 definition_json.dimensions（对齐注册页，血缘生成指标↔维度边）：
      // 用户修改过才生效——非空写入、清空则从口径移除（dirty 区分"未改保留"与"清空移除"）
      if (editDimsDirty) {
        if (editDims.length) {
          definitionJson = { ...(definitionJson ?? {}), dimensions: editDims };
        } else {
          const base = definitionJson ?? { ...(metric.definition_json ?? {}) };
          const next = { ...base };
          delete next.dimensions;
          definitionJson = next;
        }
      }
      // 依赖指标选择器合入 definition_json.dependencies（非原子指标，血缘生成原子→衍生边）
      if (metric.type !== "atomic" && editDepsDirty) {
        if (editDeps.length) {
          definitionJson = { ...(definitionJson ?? {}), dependencies: editDeps };
        } else {
          const base = definitionJson ?? { ...(metric.definition_json ?? {}) };
          const next = { ...base };
          delete next.dependencies;
          definitionJson = next;
        }
      }
      // 落地表（source_table）选择器合入 definition_json（血缘差异同步建「指标↔落地表」边）：
      // dirty 区分"未改保留"与"清空移除"（清空即解除指标↔落地表关系）
      if (editSourceTableDirty) {
        if (editSourceTable.trim()) {
          definitionJson = { ...(definitionJson ?? {}), source_table: editSourceTable.trim() };
        } else {
          const base = definitionJson ?? { ...(metric.definition_json ?? {}) };
          const next = { ...base };
          delete next.source_table;
          definitionJson = next;
        }
      }
      const govPayload: Record<string, string> = {};
      for (const f of ["dw_layer", "freshness", "time_semantics", "metric_tier"]) {
        // 枚举治理字段：用户改过且非空才传（必选枚举无需清空；未改不传 → 后端保留原值）
        if (editGovDirty.has(f) && editGovValues[f]) govPayload[f] = editGovValues[f];
      }
      // currency（币种）为可选字段：dirty 时空串也传（清空币种合法终态，后端 str 字段接受空串）
      // 修复前：allowClear 清空币种后保存被 `&& value` 过滤 → 静默保留原币种（清空意图失效）
      if (editGovDirty.has("currency")) {
        govPayload.currency = editGovValues.currency ?? "";
      }
      const req: MetricUpdateRequest = {
        name: String(values.name).trim(),
        granularity: values.granularity,
        unit: values.unit,
        aggregation: values.aggregation, // 聚合方式属口径变更，与粒度/单位同级（后端触发版本确认）
        ...govPayload,
        definition_json: definitionJson,
        change_reason: String(values.change_reason ?? "").trim(),
        row_version: metric.row_version, // 跨请求乐观锁：他人已改则 409 拒绝
      };
      // 字典未收录值治理引导：保存前检测本次请求中的字典字段是否含未收录值。
      // 含未收录 → 不直接静默保存：有收录权限引导收录、无权限确认后通知管理员收录/打回。
      const unknown = collectUnknownDictValues(req);
      if (unknown.length > 0) {
        // 后端 DB 复核（前端字典快照可能过期），仅保留确实未收录的值
        try {
          const verified = await verifyDictValues(unknown);
          if (verified.unknown.length > 0) {
            setPendingEditReq(req);
            setEditUnknownValues(verified.unknown);
            return;
          }
        } catch {
          // 复核失败（网络等）：不阻断流程——按本地检测结果引导
          setPendingEditReq(req);
          setEditUnknownValues(unknown);
          return;
        }
      }
      await doSaveEdit(req);
    } catch (err) {
      if (err instanceof Error && "errorFields" in err) return; // 表单校验错误，已高亮
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    } finally {
      setEditSaving(false);
    }
  }

  // 收集本次保存将写入指标的字典未收录值（前端已加载字典判定，引导弹窗二次经后端复核）。
  // 覆盖粒度/单位/聚合/币种/治理五属性——存量脏值（如 unit=cnt）也在保存时被识别提醒。
  function collectUnknownDictValues(req: MetricUpdateRequest) {
    // known 集合只取「字典真实收录」的选项：ensureInOptions 加入的兜底选项
    // （label 以「(不在字典中)」结尾）代表当前值本就是未收录脏值，必须排除——
    // 否则脏值永远命中 known，引导弹窗形同虚设。
    const dictValues = (opts: Array<{ value: string; label: string }>) =>
      new Set(
        opts.filter((o) => !o.label.endsWith("(不在字典中)")).map((o) => o.value),
      );
    const known: Record<string, Set<string>> = {
      granularity: dictValues(editGranularityOptions),
      unit: dictValues(editUnitOptions),
      aggregation: dictValues(editGovOptions.aggregation ?? []),
      currency: dictValues(editGovOptions.currency ?? []),
      dw_layer: dictValues(editGovOptions.dw_layer ?? []),
      freshness: dictValues(editGovOptions.freshness ?? []),
      time_semantics: dictValues(editGovOptions.time_semantics ?? []),
      metric_tier: dictValues(editGovOptions.metric_tier ?? []),
    };
    const checks: Array<{ dict_type: string; value: string | null | undefined }> = [
      { dict_type: "granularity", value: req.granularity },
      { dict_type: "unit", value: req.unit },
      { dict_type: "aggregation", value: req.aggregation },
      { dict_type: "currency", value: req.currency },
      { dict_type: "dw_layer", value: req.dw_layer },
      { dict_type: "freshness", value: req.freshness },
      { dict_type: "time_semantics", value: req.time_semantics },
      { dict_type: "metric_tier", value: req.metric_tier },
    ];
    const unknown: Array<{ dict_type: string; value: string }> = [];
    for (const c of checks) {
      if (c.value == null) continue;
      const v = String(c.value).trim();
      if (!v) continue;
      if (!known[c.dict_type].has(v)) unknown.push({ dict_type: c.dict_type, value: v });
    }
    return unknown;
  }

  // 实际执行保存（含状态提示）：引导弹窗「仍按原值保存 / 通知管理员并保存」均走此路径。
  async function doSaveEdit(req: MetricUpdateRequest) {
    if (!metric) return;
    setEditSaving(true);
    try {
      await updateMetric(metric.metric_code, req);
      if (metric.status === "REVIEW") {
        message.success("修改已保存，指标已退回草稿，请重新提交评审");
      } else if (metric.status === "PUBLISHED") {
        // 已发布指标：破坏性变更（粒度/单位/聚合/口径）触发 PENDING 确认期，
        // 治理属性变更直接生效——后端返回后提示用户进入确认流程
        message.success(
          "变更已提交：破坏性修改进入消费方确认期（确认后新口径生效），治理属性已直接更新",
        );
      } else {
        message.success("指标已更新");
      }
      setEditOpen(false);
      await load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "更新失败");
    } finally {
      setEditSaving(false);
    }
  }

  // 未收录值引导弹窗确认：notifyAdmin=true 先通知平台管理员收录/打回（仅无收录权限时）再按原值保存；
  // notifyAdmin=false 直接按原值保存（有收录权限者可选稍后自行收录）。值不自动进字典——受控词表
  // 由治理者统一维护，脏值写入同时提醒，避免静默污染字典。
  async function handleUnknownValueConfirm(notifyAdmin: boolean) {
    if (!metric || !pendingEditReq) return;
    const req = pendingEditReq;
    const unknown = editUnknownValues ?? [];
    setPendingEditReq(null);
    setEditUnknownValues(null);
    if (notifyAdmin && unknown.length > 0) {
      setEditUnknownNotifySaving(true);
      try {
        await notifyUnknownDictValues({
          metric_code: metric.metric_code,
          values: unknown,
          note: String(req.change_reason ?? "").trim() || undefined,
        });
      } catch {
        message.warning("通知管理员失败，仍按原值保存（可在参照数据管理手动收录）");
      } finally {
        setEditUnknownNotifySaving(false);
      }
    }
    await doSaveEdit(req);
  }

  // 有收录权限者选择「前往收录」：放弃本次保存，跳转参照数据管理补充词条后回来重提。
  function handleGoManageDict() {
    setPendingEditReq(null);
    setEditUnknownValues(null);
    setEditOpen(false);
    navigate("/dicts");
  }

  // 落地表搜索（对齐注册页②源表惰性搜索）：空关键词展开加载平台已采集表，输入即按关键词过滤
  async function handleEditSrcSearch(q: string) {
    try {
      const res = await listCatalogs({ keyword: q.trim() || undefined, source_status: "active" });
      const items = Array.isArray(res) ? res : res.items ?? [];
      const opts = items
        .filter((c: { entity_type?: string }) => !c.entity_type || c.entity_type === "TABLE")
        .map((c: { entity_name: string; source_name?: string | null }) => ({
          value: c.entity_name,
          label: c.source_name ? `${c.entity_name}（${c.source_name}）` : c.entity_name,
        }));
      setEditSourceTableOptions(opts);
    } catch {
      // 搜索失败静默：不影响编辑主流程（可手输落地表）
    } finally {
    }
  }

  // 口径 JSON 即时校验（对齐注册页惰性设计）：输入即校验语法，避免提交时才发现
  function handleEditJsonChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const raw = e.target.value;
    editForm.setFieldValue("definition_json", raw);
    if (!raw.trim()) {
      setEditDefinitionError(null);
      return;
    }
    try {
      JSON.parse(raw);
      setEditDefinitionError(null);
    } catch {
      setEditDefinitionError("口径定义不是合法 JSON");
    }
  }

  // 一键格式化 JSON（对齐注册页），非法时提示
  function handleFormatEditJson() {
    const raw = String(editForm.getFieldValue("definition_json") ?? "").trim();
    if (!raw) return;
    try {
      editForm.setFieldValue("definition_json", JSON.stringify(JSON.parse(raw), null, 2));
      setEditDefinitionError(null);
    } catch {
      setEditDefinitionError("口径定义不是合法 JSON，无法格式化");
    }
  }

  // 口径定义编辑模式切换（表达式/JSON ↔ SQL，对齐注册页）：双向迁移已编辑内容——
  // 表达式→SQL 提取 json.sql/etl_sql 填入 SQL 框（无则留空）；SQL→表达式将 SQL 包为
  // { sql } 写回 JSON 框。独立选择器管理的 dimensions/dependencies/source_table 不受影响。
  function handleEditDefModeChange(mode: "expression" | "sql") {
    if (mode === editDefMode) return;
    if (mode === "sql") {
      // 表达式 → SQL：从当前 JSON 提取 SQL 口径（sql/etl_sql 兼容键），其余字段保留在
      // 各独立选择器（维度/依赖/落地表），切换后 SQL 框为口径主体。
      let sql = "";
      const raw = String(editForm.getFieldValue("definition_json") ?? "").trim();
      if (raw) {
        try {
          const parsed = JSON.parse(raw) as Record<string, unknown>;
          sql = typeof parsed.sql === "string" ? parsed.sql : typeof parsed.etl_sql === "string" ? parsed.etl_sql : "";
        } catch {
          // 非法 JSON 不阻断切换：SQL 框留空，用户自行输入（提交时后端/前端校验兜底）
        }
      }
      setEditSqlText(sql);
      setEditDefinitionError(null);
    } else {
      // SQL → 表达式：将 SQL 包为 { sql } 写回 JSON 框（格式化展示，用户可继续手工调整）
      const sql = editSqlText.trim();
      editForm.setFieldValue("definition_json", sql ? JSON.stringify({ sql }, null, 2) : "");
      setEditDefinitionError(null);
    }
    setEditDefMode(mode);
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
          <ArchivedDetailPanel detail={archived.detail} domainName={(c) => domainMap.get(c) ?? c} domainInactive={(c) => domainStatusMap.get(c) === "inactive"} />
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
        {loadError ? (
          <div style={{ padding: "24px 0", textAlign: "center" }}>
            <Paragraph type="secondary">指标加载失败：{loadError}</Paragraph>
            <Button type="primary" onClick={() => { setLoadError(null); void load(); }}>重试</Button>
          </div>
        ) : (
          <Paragraph type="secondary">指标不存在</Paragraph>
        )}
      </Card>
    );
  }

  const role = currentUser?.role || "";
  const isAdmin = role === "platform_admin" || role === "domain_admin";
  const isOwnerOrAdmin = isAdmin || role === "metric_owner";
  const piiMasked = metric.pii_flag && !SENSITIVE_ROLES.includes(role);
  // 评审指派校验（对齐审批页）：仅被指派评审人可审批/灰度。
  // user 指派 → 须为被指派人本人；domain 指派 → 须为域评审角色（platform_admin/domain_admin/reviewer）；
  // 未指派 → 后端按域管理员兜底，前端不设限。最终裁决由后端 _assert_reviewer_authorized 兜底。
  const canActAsReviewer =
    isAdmin ||
    (metric?.reviewer_type === "user"
      ? metric.reviewer_id != null && metric.reviewer_id === currentUser?.id
      : metric?.reviewer_type === "domain"
        ? role === "domain_admin" || role === "reviewer"
        : true);
  const notAssignedReviewer = metric?.status === "REVIEW" && !canActAsReviewer;

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
  // 删除 DRAFT 草稿（软删）：仅平台管理员（后端 DELETE 端点 platform_admin-only）
  const canDelete = permReady && can("metric:delete");
  // 回滚是高风险操作（灰度→退回上一 PUBLISHED 版本），用专用权限点 metric:rollback 门禁
  // （而非笼统的 metric:edit），与后端 _WRITE_DEPS + PDP owner 校验形成前后端双边界。
  const canRollback = permReady && can("metric:rollback");
  // 字典收录权限（dict:create）：保存未收录字典值时，有权限者可引导前往参照数据管理
  // 自行收录；无权限者确认后通知平台管理员收录/打回（后端 DICT_UNKNOWN_NOTIFY）。
  const canManageDict = permReady && can("dict:create");
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
      {/* 提交评审仅 DRAFT / DEPRECATED（重新评审）：后端状态机 EXPERIMENTAL 无 →REVIEW 跃迁
          （灰度指标只能 promote 转正式 / rollback 回滚，不能退回评审） */}
      {(metric.status === "DRAFT" || metric.status === "DEPRECATED") && canCreate && (
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
      {/* 编辑入口（TD §13）：DRAFT/REVIEW 草稿可修改名称/粒度/单位/口径后重提——
          消除"驳回后只能原样重提或删了重建"的闭环缺口；PUBLISHED 已发布指标经此
          「发起变更申请」（后端破坏性字段触发 PENDING_VERSION 消费方确认、治理属性
          直接生效）——修复前 PUBLISHED 无编辑入口，后端 update_metric 的发布态变更
          能力前端不可达（仅 owner/admin + 创建权限） */}
      {(metric.status === "DRAFT" || metric.status === "REVIEW" || metric.status === "PUBLISHED") &&
        canCreate &&
        isOwnerOrAdmin && (
          <Button icon={<EditOutlined />} loading={busy} onClick={openEdit}>
            {metric.status === "PUBLISHED" ? "发起变更申请" : "编辑"}
          </Button>
        )}
      {/* 删除 DRAFT 草稿（软删）：仅平台管理员；删除后指标进回收站（archived 列表可恢复）。
          详情页删除入口消除「DRAFT 单删需回目录批量删」的闭环缺口（复审 D2） */}
      {metric.status === "DRAFT" && canDelete && (
        <Button
          danger
          icon={<DeleteOutlined />}
          loading={busy}
          onClick={() =>
            Modal.confirm({
              title: "确认删除草稿指标？",
              content: `「${metric.name}」（${metric.metric_code}）为 DRAFT 草稿，删除后进入回收站（可在已归档列表恢复），不可被提交评审。确认继续？`,
              okText: "确认删除",
              cancelText: "取消",
              okButtonProps: { danger: true },
              onOk: () =>
                runAction(() => deleteMetric(metric.metric_code), "删除草稿").then(() => {
                  message.success("草稿已删除");
                  navigate("/metrics");
                }),
            })
          }
        >
          删除
        </Button>
      )}
      {metric.status === "REVIEW" && canApprove && (
        <Button
          type="primary"
          loading={busy}
          onClick={() => runAction(() => approveMetric(metric.metric_code, {}), "审批通过")}
          disabled={piiUnreviewed || notAssignedReviewer}
          title={notAssignedReviewer ? "您未被指定为该指标的评审人" : undefined}
        >
          审批通过{notAssignedReviewer ? "（未被指派评审）" : piiUnreviewed ? "（需先 PII 复核）" : ""}
        </Button>
      )}
      {metric.status === "REVIEW" && canApprove && (
        <Button
          icon={<ExperimentOutlined />}
          loading={busy}
          onClick={() => setGrayOpen(true)}
          disabled={notAssignedReviewer}
          title={notAssignedReviewer ? "您未被指定为该指标的评审人" : undefined}
        >
          灰度发布
        </Button>
      )}
      {metric.status === "EXPERIMENTAL" && isOwnerOrAdmin && canEdit && (
        <>
          <Button
            icon={<RiseOutlined />}
            loading={busy}
            onClick={() =>
              Modal.confirm({
                title: "确认全量发布？",
                content: `「${metric.name}」将从灰度（EXPERIMENTAL）转为全量发布（PUBLISHED），对所有租户开放消费。确认继续？`,
                okText: "确认发布",
                cancelText: "取消",
                onOk: () =>
                  runAction(() => promoteMetric(metric.metric_code), "全量发布").then(() => {
                    message.success("已全量发布");
                  }),
              })
            }
          >
            全量发布
          </Button>
          {canRollback && (
            <Button
              danger
              icon={<RollbackOutlined />}
              loading={busy}
              onClick={() =>
                Modal.confirm({
                  title: "确认回滚灰度版本？",
                  content: `「${metric.name}」回滚为高风险操作：当前灰度版本将被归档，指标口径回退至上一发布版本，灰度租户将不可再消费新口径。确认继续？`,
                  okText: "确认回滚",
                  cancelText: "取消",
                  okButtonProps: { danger: true },
                  onOk: () =>
                    runAction(() => rollbackMetric(metric.metric_code), "回滚").then(() => {
                      message.success("已回滚");
                    }),
                })
              }
            >
              回滚
            </Button>
          )}
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
      {/* 紧急发布补审（FR-022 闭环）：紧急发布跳过 REVIEW，须由管理角色 24h 内补审；
          补审后 emergency_reviewed_at 落库，每日巡检不再告警超时 */}
      {metric.emergency_publish && !metric.emergency_reviewed_at && canEmergency && (
        <Button
          type="primary"
          icon={<CheckCircleOutlined />}
          loading={busy}
          onClick={() =>
            Modal.confirm({
              title: "完成紧急发布补审",
              content: `确认已完成对「${metric.name}」的紧急发布补审？补审后巡检将不再对该指标告警超时。`,
              okText: "确认补审",
              cancelText: "取消",
              onOk: () =>
                runAction(() => completeEmergencyReview(metric.metric_code), "补审").then(() => {
                  message.success("紧急发布补审完成");
                }),
            })
          }
        >
          紧急补审
        </Button>
      )}
      {/* 废弃仅对 PUBLISHED：后端状态机 EXPERIMENTAL 无 →DEPRECATED 跃迁（只有 promote 转正式），
          对灰度指标显示废弃按钮会被 409 拒绝——灰度指标的退出路径是「回滚」而非废弃 */}
      {metric.status === "PUBLISHED" && isOwnerOrAdmin && canDeprecate && (
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
        <Tooltip
          title={`灰度租户：${metric.gray_tenant_ids.join("、")}`}
          placement="top"
        >
          <Tag color="purple">灰度 {metric.gray_tenant_ids.length} 租户</Tag>
        </Tooltip>
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
            <Tag color={METRIC_STATUS_COLOR[metric.status]}>{METRIC_STATUS_LABEL[metric.status] ?? metric.status}</Tag>
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

      {/* 非发布状态引导：DRAFT/REVIEW/EXPERIMENTAL 尚未成为可消费口径（对齐作废/废弃横幅） */}
      {(metric.status === "DRAFT" || metric.status === "REVIEW" || metric.status === "EXPERIMENTAL") && (
        <Alert
          type="info"
          showIcon
          message={`该指标当前为「${METRIC_STATUS_LABEL[metric.status] ?? metric.status}」，尚未发布为可消费口径`}
          description="请勿在生产消费中使用该口径；发布后（或从「待办中心」处理该指标后）方可对外消费。"
          style={{ marginBottom: 16 }}
        />
      )}

      {/* 驳回原因引导（FR-005 闭环）：REVIEW→DRAFT 落库的驳回原因，引导提交人修改后重提 */}
      {metric.status === "DRAFT" && metric.reject_reason && (
        <Alert
          type="warning"
          showIcon
          message="该指标上次评审被驳回"
          description={
            <span>
              驳回原因：{metric.reject_reason}
              {metric.rejected_at && (
                <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                  （{formatCnTime(metric.rejected_at)}）
                </span>
              )}
            </span>
          }
          action={
            canCreate && isOwnerOrAdmin ? (
              <Button size="small" icon={<EditOutlined />} onClick={openEdit}>
                去修改
              </Button>
            ) : undefined
          }
          style={{ marginBottom: 16 }}
        />
      )}

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

      {/* 业务描述（治理补充）：展示已有描述；有权限时可 LLM 推断生成（能力对齐资产地图） */}
      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title="业务描述"
        extra={
          <Space size={8}>
            {isOwnerOrAdmin && canEdit && !piiMasked && (
              <Button
                size="small"
                icon={<EditOutlined />}
                onClick={() => {
                  setDescDraft(metric.description ?? "");
                  setDescEditOpen(true);
                }}
              >
                编辑描述
              </Button>
            )}
            {canInferDesc && !piiMasked ? (
              <Button
                size="small"
                icon={<RobotOutlined />}
                loading={descInferring}
                onClick={() => {
                  // 已有 LLM 描述时：后端 force=false 会静默短路返回旧值，
                  // 前端若直接调用会误报"已生成"却未真正重新生成。故区分：
                  // 已有 AI 描述 → 确认后 force=true 强制重新生成；
                  // 无描述/手动描述 → 直接生成（无需确认）。
                  const hasLlmDesc = metric.description_source === "llm" && !!metric.description;
                  const run = async () => {
                    setDescInferring(true);
                    try {
                      const updated = await inferMetricDescription(
                        metric.metric_code,
                        hasLlmDesc ? { force: true } : undefined,
                      );
                      message.success(
                        updated.description ? "AI 已生成业务描述" : "暂无可用信息生成描述",
                      );
                      if (updated.description) setMetric(updated);
                    } catch (err) {
                      message.error(
                        err instanceof UnisenseApiError
                          ? `${err.message}（${err.codeZh}）`
                          : "生成描述失败",
                      );
                    } finally {
                      setDescInferring(false);
                    }
                  };
                  if (hasLlmDesc) {
                    Modal.confirm({
                      title: "重新生成 AI 描述",
                      content: "该指标已有 AI 生成描述，重新生成将覆盖当前内容。",
                      okText: "重新生成",
                      cancelText: "取消",
                      onOk: run,
                    });
                  } else {
                    void run();
                  }
                }}
              >
                AI 生成描述
              </Button>
            ) : null}
          </Space>
        }
      >
        {piiMasked ? (
          <span className="muted">该指标含个人信息，业务描述已隐藏（与口径定义同级别脱敏保护）。</span>
        ) : metric.description ? (
          <Paragraph style={{ margin: 0, whiteSpace: "pre-wrap" }}>{metric.description}</Paragraph>
        ) : (
          <span className="muted">暂无业务描述{canInferDesc ? "，可点击右上角「AI 生成描述」自动生成" : ""}</span>
        )}
      </Card>

      {/* 关联术语（P2-11 术语绑定写路径）：指标归属业务术语治理，Owner/管理可搜索绑定 */}
      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title="关联术语"
        extra={
          isOwnerOrAdmin && canEdit && !piiMasked && metric.term_id ? (
            <Button size="small" danger onClick={() => bindTerm(null)}>
              解绑
            </Button>
          ) : null
        }
      >
        {piiMasked ? (
          <span className="muted">该指标含个人信息，术语绑定已隐藏。</span>
        ) : isOwnerOrAdmin && canEdit ? (
          <Select
            showSearch
            allowClear
            style={{ width: 320 }}
            placeholder="搜索并绑定业务术语"
            value={metric.term_id ?? undefined}
            filterOption={false}
            onSearch={handleTermSearch}
            onChange={(v) => bindTerm(v ?? null)}
            options={termOptions}
            loading={termSearching}
            notFoundContent={termSearching ? "搜索中…" : "未找到匹配术语"}
          />
        ) : metric.term_id ? (
          <span>
            已绑定术语{" "}
            {termOptions.find((t) => t.value === metric.term_id)?.label ?? `#${metric.term_id}`}
          </span>
        ) : (
          <span className="muted">未绑定业务术语</span>
        )}
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
            { key: "lineage", label: "血缘影响", children: <LineageImpact key={`${metric.metric_code}-v${metric.row_version ?? 0}`} metricCode={metric.metric_code} /> },
            { key: "versions", label: `版本历史 (${versions.length})`, children: <VersionHistory metricCode={metric.metric_code} versions={versions} effectiveVersion={metric.effective_version} onChanged={load} canConfirm={canCreate} /> },
            { key: "dims", label: "关联维度", children: <RelatedDimensions key={`dims-${metric.row_version ?? 0}`} metricId={metric.id} /> },
            { key: "audit", label: "变更审计", children: <AuditTimeline entityType="metric_definition" entityId={metric.metric_code} emptyText="暂无该指标的变更审计记录" /> },
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
                  {r.reason ? r.reason : `血缘 · ${METRIC_RELATION_EDGE_LABEL[r.edge_type] ?? r.edge_type}`}
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
        <Space wrap size={4} style={{ marginTop: 8 }}>
          <span className="muted" style={{ fontSize: 12 }}>快捷原因：</span>
          {COMMON_CHANGE_REASONS.map((r) => (
            <Tag key={r} style={{ cursor: "pointer" }} onClick={() => setEmergencyReason((v) => (v ? `${v}，${r}` : r))}>
              {r}
            </Tag>
          ))}
        </Space>
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
        <Select
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder="替代指标（选填，须为已发布指标；留空表示无替代）"
          value={successor || undefined}
          onChange={(v) => setSuccessor(v ?? "")}
          options={editDepOptions.filter((o) => o.value !== metric.metric_code)}
          notFoundContent="无已发布指标可作替代"
          {...DROPDOWN_FULL_WIDTH}
        />
        <p className="muted" style={{ marginTop: 8 }}>
          留空表示该指标无替代（直接废弃下线）；填写后废弃指标的消费方会看到「替代指标」引导。可从已发布指标搜索选择，无需手输编码。
        </p>
      </Modal>

      {/* 编辑业务描述弹窗：复用 updateMetricDescription（已发布指标也可维护非口径描述） */}
      <Modal
        title="编辑业务描述"
        open={descEditOpen}
        confirmLoading={descSaving}
        onCancel={() => setDescEditOpen(false)}
        okText="保存描述"
        onOk={async () => {
          setDescSaving(true);
          try {
            const updated = await updateMetricDescription(
              metric.metric_code,
              descDraft.trim(),
              metric.row_version, // 跨请求乐观锁：他人已改则 409 拒绝（防静默覆盖）
            );
            message.success("业务描述已更新");
            setMetric(updated);
            setDescEditOpen(false);
          } catch (err) {
            message.error(
              err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "保存描述失败",
            );
          } finally {
            setDescSaving(false);
          }
        }}
      >
        <Input.TextArea
          rows={4}
          value={descDraft}
          onChange={(e) => setDescDraft(e.target.value)}
          placeholder="填写指标的业务定义、口径背景或使用场景说明"
          maxLength={2000}
          showCount
        />
        <Paragraph type="secondary" style={{ marginTop: 8 }}>
          描述为治理补充字段，不触发版本变更；传空串可清除描述。
        </Paragraph>
      </Modal>

      {/* 提交评审弹窗（TD §13）：可指派评审用户或域评审组，审批页仅被指派者可评审 */}
      <Modal
        title="提交评审"
        open={submitOpen}
        onOk={() => {
          if (submitReviewerType === "user" && submitReviewerId == null) {
            message.warning("选择「指定评审用户」后请选择具体评审人");
            return;
          }
          return runAction(
            () =>
              submitReview(metric.metric_code, "提交评审", {
                reviewer_id: submitReviewerType === "user" ? submitReviewerId : null,
                reviewer_type: submitReviewerType,
                reviewer_domain: metric.domain,
              }),
            "提交评审",
          ).then(() => setSubmitOpen(false));
        }}
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
            {...DROPDOWN_FULL_WIDTH}
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
              {...DROPDOWN_FULL_WIDTH}
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
        <Space wrap size={4} style={{ marginTop: 6 }}>
          <span className="muted" style={{ fontSize: 12 }}>快捷原因：</span>
          {COMMON_CHANGE_REASONS.map((r) => (
            <Tag key={r} style={{ cursor: "pointer" }} onClick={() => setRenameReason((v) => (v ? `${v}，${r}` : r))}>
              {r}
            </Tag>
          ))}
        </Space>
      </Modal>
      {/* 指标编辑弹窗（TD §13）：DRAFT/REVIEW 草稿修改名称/粒度/单位/口径后重提，
          变更原因必填 + 乐观锁 row_version（他人已改则 409） */}
      <Modal
        title="编辑指标"
        open={editOpen}
        onOk={handleSubmitEdit}
        confirmLoading={editSaving}
        onCancel={() => setEditOpen(false)}
        okText="保存"
        width={760}
        className="metric-edit-modal"
      >
        {metric.status === "PUBLISHED" && (
          <Alert
            type={metric.pending_version ? "error" : "warning"}
            showIcon
            style={{ marginBottom: 12 }}
            message={
              metric.pending_version
                ? "存在待确认的破坏性变更：当前不可再次发起破坏性变更"
                : "该指标已发布：变更可能触发口径版本确认"
            }
            description={
              metric.pending_version ? (
                <span>
                  该指标存在<b>待确认的破坏性变更</b>（版本 {metric.version}），
                  修改<b>粒度/单位/聚合方式/口径定义</b>将被拒绝；请先在
                  「版本历史」完成确认或等待超时后再发起新变更。治理属性与名称仍可直接修改。
                </span>
              ) : (
                <span>
                  修改<b>粒度/单位/聚合方式/口径定义</b>（破坏性变更）将进入{" "}
                  <b>PENDING 确认期</b>，需消费方确认后新口径才生效；仅修改治理属性
                  （数仓层/时效/分级/币种等）与名称将直接生效、不触发版本确认。
                </span>
              )
            }
          />
        )}
        <Form form={editForm} layout="vertical" scrollToFirstError>
          <Form.Item
            name="name"
            label="指标名称"
            rules={[{ required: true, message: "请输入指标名称" }]}
          >
            <Input maxLength={128} placeholder="指标名称" />
          </Form.Item>
          <Space size={16} style={{ width: "100%" }}>
            <Form.Item name="granularity" label="粒度" style={{ marginBottom: 8, flex: 1 }}>
              <Select
                allowClear
                placeholder="选择粒度"
                options={editGranularityOptions}
                showSearch
                optionFilterProp="label"
                {...DROPDOWN_FULL_WIDTH}
              />
            </Form.Item>
            <Form.Item name="unit" label="单位" style={{ marginBottom: 8, flex: 1 }}>
              <Select
                allowClear
                placeholder="选择单位"
                options={editUnitOptions}
                showSearch
                optionFilterProp="label"
                {...DROPDOWN_FULL_WIDTH}
              />
            </Form.Item>
            <Form.Item
              name="aggregation"
              label="聚合方式"
              style={{ marginBottom: 8, flex: 1 }}
              tooltip="聚合方式（SUM/AVG 等）属口径变更——修改后需消费方确认（版本 PENDING），与粒度/单位同级；非普通治理属性"
            >
              <Select
                allowClear
                placeholder="选择聚合方式"
                options={editGovOptions.aggregation ?? []}
                showSearch
                optionFilterProp="label"
                {...DROPDOWN_FULL_WIDTH}
              />
            </Form.Item>
          </Space>
          {/* 治理属性（非破坏性变更，不触发版本递增）：创建后治理字段此前不可改，
              现可编辑——币种修正/数仓层纠正/时效调整/时间语义变更/分级晋升/聚合调整 */}
          <Space wrap size={12} style={{ width: "100%" }}>
            <Form.Item
              label="币种"
              style={{ marginBottom: 8, flex: 1, minWidth: 140 }}
              tooltip="ISO 4217 标准币种（受控词表，参照数据管理维护）；留空表示不设币种"
            >
              <Select
                allowClear
                placeholder="选择币种（留空清除）"
                options={editGovOptions.currency ?? []}
                showSearch
                optionFilterProp="label"
                value={editGovValues.currency}
                onChange={(v) => {
                  setEditGovValues((p) => ({ ...p, currency: v ?? "" }));
                  setEditGovDirty((p) => new Set(p).add("currency"));
                }}
                {...DROPDOWN_FULL_WIDTH}
              />
            </Form.Item>
            {(
              [
                ["dw_layer", "数仓层"],
                ["freshness", "新鲜度"],
                ["time_semantics", "时间语义"],
                ["metric_tier", "指标分级"],
              ] as const
            ).map(([field, label]) => (
              <Form.Item key={field} label={label} style={{ marginBottom: 8, flex: 1, minWidth: 160 }}>
                <Select
                  allowClear
                  placeholder={`选择${label}`}
                  options={editGovOptions[field] ?? []}
                  showSearch
                  optionFilterProp="label"
                  value={editGovValues[field]}
                  onChange={(v) => {
                    setEditGovValues((p) => ({ ...p, [field]: v ?? "" }));
                    setEditGovDirty((p) => new Set(p).add(field));
                  }}
                  {...DROPDOWN_FULL_WIDTH}
                />
              </Form.Item>
            ))}
          </Space>
          <Form.Item
            label="落地表（source_table）"
            extra="选择指标物理落地表，血缘图谱据此生成指标↔落地表边；可搜索平台已采集表或直接输入，清空则解除落地表关系。"
            style={{ marginBottom: 8 }}
          >
            <AutoComplete
              data-testid="editSourceTable"
              value={editSourceTable}
              options={editSourceTableOptions}
              // antd AutoComplete：onChange 仅在「选中选项」时触发，手输走 onSearch——
              // 两者都同步 state+dirty，保证手输与选择两种方式保存都生效
              onSearch={(v) => {
                setEditSourceTable(v);
                setEditSourceTableDirty(true);
                if (v.trim()) handleEditSrcSearch(v.trim());
              }}
              onSelect={(v) => {
                setEditSourceTable(v);
                setEditSourceTableDirty(true);
              }}
              onChange={(v) => {
                setEditSourceTable(v);
                setEditSourceTableDirty(true);
              }}
              onOpenChange={(open) => {
                if (open && !editSourceTableOptions.length) handleEditSrcSearch("");
              }}
              placeholder="搜索或输入落地表（如 dwd.sales_detail）"
              allowClear
              style={{ width: "100%" }}
              {...DROPDOWN_FULL_WIDTH}
            />
          </Form.Item>
          <Form.Item
            label="关联维度"
            extra="从平台维度清单选择，将写入口径定义 dimensions；血缘图谱据此生成指标↔维度边。"
            style={{ marginBottom: 8 }}
          >
            <Select
              mode="multiple"
              value={editDims}
              onChange={(v) => {
                setEditDims(v);
                setEditDimsDirty(true);
              }}
              placeholder="选择关联维度（可搜索）"
              options={editDimensionOptions}
              showSearch
              optionFilterProp="label"
              allowClear
              {...DROPDOWN_FULL_WIDTH}
            />
          </Form.Item>
          {metric?.type !== "atomic" && (
            <Form.Item
              label="依赖指标"
              extra="派生/复合指标的上游（从已发布指标选择）；血缘图谱据此生成 原子→衍生 边。"
              style={{ marginBottom: 8 }}
            >
              <Select
                mode="multiple"
                value={editDeps}
                onChange={(v) => {
                  setEditDeps(v);
                  setEditDepsDirty(true);
                }}
                placeholder="选择依赖指标（可搜索）"
                options={editDepOptions}
                showSearch
                optionFilterProp="label"
                allowClear
                {...DROPDOWN_FULL_WIDTH}
              />
            </Form.Item>
          )}
          {/* 口径定义编辑模式（对齐注册页）：开发人员可直接以 SQL 描述口径，
              后端 sqlglot 校验语法；SQL 模式口径变更与表达式同级触发版本确认 */}
          <Form.Item
            label="口径定义模式"
            style={{ marginBottom: 8 }}
            extra="SQL 模式对开发人员更便捷：直接编写口径 SQL，后端将用 sqlglot 校验语法。"
          >
            <Segmented
              block
              value={editDefMode}
              onChange={(v) => handleEditDefModeChange(v as "expression" | "sql")}
              options={[
                { value: "expression", label: "表达式（JSON）" },
                { value: "sql", label: "SQL 模式" },
              ]}
            />
          </Form.Item>
          {editDefMode === "sql" ? (
            <Form.Item
              label="口径 SQL"
              validateStatus={editDefinitionError ? "error" : undefined}
              help={
                editDefinitionError ||
                "留空表示不修改口径。SQL 口径变更与表达式同级触发版本确认；后端将用 sqlglot 校验 SQL 语法。"
              }
              style={{ marginBottom: 8 }}
            >
              <Input.TextArea
                rows={6}
                className="mono"
                data-testid="editSqlText"
                value={editSqlText}
                onChange={(e) => {
                  setEditSqlText(e.target.value);
                  if (editDefinitionError) setEditDefinitionError(null);
                }}
                placeholder={"SELECT SUM(amount) AS gmv\nFROM dwd.sales_detail"}
              />
            </Form.Item>
          ) : (
            <Form.Item
              name="definition_json"
              label="口径定义（JSON）"
              validateStatus={editDefinitionError ? "error" : undefined}
              help={editDefinitionError || "留空表示不修改口径。修改口径将触发破坏性变更校验与版本递增。"}
              extra={
                <Space wrap size={8}>
                  <Button size="small" icon={<RobotOutlined />} onClick={handleFormatEditJson}>
                    格式化 JSON
                  </Button>
                </Space>
              }
              style={{ marginBottom: 8 }}
            >
              <Input.TextArea
                rows={6}
                className="mono"
                onChange={handleEditJsonChange}
                placeholder={'{"expression": "sum(amount)", "source_tables": ["dwd.sales_detail"]}'}
              />
            </Form.Item>
          )}
          <Form.Item
            name="change_reason"
            label="变更原因"
            rules={[{ required: true, min: 4, message: "变更原因至少 4 字" }]}
            style={{ marginBottom: 8 }}
          >
            <Input.TextArea rows={2} placeholder="请填写变更原因（至少 4 字，将写入版本记录）" />
          </Form.Item>
          <Space wrap size={4}>
            <span className="muted" style={{ fontSize: 12 }}>快捷原因：</span>
            {COMMON_CHANGE_REASONS.map((r) => (
              <Tag
                key={r}
                style={{ cursor: "pointer" }}
                onClick={() =>
                  editForm.setFieldValue(
                    "change_reason",
                    (editForm.getFieldValue("change_reason") || "") ? `${editForm.getFieldValue("change_reason")}，${r}` : r,
                  )
                }
              >
                {r}
              </Tag>
            ))}
          </Space>
        </Form>
      </Modal>

      {/* 字典未收录值治理引导弹窗：保存前发现本次请求含字典未收录值时的分流处置。
          原则：受控词表不自动新增——有收录权限者引导前往参照数据管理收录；
          无权限者确认后通知平台管理员收录/打回（值仍原样保存，不静默进字典） */}
      <Modal
        title="发现字典未收录值"
        open={editUnknownValues != null}
        onCancel={() => {
          setPendingEditReq(null);
          setEditUnknownValues(null);
        }}
        footer={
          canManageDict ? (
            [
              <Button key="save" onClick={() => handleUnknownValueConfirm(false)}>仍按原值保存</Button>,
              <Button key="dict" type="primary" onClick={handleGoManageDict}>前往参照数据管理收录</Button>,
            ]
          ) : (
            [
              <Button key="cancel" onClick={() => { setPendingEditReq(null); setEditUnknownValues(null); }}>暂不保存</Button>,
              <Button
                key="notify"
                type="primary"
                loading={editUnknownNotifySaving}
                onClick={() => handleUnknownValueConfirm(true)}
              >
                通知管理员收录/打回并保存
              </Button>,
            ]
          )
        }
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="以下取值不在系统字典（受控词表）中，保存后将以原值写入指标"
          description={
            canManageDict
              ? "你拥有收录权限：可前往参照数据管理补充词条后重新提交，或暂按原值保存。"
              : "你无收录权限：可通知平台管理员收录/打回，或暂不保存。字典由治理者统一维护，不会自动新增。"
          }
        />
        <div style={{ maxHeight: 240, overflow: "auto" }}>
          {(editUnknownValues ?? []).map((u, i) => (
            <div key={i} style={{ display: "flex", gap: 8, padding: "4px 0", alignItems: "baseline" }}>
              <Tag color="orange">{EDIT_DICT_TYPE_LABEL[u.dict_type] ?? u.dict_type}</Tag>
              <span className="mono">{u.value}</span>
            </div>
          ))}
        </div>
      </Modal>
    </div>
  );
}
