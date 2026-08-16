import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Input, Modal, Space, Table, Tag, Tooltip, message } from "antd";
import { ArrowLeftOutlined, CheckCircleOutlined, ClockCircleOutlined } from "@ant-design/icons";
import {
  listMetrics,
  reviewMetric,
  fetchCurrentUser,
  listUsers,
  batchApproveMetrics,
  batchRejectMetrics,
  UnisenseApiError,
} from "../api";
import type { CurrentUser, MetricResponse } from "../types";
import { formatCnTime } from "../utils/timeCn";
import { usePermission } from "../hooks/usePermission";

function openReviewModal(
  metric: MetricResponse,
  approved: boolean,
  onOk: (reason: string) => Promise<void>,
) {
  let reason = "";
  Modal.confirm({
    title: approved ? `通过评审：${metric.metric_code}` : `驳回：${metric.metric_code}`,
    content: (
      <div>
        <p style={{ marginBottom: 12 }}>
          {approved
            ? "通过后该指标将进入已发布状态。"
            : "驳回后该指标将退回草稿状态。"}
        </p>
        <Input.TextArea
          rows={3}
          placeholder="变更原因（可选）"
          onChange={(e) => {
            reason = e.target.value;
          }}
        />
      </div>
    ),
    okText: approved ? "通过" : "驳回",
    cancelText: "取消",
    okButtonProps: approved ? { type: "primary" as const } : { danger: true },
    onOk: () => onOk(reason),
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
): React.ReactNode {
  if (metric.reviewer_type === "user" && metric.reviewer_id != null) {
    const name = userMap.get(metric.reviewer_id);
    return <Tag color="blue">{name ? `${name}（指定）` : `用户#${metric.reviewer_id}`}</Tag>;
  }
  if (metric.reviewer_type === "domain" && metric.reviewer_domain) {
    return <Tag color="geekblue">{metric.reviewer_domain} 域评审组</Tag>;
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
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);
  const [batchBusy, setBatchBusy] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const navigate = useNavigate();
  const { can } = usePermission();
  const canApprove = can("metric:approve");

  // 统一返回上一入口：优先回退浏览器历史，无上一页（URL 直达）时兜底总览仪表
  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  async function load() {
    setLoading(true);
    try {
      const res = await listMetrics({ status: "REVIEW", page, page_size: pageSize });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      message.error(
        err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败",
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    fetchCurrentUser().then(setCurrentUser).catch(() => {});
    listUsers()
      .then((u) => setUserMap(new Map(u.map((x) => [x.id, x.display_name || x.username]))))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize]);

  async function handleReview(metric: MetricResponse, approved: boolean, reason: string) {
    setBusyCode(metric.metric_code);
    try {
      await reviewMetric(metric.metric_code, approved, reason);
      message.success(approved ? `已通过：${metric.metric_code}` : `已驳回：${metric.metric_code}`);
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
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "域", dataIndex: "domain", key: "domain" },
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
      render: (_: unknown, r: MetricResponse) => reviewerLabel(r, userMap),
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
              onClick={() => openReviewModal(r, true, (reason) => handleReview(r, true, reason))}
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
        title="待评审指标"
        extra={
          <Space>
            {/* 批量按钮：仅在勾选中有当前用户可评审的项时可用（避免"均非指派"空操作） */}
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
          rowSelection={{
            selectedRowKeys: selectedKeys,
            onChange: (keys) => setSelectedKeys(keys),
            // 仅允许勾选当前用户可评审的项（避免批量时"均非指派"空操作）
            getCheckboxProps: (r: MetricResponse) => ({
              disabled: !canReview(r, currentUser),
            }),
          }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50],
            onChange: (p, ps) => { setPage(p); setPageSize(ps); },
            showTotal: (t) => `共 ${t} 条待评审`,
          }}
          locale={{ emptyText: "暂无待评审指标" }}
        />
      </Card>
    </div>
  );
}
