import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Alert, Button, Card, Descriptions, Input, Modal, Radio, Segmented, Select, Space, Table, Tag, Tooltip, message } from "antd";
import { ArrowLeftOutlined, CheckCircleOutlined, ClockCircleOutlined } from "@ant-design/icons";
import {
  listMetrics,
  reviewMetric,
  approveMetric,
  fetchCurrentUser,
  listUsers,
  listDomainTree,
  batchApproveMetrics,
  batchRejectMetrics,
  UnisenseApiError,
} from "../api";
import type { CurrentUser, MetricResponse, SubjectDomainTreeNode } from "../types";
import { formatCnTime } from "../utils/timeCn";
import { usePermission } from "../hooks/usePermission";
import { usePersistentPageSize } from "../hooks/usePersistentPageSize";

function openReviewModal(
  metric: MetricResponse,
  approved: boolean,
  onOk: (reason: string, mode?: "standard" | "experimental", grayTenants?: number[]) => Promise<void>,
) {
  let reason = "";
  let mode = "standard" as "standard" | "experimental";
  let grayTenants = "";
  Modal.confirm({
    title: approved ? `通过评审：${metric.metric_code}` : `驳回：${metric.metric_code}`,
    content: (
      <div>
        <p style={{ marginBottom: 12 }}>
          {approved
            ? "通过后该指标将进入已发布状态；也可选择灰度发布（评审通过但先对指定租户生效）。"
            : "驳回后该指标将退回草稿状态，请填写驳回原因（提交人据此修改重提）。"}
        </p>
        {approved && (
          <div style={{ marginBottom: 12 }}>
            <Radio.Group
              value={mode}
              onChange={(e) => {
                mode = e.target.value as "standard" | "experimental";
              }}
              style={{ marginBottom: 8 }}
            >
              <Radio value="standard">标准发布（全部消费方）</Radio>
              <Radio value="experimental">灰度发布（仅指定租户）</Radio>
            </Radio.Group>
            {mode === "experimental" && (
              <Input
                placeholder="灰度租户 ID（逗号分隔，如 101,102；留空则灰度但不指定租户）"
                onChange={(e) => {
                  grayTenants = e.target.value;
                }}
              />
            )}
          </div>
        )}
        <Input.TextArea
          rows={3}
          placeholder={approved ? "变更原因（可选）" : "驳回原因（必填，至少 4 字）"}
          onChange={(e) => {
            reason = e.target.value;
          }}
        />
      </div>
    ),
    okText: approved ? "通过" : "驳回",
    cancelText: "取消",
    okButtonProps: approved ? { type: "primary" as const } : { danger: true },
    // 驳回必须填原因：返回 Promise，拒绝时 Modal 不关闭（不提交）
    onOk: () =>
      new Promise<void>((resolve, reject) => {
        if (!approved && reason.trim().length < 4) {
          message.warning("驳回原因至少 4 字，请补充说明");
          reject();
          return;
        }
        // 灰度发布：租户 ID 须为数字（此前非数字被静默丢弃，用户误以为全部生效）
        if (approved && mode === "experimental" && grayTenants.trim()) {
          const invalid = grayTenants
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean)
            .filter((t) => !/^\d+$/.test(t));
          if (invalid.length) {
            message.warning(`灰度租户 ID 须为数字：${invalid.join("、")}，请修正后重试`);
            reject();
            return;
          }
        }
        resolve();
      }).then(() => {
        const tenants = grayTenants
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)
          .map(Number)
          .filter((n) => !Number.isNaN(n));
        return onOk(reason, approved ? mode : undefined, approved && mode === "experimental" ? tenants : undefined);
      }),
  });
}

// 评审人身份判定（TD §13）：仅被指派评审人可通过/打回；platform_admin 兜底
function canReview(metric: MetricResponse, user: CurrentUser | null): boolean {
  if (!user) return false;
  if (user.role === "platform_admin") return true;
  if (metric.reviewer_type === "user" && metric.reviewer_id != null) {
    return user.id === metric.reviewer_id;
  }
  if (metric.reviewer_type === "domain" && metric.reviewer_domain) {
    return (
      (user.role === "domain_admin" || user.role === "reviewer") &&
      user.domain === metric.reviewer_domain
    );
  }
  // 未指派：域管理员兜底
  return user.role === "domain_admin";
}

