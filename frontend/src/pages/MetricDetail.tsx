import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Input,
  message,
  Modal,
  Space,
  Tag,
  Tabs,
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
} from "@ant-design/icons";
import {
  addFavorite,
  approveMetric,
  deprecateMetric,
  emergencyPublishMetric,
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
  upsertSubscription,
  UnisenseApiError,
} from "../api";
import type {
  CurrentUser,
  MetricHealth,
  MetricResponse,
  MetricVersionResponse,
  RecommendItem,
  SubscriptionPref,
  UserBrief,
} from "../types";
import { useTracking } from "../hooks/useTracking";
import { enumLabel, METRIC_TYPE_LABEL, METRIC_TIER_LABEL, AGGREGATION_LABEL, TIME_SEMANTICS_LABEL, FRESHNESS_LABEL, DW_LAYER_LABEL, SERVING_MODE_LABEL, ADDITIVITY_LABEL, GRANULARITY_LABEL } from "../utils/enums";
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
  REVIEW: "审核",
  PUBLISHED: "已发布",
  DEPRECATED: "已废弃",
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
          {metric.sunset_until && <span className="muted">日落时间：{metric.sunset_until}</span>}
          {metric.deprecated_at && <span className="muted">废弃时间：{metric.deprecated_at}</span>}
        </Space>
      }
      style={{ marginBottom: 16 }}
    />
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
  const [metric, setMetric] = useState<MetricResponse | null>(null);
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
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const { track } = useTracking();

  async function load() {
    if (!code) return;
    setLoading(true);
    try {
      const [m, vs, me, favs, healthRes, userList, subs, rel] = await Promise.all([
        getMetric(code),
        listVersions(code),
        fetchCurrentUser(),
        listFavorites().catch(() => [] as string[]),
        getMetricHealth(code).catch(() => null),
        listUsers().catch(() => [] as UserBrief[]),
        listSubscriptions().catch(() => ({ items: [] as SubscriptionPref[] })),
        fetchRelatedMetrics(code).catch(() => [] as RecommendItem[]),
      ]);
      setMetric(m);
      setVersions(vs);
      setCurrentUser(me);
      setFavorited(favs.includes(code));
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

  if (!metric) {
    return loading ? (
      <Card loading />
    ) : (
      <Card>
        <Paragraph type="secondary">指标不存在</Paragraph>
      </Card>
    );
  }

  const role = currentUser?.role || "";
  const isAdmin = role === "platform_admin" || role === "domain_admin";
  const isOwnerOrAdmin = isAdmin || role === "metric_owner";
  const canPiiReview = isAdmin && metric.pii_flag;
  const piiMasked = metric.pii_flag && !SENSITIVE_ROLES.includes(role);

  const headerActions = (
    <Space wrap>
      <Button
        type={favorited ? "primary" : "default"}
        icon={<HeartOutlined />}
        onClick={() =>
          runAction(
            () => (favorited ? removeFavorite(metric.metric_code) : addFavorite(metric.metric_code)),
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
      <Button type="link" onClick={() => navigate("/catalog")}>
        ← 返回目录
      </Button>
    </Space>
  );

  const actions = (
    <Space wrap style={{ marginBottom: 16 }}>
      {(metric.status === "DRAFT" || metric.status === "EXPERIMENTAL") && (
        <Button icon={<SendOutlined />} loading={busy} onClick={() => runAction(() => submitReview(metric.metric_code), "提交评审")}>
          提交评审
        </Button>
      )}
      {metric.status === "REVIEW" && isAdmin && (
        <Button
          type="primary"
          loading={busy}
          onClick={() => runAction(() => approveMetric(metric.metric_code, {}), "正式发布")}
          disabled={metric.pii_flag && !metric.compliance_reviewed}
        >
          正式发布{metric.pii_flag && !metric.compliance_reviewed ? "（需先 PII 复核）" : ""}
        </Button>
      )}
      {metric.status === "REVIEW" && isAdmin && (
        <Button icon={<ExperimentOutlined />} loading={busy} onClick={() => setGrayOpen(true)}>
          灰度发布
        </Button>
      )}
      {metric.status === "EXPERIMENTAL" && isOwnerOrAdmin && (
        <>
          <Button icon={<RiseOutlined />} loading={busy} onClick={() => runAction(() => promoteMetric(metric.metric_code), "全量发布")}>
            全量发布
          </Button>
          <Button icon={<RollbackOutlined />} loading={busy} onClick={() => runAction(() => rollbackMetric(metric.metric_code), "回滚")}>
            回滚
          </Button>
        </>
      )}
      {canPiiReview && !metric.compliance_reviewed && (
        <Button loading={busy} onClick={() => runAction(() => piiReview(metric.metric_code), "PII 复核")}>
          PII 合规复核
        </Button>
      )}
      {(metric.status === "DRAFT" || metric.status === "REVIEW") && isAdmin && (
        <Button danger icon={<ThunderboltOutlined />} loading={busy} onClick={() => setEmergencyOpen(true)}>
          紧急发布
        </Button>
      )}
      {metric.status !== "DEPRECATED" && isOwnerOrAdmin && (
        <Button danger loading={busy} onClick={() => setDeprecateOpen(true)}>
          废弃
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
      {metric.gray_tenant_ids && metric.gray_tenant_ids.length > 0 && (
        <Tag color="purple">灰度 {metric.gray_tenant_ids.length} 租户</Tag>
      )}
    </Space>
  );

  return (
    <div>
      <div className="page-head">
        <div>
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
          <Descriptions.Item label="单位">{metric.unit}</Descriptions.Item>
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
        onOk={() =>
          runAction(() => emergencyPublishMetric(metric.metric_code, emergencyReason), "紧急发布").then(() => {
            setEmergencyOpen(false);
            setEmergencyReason("");
          })
        }
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
        title="废弃指标"
        open={deprecateOpen}
        onOk={() =>
          runAction(() => deprecateMetric(metric.metric_code, successor), "废弃").then(() => {
            setDeprecateOpen(false);
            setSuccessor("");
          })
        }
        confirmLoading={busy}
        onCancel={() => setDeprecateOpen(false)}
        okText="确认废弃"
        okButtonProps={{ danger: true }}
      >
        <Input
          placeholder="替代指标编码（必填，须为已发布指标）"
          value={successor}
          onChange={(e) => setSuccessor(e.target.value)}
        />
      </Modal>
    </div>
  );
}
