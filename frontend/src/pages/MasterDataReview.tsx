import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, Segmented, Space, Table, Tag, Tooltip, message } from "antd";
import { ArrowLeftOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  listDimensions,
  listMeasureCatalogs,
  listTerms,
  approveDimension,
  rejectDimension,
  approveMeasureCatalog,
  rejectMeasureCatalog,
  approveTerm,
  rejectTerm,
  fetchCurrentUser,
  listUsers,
  listDomainTree,
  UnisenseApiError,
} from "../api";
import type { CurrentUser, SubjectDomainTreeNode } from "../types";
import { formatCnTime } from "../utils/timeCn";
import { useUserNames } from "../utils/userNames";
import {
  canReviewMasterData,
  MasterDataReviewActions,
  MasterDataReviewModals,
  useMasterDataReview,
} from "../components/MasterDataReview";

/** 统一主数据审批工作台（TD §13）：聚合维度 / 逻辑度量 / 术语三类主数据的待我审与我审过的。
 *  被指派评审人/域评审组在此一处完成审批，不再散落各管理页。
 *  - 待我审：三模块 status=REVIEW 拉取 + 前端 canReviewMasterData 逐行判定（只显示我可审）
 *  - 我审过的：三模块 reviewed_by=me 过滤（通过/驳回历史完整回看） */

type ReviewKind = "dimension" | "measure" | "term";

interface ReviewItem {
  kind: ReviewKind;
  code: string;
  name: string;
  domain: string;
  status: string;
  reviewer_type?: string | null;
  reviewer_id?: number | null;
  reviewer_domain?: string | null;
  approver_id?: number | null;
  reject_reviewer_id?: number | null;
  rejected_at?: string | null;
  reject_reason?: string | null;
  reviewed_at?: string | null;
  updated_at?: string | null;
}

const KIND_META: Record<ReviewKind, { label: string; color: string }> = {
  dimension: { label: "维度", color: "blue" },
  measure: { label: "逻辑度量", color: "geekblue" },
  term: { label: "术语", color: "cyan" },
};

// 指派评审人展示文案（对齐 MetricReview.reviewerLabel）
function reviewerLabel(
  item: ReviewItem,
  userMap: Map<number, string>,
  domainMap: Record<string, string>,
): React.ReactNode {
  if (item.reviewer_type === "user" && item.reviewer_id != null) {
    const name = userMap.get(item.reviewer_id);
    return <Tag color="blue">{name ? `${name}（指定）` : "未知用户（指定）"}</Tag>;
  }
  if (item.reviewer_type === "domain" && item.reviewer_domain) {
    const dn = domainMap[item.reviewer_domain] ?? item.reviewer_domain;
    return <Tag color="geekblue">{dn} 域评审组</Tag>;
  }
  return <span className="muted">域管理员（未指派）</span>;
}

// 我的评审结论（「我审过的」视图）：approver_id=我 → 通过；reject_reviewer_id=我 → 驳回
function reviewVerdict(item: ReviewItem, userId: number | null) {
  if (userId == null) return null;
  if (item.approver_id === userId) {
    return { verdict: "approved" as const, time: item.reviewed_at ?? item.updated_at ?? null };
  }
  if (item.reject_reviewer_id === userId) {
    return {
      verdict: "rejected" as const,
      time: item.rejected_at ?? null,
      reason: item.reject_reason ?? "",
    };
  }
  return null;
}

// 三模块行 → 统一 ReviewItem（审核字段名一致，均来自 ReviewFields）
function toItem(kind: ReviewKind, row: Record<string, unknown>): ReviewItem {
  return {
    kind,
    code: String(row.code ?? ""),
    name: String(row.name ?? ""),
    domain: String(row.domain ?? ""),
    status: String(row.status ?? ""),
    reviewer_type: row.reviewer_type as string | null | undefined,
    reviewer_id: row.reviewer_id as number | null | undefined,
    reviewer_domain: row.reviewer_domain as string | null | undefined,
    approver_id: row.approver_id as number | null | undefined,
    reject_reviewer_id: row.reject_reviewer_id as number | null | undefined,
    rejected_at: row.rejected_at as string | null | undefined,
    reject_reason: row.reject_reason as string | null | undefined,
    reviewed_at: row.reviewed_at as string | null | undefined,
    updated_at: row.updated_at as string | null | undefined,
  };
}