// 指派评审人展示文案
function reviewerLabel(
  metric: MetricResponse,
  userMap: Map<number, string>,
  domainMap: Record<string, string>,
): React.ReactNode {
  if (metric.reviewer_type === "user" && metric.reviewer_id != null) {
    const name = userMap.get(metric.reviewer_id);
    return <Tag color="blue">{name ? `${name}（指定）` : `用户#${metric.reviewer_id}`}</Tag>;
  }
  if (metric.reviewer_type === "domain" && metric.reviewer_domain) {
    const dn = domainMap[metric.reviewer_domain] ?? metric.reviewer_domain;
    return <Tag color="geekblue">{dn} 域评审组</Tag>;
  }
  return <span className="muted">域管理员（未指派）</span>;
}

// 我的评审结论（「我审过的」视图）：approver_id=我 → 通过；reject_reviewer_id=我 → 驳回。
// 返回 null 表示该指标虽被 reviewed_by 命中但当前用户既非通过人也非驳回人（数据异常兜底）。
function reviewVerdict(metric: MetricResponse, userId: number | null) {
  if (userId == null) return null;
  if (metric.approver_id === userId) {
    return { verdict: "approved" as const, time: metric.approved_at ?? metric.updated_at ?? null };
  }
  if (metric.reject_reviewer_id === userId) {
    return {
      verdict: "rejected" as const,
      time: metric.rejected_at ?? null,
      reason: metric.reject_reason ?? "",
    };
  }
  return null;
}

