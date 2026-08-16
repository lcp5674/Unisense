import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Input, Modal, Radio, Segmented, Space, Table, Tag, Tooltip, message } from "antd";
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

export function MetricReview() {
  const [items, setItems] = useState<MetricResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busyCode, setBusyCode] = useState<string | null>(null);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [userMap, setUserMap] = useState<Map<number, string>>(new Map());
  // 域 code → 中文名（「域」列显示中文名，与指标目录一致）
  const [domainMap, setDomainMap] = useState<Record<string, string>>({});
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);
  const [batchBusy, setBatchBusy] = useState(false);
  // 审批工作台视角：pending=待我审（REVIEW）；reviewed=我审过的（按 reviewed_by 过滤，
  // 命中审批通过或驳回——评审历史完整不丢驳回记录）
  const [view, setView] = useState<"pending" | "reviewed">("pending");
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果（对齐目录/Dimensions/Templates）
  const loadSeq = useRef(0);
  const [page, setPage] = useState(1);
  // 每页条数持久化（对齐指标目录/Dimensions 的 usePersistentPageSize 跨页记忆）
  const { pageSize, onShowSizeChange } = usePersistentPageSize("unisense.review.pageSize", 20);
  const navigate = useNavigate();
  const { can } = usePermission();
  const canApprove = can("metric:approve");

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
              // 审批工作台 FIFO：最旧待审优先，避免积压（默认后端 updated_at desc 会新单优先）
              sort_by: "updated_at",
              sort_order: "asc",
            }
          : {
              reviewed_by: currentUser?.id,
              page,
              page_size: pageSize,
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
  }, [page, pageSize, view, currentUser?.id]);

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
  async function runBatch(approved: boolean) {
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
        : await batchRejectMetrics(codes, "批量打回，请修改后重新提交");
      const errors = res.results.filter((r) => !r.ok).map((r) => `${r.metric_code}: ${r.message}`);
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
        // 「我审过的」视图仅回看，不再提供操作
        if (view === "reviewed") {
          return <Tag>已处理</Tag>;
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
                  onClick={() => runBatch(false)}
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
        <Table
          dataSource={items}
          columns={columns}
          rowKey="metric_code"
          loading={loading}
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
      </Card>
    </div>
  );
}