export function MasterDataReview({ embedded = false }: { embedded?: boolean } = {}) {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [userMap, setUserMap] = useState<Map<number, string>>(new Map());
  // 跨组织精确解析：评审人可能不在本组织 /auth/users 列表，
  // 用 useUserNames 按已知 id 反查真实中文名，避免「用户#id」占位。
  const reviewUserNames = useUserNames(items.map((i) => i.reviewer_id));
  const effectiveUserMap = useMemo(() => {
    const m = new Map(userMap);
    for (const [idStr, u] of Object.entries(reviewUserNames)) {
      m.set(Number(idStr), u.display_name || u.username);
    }
    return m;
  }, [userMap, reviewUserNames]);
  const [domainMap, setDomainMap] = useState<Record<string, string>>({});
  // 审批工作台视角：pending=待我审（REVIEW + 我可审）；reviewed=我审过的（reviewed_by 过滤）
  const [view, setView] = useState<"pending" | "reviewed">("pending");
  // 类型聚焦筛选：all=聚合视图（默认）；dimension/measure/term=只看单一类型
  const [kindFilter, setKindFilter] = useState<"all" | ReviewKind>("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  // F1：服务端分页真实总数（三模块 total 之和），翻页可触及全部积压
  const [serverTotal, setServerTotal] = useState(0);
  // 并发查询防竞态：只有最后一次发起的请求允许落地结果
  const loadSeq = useRef(0);
  const navigate = useNavigate();
  // 复用共享审核 hook（驳回 target/busy）
  const { rejectTarget, setRejectTarget, rejectBusy, setRejectBusy, busyCode, setBusyCode } =
    useMasterDataReview();
  // 驳回弹窗当前目标（完整行，用于选择对应模块的 reject api；rejectTarget 仅存 code/name 展示）
  const [rejectItem, setRejectItem] = useState<ReviewItem | null>(null);

  async function load() {
    const seq = ++loadSeq.current;
    setLoading(true);
    try {
      // F1（审查修复）：此前 page_size 硬编码 200 + 客户端分页，REVIEW 积压超 200 条
      // 时第 201 条起完全不可见且无提示。改服务端分页透传（page/page_size），
      // 翻页即取更深层数据；total 用三模块真实总数之和。
      const params: Record<string, string | number | undefined> = {
        page,
        page_size: pageSize,
      };
      if (view === "pending") params.status = "REVIEW";
      else if (currentUser?.id != null) params.reviewed_by = currentUser.id;
      // 「我审过的」依赖 currentUser：未就绪时不发无 approver 过滤的全量首查
      if (view === "reviewed" && currentUser?.id == null) return;
      const [dims, measures, terms] = await Promise.all([
        listDimensions(params as { status?: string; page_size?: number; reviewed_by?: number; page?: number }),
        listMeasureCatalogs(params as { status?: string; page_size?: number; reviewed_by?: number; page?: number }),
        listTerms(params as { status?: string; page_size?: number; reviewed_by?: number; page?: number }),
      ]);
      if (seq !== loadSeq.current) return;
      setServerTotal((dims.total ?? dims.items.length) + (measures.total ?? measures.items.length) + (terms.total ?? terms.items.length));
      const merged: ReviewItem[] = [
        ...dims.items.map((d) =>
          toItem("dimension", {
            code: d.dim_code,
            name: d.name,
            domain: d.domain,
            status: d.status,
            reviewer_type: d.reviewer_type,
            reviewer_id: d.reviewer_id,
            reviewer_domain: d.reviewer_domain,
            approver_id: d.approver_id,
            reject_reviewer_id: d.reject_reviewer_id,
            rejected_at: d.rejected_at,
            reject_reason: d.reject_reason,
            reviewed_at: d.reviewed_at,
            updated_at: d.updated_at,
          }),
        ),
        ...measures.items.map((m) =>
          toItem("measure", {
            code: m.measure_code,
            name: m.name,
            domain: m.domain,
            status: m.status,
            reviewer_type: m.reviewer_type,
            reviewer_id: m.reviewer_id,
            reviewer_domain: m.reviewer_domain,
            approver_id: m.approver_id,
            reject_reviewer_id: m.reject_reviewer_id,
            rejected_at: m.rejected_at,
            reject_reason: m.reject_reason,
            reviewed_at: m.reviewed_at,
            updated_at: m.updated_at,
          }),
        ),
        ...terms.items.map((t) =>
          toItem("term", {
            code: t.term_code,
            name: t.name,
            domain: t.domain,
            status: t.status,
            reviewer_type: t.reviewer_type,
            reviewer_id: t.reviewer_id,
            reviewer_domain: t.reviewer_domain,
            approver_id: t.approver_id,
            reject_reviewer_id: t.reject_reviewer_id,
            rejected_at: t.rejected_at,
            reject_reason: t.reject_reason,
            reviewed_at: t.reviewed_at,
            updated_at: t.updated_at,
          }),
        ),
      ];
      // 待我审：只显示当前用户可审的行（被指派用户 / 域评审组 / 未指派域管理员兜底）
      const visible =
        view === "pending" ? merged.filter((it) => canReviewMasterData(it, currentUser)) : merged;
      setItems(visible);
      setPage(1);
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "加载失败");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  useEffect(() => {
    fetchCurrentUser().then(setCurrentUser).catch(() => {});
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
  }, []);

  useEffect(() => {
    if (view === "reviewed" && !currentUser) return;
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, currentUser?.id, page, pageSize]);

  function handleBack() {
    if (window.history.length > 1) navigate(-1);
    else navigate("/dashboard");
  }

  async function handleApprove(item: ReviewItem) {
    setBusyCode(item.code);
    try {
      if (item.kind === "dimension") await approveDimension(item.code, {});
      else if (item.kind === "measure") await approveMeasureCatalog(item.code, {});
      else await approveTerm(item.code, {});
      message.success(`已通过：${item.code}`);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    } finally {
      setBusyCode(null);
    }
  }

  async function handleReject(item: ReviewItem, reason: string) {
    setRejectBusy(true);
    try {
      if (item.kind === "dimension") await rejectDimension(item.code, { reason });
      else if (item.kind === "measure") await rejectMeasureCatalog(item.code, { reason });
      else await rejectTerm(item.code, { reason });
      message.success(`已驳回：${item.code}`);
      setRejectTarget(null);
      load();
    } catch (err) {
      message.error(err instanceof UnisenseApiError ? `${err.message}（${err.codeZh}）` : "操作失败");
    } finally {
      setRejectBusy(false);
    }
  }

  // 类型聚焦筛选：纯前端过滤（items 保持全量，切换即时响应不重新请求）
  const filteredItems =
    kindFilter === "all" ? items : items.filter((it) => it.kind === kindFilter);

  const columns = [
    {
      title: "类型",
      dataIndex: "kind",
      key: "kind",
      width: 100,
      render: (k: ReviewKind) => <Tag color={KIND_META[k].color}>{KIND_META[k].label}</Tag>,
    },
    { title: "编码", dataIndex: "code", key: "code", width: 200, render: (v: string) => <span className="mono">{v}</span> },
    { title: "名称", dataIndex: "name", key: "name", ellipsis: true },
    { title: "域", dataIndex: "domain", key: "domain", render: (v: string) => domainMap[v] ?? v },
    {
      title: "指派评审人",
      key: "reviewer",
      width: 180,
      render: (_: unknown, r: ReviewItem) => reviewerLabel(r, effectiveUserMap, domainMap),
    },
    {
      // 「我审过的」视图：展示我的处理结论（通过/驳回 + 时间 + 原因）；待我审视图不显示
      title: "处理结果",
      key: "verdict",
      width: 220,
      render: (_: unknown, r: ReviewItem) => {
        if (view !== "reviewed") return null;
        const v = reviewVerdict(r, currentUser?.id ?? null);
        if (!v) return <span className="muted">—</span>;
        if (v.verdict === "approved") {
          return (
            <Space direction="vertical" size={2}>
              <Tag color="green">已通过</Tag>
              {v.time ? <span className="muted" style={{ fontSize: 12 }}>{formatCnTime(v.time)}</span> : null}
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
                  style={{ fontSize: 12, maxWidth: 140, display: "inline-block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                >
                  {v.reason}
                </span>
              </Tooltip>
            ) : null}
            {v.time ? <span className="muted" style={{ fontSize: 12 }}>{formatCnTime(v.time)}</span> : null}
          </Space>
        );
      },
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      key: "updated_at",
      width: 150,
      render: (v: string | null) =>
        v ? <span className="mono" style={{ fontSize: 12 }}>{formatCnTime(v)}</span> : <span className="muted">—</span>,
    },
    {
      title: "操作",
      key: "actions",
      width: 120,
      render: (_: unknown, r: ReviewItem) => {
        // 「我审过的」视图：仅回看（处理结果列已展示结论）
        if (view === "reviewed") return null;
        return (
          <MasterDataReviewActions
            row={{ code: r.code, name: r.name, status: r.status, reviewer_type: r.reviewer_type, reviewer_id: r.reviewer_id, reviewer_domain: r.reviewer_domain }}
            user={currentUser}
            busyCode={busyCode}
            onApprove={() => void handleApprove(r)}
            onOpenSubmit={() => undefined}
            onOpenReject={() => {
              setRejectItem(r);
              setRejectTarget({ code: r.code, name: r.name });
            }}
          />
        );
      },
    },
  ];

  return (
    <div>
      {!embedded && (
        <div className="page-head">
          <div>
            <Button type="link" icon={<ArrowLeftOutlined />} onClick={handleBack} style={{ padding: 0, marginBottom: 4 }}>
              返回
            </Button>
            <div className="page-kicker">指标资产 / 主数据审批</div>
            <h2>主数据审批</h2>
            <p>维度 / 逻辑度量 / 术语统一审批——被指派评审人或域评审组在此通过或驳回，提交人收到通知后返回修改重提。</p>
          </div>
        </div>
      )}
      <Card
        title="主数据审批"
        extra={
          <Space>
            <Segmented
              value={view}
              onChange={(v) => {
                setView(v as "pending" | "reviewed");
                setPage(1);
              }}
              options={[
                { label: "待我审", value: "pending" },
                { label: "我审过的", value: "reviewed" },
              ]}
            />
            <Segmented
              value={kindFilter}
              onChange={(v) => {
                setKindFilter(v as "all" | ReviewKind);
                setPage(1);
              }}
              options={[
                { label: "全部", value: "all" },
                { label: "维度", value: "dimension" },
                { label: "逻辑度量", value: "measure" },
                { label: "术语", value: "term" },
              ]}
            />
            <Button size="small" icon={<ReloadOutlined />} onClick={load} loading={loading}>
              刷新
            </Button>
          </Space>
        }
      >
        <Table
          dataSource={filteredItems}
          columns={columns}
          rowKey={(r) => `${r.kind}:${r.code}`}
          loading={loading}
          pagination={{
            current: page,
            pageSize,
            total: serverTotal,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50, 100],
            onChange: (p, ps) => {
              setPage(p);
              setPageSize(ps);
            },
            showTotal: (t) => (view === "pending" ? `共 ${t} 条待您评审` : `共 ${t} 条已评审`),
          }}
          locale={{
            emptyText:
              kindFilter !== "all"
                ? view === "pending"
                  ? `当前无待您评审的「${KIND_META[kindFilter].label}」`
                  : `您还没有评审过「${KIND_META[kindFilter].label}」`
                : view === "pending"
                  ? "当前无待您评审的主数据（已全部处理或暂无指派）"
                  : "您还没有评审过主数据",
          }}
        />
        {/* 驳回审核弹窗（复用共享 MasterDataReviewModals 的驳回部分；工作台不涉及提交） */}
        <MasterDataReviewModals
          entityLabel="主数据"
          submitDescription=""
          user={currentUser}
          submitTarget={null}
          submitBusy={false}
          onCancelSubmit={() => undefined}
          onConfirmSubmit={async () => undefined}
          rejectTarget={rejectTarget}
          rejectBusy={rejectBusy}
          onCancelReject={() => {
            setRejectTarget(null);
            setRejectItem(null);
          }}
          onConfirmReject={async (reason) => {
            if (rejectItem) await handleReject(rejectItem, reason);
          }}
        />
      </Card>
    </div>
  );
}