// 评审记录详情弹窗（「我审过的」行点击/查看详情）：展示我的处理结论 + 指标完整口径，
// 解决"只看到已处理标签、看不到审了什么、怎么处理的"的评审回看盲区。
function ReviewDetailModal({
  metric,
  verdict,
  userMap,
  domainMap,
  onClose,
}: {
  metric: MetricResponse;
  verdict: { verdict: "approved" | "rejected"; time: string | null; reason?: string };
  userMap: Map<number, string>;
  domainMap: Record<string, string>;
  onClose: () => void;
}) {
  const def = metric.definition_json ?? {};
  const expression = typeof def.expression === "string" ? def.expression : undefined;
  const dependencies: string[] = Array.isArray(def.dependencies)
    ? def.dependencies.map((s) => String(s))
    : [];
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
  const approved = verdict.verdict === "approved";
  return (
    <Modal
      title={`评审记录：${metric.metric_code}`}
      open
      onCancel={onClose}
      footer={null}
      width={720}
    >
      <Alert
        type={approved ? "success" : "error"}
        showIcon
        style={{ marginBottom: 16 }}
        message={approved ? "已通过评审" : "已驳回"}
        description={
          <Space direction="vertical" size={2}>
            <span>
              处理时间：{verdict.time ? formatCnTime(verdict.time) : "—"}
            </span>
            {!approved && verdict.reason ? <span>驳回原因：{verdict.reason}</span> : null}
          </Space>
        }
      />
      <Descriptions column={1} size="small" bordered style={{ marginBottom: 16 }}>
        <Descriptions.Item label="名称">{metric.name}</Descriptions.Item>
        <Descriptions.Item label="所属域">
          {domainMap[metric.domain] ?? metric.domain}
        </Descriptions.Item>
        <Descriptions.Item label="类型">{metric.type}</Descriptions.Item>
        <Descriptions.Item label="状态">{metric.status}</Descriptions.Item>
        <Descriptions.Item label="版本">v{metric.version}</Descriptions.Item>
        <Descriptions.Item label="指派评审人">
          {reviewerLabel(metric, userMap, domainMap)}
        </Descriptions.Item>
        {metric.description ? (
          <Descriptions.Item label="业务描述">{metric.description}</Descriptions.Item>
        ) : null}
      </Descriptions>
      <Card title="口径定义" size="small" style={{ marginBottom: 16 }}>
        <Descriptions column={1} size="small" bordered>
          {expression && (
            <Descriptions.Item label={metric.type === "atomic" ? "聚合表达式" : "计算表达式"}>
              {/* 长表达式超宽时换行而非撑破弹窗：inline-block 使 wordBreak 生效 */}
              <code className="mono" style={{ display: "inline-block", maxWidth: "100%", wordBreak: "break-word" }}>
                {expression}
              </code>
            </Descriptions.Item>
          )}
          {sourceTables.length > 0 && (
            <Descriptions.Item label="依赖表（上游）">
              {sourceTables.map((t) => (
                <Tag key={t} className="mono">{t}</Tag>
              ))}
            </Descriptions.Item>
          )}
          {dependencies.length > 0 && (
            <Descriptions.Item label="依赖指标">
              {dependencies.map((d) => (
                <Tag key={d} className="mono">{d}</Tag>
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
              {/* pre-wrap + wordBreak：长 SQL 行自动换行，maxWidth 兜底不撑破弹窗；maxHeight 控制纵向滚动 */}
              <pre
                style={{
                  background: "var(--paper)",
                  padding: 8,
                  borderRadius: 4,
                  margin: 0,
                  fontSize: 12,
                  overflow: "auto",
                  maxHeight: 200,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  maxWidth: "100%",
                  boxSizing: "border-box",
                }}
              >
                {etlSql}
              </pre>
            </Descriptions.Item>
          )}
          <Descriptions.Item label="完整 JSON">
            <pre
              style={{
                background: "var(--paper)",
                padding: 8,
                borderRadius: 4,
                margin: 0,
                fontSize: 12,
                overflow: "auto",
                maxHeight: 220,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                maxWidth: "100%",
                boxSizing: "border-box",
              }}
            >
              {JSON.stringify(def, null, 2)}
            </pre>
          </Descriptions.Item>
        </Descriptions>
      </Card>
    </Modal>
  );
}

export function MetricReview() {
  const [items, setItems] = useState<MetricResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busyCode, setBusyCode] = useState<string | null>(null);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [userMap, setUserMap] = useState<Map<number, string>>(new Map());
  // 域 code → 中文名（「域」列显示中文名，与指标目录一致）
  const [domainMap, setDomainMap] = useState<Record<string, string>>({});
  // 「我审过的」详情弹窗：点击行/查看详情打开，展示我的处理结论 + 完整口径
  const [detailMetric, setDetailMetric] = useState<MetricResponse | null>(null);
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);
  const [batchBusy, setBatchBusy] = useState(false);
  // 批量打回原因（L1）：批量打回须填写原因（对齐单条驳回），提交人据此修改重提
  const [batchRejectOpen, setBatchRejectOpen] = useState(false);
  const [batchRejectReason, setBatchRejectReason] = useState("");
  // 审批工作台视角：pending=待我审（REVIEW）；reviewed=我审过的（按 reviewed_by 过滤，
  // 命中审批通过或驳回——评审历史完整不丢驳回记录）
  const [view, setView] = useState<"pending" | "reviewed">("pending");
  // 审批筛选（复审 D4）：关键词（编码/名称）与域过滤，缓解审批积压时翻页找目标
  // URL 同步（复审 P2-8）：刷新/分享保留筛选视图，对齐目录页 ownerFilter 的 URL 驱动模式
  const [searchParams, setSearchParams] = useSearchParams();
  const [keyword, setKeyword] = useState(searchParams.get("keyword") ?? "");
  const [domain, setDomain] = useState<string | undefined>(
    searchParams.get("domain") ?? undefined,
  );
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果（对齐目录/Dimensions/Templates）
  const loadSeq = useRef(0);
  const [page, setPage] = useState(1);
  // 每页条数持久化（对齐指标目录/Dimensions 的 usePersistentPageSize 跨页记忆）
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.review.pageSize", 20);
  const navigate = useNavigate();
  const { can } = usePermission();
  const canApprove = can("metric:approve");

  // 筛选变更写回 URL query（合并保留其它参数，replace 避免堆历史）——刷新/分享不丢筛选视图
  function syncFilter(nextKeyword: string, nextDomain: string | undefined) {
    const next = new URLSearchParams(searchParams);
    if (nextKeyword) next.set("keyword", nextKeyword);
    else next.delete("keyword");
    if (nextDomain) next.set("domain", nextDomain);
    else next.delete("domain");
    setSearchParams(next, { replace: true });
  }

  // 统一返回上一入口：优先回退浏览器历史，无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  async function load() {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      // pending=待我审（REVIEW）；reviewed=我审过的（按 reviewed_by 过滤——通过或驳回都回看）
      const res = await listMetrics(
        view === "pending"
          ? {
              status: "REVIEW",
              page,
              page_size: pageSize,
              keyword: keyword || undefined,
              domain: domain || undefined,
              // 审批工作台 FIFO：最旧待审优先，避免积压（默认后端 updated_at desc 会新单优先）
              sort_by: "updated_at",
              sort_order: "asc",
            }
          : {
              reviewed_by: currentUser?.id,
              page,
              page_size: pageSize,
              keyword: keyword || undefined,
              domain: domain || undefined,
              sort_by: "updated_at",
              sort_order: "desc",
            },
      );
      if (seq !== loadSeq.current) return;
      setItems(res.items);
      setTotal(res.total);
      // 空页回退：深页审批/打回后列表缩短，当前页无数据且非首页时回退上一页
      // （依赖 page 变化自动重查；与指标目录的空页回退语义一致）
      if (page > 1 && res.items.length === 0 && res.total > 0) {
        setPage(Math.max(1, page - 1));
        return;
      }
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败",
      );
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  // 浏览器前进/后退改 URL 时回读筛选状态（仅在值不同时写，避免与 syncFilter 相互触发死循环）
  useEffect(() => {
    const urlKeyword = searchParams.get("keyword") ?? "";
    const urlDomain = searchParams.get("domain") ?? undefined;
    if (urlKeyword !== keyword) setKeyword(urlKeyword);
    if (urlDomain !== domain) setDomain(urlDomain);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  useEffect(() => {
    fetchCurrentUser().then(setCurrentUser).catch(() => {});
    // 「我审过的」视角依赖 currentUser 过滤：未就绪时不发无 approver 过滤的全量首查
    // （此前会在 mount 时多发一次全量请求，虽被 loadSeq 丢弃但不产生无意义查询）
    if (view === "reviewed" && !currentUser) return;
    load();
    listUsers()
      .then((u) => setUserMap(new Map(u.map((x) => [x.id, x.display_name || x.username]))))
      .catch(() => {});
    listDomainTree()
      .then((tree) => {
        const m: Record<string, string> = {};
        const walk = (nodes: SubjectDomainTreeNode[]) => {
          for (const n of nodes) {
            m[n.code] = n.name;
            if (n.children?.length) walk(n.children);
          }
        };
        walk(tree);
        setDomainMap(m);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, view, currentUser?.id, keyword, domain]);

  async function handleReview(
    metric: MetricResponse,
    approved: boolean,
    reason: string,
    mode?: "standard" | "experimental",
    grayTenants?: number[],
  ) {
    setBusyCode(metric.metric_code);
    try {
      if (approved) {
        await approveMetric(metric.metric_code, {
          mode,
          gray_tenant_ids: mode === "experimental" ? grayTenants ?? [] : undefined,
        });
      } else {
        await reviewMetric(metric.metric_code, false, reason);
      }
      message.success(
        approved
          ? `已通过：${metric.metric_code}${mode === "experimental" ? "（灰度）" : ""}`
          : `已驳回：${metric.metric_code}`,
      );
      load();
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败",
      );
    } finally {
      setBusyCode(null);
    }
  }

  // 批量操作：通过 / 打回（逐条收集结果）
  async function runBatch(approved: boolean, reason?: string) {
    const keys = selectedKeys.map(String);
    const targets = items.filter((m) => keys.includes(m.metric_code));
    if (!targets.length) {
      message.warning("请先勾选指标");
      return;
    }
    // 前端预筛：跳过当前用户无评审权的项
    const authorized = targets.filter((m) => canReview(m, currentUser));
    if (!authorized.length) {
      message.warning("勾选的指标均非指派给您的评审项");
      return;
    }
    setBatchBusy(true);
    try {
      const codes = authorized.map((m) => m.metric_code);
      const res = approved
        ? await batchApproveMetrics(codes)
        // 批量打回原因（L1）：由评审人在弹窗填写（runBatch(false, reason)），
        // 不再硬编码「批量打回，请修改后重新提交」
        : await batchRejectMetrics(codes, reason?.trim() || "批量打回，请修改后重新提交");
      const errors = res.results.filter((r) => !r.ok).map((r) => `${r.code}: ${r.message}`);
      if (res.ok_count) message.success(`${approved ? "通过" : "打回"}成功 ${res.ok_count} 个`);
      if (errors.length) message.error(errors.slice(0, 3).join("；"));
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "批量操作失败",
      );
    } finally {
      setBatchBusy(false);
      setSelectedKeys([]);
      load();
    }
  }

  const columns = [
    {
      title: "编码",
      dataIndex: "metric_code",
      key: "metric_code",
      render: (code: string) => (
        <Button type="link" size="small" onClick={() => navigate(`/detail/${code}`)}>
          {code}
        </Button>
      ),
    },
    { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
    { title: "域", dataIndex: "domain", key: "domain", render: (v: string) => domainMap[v] ?? v },
    {
      title: "PII",
      key: "pii",
      render: (_: unknown, r: MetricResponse) =>
        r.pii_flag ? (
          <Tag color={r.compliance_reviewed ? "green" : "orange"}>
            {r.compliance_reviewed ? "PII 已复核" : "PII 待复核"}
          </Tag>
        ) : (
          <Tag>否</Tag>
        ),
    },
    {
      title: "指派评审人",
      key: "reviewer",
      render: (_: unknown, r: MetricResponse) => reviewerLabel(r, userMap, domainMap),
    },
    {
      // 「我审过的」视图：展示我的处理结论（通过/驳回 + 时间 + 原因）；待我审视图不显示
      title: "处理结果",
      key: "verdict",
      render: (_: unknown, r: MetricResponse) => {
        if (view !== "reviewed") return null;
        const v = reviewVerdict(r, currentUser?.id ?? null);
        if (!v) return <span className="muted">—</span>;
        if (v.verdict === "approved") {
          return (
            <Space direction="vertical" size={2}>
              <Tag color="green">已通过</Tag>
              {v.time ? (
                <span className="muted" style={{ fontSize: 12 }}>
                  {formatCnTime(v.time)}
                </span>
              ) : null}
            </Space>
          );
        }
        return (
          <Space direction="vertical" size={2}>
            <Tag color="red">已驳回</Tag>
            {v.reason ? (
              <Tooltip title={v.reason}>
                <span
                  className="muted"
                  style={{
                    fontSize: 12,
                    maxWidth: 180,
                    display: "inline-block",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {v.reason}
                </span>
              </Tooltip>
            ) : null}
            {v.time ? (
              <span className="muted" style={{ fontSize: 12 }}>
                {formatCnTime(v.time)}
              </span>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: "版本",
      dataIndex: "version",
      key: "version",
      render: (v: number) => `v${v}`,
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      key: "updated_at",
      render: (v: string | null) =>
        v ? (
          <span className="mono" style={{ fontSize: 12 }}>
            {formatCnTime(v)}
          </span>
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      title: "操作",
      key: "actions",
      render: (_: unknown, r: MetricResponse) => {
        // 「我审过的」视图：仅回看——查看详情弹窗展示处理结论（通过/驳回+原因+时间）与完整口径
        if (view === "reviewed") {
          return (
            <Button size="small" type="link" onClick={() => setDetailMetric(r)}>
              查看详情
            </Button>
          );
        }
        // 同时满足权限点与行级/域级评审人身份才允许操作
        const allowed = canApprove && canReview(r, currentUser);
        // PII 待复核：后端 approve 会拦 COMPLIANCE_BLOCKED，前端直接禁用「通过」并提示先完成合规复核
        const piiPending = r.pii_flag && !r.compliance_reviewed;
        return (
          <Space>
            <Button
              size="small"
              type="primary"
              disabled={!allowed || piiPending || busyCode === r.metric_code}
              onClick={() =>
                openReviewModal(r, true, (reason, mode, grayTenants) =>
                  handleReview(r, true, reason, mode, grayTenants),
                )
              }
            >
              通过
            </Button>
            <Button
              size="small"
              danger
              disabled={!allowed || busyCode === r.metric_code}
              onClick={() => openReviewModal(r, false, (reason) => handleReview(r, false, reason))}
            >
              驳回
            </Button>
            {piiPending ? (
              <Tooltip title="该指标含 PII，需先在详情页完成合规复核后方可通过">
                <Tag color="orange" style={{ cursor: "help" }}>PII 待复核</Tag>
              </Tooltip>
            ) : null}
          </Space>
        );
      },
    },
  ];

  return (
    <div>
      <div className="page-head">
        <div>
          <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
            返回
          </Button>
          <div className="page-kicker">指标资产 / 指标审批</div>
          <h2>指标审批</h2>
          <p>待评审指标——仅被指派评审人/域评审组可通过或打回。</p>
        </div>
      </div>
      <Card
        title="指标审批"
        extra={
          <Space>
            <Segmented
              value={view}
              onChange={(v) => {
                setView(v as "pending" | "reviewed");
                setPage(1);
                setSelectedKeys([]);
              }}
              options={[
                { label: "待我审", value: "pending" },
                { label: "我审过的", value: "reviewed" },
              ]}
            />
            {/* 批量按钮：仅待我审视图下展示；在勾选中有当前用户可评审的项时可用（避免"均非指派"空操作） */}
            {view === "pending" && (
              <>
                <Button
                  size="small"
                  icon={<CheckCircleOutlined />}
                  disabled={!selectedKeys.length || batchBusy || !canApprove || !items.some((m) => selectedKeys.includes(m.metric_code) && canReview(m, currentUser))}
                  onClick={() => runBatch(true)}
                >
                  批量通过
                </Button>
                <Button
                  size="small"
                  danger
                  icon={<ClockCircleOutlined />}
                  disabled={!selectedKeys.length || batchBusy || !canApprove || !items.some((m) => selectedKeys.includes(m.metric_code) && canReview(m, currentUser))}
                  onClick={() => {
                    setBatchRejectReason("");
                    setBatchRejectOpen(true);
                  }}
                >
                  批量打回
                </Button>
              </>
            )}
            <Button size="small" onClick={load} loading={loading}>
              刷新
            </Button>
          </Space>
        }
      >
        {/* 审批筛选栏（复审 D4）：关键词 + 域，变更即回第 1 页重查 */}
        <Space style={{ marginBottom: 16 }} wrap>
          <Input.Search
            allowClear
            placeholder="搜索指标编码 / 名称"
            style={{ width: 240 }}
            onSearch={(v) => {
              const kw = v.trim();
              setKeyword(kw);
              setPage(1);
              syncFilter(kw, domain);
            }}
          />
          <Select
            allowClear
            placeholder="按域筛选"
            style={{ width: 180 }}
            value={domain}
            onChange={(v) => {
              const d = v || undefined;
              setDomain(d);
              setPage(1);
              syncFilter(keyword, d);
            }}
            options={Object.entries(domainMap).map(([code, name]) => ({
              value: code,
              label: `${name}（${code}）`,
            }))}
          />
        </Space>
        <Table
          dataSource={items}
          columns={columns}
          rowKey="metric_code"
          loading={loading}
          // 「我审过的」视图行点击 → 查看评审记录详情（处理结论 + 完整口径）
          onRow={(r) => ({
            onClick: () => {
              if (view === "reviewed") setDetailMetric(r);
            },
            style: view === "reviewed" ? { cursor: "pointer" } : undefined,
          })}
          rowSelection={
            view === "pending"
              ? {
                  selectedRowKeys: selectedKeys,
                  onChange: (keys) => setSelectedKeys(keys),
                  // 仅允许勾选当前用户可评审的项（避免批量时"均非指派"空操作）
                  getCheckboxProps: (r: MetricResponse) => ({
                    disabled: !canReview(r, currentUser),
                  }),
                }
              : undefined
          }
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50],
            onChange: (p, ps) => { setPage(p); onShowSizeChange(p, ps); },
            showTotal: (t) => (view === "pending" ? `共 ${t} 条待评审` : `共 ${t} 条已评审`),
          }}
          locale={{
            emptyText:
              view === "pending"
                ? "当前无待您评审的指标（已全部处理或暂无指派）"
                : "您还没有评审过指标",
          }}
        />
        {/* 批量打回原因弹窗（L1）：对齐单条驳回——原因必填（至少 4 字），提交人据此修改重提 */}
        <Modal
          title="批量打回"
          open={batchRejectOpen}
          onOk={() => {
            if (batchRejectReason.trim().length < 4) {
              message.warning("驳回原因至少 4 字，请补充说明");
              return;
            }
            setBatchRejectOpen(false);
            void runBatch(false, batchRejectReason);
          }}
          onCancel={() => setBatchRejectOpen(false)}
          okText="打回"
          cancelText="取消"
          okButtonProps={{ danger: true }}
        >
          <p>将退回 {selectedKeys.length} 个指标至草稿，请填写驳回原因（提交人据此修改重提）。</p>
          <Input.TextArea
            rows={3}
            value={batchRejectReason}
            placeholder="批量驳回原因（必填，至少 4 字）"
            onChange={(e) => setBatchRejectReason(e.target.value)}
          />
        </Modal>
        {/* 「我审过的」评审记录详情弹窗：处理结论（通过/驳回+原因+时间）+ 完整口径 */}
        {detailMetric && view === "reviewed"
          ? (() => {
              const v = reviewVerdict(detailMetric, currentUser?.id ?? null);
              if (!v) return null;
              return (
                <ReviewDetailModal
                  metric={detailMetric}
                  verdict={v}
                  userMap={userMap}
                  domainMap={domainMap}
                  onClose={() => setDetailMetric(null)}
                />
              );
            })()
          : null}
      </Card>
    </div>
  );
}
